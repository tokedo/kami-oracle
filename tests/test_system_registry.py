"""Tests for the historical-snapshot registry fix.

Session 2.5 discovered that Kamigotchi system contracts are redeployed
periodically, so resolving the registry once at head misses older
deployments and silently drops historical txs at the match step. The
fix probes the registry at multiple block heights and unions the
resulting address sets.

These tests exercise ``SystemRegistry.extend`` and
``probe_historical_systems`` in isolation, with ``resolve_systems``
monkey-patched to return different addresses at different block tags.
No network I/O.
"""

from __future__ import annotations

import pytest
from web3 import Web3

from ingester import system_registry
from ingester.system_registry import (
    SystemInfo,
    SystemRegistry,
    evenly_spaced_probes,
    probe_historical_systems,
)


def _info(
    sid: str,
    addr: str,
    *,
    first: int | None = None,
    last: int | None = None,
    abi_name: str | None = None,
) -> SystemInfo:
    from ingester.system_registry import SYSTEM_ID_TO_ABI
    return SystemInfo(
        system_id=sid,
        address=Web3.to_checksum_address(addr),
        abi_name=abi_name or SYSTEM_ID_TO_ABI.get(sid, "GenericSystem.json"),
        first_seen_block=first,
        last_seen_block=last,
    )


def _reg(infos: list[SystemInfo]) -> SystemRegistry:
    return SystemRegistry({i.address: i for i in infos})


def test_extend_unions_addresses_for_same_system():
    a = _info("system.harvest.start", "0x" + "a" * 40, first=1000, last=1000)
    b = _info("system.harvest.start", "0x" + "b" * 40, first=2000, last=2000)
    reg = _reg([a])
    newly = reg.extend(_reg([b]))
    assert len(newly) == 1
    assert reg.addresses_for_system("system.harvest.start") == {a.address, b.address}
    # by_system_id picks the most-recently-observed deployment.
    assert reg.get_by_system_id("system.harvest.start").address == b.address


def test_extend_widens_first_last_seen_for_shared_address():
    addr = "0x" + "a" * 40
    a1 = _info("system.harvest.start", addr, first=1500, last=1500)
    a2 = _info("system.harvest.start", addr, first=1000, last=2000)
    reg = _reg([a1])
    newly = reg.extend(_reg([a2]))
    # Address already known — not "newly" added.
    assert newly == set()
    stored = reg.get_by_address(addr)
    assert stored.first_seen_block == 1000
    assert stored.last_seen_block == 2000


def test_extend_preserves_distinct_system_ids():
    h = _info("system.harvest.start", "0x" + "a" * 40, first=100, last=100)
    l = _info("system.kami.level", "0x" + "c" * 40, first=100, last=100)
    reg = _reg([h])
    reg.extend(_reg([l]))
    assert reg.system_ids() == {"system.harvest.start", "system.kami.level"}
    assert reg.known_addresses() == {h.address, l.address}


def test_probe_historical_unions_across_block_heights(monkeypatch):
    """Simulate 3 probes where the same system_id resolves to different
    addresses at different heights. The unioned registry must contain
    every address seen at any probe."""
    addr_a = Web3.to_checksum_address("0x" + "a" * 40)
    addr_b = Web3.to_checksum_address("0x" + "b" * 40)
    addr_c = Web3.to_checksum_address("0x" + "c" * 40)

    addr_by_height = {
        1000: addr_a,
        2000: addr_b,
        3000: addr_b,  # still the middle deployment
        4000: addr_c,
    }
    # Second system is stable across the window — mirrors real chain behavior
    # where most system IDs don't redeploy.
    stable_addr = Web3.to_checksum_address("0x" + "1" * 40)

    probe_calls: list[int | str | None] = []

    def fake_resolve(client, world, abi_dir, block_identifier=None):
        probe_calls.append(block_identifier)
        addr = addr_by_height[block_identifier]
        infos = {
            addr: SystemInfo(
                system_id="system.harvest.start",
                address=addr,
                abi_name="HarvestStartSystem.json",
                first_seen_block=block_identifier,
                last_seen_block=block_identifier,
            ),
            stable_addr: SystemInfo(
                system_id="system.kami.level",
                address=stable_addr,
                abi_name="KamiLevelSystem.json",
                first_seen_block=block_identifier,
                last_seen_block=block_identifier,
            ),
        }
        return SystemRegistry(infos)

    monkeypatch.setattr(system_registry, "resolve_systems", fake_resolve)

    reg = probe_historical_systems(None, "0xworld", None, [1000, 2000, 3000, 4000])

    assert probe_calls == [1000, 2000, 3000, 4000]
    # All three harvest addresses captured.
    assert reg.addresses_for_system("system.harvest.start") == {addr_a, addr_b, addr_c}
    # Stable system address widened to span the full window.
    stable = reg.get_by_address(stable_addr)
    assert stable.first_seen_block == 1000
    assert stable.last_seen_block == 4000
    # First/last on the middle deployment spans the two probes where it was seen.
    b_info = reg.get_by_address(addr_b)
    assert b_info.first_seen_block == 2000
    assert b_info.last_seen_block == 3000


