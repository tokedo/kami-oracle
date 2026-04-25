"""Tests for ``ingester.musu.decode_musu_drains``.

Pure unit tests — no chain/DB I/O. The "real receipt" case loads a
fixture captured during Session 7 validation against tx
``0x59ecb09fa31053e0…`` (a harvest_collect for kami "Disco Wrench"
that drained 1001 MUSU). See
``memory/decoder-notes.md`` "Session 7 — MUSU Transfer probe" for
the recording details.
"""

from __future__ import annotations

import json
from pathlib import Path

from ingester.musu import (
    COMPONENT_VALUE_SET_TOPIC0,
    VALUE_COMPONENT_ID,
    WORLD_ADDRESS_LOWER,
    decode_musu_drains,
    musu_amount_for_harvest,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _abi_uint256_bytes_payload(value: int) -> str:
    """ABI-encode a single uint256 as ``bytes`` (offset + length + content)."""
    return (
        "0x"
        + (32).to_bytes(32, "big").hex()
        + (32).to_bytes(32, "big").hex()
        + value.to_bytes(32, "big").hex()
    )


def _topic_uint256(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def _make_cvs_log(*, log_index: int, address: str, component_id: int,
                  entity: int, value: int) -> dict:
    return {
        "logIndex": log_index,
        "address": address,
        "topics": [
            "0x" + COMPONENT_VALUE_SET_TOPIC0.hex(),
            _topic_uint256(component_id),
            _topic_uint256(0xdeadbeef),  # component address — unused
            _topic_uint256(entity),
        ],
        "data": _abi_uint256_bytes_payload(value),
    }


def test_empty_receipt_returns_empty_dict():
    assert decode_musu_drains({"logs": []}) == {}


def test_single_harvest_drain_returns_max_value():
    # Two writes to the same harvest entity: bounty then 0.
    receipt = {"logs": [
        _make_cvs_log(log_index=1, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=42, value=1001),
        _make_cvs_log(log_index=6, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=42, value=0),
    ]}
    assert decode_musu_drains(receipt) == {42: 1001}


def test_multiple_harvests_in_batched_tx():
    # Each harvest entity has its own (bounty, 0) pair. They should be
    # distinguished by entity id, not by log order.
    receipt = {"logs": [
        _make_cvs_log(log_index=1, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=11, value=300),
        _make_cvs_log(log_index=2, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=22, value=500),
        _make_cvs_log(log_index=3, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=11, value=0),
        _make_cvs_log(log_index=4, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=22, value=0),
    ]}
    assert decode_musu_drains(receipt) == {11: 300, 22: 500}


def test_filters_logs_from_other_contracts():
    # WETH Transfer events live at 0xE1Ff7038… — we must not record them
    # as MUSU drains. The Session 6 hand-off conflated the two.
    receipt = {"logs": [
        # Real MUSU drain on World
        _make_cvs_log(log_index=1, address=WORLD_ADDRESS_LOWER,
                      component_id=VALUE_COMPONENT_ID, entity=42, value=1001),
        # WETH Transfer at the WETH contract — wrong address; should be ignored
        {
            "logIndex": 2,
            "address": "0xE1Ff7038eAAAF027031688E1535a055B2Bac2546",
            "topics": [
                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                _topic_uint256(0xaaaa),
                _topic_uint256(0xbbbb),
            ],
            "data": _topic_uint256(123_456_789),
        },
    ]}
    assert decode_musu_drains(receipt) == {42: 1001}


def test_filters_other_event_signatures_on_world():
    # World emits ComponentRegistered and SystemRegistered too — different
    # topic0. They share an address with ComponentValueSet and must not
    # be misread.
    receipt = {"logs": [
        {
            "logIndex": 1,
            "address": WORLD_ADDRESS_LOWER,
            "topics": [
                "0x" + ("11" * 32),  # bogus topic0
                _topic_uint256(VALUE_COMPONENT_ID),
                _topic_uint256(0),
                _topic_uint256(99),
            ],
            "data": _abi_uint256_bytes_payload(1234),
        },
    ]}
    assert decode_musu_drains(receipt) == {}


def test_filters_other_components():
    # ExperienceComponent writes also flow through ComponentValueSet on
    # World; they must not be picked up as MUSU drains.
    EXPERIENCE_CID = (
        111865635092640564549274176173858093345993986210952789855877890964514230573789
    )
    receipt = {"logs": [
        _make_cvs_log(log_index=1, address=WORLD_ADDRESS_LOWER,
                      component_id=EXPERIENCE_CID, entity=42, value=10_000),
    ]}
    assert decode_musu_drains(receipt) == {}


def test_skips_non_uint256_payloads():
    # ValueComponent could in principle hold non-32-byte payloads (tested
    # defensively). Such writes should be skipped, not crash the decoder.
    receipt = {"logs": [
        {
            "logIndex": 1,
            "address": WORLD_ADDRESS_LOWER,
            "topics": [
                "0x" + COMPONENT_VALUE_SET_TOPIC0.hex(),
                _topic_uint256(VALUE_COMPONENT_ID),
                _topic_uint256(0xdeadbeef),
                _topic_uint256(42),
            ],
            # Length=64 instead of 32 → non-uint256 payload
            "data": (
                "0x"
                + (32).to_bytes(32, "big").hex()
                + (64).to_bytes(32, "big").hex()
                + ("ab" * 64)
            ),
        },
    ]}
    assert decode_musu_drains(receipt) == {}


def test_real_recorded_receipt_harvest_collect():
    # Recorded harvest_collect for kami 'Disco Wrench' on 2026-04-25.
    # Validation in memory/decoder-notes.md "Session 7" recorded the
    # gross MUSU drained as 1001 against harvest_id 81194157…498971494.
    with open(FIXTURE_DIR / "musu_collect_receipt.json") as f:
        receipt = json.load(f)
    drains = decode_musu_drains(receipt)
    expected_harvest_id = (
        81194157677225127244477197466586143695500790854237985918369451613091748971494
    )
    assert drains.get(expected_harvest_id) == 1001
    # And the convenience helper returns the decimal-string amount.
    assert musu_amount_for_harvest(drains, str(expected_harvest_id)) == "1001"
    assert musu_amount_for_harvest(drains, expected_harvest_id) == "1001"
    assert musu_amount_for_harvest(drains, None) is None
    assert musu_amount_for_harvest(drains, "999999") is None


def test_supports_hexbytes_topics():
    # web3.py returns topics as HexBytes (a bytes subclass). Our normalizer
    # should handle them transparently.
    receipt = {"logs": [
        {
            "logIndex": 1,
            "address": WORLD_ADDRESS_LOWER,
            "topics": [
                COMPONENT_VALUE_SET_TOPIC0,
                VALUE_COMPONENT_ID.to_bytes(32, "big"),
                (0).to_bytes(32, "big"),
                (42).to_bytes(32, "big"),
            ],
            "data": bytes.fromhex(
                _abi_uint256_bytes_payload(7).removeprefix("0x")
            ),
        },
    ]}
    assert decode_musu_drains(receipt) == {42: 7}
