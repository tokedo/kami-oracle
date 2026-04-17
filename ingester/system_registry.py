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


class SystemRegistry:
    """Holds the address -> system_id map + per-system ABI."""

    def __init__(self, by_address: dict[str, SystemInfo]):
        self._by_address = by_address
        self._by_system_id = {s.system_id: s for s in by_address.values()}

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

    def __len__(self) -> int:
        return len(self._by_address)

    def as_dict(self) -> dict[str, dict[str, str]]:
        """For persistence in ingest_cursor.metadata_json."""
        return {
            addr: {"system_id": s.system_id, "abi_name": s.abi_name}
            for addr, s in self._by_address.items()
        }

    @classmethod
    def from_dict(cls, d: dict[str, dict[str, str]]) -> "SystemRegistry":
        by_address = {
            addr: SystemInfo(
                system_id=v["system_id"],
                address=addr,
                abi_name=v["abi_name"],
            )
            for addr, v in d.items()
        }
        return cls(by_address)


def _load_known_abi_files(abi_dir: Path) -> set[str]:
    return {p.name for p in abi_dir.glob("*System.json")}


def resolve_systems(
    client: ChainClient,
    world_address: str,
    abi_dir: Path,
) -> SystemRegistry:
    """Resolve all mapped system IDs via on-chain lookup."""
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
    registry_addr = client.call_contract_fn(world, "systems")
    log.info("system_registry: systems registry at %s", registry_addr)

    registry = client.w3.eth.contract(
        address=Web3.to_checksum_address(registry_addr),
        abi=REGISTRY_ABI,
    )

    by_address: dict[str, SystemInfo] = {}
    for sid, abi_name in SYSTEM_ID_TO_ABI.items():
        if abi_name not in known_abis:
            continue
        sid_hash = int.from_bytes(keccak(text=sid), "big")
        entities = client.call_contract_fn(
            registry, "getEntitiesWithValue", sid_hash
        )
        if not entities:
            log.warning("system_registry: %s not resolved on-chain", sid)
            continue
        addr = Web3.to_checksum_address("0x" + format(entities[0], "040x"))
        if addr in by_address:
            log.warning(
                "system_registry: address %s collides between %s and %s",
                addr, by_address[addr].system_id, sid,
            )
            continue
        by_address[addr] = SystemInfo(system_id=sid, address=addr, abi_name=abi_name)

    log.info("system_registry: resolved %d systems", len(by_address))
    return SystemRegistry(by_address)


def load_abi(abi_dir: Path, abi_name: str) -> list[dict[str, Any]]:
    with (abi_dir / abi_name).open() as f:
        return json.load(f)["abi"]