def test_probe_historical_rejects_empty_heights():
    with pytest.raises(ValueError):
        probe_historical_systems(None, "0xworld", None, [])


def test_probe_historical_dedupes_duplicate_heights(monkeypatch):
    calls: list[int | str | None] = []

    def fake_resolve(client, world, abi_dir, block_identifier=None):
        calls.append(block_identifier)
        return SystemRegistry({})

    monkeypatch.setattr(system_registry, "resolve_systems", fake_resolve)
    probe_historical_systems(None, "0xworld", None, [5, 5, 10, 10, 10])
    # Duplicates collapsed.
    assert calls == [5, 10]


def test_evenly_spaced_probes_spans_window():
    heights = evenly_spaced_probes(100, 1000, n=10)
    assert heights[0] == 100
    assert heights[-1] == 1000
    assert len(heights) == 10
    # Monotonic nondecreasing.
    for a, b in zip(heights, heights[1:]):
        assert a <= b


def test_evenly_spaced_probes_handles_degenerate_windows():
    assert evenly_spaced_probes(100, 100, n=10) == [100]
    assert evenly_spaced_probes(100, 1000, n=1) == [1000]


def test_snapshot_roundtrip_preserves_first_last_seen():
    a = _info("system.harvest.start", "0x" + "a" * 40, first=100, last=500)
    b = _info("system.harvest.start", "0x" + "b" * 40, first=600, last=900)
    reg = _reg([a, b])
    rows = reg.to_snapshot_rows()
    restored = SystemRegistry.from_snapshot_rows(rows)
    assert restored.addresses_for_system("system.harvest.start") == {a.address, b.address}
    assert restored.get_by_address(a.address).first_seen_block == 100
    assert restored.get_by_address(b.address).last_seen_block == 900


def test_decoder_routes_historical_address_by_system_id():
    """The key invariant of the fix: the decoder dispatches on system_id,
    so a historical address resolves to the same ABI as the current one."""
    from pathlib import Path
    from ingester.decoder import Decoder

    abi_dir = Path(__file__).resolve().parent.parent / "kami_context" / "abi"
    cur = _info("system.harvest.stop", "0x" + "a" * 40, first=2000, last=2000)
    old = _info("system.harvest.stop", "0x" + "b" * 40, first=1000, last=1000)
    # Both addresses tagged to the same system_id / ABI file.
    reg = _reg([cur, old])
    dec = Decoder(abi_dir, reg)

    # A real HarvestStop executeTyped(uint256) calldata, decodable under the
    # vendored HarvestStopSystem.json. Sent to the OLD address — the fix is
    # that this still decodes.
    calldata = bytes.fromhex(
        "3e991df3"
        "b6832b52ae4e5ae30f01d6efb8a5a3c0c2ee4f90dc3478d1ed35cfbe2c37e44f"
    )
    r = dec.decode_tx(
        tx_hash="0x" + "ff" * 32,
        from_addr="0x0000000000000000000000000000000000000001",
        to_addr=old.address,
        calldata=calldata,
        block_number=1500, block_timestamp=1, status=1,
    )
    assert r.status == "ok"
    assert len(r.actions) == 1
    assert r.actions[0].action_type == "harvest_stop"
    assert r.actions[0].system_id == "system.harvest.stop"
