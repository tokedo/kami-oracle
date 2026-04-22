"""Resolve player-facing Kamigotchi system IDs to on-chain addresses.

The World contract exposes a ``systems()`` view returning the address of the
registry component. Each system ID (e.g. ``system.harvest.start``) is hashed
with keccak256 and looked up via ``getEntitiesWithValue`` to recover the
system contract address.

We do this once at startup (or on demand) and cache results. A session's
resolution can also be persisted to ``ingest_cursor`` so the next restart
can skip the RPC round-trip.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_utils import keccak
from web3 import Web3

from .chain_client import ChainClient

log = logging.getLogger(__name__)

WORLD_ABI = [
    {
        "name": "systems",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
    },
]
REGISTRY_ABI = [
    {
        "name": "getEntitiesWithValue",
        "type": "function",
        "inputs": [{"type": "uint256", "name": "v"}],
        "outputs": [{"type": "uint256[]"}],
        "stateMutability": "view",
    },
]


# Map of system ID -> ABI filename (in kami_context/abi/). Only systems whose
# txs we want to decode need an ABI; everything else is skipped with a log
# entry to memory/unknown-systems.md.
#
# Derived from kami_context/system-ids.md. Keep alphabetized inside each
# group.
SYSTEM_ID_TO_ABI: dict[str, str] = {
    # Account
    "system.account.fund": "AccountFundSystem.json",
    "system.account.move": "AccountMoveSystem.json",
    "system.account.register": "AccountRegisterSystem.json",
    "system.account.set.name": "AccountSetNameSystem.json",
    "system.account.set.operator": "AccountSetOperatorSystem.json",
    "system.account.use.item": "AccountUseItemSystem.json",
    # Echo (view-ish but still txs in practice)
    "system.echo.kamis": "EchoKamisSystem.json",
    "system.echo.room": "EchoRoomSystem.json",
    # Friend
    "system.friend.accept": "FriendAcceptSystem.json",
    "system.friend.block": "FriendBlockSystem.json",
    "system.friend.cancel": "FriendCancelSystem.json",
    "system.friend.request": "FriendRequestSystem.json",
    # Goal / Scavenge
    "system.goal.claim": "GoalClaimSystem.json",
    "system.goal.contribute": "GoalContributeSystem.json",
    "system.scavenge.claim": "ScavengeClaimSystem.json",
    # Harvest
    "system.harvest.collect": "HarvestCollectSystem.json",
    "system.harvest.liquidate": "HarvestLiquidateSystem.json",
    "system.harvest.start": "HarvestStartSystem.json",
    "system.harvest.stop": "HarvestStopSystem.json",
    # Items / craft
    "system.craft": "CraftSystem.json",
    "system.droptable.item.reveal": "DroptableRevealSystem.json",
    "system.item.burn": "ItemBurnSystem.json",
    # Kami (core)
    "system.kami.gacha.mint": "KamiGachaMintSystem.json",
    "system.kami.gacha.reroll": "KamiGachaRerollSystem.json",
    "system.kami.level": "KamiLevelSystem.json",
    "system.kami.name": "KamiNameSystem.json",
    "system.kami.use.item": "KamiUseItemSystem.json",
    # Listings (NPC merchants)
    "system.listing.buy": "ListingBuySystem.json",
    "system.listing.sell": "ListingSellSystem.json",
    # Quest
    "system.quest.accept": "QuestAcceptSystem.json",
    "system.quest.complete": "QuestCompleteSystem.json",
    "system.quest.drop": "QuestDropSystem.json",
    # Relationship
    "system.relationship.advance": "RelationshipAdvanceSystem.json",
    # Skills
    "system.skill.respec": "SkillResetSystem.json",
    "system.skill.upgrade": "SkillUpgradeSystem.json",
}
# NOTE on missing ABIs: equip/unequip, marketplace, trade, onyx, sacrifice,
# send, kami721, gacha.reveal, buy.gacha.ticket, newbievendor, auction, and
# erc20.portal are absent in this vendored snapshot. Their txs will not map
# to a system_id and will be logged to memory/unknown-systems.md. Revisit
# after re-vendoring.


@dataclass(frozen=True)
class SystemInfo:
    system_id: str
    address: str       # checksummed
    abi_name: str      # filename under kami_context/abi/
    # Earliest/latest probe block where this address was observed resolving to
    # `system_id`. Populated when the registry is built via historical probes;
    # ``None`` when the registry was resolved at the default ``latest`` tag.
    first_seen_block: int | None = None
    last_seen_block: int | None = None


class SystemRegistry:
    """Holds the address -> system_id map + per-system ABI.

    A single ``system_id`` can map to multiple addresses across history —
    Kamigotchi periodically redeploys system contracts, and a multi-height
    probe unions every deployment observed in the target window into one
    registry. For decoding purposes each deployment shares the same ABI, so
    the decoder only needs ``system_id`` / ``abi_name`` from ``SystemInfo``;
    the specific ``address`` field on ``SystemInfo`` identifies the matched
    deployment for logging.
    """

    def __init__(self, by_address: dict[str, SystemInfo]):
        self._by_address: dict[str, SystemInfo] = dict(by_address)
        self._by_system_id: dict[str, SystemInfo] = {}
        self._rebuild_system_id_index()

    def _rebuild_system_id_index(self) -> None:
        self._by_system_id = {}
        for info in self._by_address.values():
            existing = self._by_system_id.get(info.system_id)
            # Prefer the most recently observed deployment as the "canonical"
            # pointer for get_by_system_id callers (e.g. kami_static population
            # via the current GetterSystem). Tie-break: keep the existing one.
            existing_last = existing.last_seen_block if existing and existing.last_seen_block is not None else -1
            info_last = info.last_seen_block if info.last_seen_block is not None else -1
            if existing is None or info_last > existing_last:
                self._by_system_id[info.system_id] = info

    def known_addresses(self) -> set[str]:
        return set(self._by_address.keys())

    def get_by_address(self, addr: str) -> SystemInfo | None:
        if addr is None:
            return None
        # web3 gives checksummed addresses; normalize defensively.
        try:
            return self._by_address.get(Web3.to_checksum_address(addr))
        except ValueError:
            return None

    def get_by_system_id(self, sid: str) -> SystemInfo | None:
        return self._by_system_id.get(sid)

    def addresses_for_system(self, sid: str) -> set[str]:
        return {a for a, info in self._by_address.items() if info.system_id == sid}

    def system_ids(self) -> set[str]:
        return set(self._by_system_id.keys())

    def __len__(self) -> int:
        return len(self._by_address)

    def extend(self, other: "SystemRegistry") -> set[str]:
        """Union addresses from ``other`` into self.

        Returns the set of addresses that were newly added (useful for the
        poller's periodic re-probe logging). Existing entries have their
        ``first_seen_block`` / ``last_seen_block`` widened to cover the union
        of observation blocks.
        """
        newly: set[str] = set()
        for addr, info in other._by_address.items():
            existing = self._by_address.get(addr)
            if existing is None:
                self._by_address[addr] = info
                newly.add(addr)
                continue
            merged_first = _min_opt(existing.first_seen_block, info.first_seen_block)
            merged_last = _max_opt(existing.last_seen_block, info.last_seen_block)
            if merged_first == existing.first_seen_block and merged_last == existing.last_seen_block:
                continue
            self._by_address[addr] = SystemInfo(
                system_id=existing.system_id,
                address=existing.address,
                abi_name=existing.abi_name,
                first_seen_block=merged_first,
                last_seen_block=merged_last,
            )
        self._rebuild_system_id_index()
        return newly

    def to_snapshot_rows(self) -> list[tuple[str, str, str, int | None, int | None]]:
        """Rows for persistence into ``system_address_snapshot``."""
        return [
            (info.system_id, info.address, info.abi_name, info.first_seen_block, info.last_seen_block)
            for info in self._by_address.values()
        ]

    @classmethod
    def from_snapshot_rows(
        cls,
        rows: list[tuple[str, str, str, int | None, int | None]],
    ) -> "SystemRegistry":
        by_address: dict[str, SystemInfo] = {}
        for sid, addr, abi_name, fsb, lsb in rows:
            by_address[addr] = SystemInfo(
                system_id=sid,
                address=addr,
                abi_name=abi_name,
                first_seen_block=fsb,
                last_seen_block=lsb,
            )
        return cls(by_address)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """For persistence in ingest_cursor.metadata_json."""
        return {
            addr: {
                "system_id": s.system_id,
                "abi_name": s.abi_name,
                "first_seen_block": s.first_seen_block,
                "last_seen_block": s.last_seen_block,
            }
            for addr, s in self._by_address.items()
        }

    @classmethod
    def from_dict(cls, d: dict[str, dict[str, Any]]) -> "SystemRegistry":
        by_address = {
            addr: SystemInfo(
                system_id=v["system_id"],
                address=addr,
                abi_name=v["abi_name"],
                first_seen_block=v.get("first_seen_block"),
                last_seen_block=v.get("last_seen_block"),
            )
            for addr, v in d.items()
        }
        return cls(by_address)


def _min_opt(a: int | None, b: int | None) -> int | None:
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def _max_opt(a: int | None, b: int | None) -> int | None:
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


def _load_known_abi_files(abi_dir: Path) -> set[str]:
    return {p.name for p in abi_dir.glob("*System.json")}


def resolve_systems(
    client: ChainClient,
    world_address: str,
    abi_dir: Path,
    block_identifier: int | str | None = None,
) -> SystemRegistry:
    """Resolve all mapped system IDs via on-chain lookup.

    ``block_identifier`` selects the chain state against which the registry
    is probed. ``None`` (default) means the RPC's ``latest`` tag. Passing an
    integer pins the probe to a specific historical block — used by
    ``probe_historical_systems`` to capture redeployed system addresses.
    """
    known_abis = _load_known_abi_files(abi_dir)
    missing = [
        (sid, abi)
        for sid, abi in SYSTEM_ID_TO_ABI.items()
        if abi not in known_abis
    ]
    if missing:
        for sid, abi in missing:
            log.warning("system_registry: no ABI file for %s (expected %s)", sid, abi)

    world = client.w3.eth.contract(
        address=Web3.to_checksum_address(world_address),
        abi=WORLD_ABI,
    )
    registry_addr = client.call_contract_fn(
        world, "systems", block_identifier=block_identifier
    )
    log.info(
        "system_registry: systems registry at %s (block=%s)",
        registry_addr, block_identifier if block_identifier is not None else "latest",
    )

    registry = client.w3.eth.contract(
        address=Web3.to_checksum_address(registry_addr),
        abi=REGISTRY_ABI,
    )

    seen_block = block_identifier if isinstance(block_identifier, int) else None
    by_address: dict[str, SystemInfo] = {}
    for sid, abi_name in SYSTEM_ID_TO_ABI.items():
        if abi_name not in known_abis:
            continue
        sid_hash = int.from_bytes(keccak(text=sid), "big")
        entities = client.call_contract_fn(
            registry,
            "getEntitiesWithValue",
            sid_hash,
            block_identifier=block_identifier,
        )
        if not entities:
            log.debug("system_registry: %s not resolved at block=%s", sid, block_identifier)
            continue
        addr = Web3.to_checksum_address("0x" + format(entities[0], "040x"))
        if addr in by_address:
            log.warning(
                "system_registry: address %s collides between %s and %s at block=%s",
                addr, by_address[addr].system_id, sid, block_identifier,
            )
            continue
        by_address[addr] = SystemInfo(
            system_id=sid,
            address=addr,
            abi_name=abi_name,
            first_seen_block=seen_block,
            last_seen_block=seen_block,
        )

    log.info(
        "system_registry: resolved %d systems at block=%s",
        len(by_address), block_identifier if block_identifier is not None else "latest",
    )
    return SystemRegistry(by_address)


def probe_historical_systems(
    client: ChainClient,
    world_address: str,
    abi_dir: Path,
    block_heights: list[int],
) -> SystemRegistry:
    """Resolve the registry at each block height; union the results.

    Kamigotchi system contracts are redeployed periodically (~4 distinct
    ``system.harvest.start`` addresses observed in a 22-day window per
    session 2.5 findings). A single ``resolve_systems()`` call at head
    misses historical deployments, so any tx in the backfill window that
    targeted a previous deployment is silently dropped at the match step.

    This function probes each height in ``block_heights`` and unions all
    observed ``(system_id, address)`` pairs, widening
    ``first_seen_block`` / ``last_seen_block`` per address. The resulting
    registry's ``known_addresses()`` set covers every deployment active at
    any probed height.

    Cost: ~35 RPC calls per probe × len(block_heights). At ~250ms/call on
    the public Yominet RPC, 10 probes ≈ ~90s one-time startup cost.
    """
    if not block_heights:
        raise ValueError("block_heights must be non-empty")
    unique_sorted = sorted(set(block_heights))
    combined: SystemRegistry | None = None
    for h in unique_sorted:
        snap = resolve_systems(client, world_address, abi_dir, block_identifier=h)
        if combined is None:
            combined = snap
        else:
            combined.extend(snap)
    assert combined is not None
    log.info(
        "system_registry: unioned %d systems / %d addresses across %d probes",
        len(combined.system_ids()), len(combined), len(unique_sorted),
    )
    return combined


def evenly_spaced_probes(start: int, head: int, n: int = 10) -> list[int]:
    """Return ``n`` evenly-spaced block heights spanning ``[start, head]``.

    Guarantees ``start`` and ``head`` are always included; intermediate
    values are integer-spaced. Deduplication happens inside
    ``probe_historical_systems``, so a short window that collapses to
    fewer than ``n`` unique heights is handled cleanly.
    """
    if n < 2:
        return [head]
    if head <= start:
        return [head]
    step = (head - start) / (n - 1)
    heights = [int(round(start + i * step)) for i in range(n)]
    heights[0] = start
    heights[-1] = head
    return heights


def load_abi(abi_dir: Path, abi_name: str) -> list[dict[str, Any]]:
    with (abi_dir / abi_name).open() as f:
        return json.load(f)["abi"]
