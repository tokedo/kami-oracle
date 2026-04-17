"""Decode Kamigotchi system-contract transactions into ``kami_action`` rows.

Design:

* For each known system we load its vendored ABI and build a map from
  4-byte selector to ``(fn_name, inputs)`` where ``inputs`` is the ABI
  input spec.
* Given a raw tx, we look up the system by ``to`` address, match the
  selector against the system's ABI, and decode args with ``eth_abi``.
* ``execute(bytes)`` is decoded by recursively ABI-decoding the inner
  bytes against the system's ``executeTyped(...)`` signature (when one
  exists).
* Batched calls (any input arg whose type is an array) fan out — one
  row per array element, with scalar args copied across.
* The action mapping translates ABI-named inputs into the four
  first-class columns of ``kami_action`` (``kami_id``, ``target_kami_id``,
  ``node_id``, ``amount``, ``item_index``). Everything else goes into
  ``metadata_json``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import keccak

from .system_registry import SystemInfo, SystemRegistry, load_abi

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System -> action_type mapping.
# ---------------------------------------------------------------------------

# Supplemental function signatures not present in the vendored JSON ABIs but
# documented in ``kami_context/system-ids.md``. Adding them here is NOT
# guessing — each entry is cited against a doc anchor (see comments).
#
# Format: system_id -> list of (fn_name, [(arg_name, arg_type), ...]).
# Args are fanned out on arrays by ``_args_to_actions`` automatically, so the
# batched forms below simply use array types on the primary fan-out arg.
SYSTEM_ABI_OVERLAY: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
    # "Non-Standard Entry Points" table in system-ids.md:
    #   system.harvest.start: executeTyped(uint256 kamiID, uint32 nodeIndex,
    #                                      uint256 taxerID, uint256 taxAmt)
    #                          executeBatched(uint256[] kamiIDs, uint32 nodeIndex,
    #                                         uint256 taxerID, uint256 taxAmt)
    "system.harvest.start": [
        ("executeTyped", [
            ("kamiID",     "uint256"),
            ("nodeIndex",  "uint32"),
            ("taxerID",    "uint256"),
            ("taxAmt",     "uint256"),
        ]),
        ("executeBatched", [
            ("kamiIDs",    "uint256[]"),
            ("nodeIndex",  "uint32"),
            ("taxerID",    "uint256"),
            ("taxAmt",     "uint256"),
        ]),
    ],
    # system.harvest.stop: executeBatched / executeBatchedAllowFailure /
    # executeAllowFailure (all variants documented in system-ids.md).
    "system.harvest.stop": [
        ("executeBatched",                [("ids", "uint256[]")]),
        ("executeBatchedAllowFailure",    [("ids", "uint256[]")]),
        ("executeAllowFailure",           [("arguments", "bytes")]),
    ],
    "system.harvest.collect": [
        ("executeBatched",                [("ids", "uint256[]")]),
        ("executeBatchedAllowFailure",    [("ids", "uint256[]")]),
        ("executeAllowFailure",           [("arguments", "bytes")]),
    ],
    # system.craft: deployed CraftSystem bytecode at
    # 0xd5dDd9102900cbF6277e16D3eECa9686F2531951 contains selector 0x5c817c70
    # (executeTyped(uint32,uint256)) and does NOT contain the documented
    # 3-arg 0xa693008c. Vendored CraftSystem.json is stale — confirmed
    # off-VM 2026-04-17. Sample decodes (34,2), (29,1), (6,2) match
    # (recipe_index, amount).
    "system.craft": [
        ("executeTyped", [
            ("index", "uint32"),
            ("amt",   "uint256"),
        ]),
    ],
}


SYSTEM_TO_ACTION: dict[str, str] = {
    "system.harvest.start":      "harvest_start",
    "system.harvest.stop":       "harvest_stop",
    "system.harvest.collect":    "harvest_collect",
    "system.harvest.liquidate":  "harvest_liquidate",
    "system.account.move":       "move",
    "system.account.register":   "register",
    "system.account.fund":       "account_fund",
    "system.account.set.name":   "account_set_name",
    "system.account.set.operator": "account_set_operator",
    "system.account.use.item":   "item_use",
    "system.kami.use.item":      "feed",
    "system.kami.level":         "lvlup",
    "system.kami.name":          "kami_name",
    "system.kami.gacha.mint":    "gacha_mint",
    "system.kami.gacha.reroll":  "gacha_reroll",
    "system.skill.upgrade":      "skill_upgrade",
    "system.skill.respec":       "skill_respec",
    "system.quest.accept":       "quest_accept",
    "system.quest.complete":     "quest_complete",
    "system.quest.drop":         "quest_drop",
    "system.craft":              "item_craft",
    "system.item.burn":          "item_burn",
    "system.droptable.item.reveal": "droptable_reveal",
    "system.friend.request":     "friend_request",
    "system.friend.accept":      "friend_accept",
    "system.friend.cancel":      "friend_cancel",
    "system.friend.block":       "friend_block",
    "system.goal.contribute":    "goal_contribute",
    "system.goal.claim":         "goal_claim",
    "system.scavenge.claim":     "scavenge_claim",
    "system.listing.buy":        "listing_buy",
    "system.listing.sell":       "listing_sell",
    "system.relationship.advance": "relationship_advance",
    "system.echo.kamis":         "echo_kamis",
    "system.echo.room":          "echo_room",
}

# Per-system field map: arg_name -> column or ("metadata", meta_key).
#
# Arg names come from the ABI's ``name`` field. Explicit mappings are needed
# because the same arg name means different things across systems (``id`` is
# a kami entity ID in KamiLevel but a harvest-instance ID in HarvestStop;
# ``targetID`` on SkillReset is the kami being respec'd, not a PvP target).
#
# Form:
#   "<system.id>": {
#     "<arg_name>": "<column>"                # first-class column
#     "<arg_name>": ("metadata", "<meta_key>"),  # bucket into metadata_json
#   }
#
# Args not listed fall through to metadata_json with their raw arg name.
SYSTEM_FIELD_MAP: dict[str, dict[str, Any]] = {
    # Harvest
    "system.harvest.start": {
        "kamiID":    "kami_id",
        "kamiIDs":   "kami_id",        # fanned out from executeBatched
        "nodeID":    "node_id",        # 2-arg executeTyped (vendored JSON)
        "nodeIndex": "node_id",        # 4-arg overlay variant
        "taxerID":   ("metadata", "taxer_id"),
        "taxAmt":    ("metadata", "tax_amt"),
    },
    "system.harvest.stop": {
        "id":  ("metadata", "harvest_id"),
        "ids": ("metadata", "harvest_id"),   # batched variant (one row per id)
    },
    "system.harvest.collect": {
        "id":  ("metadata", "harvest_id"),
        "ids": ("metadata", "harvest_id"),
    },
    "system.harvest.liquidate": {
        "killerID": "kami_id",
        "victimHarvID": ("metadata", "victim_harvest_id"),
    },
    # Kami
    "system.kami.level": {
        "id": "kami_id",
    },
    "system.kami.name": {
        "id": "kami_id",
    },
    "system.kami.use.item": {
        "kamiID": "kami_id",
        "itemIndex": "item_index",
    },
    "system.kami.gacha.mint": {
        "amount": "amount",
    },
    # Skills
    "system.skill.upgrade": {
        "holderID": "kami_id",
        "skillIndex": ("metadata", "skill_index"),
    },
    "system.skill.respec": {
        "targetID": "kami_id",
    },
    # Account
    "system.account.move": {
        "toIndex": "node_id",  # we store room index in node_id for moves
    },
    "system.account.use.item": {
        "itemIndex": "item_index",
        "amt": "amount",
    },
    # Items / craft
    "system.craft": {
        "assignerID": ("metadata", "assigner_id"),
        "index": ("metadata", "recipe_index"),
        "amt": "amount",
    },
    "system.item.burn": {
        "indices": "item_index",
        "amts": "amount",
    },
    "system.droptable.item.reveal": {
        "ids": ("metadata", "commit_id"),
    },
    # Quest
    "system.quest.accept": {
        "assignerID": ("metadata", "assigner_id"),
        "index": ("metadata", "quest_index"),
    },
    "system.quest.complete": {
        "id": ("metadata", "quest_id"),
    },
    "system.quest.drop": {
        "id": ("metadata", "quest_id"),
    },
    # Friend
    "system.friend.request": {},
    "system.friend.accept": {
        "requestID": ("metadata", "request_id"),
    },
    "system.friend.cancel": {
        "id": ("metadata", "request_id"),
    },
    "system.friend.block": {},
    # Goal / Scavenge
    "system.goal.contribute": {
        "goalIndex": ("metadata", "goal_index"),
        "amt": "amount",
    },
    "system.goal.claim": {
        "goalIndex": ("metadata", "goal_index"),
    },
    "system.scavenge.claim": {
        "id": ("metadata", "scavenge_id"),
    },
    # Listing
    "system.listing.buy": {
        "merchantIndex": ("metadata", "merchant_index"),
        "itemIndices": "item_index",
        "amts": "amount",
    },
    "system.listing.sell": {
        "merchantIndex": ("metadata", "merchant_index"),
        "itemIndices": "item_index",
        "amts": "amount",
    },
    # Relationship
    "system.relationship.advance": {
        "npcIndex": ("metadata", "npc_index"),
        "relIndex": ("metadata", "rel_index"),
    },
}


# ---------------------------------------------------------------------------
# Internal ABI representation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbiInput:
    name: str
    type_: str  # canonical ABI type, e.g. "uint256", "uint32[]"


@dataclass(frozen=True)
class AbiFn:
    name: str
    selector: bytes  # 4 bytes
    inputs: tuple[AbiInput, ...]

    @property
    def signature(self) -> str:
        return f"{self.name}({','.join(i.type_ for i in self.inputs)})"


def _canonicalize_type(t: str) -> str:
    # ABI JSON types are already canonical here; kept as a pass-through hook
    # so we could handle tuples if they ever show up in system entry points.
    return t


def _build_fn_index(abi: list[dict[str, Any]]) -> dict[bytes, AbiFn]:
    by_selector: dict[bytes, AbiFn] = {}
    for item in abi:
        if item.get("type") != "function":
            continue
        inputs = tuple(
            AbiInput(name=i.get("name", ""), type_=_canonicalize_type(i["type"]))
            for i in item.get("inputs", [])
        )
        sig = f"{item['name']}({','.join(i.type_ for i in inputs)})"
        selector = keccak(text=sig)[:4]
        by_selector[selector] = AbiFn(name=item["name"], selector=selector, inputs=inputs)
    return by_selector


# ---------------------------------------------------------------------------
# Decoded row representation.
# ---------------------------------------------------------------------------


@dataclass
class DecodedAction:
    tx_hash: str
    sub_index: int
    block_number: int
    block_timestamp: int       # unix seconds; caller converts to TIMESTAMP
    action_type: str
    system_id: str
    from_addr: str
    status: int
    kami_id: str | None = None
    target_kami_id: str | None = None
    node_id: str | None = None
    amount: str | None = None
    item_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def metadata_json(self) -> str:
        return json.dumps(self.metadata, sort_keys=True, default=_jsonable)


def _jsonable(v: Any) -> Any:
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, int):
        return str(v)  # uint256 can exceed JS integer range; stringify for safety
    raise TypeError(f"not JSON-serializable: {type(v).__name__}")


# ---------------------------------------------------------------------------
# Decode outcomes.
# ---------------------------------------------------------------------------


class DecodeResult:
    """Container for one tx's decode outcome."""

    __slots__ = ("actions", "status", "reason", "selector_hex", "raw_args")

    def __init__(
        self,
        actions: list[DecodedAction],
        status: str,
        reason: str | None = None,
        selector_hex: str | None = None,
        raw_args: Any = None,
    ):
        self.actions = actions
        self.status = status            # "ok" | "unknown_system" | "unknown_selector" | "decode_error"
        self.reason = reason
        self.selector_hex = selector_hex
        self.raw_args = raw_args


