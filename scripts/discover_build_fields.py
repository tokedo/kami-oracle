#!/usr/bin/env python
"""Session 10 — discovery dump for build fields (chain-side audit).

One-shot exploratory script. Resolves the World's components registry,
looks up SlotsComponent / IDOwnsSkillComponent / IndexSkillComponent /
SkillPointComponent / IDOwnsInventoryComponent (and tries
component.id.equipment.owns), then dumps the raw on-chain build state
for one bpeon fixture kami so the founder can eyeball it against the
in-game UI.

Output is written to memory/session-10-discovery.txt (and also stdout).

Usage:
    .venv/bin/python scripts/discover_build_fields.py [--kami-id ID]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eth_utils import keccak  # noqa: E402
from web3 import Web3  # noqa: E402

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import load_config  # noqa: E402
from ingester.system_registry import REGISTRY_ABI, load_abi  # noqa: E402

log = logging.getLogger("discover_build")

# Bpeon's kami #43 "Zephyr" — fixture used since Session 7.
ZEPHYR_KAMI_ID = "28257207240752812050526875800976233322376494609598859084860556459780762796410"

# Components we want to resolve, with the ABI filename to attach.
COMPONENTS = [
    ("component.stat.slots", "SlotsComponent.json"),
    ("component.id.skill.owns", "IDOwnsSkillComponent.json"),
    ("component.index.skill", "IndexSkillComponent.json"),
    ("component.skill.point", "SkillPointComponent.json"),
    # Equipment is documented in state-reading.md but not in the vendored
    # components registry — try it anyway, expect None.
    ("component.id.equipment.owns", "IDOwnsSkillComponent.json"),  # ABI shape match
    ("component.index.item", "IndexItemComponent.json"),
    ("component.for.string", "ForStringComponent.json"),
]

WORLD_ABI_FRAGMENT = [
    {"name": "components", "type": "function", "inputs": [],
     "outputs": [{"type": "address"}], "stateMutability": "view"},
]


def resolve_component_address(client: ChainClient, components_registry, name: str) -> str | None:
    """Look up a component's contract address by its string ID."""
    name_hash = int.from_bytes(keccak(text=name), "big")
    entities = client.call_contract_fn(components_registry, "getEntitiesWithValue", name_hash)
    if not entities:
        return None
    return Web3.to_checksum_address("0x" + format(entities[0], "040x"))


