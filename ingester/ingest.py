"""Shared ingest workflow used by poller and backfill.

``process_block_range(...)`` fetches blocks in [start, end] inclusive, filters
transactions to registered Kamigotchi system contracts, decodes each, and
upserts raw_tx + kami_action rows. Receipts are pulled to capture revert
status and gas usage.

Idempotent — running the same range twice produces the same DB state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from web3 import Web3

from .chain_client import ChainClient
from .decoder import Decoder, DecodedAction
from .harvest_resolver import HarvestResolver
from .musu import decode_musu_drains, musu_amount_for_harvest
from .storage import RawTx, Storage
from .system_registry import SystemRegistry

# Action types whose ``amount`` should be filled from receipt-log MUSU drains.
# ``harvest_start`` is excluded — it has no payout (the harvest entity's
# bounty is 0 until the first stop/collect/liquidate). See
# ``memory/decoder-notes.md`` "Session 7 — MUSU Transfer probe".
MUSU_PAYOUT_ACTIONS = frozenset({
    "harvest_collect",
    "harvest_stop",
    "harvest_liquidate",
})

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    blocks_scanned: int = 0
    txs_seen: int = 0
    txs_matched: int = 0     # to known system contract
    txs_decoded: int = 0     # produced >= 1 action
    actions: int = 0
    unknown_selector: int = 0
    decode_errors: int = 0

    def bump(self, status: str, n_actions: int) -> None:
        self.txs_matched += 1
        self.actions += n_actions
        if status == "ok":
            self.txs_decoded += 1
        elif status == "unknown_selector":
            self.unknown_selector += 1
        elif status == "decode_error":
            self.decode_errors += 1


def log_unknown(path: Path, entries: Iterable[str]) -> None:
    lines = list(entries)
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for line in lines:
            f.write(line + "\n")


def process_block_range(
    *,
    client: ChainClient,
    decoder: Decoder,
    registry: SystemRegistry,
    storage: Storage | None,   # None = dry-run
    start_block: int,
    end_block: int,
    vendor_sha: str | None,
    unknown_log_path: Path | None = None,
    resolver: HarvestResolver | None = None,
) -> IngestStats:
    stats = IngestStats()
    known_addrs = registry.known_addresses()
    unknown_lines: list[str] = []

    for n in range(start_block, end_block + 1):
        block = client.get_block(n, full=True)
        stats.blocks_scanned += 1
        block_ts = int(block["timestamp"])

        raw_batch: list[RawTx] = []
        action_batch: list[DecodedAction] = []

        for tx in block.get("transactions", []):
            stats.txs_seen += 1
            to = tx.get("to")
            if to is None:
                continue
            try:
                to_cs = Web3.to_checksum_address(to)
            except ValueError:
                continue
            if to_cs not in known_addrs:
                continue

            info = registry.get_by_address(to_cs)
            calldata = tx["input"]
            if isinstance(calldata, str):
                calldata = bytes.fromhex(calldata[2:] if calldata.startswith("0x") else calldata)
            else:
                calldata = bytes(calldata)

            method_sig = "0x" + calldata[:4].hex() if len(calldata) >= 4 else "0x"
            from_addr = Web3.to_checksum_address(tx["from"])
            tx_hash_hex = tx["hash"].hex() if hasattr(tx["hash"], "hex") else str(tx["hash"])
            if not tx_hash_hex.startswith("0x"):
                tx_hash_hex = "0x" + tx_hash_hex

            # Receipt for status/gas. One extra RPC hop per matched tx; the
            # public Yominet RPC handles this volume comfortably at the tx
            # rates we saw during probing.
            try:
                receipt = client.get_tx_receipt(tx_hash_hex)
                status = int(receipt.get("status", 1))
                gas_used = int(receipt.get("gasUsed", 0)) or None
            except Exception as e:  # noqa: BLE001
                log.warning("ingest: receipt fetch failed for %s: %s", tx_hash_hex, e)
                status, gas_used = 1, None

            gas_price = tx.get("gasPrice")
            gas_price = int(gas_price) if gas_price is not None else None

            raw_batch.append(RawTx(
                tx_hash=tx_hash_hex,
                block_number=n,
                block_timestamp=block_ts,
                tx_index=int(tx["transactionIndex"]),
                from_addr=from_addr,
                to_addr=to_cs,
                method_sig=method_sig,
                system_id=info.system_id if info else None,
                raw_calldata=calldata,
                status=status,
                gas_used=gas_used,
                gas_price_wei=gas_price,
            ))

            result = decoder.decode_tx(
                tx_hash=tx_hash_hex,
                from_addr=from_addr,
                to_addr=to_cs,
                calldata=calldata,
                block_number=n,
                block_timestamp=block_ts,
                status=status,
            )
            stats.bump(result.status, len(result.actions))

            # Pair MUSU bounty drains from the receipt with harvest_*
            # action rows. The receipt was already fetched above, so this
            # adds no extra RPC calls. Drains are keyed by harvest_id
            # (uint256), which the action rows carry as ``harvest_id``
            # (or, on older liquidate rows, in metadata.victim_harvest_id).
            if result.actions:
                drains = decode_musu_drains(receipt)
                if drains:
                    for action in result.actions:
                        if action.action_type not in MUSU_PAYOUT_ACTIONS:
                            continue
                        h_id = action.harvest_id or action.metadata.get(
                            "victim_harvest_id"
                        )
                        action.amount = musu_amount_for_harvest(drains, h_id)

            action_batch.extend(result.actions)

            if result.status == "unknown_selector":
                unknown_lines.append(
                    f"- tx={tx_hash_hex} to={to_cs} system={info.system_id if info else '?'} "
                    f"selector={result.selector_hex} reason={result.reason}"
                )
            elif result.status == "decode_error":
                unknown_lines.append(
                    f"- tx={tx_hash_hex} to={to_cs} system={info.system_id if info else '?'} "
                    f"selector={result.selector_hex} DECODE_ERR reason={result.reason}"
                )

        if resolver is not None and action_batch:
            # Fold every fresh kami_id into the map first, then stitch. This
            # ordering matters when a single block contains both a
            # harvest_start and a harvest_stop for the same kami — register
            # the start before the stop reads back from the map.
            resolver.observe_actions(action_batch)
            n_stitched = resolver.stitch(action_batch)
            if n_stitched:
                log.debug("ingest: stitched %d kami_id from harvest_id", n_stitched)

        if storage is not None and (raw_batch or action_batch):
            storage.upsert_raw_txs(raw_batch)
            storage.upsert_actions(action_batch)

        if storage is not None:
            storage.set_cursor(
                block_number=n,
                block_timestamp=block_ts,
                vendor_sha=vendor_sha,
            )

    if unknown_log_path is not None and unknown_lines:
        log_unknown(unknown_log_path, unknown_lines)

    return stats
