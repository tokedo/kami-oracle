"""Continuous tail poller.

Reads the cursor, fetches new blocks, decodes, upserts, advances cursor.
Idempotent — running it twice over the same block range produces the same
DB state. Exits cleanly on SIGTERM / SIGINT.

Usage:
    python -m ingester.poller
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from pathlib import Path

from .chain_client import ChainClient
from .config import configure_logging, load_config
from .decoder import Decoder
from .ingest import process_block_range
from .storage import Storage, read_schema_sql
from .system_registry import resolve_systems

log = logging.getLogger(__name__)

STOP = False

REPO_ROOT = Path(__file__).resolve().parent.parent
UNKNOWN_LOG = REPO_ROOT / "memory" / "unknown-systems.md"


def _on_signal(signum, _frame):
    global STOP
    log.info("poller: signal %d received; shutting down after current block", signum)
    STOP = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-block", type=int, help="Override starting block (default: cursor + 1 or latest)")
    ap.add_argument("--once", action="store_true", help="Run one pass and exit")
    ap.add_argument("--max-catchup", type=int, default=50,
                    help="Max blocks to process in a single pass before sleeping")
    args = ap.parse_args()

    cfg = load_config()
    configure_logging(cfg.log_level)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("poller: RPC %s not reachable", cfg.rpc_url)
        return 1

    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))
    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    decoder = Decoder(cfg.abi_dir, registry)

    vendor_sha = cfg.vendor_sha_path.read_text().strip() if cfg.vendor_sha_path.exists() else None

    cursor = storage.get_cursor()
    if args.start_block is not None:
        next_block = args.start_block
    elif cursor is not None:
        next_block = cursor + 1
    else:
        next_block = client.block_number()
        log.info("poller: fresh start — beginning at head block %d", next_block)

    # The body has two inner try/excepts (block_number + process_block_range)
    # for tight retry on the common transport-fault case. The outer
    # try/except is a belt-and-braces survival net for anything unexpected —
    # a programming bug, an import-time surprise, anything not on the happy
    # path. Mirrors the backfill's outer loop; keeps a long-running poller
    # alive across nightly RPC blips without needing a human minder.
    while not STOP:
        try:
            try:
                head = client.block_number()
            except Exception as e:  # noqa: BLE001
                log.warning("poller: block_number failed: %s", e)
                time.sleep(cfg.poll_interval_s)
                continue

            if next_block > head:
                if args.once:
                    break
                time.sleep(cfg.poll_interval_s)
                continue

            end = min(head, next_block + args.max_catchup - 1)
            log.info("poller: scanning blocks %d..%d (head=%d)", next_block, end, head)
            try:
                stats = process_block_range(
                    client=client,
                    decoder=decoder,
                    registry=registry,
                    storage=storage,
                    start_block=next_block,
                    end_block=end,
                    vendor_sha=vendor_sha,
                    unknown_log_path=UNKNOWN_LOG,
                )
                log.info(
                    "poller: blocks=%d txs_seen=%d matched=%d decoded=%d actions=%d unknown=%d errors=%d",
                    stats.blocks_scanned, stats.txs_seen, stats.txs_matched,
                    stats.txs_decoded, stats.actions, stats.unknown_selector, stats.decode_errors,
                )
                next_block = end + 1
            except Exception as e:  # noqa: BLE001
                log.exception("poller: range %d..%d failed, will retry: %s", next_block, end, e)
                time.sleep(cfg.poll_interval_s)
                continue

            if args.once:
                break
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("poller: unhandled exception, sleeping 60s before resume: %s", e)
            time.sleep(60)
            # Refresh next_block from DB cursor so we don't re-scan blocks
            # that process_block_range already committed before the crash.
            db_cur = storage.get_cursor()
            if db_cur is not None and db_cur + 1 > next_block:
                log.info("poller: advancing next_block %d -> %d from DB cursor", next_block, db_cur + 1)
                next_block = db_cur + 1

    storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
