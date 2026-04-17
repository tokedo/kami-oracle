"""Decoder unit tests using canned on-chain calldata samples.

These tests avoid any network I/O. A minimal fake ``SystemRegistry`` is
constructed directly, pointing to the vendored ABIs.

Samples are real Yominet txs captured during Session 1 validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingester.decoder import Decoder
from ingester.system_registry import SYSTEM_ID_TO_ABI, SystemInfo, SystemRegistry

ROOT = Path(__file__).resolve().parent.parent
ABI_DIR = ROOT / "kami_context" / "abi"


def _registry_for(system_ids: list[str]) -> SystemRegistry:
    # Synthesize fake addresses; decoder only cares about the system_id/abi.
    by_address: dict[str, SystemInfo] = {}
    for i, sid in enumerate(system_ids):
        abi_name = SYSTEM_ID_TO_ABI[sid]
        addr = "0x" + format(i + 1, "040x")
        from web3 import Web3
        addr_cs = Web3.to_checksum_address(addr)
        by_address[addr_cs] = SystemInfo(system_id=sid, address=addr_cs, abi_name=abi_name)
    return SystemRegistry(by_address)


def _addr_for(reg: SystemRegistry, sid: str) -> str:
    for a, info in reg._by_address.items():  # noqa: SLF001
        if info.system_id == sid:
            return a
    raise KeyError(sid)


@pytest.fixture()
def decoder_and_addrs():
    reg = _registry_for([
        "system.harvest.start",
        "system.harvest.stop",
        "system.harvest.collect",
        "system.kami.use.item",
        "system.kami.level",
        "system.skill.upgrade",
    ])
    dec = Decoder(ABI_DIR, reg)
    return dec, reg


def test_harvest_stop_executeTyped(decoder_and_addrs):
    """Canned: single-arg executeTyped(uint256) on HarvestStopSystem."""
    dec, reg = decoder_and_addrs
    addr = _addr_for(reg, "system.harvest.stop")
    # Real calldata from block 27778229.
    calldata = bytes.fromhex(
        "3e991df3"
        "b6832b52ae4e5ae30f01d6efb8a5a3c0c2ee4f90dc3478d1ed35cfbe2c37e44f"
    )
    r = dec.decode_tx(
        tx_hash="0x7df0f9d5791eb46cc0fd8f83b37987b7164123eff34269bc511c88fc2069cb01",
        from_addr="0x1f8a82d89B666e2aC8C1B9682a34ed699C587e96",
        to_addr=addr,
        calldata=calldata,
        block_number=27778229,
        block_timestamp=1744900000,
        status=1,
    )
    assert r.status == "ok"
    assert len(r.actions) == 1
    a = r.actions[0]
    assert a.action_type == "harvest_stop"
    assert a.kami_id is None           # harvest.stop takes harvest instance ID
    assert a.metadata["harvest_id"] == str(
        0xb6832b52ae4e5ae30f01d6efb8a5a3c0c2ee4f90dc3478d1ed35cfbe2c37e44f
    )


def test_harvest_start_executeTyped_4arg_overlay(decoder_and_addrs):
    """4-arg executeTyped not in vendored JSON ABI — served by overlay."""
    dec, reg = decoder_and_addrs
    addr = _addr_for(reg, "system.harvest.start")
    # executeTyped(uint256,uint32,uint256,uint256): kamiID=7, nodeIndex=16,
    # taxerID=0, taxAmt=0
    calldata = bytes.fromhex(
        "c8372a87"
        + "0000000000000000000000000000000000000000000000000000000000000007"  # kami
        + "0000000000000000000000000000000000000000000000000000000000000010"  # node=16
        + "0000000000000000000000000000000000000000000000000000000000000000"  # taxer
        + "0000000000000000000000000000000000000000000000000000000000000000"  # taxAmt
    )
    r = dec.decode_tx(
        tx_hash="0x" + "11" * 32,
        from_addr="0x0000000000000000000000000000000000000001",
        to_addr=addr,
        calldata=calldata,
        block_number=1, block_timestamp=1, status=1,
    )
    assert r.status == "ok", r.reason
    assert len(r.actions) == 1
    a = r.actions[0]
    assert a.action_type == "harvest_start"
    assert a.kami_id == "7"
    assert a.node_id == "16"
    assert a.metadata["taxer_id"] == "0"
    assert a.metadata["tax_amt"] == "0"


def test_harvest_start_executeBatched_fans_out(decoder_and_addrs):
    """executeBatched(uint256[] kamiIDs, ...) should fan out to one row per kami."""
    dec, reg = decoder_and_addrs
    addr = _addr_for(reg, "system.harvest.start")
    # executeBatched with 2 kami IDs at node 42.
    # Layout: selector + offset_to_array(0x80) + nodeIndex + taxer + taxAmt + [len, elem0, elem1]
    payload = (
        "0000000000000000000000000000000000000000000000000000000000000080"  # offset to dynamic array
        "000000000000000000000000000000000000000000000000000000000000002a"  # node=42
        "0000000000000000000000000000000000000000000000000000000000000000"  # taxer
        "0000000000000000000000000000000000000000000000000000000000000000"  # taxAmt
        "0000000000000000000000000000000000000000000000000000000000000002"  # len=2
        "0000000000000000000000000000000000000000000000000000000000000001"  # k1
        "0000000000000000000000000000000000000000000000000000000000000002"  # k2
    )
    calldata = bytes.fromhex("68f37c94" + payload)
    r = dec.decode_tx(
        tx_hash="0x" + "22" * 32,
        from_addr="0x0000000000000000000000000000000000000001",
        to_addr=addr,
        calldata=calldata,
        block_number=1, block_timestamp=1, status=1,
    )
    assert r.status == "ok", r.reason
    assert len(r.actions) == 2
    assert [a.kami_id for a in r.actions] == ["1", "2"]
    assert all(a.node_id == "42" for a in r.actions)
    assert r.actions[0].sub_index == 0
    assert r.actions[1].sub_index == 1


def test_kami_use_item_feed(decoder_and_addrs):
    """KamiUseItem: executeTyped(uint256 kamiID, uint32 itemIndex) -> feed."""
    dec, reg = decoder_and_addrs
    addr = _addr_for(reg, "system.kami.use.item")
    # executeTyped(uint256,uint32): kami=99, item=11301
    calldata = bytes.fromhex(
        "e60f3a76"
        + "0000000000000000000000000000000000000000000000000000000000000063"  # kami=99
        + "0000000000000000000000000000000000000000000000000000000000002c25"  # item=11301
    )
    r = dec.decode_tx(
        tx_hash="0x" + "33" * 32,
        from_addr="0x0000000000000000000000000000000000000001",
        to_addr=addr,
        calldata=calldata,
        block_number=1, block_timestamp=1, status=1,
    )
    assert r.status == "ok"
    assert len(r.actions) == 1
    a = r.actions[0]
    assert a.action_type == "feed"
    assert a.kami_id == "99"
    assert a.item_index == 11301


def test_unknown_system_address_returns_error(decoder_and_addrs):
    dec, _reg = decoder_and_addrs
    r = dec.decode_tx(
        tx_hash="0xabc",
        from_addr="0x0000000000000000000000000000000000000002",
        to_addr="0x00000000000000000000000000000000000000dd",
        calldata=b"\x00" * 36,
        block_number=1, block_timestamp=1, status=1,
    )
    assert r.status == "unknown_system"
    assert r.actions == []


def test_unknown_selector_is_logged(decoder_and_addrs):
    dec, reg = decoder_and_addrs
    addr = _addr_for(reg, "system.kami.level")
    # Totally fake selector on a known system.
    calldata = bytes.fromhex("deadbeef" + "00" * 32)
    r = dec.decode_tx(
        tx_hash="0xabc",
        from_addr="0x0000000000000000000000000000000000000002",
        to_addr=addr,
        calldata=calldata,
        block_number=1, block_timestamp=1, status=1,
    )
    assert r.status == "unknown_selector"
    assert r.selector_hex == "0xdeadbeef"
    assert r.actions == []