def stat_total(base: int, shift: int, boost: int) -> int:
    """Effective stat from kamigotchi-context state-reading.md / health.md.

        effective = max(0, floor((1000 + boost) * (base + shift) / 1000))
    """
    raw = (1000 + boost) * (base + shift)
    if raw <= 0:
        return 0
    return raw // 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kami-id", default=ZEPHYR_KAMI_ID,
                    help="kami entity id (default: bpeon's Zephyr)")
    ap.add_argument("--out", default=str(REPO_ROOT / "memory" / "session-10-discovery.txt"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("RPC not reachable: %s", cfg.rpc_url)
        return 1

    abi_dir = cfg.abi_dir
    out_lines: list[str] = []
    def emit(s: str = "") -> None:
        print(s)
        out_lines.append(s)

    emit(f"# Session 10 — build-fields discovery dump")
    emit(f"# kami_id = {args.kami_id}")
    emit(f"# Yominet head block @ {client.block_number()}")
    emit("")

    # 1. Resolve the components registry.
    world = client.w3.eth.contract(
        address=Web3.to_checksum_address(cfg.world_address), abi=WORLD_ABI_FRAGMENT,
    )
    components_addr = client.call_contract_fn(world, "components")
    emit(f"World.components() = {components_addr}")
    components_registry = client.w3.eth.contract(
        address=Web3.to_checksum_address(components_addr), abi=REGISTRY_ABI,
    )

    # 2. Resolve each component address.
    addrs: dict[str, str | None] = {}
    for name, _abi in COMPONENTS:
        addr = resolve_component_address(client, components_registry, name)
        addrs[name] = addr
        emit(f"  {name:40s} -> {addr if addr else 'NOT FOUND'}")
    emit("")

    # 3. getKami(kami_id) — already used by populator.
    getter_abi = load_abi(abi_dir, "GetterSystem.json")
    # We need GetterSystem address. Re-resolve via systems().
    world_full = client.w3.eth.contract(
        address=Web3.to_checksum_address(cfg.world_address),
        abi=WORLD_ABI_FRAGMENT + [
            {"name": "systems", "type": "function", "inputs": [],
             "outputs": [{"type": "address"}], "stateMutability": "view"},
        ],
    )
    systems_addr = client.call_contract_fn(world_full, "systems")
    systems_registry = client.w3.eth.contract(
        address=Web3.to_checksum_address(systems_addr), abi=REGISTRY_ABI,
    )
    getter_id_hash = int.from_bytes(keccak(text="system.getter"), "big")
    entities = client.call_contract_fn(systems_registry, "getEntitiesWithValue", getter_id_hash)
    getter_addr = Web3.to_checksum_address("0x" + format(entities[0], "040x"))
    emit(f"GetterSystem = {getter_addr}")
    getter = client.w3.eth.contract(address=getter_addr, abi=getter_abi)

    kid_int = int(args.kami_id)
    shape = client.call_contract_fn(getter, "getKami", kid_int)
    _id, kami_index, name, _media, stats, traits, affinities, account, level, xp, room, state = shape
    health, power, harmony, violence = stats
    emit("")
    emit(f"## getKami({args.kami_id}) — high level")
    emit(f"  kami_index = {kami_index}")
    emit(f"  name       = {name}")
    emit(f"  account    = {account}")
    emit(f"  level      = {level}")
    emit(f"  xp         = {xp}")
    emit(f"  room       = {room}")
    emit(f"  state      = {state}")
    emit(f"  affinities = {list(affinities)}")
    emit("")
    emit("## stats — (base, shift, boost, sync) per stat; effective per kamigotchi-context formula")
    for sname, t in [("health", health), ("power", power), ("harmony", harmony), ("violence", violence)]:
        b, sh, bo, sy = int(t[0]), int(t[1]), int(t[2]), int(t[3])
        eff = stat_total(b, sh, bo)
        emit(f"  {sname:10s} base={b:>5} shift={sh:>5} boost={bo:>5} sync={sy:>5} -> effective={eff}")

    # 4. SlotsComponent.safeGet(kami_id) — does it exist? Read it.
    slots_addr = addrs.get("component.stat.slots")
    if slots_addr:
        slots_abi = load_abi(abi_dir, "SlotsComponent.json")
        slots = client.w3.eth.contract(address=Web3.to_checksum_address(slots_addr), abi=slots_abi)
        try:
            slot_tuple = client.call_contract_fn(slots, "safeGet", kid_int)
            sb, ss, sbo, ssy = int(slot_tuple[0]), int(slot_tuple[1]), int(slot_tuple[2]), int(slot_tuple[3])
            emit("")
            emit(f"## SlotsComponent.safeGet({args.kami_id})")
            emit(f"  slots base={sb} shift={ss} boost={sbo} sync={ssy} -> effective={stat_total(sb, ss, sbo)}")
        except Exception as e:  # noqa: BLE001
            emit(f"  SlotsComponent.safeGet failed: {e!r}")

    # 5. Skills enumeration.
    skills_owns_addr = addrs.get("component.id.skill.owns")
    idx_skill_addr = addrs.get("component.index.skill")
    skill_pt_addr = addrs.get("component.skill.point")
    skill_entities: list[int] = []
    if skills_owns_addr:
        owns_abi = load_abi(abi_dir, "IDOwnsSkillComponent.json")
        owns = client.w3.eth.contract(address=Web3.to_checksum_address(skills_owns_addr), abi=owns_abi)
        try:
            skill_entities = list(client.call_contract_fn(owns, "getEntitiesWithValue", kid_int))
        except Exception as e:  # noqa: BLE001
            emit(f"  IDOwnsSkillComponent.getEntitiesWithValue failed: {e!r}")
    emit("")
    emit(f"## skills — {len(skill_entities)} entities owned by kami")
    if skill_entities and idx_skill_addr and skill_pt_addr:
        idx_abi = load_abi(abi_dir, "IndexSkillComponent.json")
        pt_abi = load_abi(abi_dir, "SkillPointComponent.json")
        idx_c = client.w3.eth.contract(address=Web3.to_checksum_address(idx_skill_addr), abi=idx_abi)
        pt_c = client.w3.eth.contract(address=Web3.to_checksum_address(skill_pt_addr), abi=pt_abi)
        skills_decoded: list[dict] = []
        for sid in skill_entities:
            sidx = int(client.call_contract_fn(idx_c, "safeGet", sid))
            spt = int(client.call_contract_fn(pt_c, "safeGet", sid))
            skills_decoded.append({"index": sidx, "points": spt})
            emit(f"  skill_entity={sid}  index={sidx}  points={spt}")
        emit("")
        emit(f"  skills_json = {json.dumps(skills_decoded)}")

    # 6. Equipment enumeration (best-effort; expected to fail).
    equip_owns_addr = addrs.get("component.id.equipment.owns")
    emit("")
    emit("## equipment")
    if equip_owns_addr is None:
        emit("  component.id.equipment.owns NOT FOUND in world.components()")
        emit("  -> Stage 1 deferral: equipment_json column dropped from this migration")
    else:
        emit(f"  resolved at {equip_owns_addr} — try enumerating")
        owns_abi = load_abi(abi_dir, "IDOwnsSkillComponent.json")
        owns = client.w3.eth.contract(address=Web3.to_checksum_address(equip_owns_addr), abi=owns_abi)
        try:
            ents = list(client.call_contract_fn(owns, "getEntitiesWithValue", kid_int))
            emit(f"  equipment entities = {ents}")
        except Exception as e:  # noqa: BLE001
            emit(f"  enumeration failed: {e!r}")

    Path(args.out).write_text("\n".join(out_lines) + "\n")
    emit("")
    emit(f"# wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
