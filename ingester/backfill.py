"""One-shot backfill going backward from head.

Walks backward from a head block for ``--days`` (or ``--weeks``) worth of
blocks, or an explicit block range, and processes each chunk like the
poller. Intended for initial population; do not run in parallel with the
poller on the same DB.

Usage:
    python -m ingester.backfill --days 7
    python -m ingester.backfill --weeks 1
    python -m ingester.backfill --from-block 27700000 --to-block 27778000
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from web3.exceptions import Web3Exception

from .chain_client import ChainClient
from .config import configure_logging, load_config
from .decoder import Decoder
from .ingest import process_block_range
from .storage import Storage, read_schema_sql
from .system_registry import resolve_systems

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
UNKNOWN_LOG = REPO_ROOT / "memory" / "unknown-systems.md"

# Yominet blocktime is ~1–2s. At 1.5s average: 86400 / 1.5 = ~57600 blocks/day.
# We err on the generous side to guarantee full coverage; the poller will
# dedupe via ON CONFLICT DO NOTHING and the cursor.
BLOCKS_PER_DAY = 60_000
BLOCKS_PER_WEEK = 7 * BLOCKS_PER_DAY

# Safety buffer when probing earliest retained block. The public Yominet RPC
# appears load-balanced across multiple backends with slightly different
# retention depths; requests near the pruning edge hit "block not found"
# retries even a few thousand blocks past the probed-earliest value. 20k
# blocks ≈ 8h of real time — a few hours of history lost, several hours of
# retry time saved during backfill.
RETENTION_BUFFER_BLOCKS = 20_000


def earliest_retained_block(client: ChainClient, head: int, floor: int) -> int:
    """Binary-search the lowest block the RPC will serve in [floor, head].

    Public Yominet RPC prunes blocks older than ~3.2 weeks (session 2,
    2026-04-17). Probing up-front lets us clamp the backfill start instead
    of burning through retries on every doomed historical fetch.

    Returns the earliest block present on the RPC. If all probed blocks
    exist, returns ``floor``.
    """
    def exists(n: int) -> bool:
        try:
            client.w3.eth.get_block(n, full_transactions=False)
            return True
        except Web3Exception as e:
            if "not found" in str(e).lower():
                return False
            raise

    if exists(floor):
        return floor
    lo, hi = floor, head
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if exists(mid):
            hi = mid
        else:
            lo = mid
    return hi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="Walk this many days back from head (preferred over --weeks).")
    ap.add_argument("--weeks", type=int, default=None,
                    help="Walk this many weeks back from head.")
    ap.add_argument("--from-block", type=int, default=None,
                    help="Explicit start block (inclusive).")
    ap.add_argument("--to-block", type=int, default=None,
                    help="Explicit end block (inclusive, default: head).")
    ap.add_argument("--chunk-size", type=int, default=500,
                    help="Blocks per batch (default 500).")
    ap.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    args = ap.parse_args()

    if args.days is None and args.weeks is None and args.from_block is None:
        ap.error("supply --days, --weeks, or --from-block")
    if args.days is not None and args.weeks is not None:
        ap.error("--days and --weeks are mutually exclusive")

    cfg = load_config()
    configure_logging(cfg.log_level)

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("backfill: RPC %s not reachable", cfg.rpc_url)
        return 1

    head = args.to_block if args.to_block is not None else client.block_number()
    if args.from_block is not None:
        start = args.from_block
    elif args.days is not None:
        start = max(1, head - args.days * BLOCKS_PER_DAY)
    else:
        start = max(1, head - args.weeks * BLOCKS_PER_WEEK)

    # Probe the RPC's retention depth and clamp start upward if requested
    # history is beyond the pruning boundary. Skip when caller set an
    # explicit --from-block (they've presumably done their own math).
    if args.from_block is None:
        earliest = earliest_retained_block(client, head, start)
        if earliest > start:
            clamped = earliest + RETENTION_BUFFER_BLOCKS
            log.warning(
                "backfill: RPC retains from block %d; requested start %d is pruned. "
                "Clamping to %d (%d-block safety buffer). Full window unreachable on this endpoint.",
                earliest, start, clamped, RETENTION_BUFFER_BLOCKS,
            )
            start = clamped

    log.info("backfill: head=%d start=%d (%d blocks)", head, start, head - start + 1)

    storage = None if args.dry_run else Storage(cfg.db_path)
    if storage is not None:
        storage.bootstrap(read_schema_sql(REPO_ROOT))

    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    decoder = Decoder(cfg.abi_dir, registry)
    vendor_sha = cfg.vendor_sha_path.read_text().strip() if cfg.vendor_sha_path.exists() else None

    total_blocks = 0
    total_actions = 0
    cur = start
    # Outer survival loop: any unhandled exception escaping the chunk loop
    # (e.g. a transport fault the retry wrapper couldn't absorb, or a
    # programming bug tripped by surprise data) kills the inner loop. We
    # log, sleep, refresh the cursor from DB, and resume. Without this the
    # 4-week session-2 backfill died after ~6h on a bare
    # requests.ConnectionError; running backfills shouldn't need a human
    # minder.
    while cur <= head:
        try:
            while cur <= head:
                chunk_end = min(head, cur + args.chunk_size - 1)
                stats = process_block_range(
                    client=client,
                    decoder=decoder,
                    registry=registry,
                    storage=storage,
                    start_block=cur,
                    end_block=chunk_end,
                    vendor_sha=vendor_sha,
                    unknown_log_path=UNKNOWN_LOG,
                )
                total_blocks += stats.blocks_scanned
                total_actions += stats.actions
                log.info(
                    "backfill: %d..%d done (actions=%d, running total=%d)",
                    cur, chunk_end, stats.actions, total_actions,
                )
                cur = chunk_end + 1
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception(
                "backfill: unhandled exception at cur=%d, sleeping 60s before resume: %s",
                cur, e,
            )
            time.sleep(60)
            # process_block_range commits the cursor chunk-by-chunk, so the
            # DB may already be past `cur` if the failure was mid-chunk
            # post-commit. Trust the DB over our in-memory value.
            if storage is not None:
                db_cur = storage.get_cursor()
                if db_cur is not None and db_cur + 1 > cur:
                    log.info("backfill: advancing local cur %d -> %d from DB cursor", cur, db_cur + 1)
                    cur = db_cur + 1

    log.info("backfill: complete — %d blocks, %d actions", total_blocks, total_actions)
    if storage is not None:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
