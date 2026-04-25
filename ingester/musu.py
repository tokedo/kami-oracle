"""Decode MUSU bounty drains from a tx receipt.

MUSU is **not** an ERC-20. Per ``kami_context/chain.md`` (vendor SHA
332db78, "$MUSU (In-Game Currency)" section), MUSU is an in-game
inventory item (item index 1) tracked by the MUDS ``ValueComponent``
under entity id ``keccak256(abi.encodePacked("inventory.instance",
uint256 accountId, uint32 1))``. The harvest entity holds the
*unclaimed accrued bounty* on its own ``ValueComponent`` slot.

When ``system.harvest.collect`` / ``stop`` / ``liquidate`` runs, the
system writes the accrued bounty to the harvest entity, then drains
it to zero. Both writes are emitted by the World contract as
``ComponentValueSet(componentId, address component, uint256 entity,
bytes data)``. Walking those events and taking the max value written
to the action's ``harvest_id`` recovers the gross MUSU drained — no
prior-state read or 1e18 scaling needed.

See ``memory/decoder-notes.md`` "Session 7 — MUSU Transfer probe"
for the full derivation and validation samples.
"""

from __future__ import annotations

from typing import Any, Iterable

from web3 import Web3

# World contract on Yominet. Source: ``ingester/config.py::Config.world_address``
# and ``kami_context/chain.md`` "Currencies" section.
WORLD_ADDRESS_LOWER = "0x2729174c265dbbd8416c6449e0e813e88f43d0e7"

# topic0 = keccak256("ComponentValueSet(uint256,address,uint256,bytes)").
# Hardcoded constant (the World event signature is part of the MUDS ABI
# in ``kami_context/abi/World.json``; this mirrors that without an
# import-time keccak call).
COMPONENT_VALUE_SET_TOPIC0 = bytes.fromhex(
    "6ac31c38682e0128240cf68316d7ae751020d8f74c614e2a30278afcec8a6073"
)

# componentId = uint256(keccak256("component.value")) — the MUDS
# ValueComponent registered on World. Source: ``kami_context/system-ids.md``
# "Reading Inventory Balance" plus the "Key Components for Bot Development"
# table.
VALUE_COMPONENT_ID = (
    80678919686888423251211770875952264544944593537285580074425903087691541684961
)


def _decode_uint256_bytes(data: bytes) -> int | None:
    """Return the uint256 value packed inside an ABI-encoded ``bytes`` payload.

    The ``data`` field of a ``ComponentValueSet`` event is ABI-encoded
    ``bytes``: a 32-byte offset (always 0x20), a 32-byte length, then the
    content padded to a multiple of 32 bytes. For ``component.value``
    writes the content is a single 32-byte big-endian uint256. Returns
    None if the payload doesn't match that shape.
    """
    if len(data) < 96:
        return None
    length = int.from_bytes(data[32:64], "big")
    if length != 32:
        return None
    return int.from_bytes(data[64:96], "big")


def decode_musu_drains(receipt: Any) -> dict[int, int]:
    """Return ``{harvest_id_int: max_value}`` for every harvest entity drained
    in this receipt.

    Walks ``receipt['logs']`` once and, for each ``ComponentValueSet``
    event from the World contract on the ``component.value`` component,
    records the value written. The returned mapping uses the entity id
    (a uint256 carried in topics[3]) as the key; the value is the
    max uint256 written to that entity inside this tx — which is the
    pre-drain bounty, since the post-drain write is always 0.

    Pure function: no DB access, no chain calls. Filters silently
    skip logs from other contracts and other event signatures. Logs
    whose ABI-encoded ``data`` doesn't decode as a 32-byte uint256
    are skipped (could happen if MUDS ever stores a non-scalar value
    on ``component.value``; we haven't seen that on Yominet).
    """
    drains: dict[int, int] = {}
    logs: Iterable[Any] = receipt.get("logs") if isinstance(receipt, dict) else receipt["logs"]
    for log in logs:
        if _log_address_lower(log) != WORLD_ADDRESS_LOWER:
            continue
        topics = log["topics"]
        if not topics:
            continue
        if _topic_bytes(topics[0]) != COMPONENT_VALUE_SET_TOPIC0:
            continue
        if len(topics) < 4:
            continue
        component_id = int.from_bytes(_topic_bytes(topics[1]), "big")
        if component_id != VALUE_COMPONENT_ID:
            continue
        entity_id = int.from_bytes(_topic_bytes(topics[3]), "big")
        data = log["data"]
        if isinstance(data, str):
            data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        else:
            data = bytes(data)
        value = _decode_uint256_bytes(data)
        if value is None:
            continue
        prev = drains.get(entity_id)
        if prev is None or value > prev:
            drains[entity_id] = value
    return drains


def _log_address_lower(log: Any) -> str:
    """Web3 returns checksummed hex; normalize to lowercase 0x-prefixed."""
    addr = log["address"]
    if hasattr(addr, "hex"):
        addr = "0x" + addr.hex()
    return str(addr).lower()


def _topic_bytes(topic: Any) -> bytes:
    """Topics may be HexBytes, bytes, or hex strings; return raw bytes."""
    if isinstance(topic, (bytes, bytearray)):
        return bytes(topic)
    if hasattr(topic, "hex"):
        return bytes(topic)  # HexBytes is a bytes subclass
    s = str(topic)
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s)


def musu_amount_for_harvest(drains: dict[int, int], harvest_id: str | int | None) -> str | None:
    """Helper: look up the MUSU drain for a row by its decimal-string
    ``harvest_id``. Returns None if no harvest_id or no matching event.
    The returned amount is a decimal string (matches the ``amount`` column
    type) — there's no 1e18 scaling because MUSU is an integer item count,
    not an 18-decimal token.
    """
    if harvest_id is None or harvest_id == "":
        return None
    try:
        h = int(harvest_id)
    except (TypeError, ValueError):
        return None
    v = drains.get(h)
    if v is None:
        return None
    return str(v)


# Backwards-compatible aliases — let the old ``decode_musu_transfers`` name
# show up as a clear NotImplementedError if called, so the Session 6 mental
# model can't silently slip back in.
def decode_musu_transfers(receipt: Any) -> list:  # pragma: no cover
    raise NotImplementedError(
        "MUSU is not an ERC-20 — there is no Transfer event to decode. "
        "See memory/decoder-notes.md 'Session 7 — MUSU Transfer probe'. "
        "Use decode_musu_drains(receipt) instead."
    )
