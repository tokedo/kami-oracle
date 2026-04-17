"""Dry-run decoder validation over a small block window.

Reads the last N blocks from Yominet, filters txs to registered system
contracts, runs the decoder, and prints a coverage summary. Writes nothing
to the real DuckDB file — uses an in-memory connection.

Usage:
    python scripts/validate_decode.py                    # last 500 blocks
    python scripts/validate_decode.py --blocks 1000
    python scripts/validate_decode.py --from-block 27770000 --to-block 27770500
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Allow running as a script without installing the package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import configure_logging, load_config  # noqa: E402
from ingester.decoder import Decoder  # noqa: E402
from ingester.ingest import process_block_range  # noqa: E402
from ingester.system_registry import resolve_systems  # noqa: E402

log = logging.getLogger(__name__)

UNKNOWN_LOG = ROOT / "memory" / "unknown-systems.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=500, help="Blocks back from head (default 500)")
    ap.add_argument("--from-block", type=int, default=None)
    ap.add_argument("--to-block", type=int, default=None)
    ap.add_argument("--examples", type=int, default=3,
                    help="Number of example decoded rows per action_type")
    args = ap.parse_args()

    cfg = load_config()
    configure_logging(cfg.log_level)

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("validate: RPC not reachable")
        return 1

    head = args.to_block or client.block_number()
    start = args.from_block or (head - args.blocks + 1)
    log.info("validate: head=%d start=%d (%d blocks)", head, start, head - start + 1)

    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    decoder = Decoder(cfg.abi_dir, registry)

    # Collect actions as they stream through. We piggy-back on the same
    # pipeline the poller uses — just pass storage=None.
    #
    # To capture example rows, we wrap decoder.decode_tx so examples are
    # accumulated per action_type without re-pulling blocks.
    examples: dict[str, list[str]] = defaultdict(list)
    action_counts: Counter[str] = Counter()
    system_counts: Counter[str] = Counter()

    orig_decode = decoder.decode_tx

    def _wrap(**kw):
        r = orig_decode(**kw)
        for a in r.actions:
            action_counts[a.action_type] += 1
            system_counts[a.system_id] += 1
            if len(examples[a.action_type]) < args.examples:
                examples[a.action_type].append(
                    json.dumps({
                        "tx": a.tx_hash,
                        "block": a.block_number,
                        "from": a.from_addr,
                        "kami_id": a.kami_id,
                        "node_id": a.node_id,
                        "item_index": a.item_index,
                        "amount": a.amount,
                        "meta": a.metadata,
                    }, default=str)
                )
        return r

    decoder.decode_tx = _wrap  # type: ignore[method-assign]

    stats = process_block_range(
        client=client,
        decoder=decoder,
        registry=registry,
        storage=None,
        start_block=start,
        end_block=head,
        vendor_sha=None,
        unknown_log_path=UNKNOWN_LOG,
    )

    print("=" * 72)
    print("VALIDATION SUMMARY")
    print(f"  block range      : {start}..{head}  ({stats.blocks_scanned} blocks)")
    print(f"  txs seen         : {stats.txs_seen}")
    print(f"  txs matched      : {stats.txs_matched}  "
          f"(to a known system contract)")
    print(f"  txs decoded      : {stats.txs_decoded}")
    print(f"  unknown selector : {stats.unknown_selector}")
    print(f"  decode errors    : {stats.decode_errors}")
    print(f"  actions produced : {stats.actions}")
    print()
    print("action_type counts:")
    for at, c in action_counts.most_common():
        print(f"  {at:28s} {c}")
    print()
    print("system_id counts:")
    for sid, c in system_counts.most_common():
        print(f"  {sid:32s} {c}")
    print()
    print("examples (up to", args.examples, "per action_type):")
    for at in sorted(examples):
        print(f"\n  [{at}]")
        for ex in examples[at]:
            print("    " + ex)

    print()
    if UNKNOWN_LOG.exists() and UNKNOWN_LOG.stat().st_size > 0:
        print(f"note: appended to {UNKNOWN_LOG.relative_to(ROOT)} — review it.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
