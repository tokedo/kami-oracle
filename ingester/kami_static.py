"""Per-kami trait + stat backfill via the GetterSystem.

The decoded ``kami_action`` rows know each kami by its entity ID. To turn
those entity IDs into human-readable names + traits, we call
``GetterSystem.getKami(uint256 id)`` once per kami and persist the result
into ``kami_static``. Trait data is effectively immutable; level / xp /
state drift over time, so we periodically refresh the table.

This module is fully read-only against the chain — it never sends a tx.

Two entry points:
    backfill_all(...)      walk every distinct kami_id ever seen and
                           upsert into kami_static.
    refresh_stale(...)     re-read only kamis whose ``last_refreshed_ts``
                           is older than ``max_age_hours``.

Both are idempotent. ``backfill_all`` is safe to re-run.

Threading: read-only eth_calls fan out across a small thread pool — RPC
latency-bound, ~150-300 ms / call on the public Yominet endpoint. DB writes
are serialised behind ``Storage.lock`` (single-writer DuckDB).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable

from web3 import Web3

from .chain_client import ChainClient
from .storage import Storage
from .system_registry import SystemRegistry, load_abi

log = logging.getLogger(__name__)

# Address-cast: account_id is uint256(uint160(owner_address)), so the low 160
# bits ARE the address. Masking gives us the wallet without an extra eth_call.
_ADDRESS_MASK = (1 << 160) - 1


@dataclass
class KamiStatic:
    kami_id: str
    kami_index: int | None
    name: str | None
    owner_address: str | None
    account_id: str | None
    account_index: int | None
    account_name: str | None
    body: int | None
    hand: int | None
    face: int | None
    background: int | None
    color: int | None
    affinities: list[str]
    base_health: int | None
    base_power: int | None
    base_harmony: int | None
    base_violence: int | None


# GetterSystem.getAccount(uint256) is documented in
# kami_context/system-ids.md (Getter System section) but missing from the
# vendored GetterSystem.json ABI. We merge this minimal fragment into the
# loaded ABI at construction time so the contract object exposes the
# function. Tier-A overlay per CLAUDE.md (doc-cited).
_GET_ACCOUNT_ABI_FRAGMENT: dict = {
    "name": "getAccount",
    "type": "function",
    "stateMutability": "view",
    "inputs": [{"name": "accountId", "type": "uint256"}],
    "outputs": [
        {
            "type": "tuple",
            "components": [
                {"name": "index", "type": "uint32"},
                {"name": "name", "type": "string"},
                {"name": "currStamina", "type": "int32"},
                {"name": "room", "type": "uint32"},
            ],
        },
    ],
}


def _account_id_to_address(account_id_int: int) -> str:
    """Recover the owner wallet from an account entity id (offline)."""
    addr_int = account_id_int & _ADDRESS_MASK
    return Web3.to_checksum_address("0x" + format(addr_int, "040x"))


def _kami_shape_to_static(kami_id: str, shape: tuple) -> KamiStatic:
    """Map the GetterSystem.getKami(...) tuple into a KamiStatic row.

    Layout (per kami_context/system-ids.md GETTER_ABI):
        (id, index, name, mediaURI, stats, traits, affinities, account, level, xp, room, state)

    stats   = (health, power, harmony, violence) where each = (base, shift, boost, sync)
    traits  = (face, hand, body, background, color)
    """
    _id, kami_index, name, _media, stats, traits, affinities, account, _lvl, _xp, _room, _state = shape

    health, power, harmony, violence = stats
    face, hand, body, background, color = traits

    account_int = int(account)
    owner = _account_id_to_address(account_int) if account_int != 0 else None

    return KamiStatic(
        kami_id=str(kami_id),
        kami_index=int(kami_index),
        name=str(name) if name else None,
        owner_address=owner,
        account_id=str(account_int),
        account_index=None,
        account_name=None,
        body=int(body),
        hand=int(hand),
        face=int(face),
        background=int(background),
        color=int(color),
        affinities=[str(a) for a in affinities],
        base_health=int(health[0]),
        base_power=int(power[0]),
        base_harmony=int(harmony[0]),
        base_violence=int(violence[0]),
    )


class KamiStaticReader:
    """Wraps the GetterSystem contract.

    Constructed once at startup; reuses a single ``ChainClient``. eth_call
    failures (revert, timeout) bubble up to the caller — backfill / refresh
    handles them, logs, and skips that kami.

    Account lookups (`getAccount`) are cached on the reader instance so a
    population pass across many kamis sharing one operator (e.g. bpeon
    owns dozens) makes one chain call per distinct ``account_id``, not
    one per kami.
    """

    def __init__(self, client: ChainClient, registry: SystemRegistry, abi_dir):
        self.client = client
        info = registry.get_by_system_id("system.getter")
        if info is None:
            raise RuntimeError(
                "system.getter not in registry — add to SYSTEM_ID_TO_ABI and re-resolve"
            )
        abi = load_abi(abi_dir, info.abi_name)
        # Merge the doc-cited getAccount fragment in; vendored ABI does not
        # carry it. Avoid duplicating if a future re-vendor adds it.
        if not any(e.get("name") == "getAccount" for e in abi if isinstance(e, dict)):
            abi = list(abi) + [_GET_ACCOUNT_ABI_FRAGMENT]
        self.contract = client.w3.eth.contract(
            address=Web3.to_checksum_address(info.address),
            abi=abi,
        )
        self._account_cache: dict[str, tuple[int | None, str | None]] = {}

    def fetch(self, kami_id: str) -> KamiStatic:
        kid_int = int(kami_id)
        shape = self.client.call_contract_fn(self.contract, "getKami", kid_int)
        row = _kami_shape_to_static(kami_id, shape)
        if row.account_id is not None and row.account_id != "0":
            idx, name = self.fetch_account(row.account_id)
            row.account_index = idx
            row.account_name = name
        return row

    def fetch_account(self, account_id: str) -> tuple[int | None, str | None]:
        """Look up (account_index, account_name) for a uint256 account id.

        Returns (None, None) if the account doesn't resolve cleanly: a
        revert, a default/empty shape, or an empty name string. Caches
        results on the reader instance so repeat calls within a population
        pass are free.
        """
        cached = self._account_cache.get(account_id)
        if cached is not None:
            return cached
        result: tuple[int | None, str | None]
        try:
            shape = self.client.call_contract_fn(
                self.contract, "getAccount", int(account_id)
            )
        except Exception as e:  # noqa: BLE001
            # getAccount on a non-account entity reverts; treat as anonymous.
            log.debug("kami_static: getAccount(%s) failed: %s", account_id, e)
            result = (None, None)
        else:
            idx, name, _stamina, _room = shape
            idx = int(idx) if idx is not None else None
            name = str(name) if name else None
            result = (idx, name)
        self._account_cache[account_id] = result
        return result


# ---------------------------------------------------------------------------
# Storage helpers.
# ---------------------------------------------------------------------------


def upsert_kami_static(storage: Storage, rows: Iterable[KamiStatic]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    now = dt.datetime.now(tz=dt.timezone.utc)
    payload = [
        (
            r.kami_id, r.kami_index, r.name, r.owner_address, r.account_id,
            r.account_index, r.account_name,
            r.body, r.hand, r.face, r.background, r.color,
            json.dumps(r.affinities) if r.affinities is not None else None,
            r.base_health, r.base_power, r.base_harmony, r.base_violence,
            now, now,
        )
        for r in rows
    ]
    with storage.lock:
        storage.conn.executemany(
            """
            INSERT INTO kami_static
                (kami_id, kami_index, name, owner_address, account_id,
                 account_index, account_name,
                 body, hand, face, background, color, affinities,
                 base_health, base_power, base_harmony, base_violence,
                 first_seen_ts, last_refreshed_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (kami_id) DO UPDATE SET
                kami_index = excluded.kami_index,
                name = excluded.name,
                owner_address = excluded.owner_address,
                account_id = excluded.account_id,
                account_index = excluded.account_index,
                account_name = excluded.account_name,
                body = excluded.body,
                hand = excluded.hand,
                face = excluded.face,
                background = excluded.background,
                color = excluded.color,
                affinities = excluded.affinities,
                base_health = excluded.base_health,
                base_power = excluded.base_power,
                base_harmony = excluded.base_harmony,
                base_violence = excluded.base_violence,
                last_refreshed_ts = excluded.last_refreshed_ts
            """,
            payload,
        )
    return len(payload)


def _candidate_kami_ids(storage: Storage) -> list[str]:
    rows = storage.fetchall(
        "SELECT DISTINCT kami_id FROM kami_action WHERE kami_id IS NOT NULL"
    )
    return [str(r[0]) for r in rows]


def _stale_kami_ids(storage: Storage, max_age_hours: int) -> list[str]:
    """Kamis that have been observed in actions but are missing from
    kami_static, OR whose last_refreshed_ts is older than the cutoff."""
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    rows = storage.fetchall(
        """
        SELECT DISTINCT a.kami_id
        FROM kami_action a
        LEFT JOIN kami_static s USING (kami_id)
        WHERE a.kami_id IS NOT NULL
          AND (s.kami_id IS NULL OR s.last_refreshed_ts < ?)
        """,
        [cutoff],
    )
    return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


def _fetch_many(
    reader: KamiStaticReader,
    kami_ids: list[str],
    *,
    workers: int,
    flush_every: int,
    storage: Storage,
) -> tuple[int, int]:
    """Fetch each kami in parallel; flush in chunks. Returns (n_ok, n_fail)."""
    n_ok = n_fail = 0
    pending: list[KamiStatic] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(reader.fetch, kid): kid for kid in kami_ids}
        for fut in as_completed(futures):
            kid = futures[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                log.warning("kami_static: getKami(%s) failed: %s", kid, e)
                continue
            pending.append(row)
            n_ok += 1
            if len(pending) >= flush_every:
                upsert_kami_static(storage, pending)
                pending.clear()
                log.info("kami_static: progress ok=%d fail=%d / total=%d",
                         n_ok, n_fail, len(kami_ids))
    if pending:
        upsert_kami_static(storage, pending)
    return n_ok, n_fail


def backfill_all(
    storage: Storage,
    reader: KamiStaticReader,
    *,
    workers: int = 8,
    flush_every: int = 200,
) -> dict[str, int]:
    """Refresh every kami_id that has ever appeared in kami_action."""
    kami_ids = _candidate_kami_ids(storage)
    log.info("kami_static: backfill_all — %d kami_ids", len(kami_ids))
    n_ok, n_fail = _fetch_many(
        reader, kami_ids,
        workers=workers, flush_every=flush_every, storage=storage,
    )
    log.info("kami_static: backfill_all done — ok=%d fail=%d", n_ok, n_fail)
    return {"candidates": len(kami_ids), "ok": n_ok, "fail": n_fail}


def refresh_stale(
    storage: Storage,
    reader: KamiStaticReader,
    *,
    max_age_hours: int = 24,
    workers: int = 8,
    flush_every: int = 200,
) -> dict[str, int]:
    """Refresh only kamis missing from kami_static or older than the cutoff."""
    kami_ids = _stale_kami_ids(storage, max_age_hours)
    log.info("kami_static: refresh_stale — %d candidates (max_age=%dh)",
             len(kami_ids), max_age_hours)
    if not kami_ids:
        return {"candidates": 0, "ok": 0, "fail": 0}
    n_ok, n_fail = _fetch_many(
        reader, kami_ids,
        workers=workers, flush_every=flush_every, storage=storage,
    )
    log.info("kami_static: refresh_stale done — ok=%d fail=%d", n_ok, n_fail)
    return {"candidates": len(kami_ids), "ok": n_ok, "fail": n_fail}