# ---------------------------------------------------------------------------
# Decoder.
# ---------------------------------------------------------------------------


class Decoder:
    def __init__(self, abi_dir: Path, registry: SystemRegistry):
        self.abi_dir = abi_dir
        self.registry = registry
        # Per-system selector index.
        self._index: dict[str, dict[bytes, AbiFn]] = {}
        self._typed_fn: dict[str, AbiFn] = {}  # preferred executeTyped per system
        self._load_all()

    def _load_all(self) -> None:
        for info in self.registry._by_address.values():  # noqa: SLF001
            abi = load_abi(self.abi_dir, info.abi_name)
            fn_idx = _build_fn_index(abi)
            # Merge overlay entries (documented signatures missing from JSON).
            for fn_name, inputs in SYSTEM_ABI_OVERLAY.get(info.system_id, []):
                ins = tuple(AbiInput(name=n, type_=t) for n, t in inputs)
                sig = f"{fn_name}({','.join(i.type_ for i in ins)})"
                selector = keccak(text=sig)[:4]
                if selector in fn_idx:
                    continue  # overlay is additive; never override JSON
                fn_idx[selector] = AbiFn(name=fn_name, selector=selector, inputs=ins)
            self._index[info.system_id] = fn_idx
            typed = next(
                (fn for fn in fn_idx.values() if fn.name == "executeTyped"),
                None,
            )
            if typed is not None:
                self._typed_fn[info.system_id] = typed

    def decode_tx(
        self,
        *,
        tx_hash: str,
        from_addr: str,
        to_addr: str,
        calldata: bytes,
        block_number: int,
        block_timestamp: int,
        status: int,
    ) -> DecodeResult:
        """Decode a single tx. Returns DecodeResult with 0+ actions."""
        info = self.registry.get_by_address(to_addr)
        if info is None:
            return DecodeResult([], "unknown_system",
                                reason=f"to={to_addr} not in system registry")

        if len(calldata) < 4:
            return DecodeResult([], "decode_error",
                                reason="calldata shorter than 4 bytes",
                                selector_hex="0x" if not calldata else "0x" + calldata.hex())

        selector = bytes(calldata[:4])
        payload = bytes(calldata[4:])
        selector_hex = "0x" + selector.hex()

        fn_idx = self._index.get(info.system_id, {})
        fn = fn_idx.get(selector)
        if fn is None:
            return DecodeResult([], "unknown_selector",
                                reason=f"selector not in {info.abi_name}",
                                selector_hex=selector_hex)

        try:
            args = self._decode_args(info, fn, payload)
        except Exception as e:  # noqa: BLE001 — ABI decode can raise broadly
            return DecodeResult([], "decode_error",
                                reason=f"{fn.signature}: {e}",
                                selector_hex=selector_hex)

        rows = self._args_to_actions(
            info=info,
            fn=fn,
            args=args,
            tx_hash=tx_hash,
            from_addr=from_addr,
            block_number=block_number,
            block_timestamp=block_timestamp,
            status=status,
        )
        return DecodeResult(rows, "ok", selector_hex=selector_hex, raw_args=args)

    # ------------------------------------------------------------------
    # Arg decode helpers.
    # ------------------------------------------------------------------

    def _decode_args(
        self,
        info: SystemInfo,
        fn: AbiFn,
        payload: bytes,
    ) -> list[tuple[str, str, Any]]:
        """Return list of (arg_name, arg_type, value)."""
        if fn.name == "execute" and len(fn.inputs) == 1 and fn.inputs[0].type_ == "bytes":
            inner_bytes = abi_decode(["bytes"], payload)[0]
            typed = self._typed_fn.get(info.system_id)
            if typed is None:
                # No executeTyped known — expose raw bytes for inspection.
                return [("arguments", "bytes", inner_bytes)]
            decoded = abi_decode(
                [i.type_ for i in typed.inputs],
                inner_bytes,
            )
            return [
                (inp.name, inp.type_, val)
                for inp, val in zip(typed.inputs, decoded, strict=True)
            ]

        if not fn.inputs:
            return []

        decoded = abi_decode([i.type_ for i in fn.inputs], payload)
        return [
            (inp.name, inp.type_, val)
            for inp, val in zip(fn.inputs, decoded, strict=True)
        ]

    # ------------------------------------------------------------------
    # Action row assembly.
    # ------------------------------------------------------------------

    def _args_to_actions(
        self,
        *,
        info: SystemInfo,
        fn: AbiFn,
        args: list[tuple[str, str, Any]],
        tx_hash: str,
        from_addr: str,
        block_number: int,
        block_timestamp: int,
        status: int,
    ) -> list[DecodedAction]:
        action_type = SYSTEM_TO_ACTION.get(info.system_id, info.system_id)

        # Detect batched: at least one input is an array type.
        array_idxs = [i for i, (_, t, _) in enumerate(args) if t.endswith("[]")]
        if not array_idxs:
            return [self._one_row(
                info=info, action_type=action_type, fn=fn,
                scalar_args=args, batch_idx=0, sub_index=0,
                tx_hash=tx_hash, from_addr=from_addr,
                block_number=block_number, block_timestamp=block_timestamp,
                status=status,
            )]

        # If multiple array args exist of the same length, zip them. If only
        # one, fan out on it; other scalars are shared. Mixed-length arrays
        # fall back to a single row with the arrays in metadata.
        lengths = {len(args[i][2]) for i in array_idxs}
        if len(lengths) != 1:
            # Not straightforward to fan out. Emit single row.
            return [self._one_row(
                info=info, action_type=action_type, fn=fn,
                scalar_args=args, batch_idx=0, sub_index=0,
                tx_hash=tx_hash, from_addr=from_addr,
                block_number=block_number, block_timestamp=block_timestamp,
                status=status,
            )]

        (batch_len,) = lengths
        if batch_len == 0:
            # Empty batch: nothing to fan out on. Record a single row noting
            # the empty-array call.
            return [self._one_row(
                info=info, action_type=action_type, fn=fn,
                scalar_args=args, batch_idx=0, sub_index=0,
                tx_hash=tx_hash, from_addr=from_addr,
                block_number=block_number, block_timestamp=block_timestamp,
                status=status,
            )]

        rows: list[DecodedAction] = []
        for j in range(batch_len):
            scalar_args: list[tuple[str, str, Any]] = []
            for i, (name, typ, val) in enumerate(args):
                if i in array_idxs:
                    scalar_args.append((name, typ[:-2], val[j]))
                else:
                    scalar_args.append((name, typ, val))
            rows.append(self._one_row(
                info=info, action_type=action_type, fn=fn,
                scalar_args=scalar_args, batch_idx=j, sub_index=j,
                tx_hash=tx_hash, from_addr=from_addr,
                block_number=block_number, block_timestamp=block_timestamp,
                status=status,
            ))
        return rows

    def _one_row(
        self,
        *,
        info: SystemInfo,
        action_type: str,
        fn: AbiFn,
        scalar_args: list[tuple[str, str, Any]],
        batch_idx: int,
        sub_index: int,
        tx_hash: str,
        from_addr: str,
        block_number: int,
        block_timestamp: int,
        status: int,
    ) -> DecodedAction:
        row = DecodedAction(
            tx_hash=tx_hash,
            sub_index=sub_index,
            block_number=block_number,
            block_timestamp=block_timestamp,
            action_type=action_type,
            system_id=info.system_id,
            from_addr=from_addr,
            status=status,
        )
        metadata: dict[str, Any] = {"fn": fn.name}
        if batch_idx > 0 or sub_index > 0:
            metadata["batch_idx"] = batch_idx

        field_map = SYSTEM_FIELD_MAP.get(info.system_id, {})

        for name, typ, val in scalar_args:
            rule = field_map.get(name)
            if rule is None:
                # No explicit rule — stash in metadata under the raw arg name.
                metadata[name or f"arg{len(metadata)}"] = _meta_value(typ, val)
                continue
            if isinstance(rule, tuple) and rule[0] == "metadata":
                metadata[rule[1]] = _meta_value(typ, val)
                continue
            # rule is a first-class column name.
            if rule == "kami_id":
                row.kami_id = _coerce_decimal(val)
            elif rule == "target_kami_id":
                row.target_kami_id = _coerce_decimal(val)
            elif rule == "node_id":
                row.node_id = _coerce_decimal(val)
            elif rule == "amount":
                row.amount = _coerce_decimal(val)
            elif rule == "item_index":
                row.item_index = _coerce_int(val)
            else:
                log.warning(
                    "decoder: unknown field-map target %r for %s.%s",
                    rule, info.system_id, name,
                )
                metadata[name] = _meta_value(typ, val)

        row.metadata = metadata
        return row


def _coerce_decimal(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return str(int.from_bytes(v, "big"))
    return str(v)


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return int(v)
    return None


def _meta_value(typ: str, val: Any) -> Any:
    if isinstance(val, (bytes, bytearray)):
        return "0x" + bytes(val).hex()
    if isinstance(val, int):
        # Represent as decimal string; ints fit in JSON but uint256 may lose
        # precision in consumers. Stringify for safety.
        return str(val)
    if isinstance(val, (list, tuple)):
        return [_meta_value("", x) for x in val]
    return val


