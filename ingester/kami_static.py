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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from eth_utils import keccak
from web3 import Web3

from .chain_client import ChainClient
from .skill_catalog import ALL_MODIFIER_COLUMNS, SkillCatalog, load_skill_catalog
from .storage import Storage
from .system_registry import REGISTRY_ABI, SystemRegistry, load_abi

log = logging.getLogger(__name__)

# Address-cast: account_id is uint256(uint160(owner_address)), so the low 160
# bits ARE the address. Masking gives us the wallet without an extra eth_call.
_ADDRESS_MASK = (1 << 160) - 1


def _stat_total(base: int, shift: int, boost: int) -> int:
    """Effective stat scalar from the (base, shift, boost, sync) tuple.

    Formula per ``kamigotchi-context/systems/state-reading.md`` and
    ``health.md``, used by every Kamigotchi client and applied identically
    to health / power / harmony / violence / slots:

        effective = max(0, floor((1000 + boost) * (base + shift) / 1000))

    The ``sync`` field is the last-synced *current* depletable value (HP
    only) — not part of the build snapshot.
    """
    raw = (1000 + boost) * (base + shift)
    if raw <= 0:
        return 0
    return raw // 1000


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
    # Session 12 affinity scalars. Extracted from `affinities` (chain
    # returns string[2] = [body_affinity, hand_affinity]) for query
    # ergonomics — denormalized so kami-agent can JOIN/GROUP BY without
    # parsing JSON inline. NULL when affinities array shape is unexpected
    # (length != 2); see _kami_shape_to_static defensive path.
    body_affinity: str | None = None
    hand_affinity: str | None = None
    # Session 10 build snapshot fields. All optional — populator writes
    # them on success, leaves NULL + sets build_refreshed_ts on per-kami
    # build-fetch failure so the row isn't infinite-retried.
    level: int | None = None
    xp: int | None = None
    total_health: int | None = None
    total_power: int | None = None
    total_violence: int | None = None
    total_harmony: int | None = None
    total_slots: int | None = None
    skills_json: str | None = None
    equipment_json: str | None = None
    build_refreshed_ts: dt.datetime | None = None
    # Session 11 modifier columns. Default 0 (a kami with no skill /
    # equipment investment in a given effect contributes 0). NULL is
    # reserved for "modifier compute failed" — populator sets the dict
    # to None in that path so the column stays NULL on UPSERT.
    strain_boost: int | None = 0
    harvest_fertility_boost: int | None = 0
    harvest_intensity_boost: int | None = 0
    harvest_bounty_boost: int | None = 0
    rest_recovery_boost: int | None = 0
    cooldown_shift: int | None = 0
    attack_threshold_shift: int | None = 0
    attack_threshold_ratio: int | None = 0
    attack_spoils_ratio: int | None = 0
    defense_threshold_shift: int | None = 0
    defense_threshold_ratio: int | None = 0
    defense_salvage_ratio: int | None = 0


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

    Resolves the per-stat effective totals via the canonical game formula
    ``floor((1000 + boost) * (base + shift) / 1000)`` and persists ``level``
    and ``xp`` directly. Slots / skills / equipment require additional
    component reads — handled by ``KamiStaticReader._fetch_build_extras``.
    """
    _id, kami_index, name, _media, stats, traits, affinities, account, level, xp, _room, _state = shape

    health, power, harmony, violence = stats
    face, hand, body, background, color = traits

    account_int = int(account)
    owner = _account_id_to_address(account_int) if account_int != 0 else None

    # Affinity scalars (Session 12). Chain contract guarantees a 2-string
    # array [body_affinity, hand_affinity] per kamigotchi-context
    # state-reading.md:101 — but defensively NULL+log if a particular
    # kami returns something else, matching the build-extras failure
    # posture (one bad kami doesn't break the sweep).
    affinities_list = [str(a) for a in affinities]
    body_aff: str | None
    hand_aff: str | None
    if len(affinities_list) == 2:
        body_aff = affinities_list[0]
        hand_aff = affinities_list[1]
    else:
        log.warning(
            "kami_static: kami_id=%s affinities length=%d (expected 2): %r — body_affinity / hand_affinity NULL",
            kami_id, len(affinities_list), affinities_list,
        )
        body_aff = None
        hand_aff = None

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
        affinities=affinities_list,
        base_health=int(health[0]),
        base_power=int(power[0]),
        base_harmony=int(harmony[0]),
        base_violence=int(violence[0]),
        body_affinity=body_aff,
        hand_affinity=hand_aff,
        level=int(level),
        xp=int(xp),
        total_health=_stat_total(int(health[0]), int(health[1]), int(health[2])),
        total_power=_stat_total(int(power[0]), int(power[1]), int(power[2])),
        total_harmony=_stat_total(int(harmony[0]), int(harmony[1]), int(harmony[2])),
        total_violence=_stat_total(int(violence[0]), int(violence[1]), int(violence[2])),
    )


_WORLD_COMPONENTS_ABI = [
    {
        "name": "components",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
    },
]

# Build-fetch components, with the ABI filename to attach. Each is resolved
# at reader construction via the world.components() registry — the same
# getEntitiesWithValue(uint256) pattern as systems but against a different
# registry contract. See memory/decoder-notes.md "Session 10 — build fields
# on chain" for resolved addresses observed during discovery.
_BUILD_COMPONENTS: list[tuple[str, str]] = [
    ("component.stat.slots", "SlotsComponent.json"),
    ("component.id.skill.owns", "IDOwnsSkillComponent.json"),
    ("component.index.skill", "IndexSkillComponent.json"),
    ("component.skill.point", "SkillPointComponent.json"),
    # Equipment-owns reuses the IDOwnsSkillComponent ABI shape
    # (uint256 -> uint256[]); it is resolvable on chain even though the
    # vendored components.json cheat sheet omits it.
    ("component.id.equipment.owns", "IDOwnsSkillComponent.json"),
    ("component.index.item", "IndexItemComponent.json"),
]


class KamiStaticReader:
    """Wraps the GetterSystem contract + per-kami build component reads.

    Constructed once at startup; reuses a single ``ChainClient``. eth_call
    failures (revert, timeout) bubble up to the caller — backfill / refresh
    handles them, logs, and skips that kami.

    Account lookups (`getAccount`) are cached on the reader instance so a
    population pass across many kamis sharing one operator (e.g. bpeon
    owns dozens) makes one chain call per distinct ``account_id``, not
    one per kami. Build extras (slots / skills / equipment) are per-kami
    by definition and are NOT cached across kamis; the per-pass cache
    only covers account lookups.
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
        # Resolve build-component contracts via world.components(). Held as
        # web3 contract objects keyed by short name so _fetch_build_extras
        # can dispatch without re-resolving.
        self._build_components = self._resolve_build_components(client, registry, abi_dir)
        # Skill+equipment catalog for the 12 Session 11 modifier columns.
        # Loaded once at construction; immutable across the population pass.
        # Same single-load shape as the per-pass account_id cache.
        from .config import load_config  # noqa: PLC0415
        cfg = load_config()
        catalogs_dir = Path(cfg.abi_dir).parent / "catalogs"
        self.skill_catalog: SkillCatalog = load_skill_catalog(catalogs_dir)

    def _resolve_build_components(
        self, client: ChainClient, registry: SystemRegistry, abi_dir,
    ) -> dict[str, "Web3.eth.contract"]:
        """Resolve component contract addresses via world.components().

        Returns a dict keyed by short name (e.g. ``"slots"``, ``"skills_owns"``)
        whose values are web3 contract objects bound to the ABI. A name that
        fails to resolve maps to ``None`` — the build-extras path tolerates
        missing components and just leaves the corresponding column NULL.
        """
        # Re-derive world address from any registered SystemInfo — the
        # getter info gives us one, and the world has a stable address per
        # config. Cleaner: pull from the same place serve.py does
        # (config.world_address). To avoid threading config in here, we use
        # the world_address we already have via the registry's connection
        # to the getter's contract — instead, take it from the chain by
        # asking any known World-side component via the same w3 instance.
        from .config import load_config  # noqa: PLC0415 — avoid circular at import

        cfg = load_config()
        world = client.w3.eth.contract(
            address=Web3.to_checksum_address(cfg.world_address),
            abi=_WORLD_COMPONENTS_ABI,
        )
        components_addr = client.call_contract_fn(world, "components")
        components_registry = client.w3.eth.contract(
            address=Web3.to_checksum_address(components_addr),
            abi=REGISTRY_ABI,
        )
        log.info(
            "kami_static: world.components() = %s (resolving %d build components)",
            components_addr, len(_BUILD_COMPONENTS),
        )

        out: dict[str, object] = {}
        short_name_map = {
            "component.stat.slots": "slots",
            "component.id.skill.owns": "skills_owns",
            "component.index.skill": "skill_index",
            "component.skill.point": "skill_point",
            "component.id.equipment.owns": "equip_owns",
            "component.index.item": "item_index",
        }
        for full_name, abi_filename in _BUILD_COMPONENTS:
            short = short_name_map[full_name]
            name_hash = int.from_bytes(keccak(text=full_name), "big")
            entities = client.call_contract_fn(
                components_registry, "getEntitiesWithValue", name_hash,
            )
            if not entities:
                log.warning("kami_static: component %s did not resolve", full_name)
                out[short] = None
                continue
            addr = Web3.to_checksum_address("0x" + format(entities[0], "040x"))
            out[short] = client.w3.eth.contract(
                address=addr, abi=load_abi(abi_dir, abi_filename),
            )
            log.info("kami_static: %s -> %s", full_name, addr)
        return out

    def fetch(self, kami_id: str) -> KamiStatic:
        kid_int = int(kami_id)
        shape = self.client.call_contract_fn(self.contract, "getKami", kid_int)
        row = _kami_shape_to_static(kami_id, shape)
        if row.account_id is not None and row.account_id != "0":
            idx, name = self.fetch_account(row.account_id)
            row.account_index = idx
            row.account_name = name
        # Build extras — per-kami component reads. Failures here leave the
        # respective columns NULL but still set build_refreshed_ts so we
        # don't infinite-retry a permanently-broken kami.
        try:
            extras = self._fetch_build_extras(kid_int)
            row.total_slots = extras.get("total_slots")
            row.skills_json = extras.get("skills_json")
            row.equipment_json = extras.get("equipment_json")
        except Exception as e:  # noqa: BLE001
            log.warning("kami_static: build_extras(%s) failed: %s", kami_id, e)
        # Session 11 modifier columns — pure aggregation over the
        # already-fetched skills_json + equipment_json via the catalog
        # cache. Zero new chain calls per kami. If catalog compute throws
        # for any reason, NULL the 12 columns on this kami so the
        # build_refreshed_ts still ticks forward (matching the build-extras
        # failure posture).
        try:
            mods = self.skill_catalog.compute_modifiers(
                row.skills_json, row.equipment_json,
            )
            for col, val in mods.items():
                setattr(row, col, val)
        except Exception as e:  # noqa: BLE001
            log.warning("kami_static: modifier compute(%s) failed: %s", kami_id, e)
            for col in ALL_MODIFIER_COLUMNS:
                setattr(row, col, None)
        row.build_refreshed_ts = dt.datetime.now(tz=dt.timezone.utc)
        return row

    def _fetch_build_extras(self, kid_int: int) -> dict[str, object]:
        """Read slots / skills / equipment for one kami.

        Returns a dict with keys ``total_slots`` (int|None), ``skills_json``
        (str|None), ``equipment_json`` (str|None). Per-component failures
        are logged at DEBUG and leave the corresponding key absent (caller
        treats absent as NULL).
        """
        out: dict[str, object] = {}

        # Slots — (base, shift, boost, sync) tuple, formula-resolved.
        slots = self._build_components.get("slots")
        if slots is not None:
            try:
                tup = self.client.call_contract_fn(slots, "safeGet", kid_int)
                out["total_slots"] = _stat_total(int(tup[0]), int(tup[1]), int(tup[2]))
            except Exception as e:  # noqa: BLE001
                log.debug("kami_static: SlotsComponent.safeGet(%d) failed: %s", kid_int, e)

        # Skills — enumerate then read index + points per skill instance.
        skills_owns = self._build_components.get("skills_owns")
        skill_index = self._build_components.get("skill_index")
        skill_point = self._build_components.get("skill_point")
        if skills_owns is not None and skill_index is not None and skill_point is not None:
            try:
                ents = list(self.client.call_contract_fn(
                    skills_owns, "getEntitiesWithValue", kid_int,
                ))
                skills: list[dict[str, int]] = []
                for sid in ents:
                    sidx = int(self.client.call_contract_fn(skill_index, "safeGet", sid))
                    spt = int(self.client.call_contract_fn(skill_point, "safeGet", sid))
                    skills.append({"index": sidx, "points": spt})
                out["skills_json"] = json.dumps(skills)
            except Exception as e:  # noqa: BLE001
                log.debug("kami_static: skills enumeration(%d) failed: %s", kid_int, e)

        # Equipment — enumerate then read item_index per equip instance.
        # Slot-name resolution (component.for.string) deferred — does not
        # resolve in the current registry snapshot.
        equip_owns = self._build_components.get("equip_owns")
        item_index = self._build_components.get("item_index")
        if equip_owns is not None and item_index is not None:
            try:
                ents = list(self.client.call_contract_fn(
                    equip_owns, "getEntitiesWithValue", kid_int,
                ))
                equips: list[int] = []
                for eid in ents:
                    iidx = int(self.client.call_contract_fn(item_index, "safeGet", eid))
                    equips.append(iidx)
                out["equipment_json"] = json.dumps(equips)
            except Exception as e:  # noqa: BLE001
                log.debug("kami_static: equipment enumeration(%d) failed: %s", kid_int, e)

        return out

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
            r.level, r.xp,
            r.total_health, r.total_power, r.total_violence, r.total_harmony,
            r.total_slots, r.skills_json, r.equipment_json,
            r.build_refreshed_ts,
            r.strain_boost, r.harvest_fertility_boost, r.harvest_intensity_boost,
            r.harvest_bounty_boost, r.rest_recovery_boost, r.cooldown_shift,
            r.attack_threshold_shift, r.attack_threshold_ratio, r.attack_spoils_ratio,
            r.defense_threshold_shift, r.defense_threshold_ratio, r.defense_salvage_ratio,
            r.body_affinity, r.hand_affinity,
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
                 level, xp,
                 total_health, total_power, total_violence, total_harmony,
                 total_slots, skills_json, equipment_json,
                 build_refreshed_ts,
                 strain_boost, harvest_fertility_boost, harvest_intensity_boost,
                 harvest_bounty_boost, rest_recovery_boost, cooldown_shift,
                 attack_threshold_shift, attack_threshold_ratio, attack_spoils_ratio,
                 defense_threshold_shift, defense_threshold_ratio, defense_salvage_ratio,
                 body_affinity, hand_affinity,
                 first_seen_ts, last_refreshed_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?)
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
                level = excluded.level,
                xp = excluded.xp,
                total_health = excluded.total_health,
                total_power = excluded.total_power,
                total_violence = excluded.total_violence,
                total_harmony = excluded.total_harmony,
                total_slots = excluded.total_slots,
                skills_json = excluded.skills_json,
                equipment_json = excluded.equipment_json,
                build_refreshed_ts = excluded.build_refreshed_ts,
                strain_boost = excluded.strain_boost,
                harvest_fertility_boost = excluded.harvest_fertility_boost,
                harvest_intensity_boost = excluded.harvest_intensity_boost,
                harvest_bounty_boost = excluded.harvest_bounty_boost,
                rest_recovery_boost = excluded.rest_recovery_boost,
                cooldown_shift = excluded.cooldown_shift,
                attack_threshold_shift = excluded.attack_threshold_shift,
                attack_threshold_ratio = excluded.attack_threshold_ratio,
                attack_spoils_ratio = excluded.attack_spoils_ratio,
                defense_threshold_shift = excluded.defense_threshold_shift,
                defense_threshold_ratio = excluded.defense_threshold_ratio,
                defense_salvage_ratio = excluded.defense_salvage_ratio,
                body_affinity = excluded.body_affinity,
                hand_affinity = excluded.hand_affinity,
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
