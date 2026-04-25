#!/usr/bin/env python
"""Backfill kami_action.amount on historical harvest_*  rows by walking
each tx's receipt for ``ComponentValueSet`` events on the harvest entity.

Uses ``ingester.musu.decode_musu_drains`` so the live decoder and the
backfill share one source of truth. See ``memory/decoder-notes.md``
"Session 7 — MUSU Transfer probe" for the derivation.

The DuckDB file is held under exclusive lock by the running service,
so this script must run with the service stopped:

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_musu_amount.py
    sudo systemctl start kami-oracle

Idempotent: only touches rows where ``amount IS NULL`` and the action
type is ``harvest_collect`` / ``harvest_stop`` / ``harvest_liquidate``.
A second run is a no-op once the first run completes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local as thread_local

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import load_config  # noqa: E402
from ingester.musu import decode_musu_drains  # noqa: E402

log = logging.getLogger("backfill_musu_amount")

# Same set as ``ingester.ingest.MUSU_PAYOUT_ACTIONS`` (avoid the import
# cycle if ingest.py grows; this list is short and stable).
MUSU_PAYOUT_ACTIONS = ("harvest_collect", "harvest_stop", "harvest_liquidate")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(REPO_ROOT / "db" / "kami-oracle.duckdb"),
        help="path to DuckDB file",
    )
    ap.add_argument(
        "--workers", type=int, default=16,
        help="parallel RPC fetchers (default 16; 32 starves the main thread)",
    )
    ap.add_argument(
        "--batch", type=int, default=0,
        help=(
            "commit DB updates every N paired rows. 0 (default) means "
            "accumulate all updates in memory and commit once at end. "
            "DuckDB rewrites whole row groups on UPDATE, so each commit "
            "scans the full table — 30 batches of 2000 cost 30× one "
            "single commit. The trade-off is that a crash mid-run loses "
            "all in-memory updates; the script is idempotent so a re-run "
            "starts over from NULL rows."
        ),
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="stop after processing N tx (0 = no limit; for smoke tests)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("db file not found: %s", db_path)
        return 1

    cfg = load_config()
    # Each worker thread gets its own ChainClient so the underlying
    # ``requests.Session`` connection pools don't bottleneck on the
    # default 10-connection limit. Verified: shared client tops out at
    # ~10 tx/s with 32 workers; per-thread clients sustain 30-50 tx/s.
    tlocal = thread_local()
    def _client_for_thread() -> ChainClient:
        c = getattr(tlocal, "client", None)
        if c is None:
            c = ChainClient(cfg.rpc_url)
            tlocal.client = c
        return c

    if not _client_for_thread().is_connected():
        log.error("rpc not reachable: %s", cfg.rpc_url)
        return 1

    log.info("opening %s for write", db_path)
    conn = duckdb.connect(str(db_path), read_only=False)

    # Pull every (tx_hash, sub_index, harvest_id, action_type,
    # metadata_json) that needs amount populated. Rows are grouped by
    # tx_hash so we fetch each receipt exactly once.
    placeholders = ", ".join(["?"] * len(MUSU_PAYOUT_ACTIONS))
    rows = conn.execute(
        f"""
        SELECT tx_hash, sub_index, harvest_id, action_type, metadata_json
        FROM kami_action
        WHERE amount IS NULL
          AND action_type IN ({placeholders})
        ORDER BY tx_hash
        """,
        list(MUSU_PAYOUT_ACTIONS),
    ).fetchall()

    log.info("rows to backfill: %d", len(rows))
    by_tx: dict[str, list[tuple]] = {}
    for tx, sub_index, h_id, atype, meta_json in rows:
        by_tx.setdefault(tx, []).append((sub_index, h_id, atype, meta_json))

    tx_list = list(by_tx.keys())
    if args.limit > 0:
        tx_list = tx_list[: args.limit]
    log.info("unique tx to fetch: %d (workers=%d, batch=%d)",
             len(tx_list), args.workers, args.batch)

    pending_updates: list[tuple[str | None, str, int]] = []
    paired = 0
    unpaired = 0
    receipt_failures = 0
    start = time.time()
    last_progress = start

    def _fetch(tx_hash: str):
        """Run on worker thread: receipt fetch only. Decoding stays here
        too because ``decode_musu_drains`` is pure-Python and CPU-cheap."""
        try:
            receipt = _client_for_thread().get_tx_receipt(tx_hash)
        except Exception as e:  # noqa: BLE001
            return tx_hash, None, str(e)
        return tx_hash, decode_musu_drains(receipt), None

    def _commit_batch():
        """Apply the accumulated updates as a single set-based UPDATE.

        DuckDB ``UPDATE``s are O(rowgroup); 2000-row ``executemany`` of
        per-row updates blocks the main thread for minutes once the
        table has hundreds of thousands of rows. The temp-table + JOIN
        pattern (same shape as migration 002) folds the whole batch
        into one query that finishes in < 1s.
        """
        nonlocal pending_updates
        if not pending_updates:
            return
        # rows: (amount, id, action_type) — same triple that
        # ``executemany`` would have consumed.
        rows = pending_updates
        conn.execute("CREATE TEMP TABLE _musu_patch (amount VARCHAR, id VARCHAR, action_type VARCHAR)")
        conn.executemany("INSERT INTO _musu_patch VALUES (?, ?, ?)", rows)
        conn.execute(
            """
            UPDATE kami_action a
            SET amount = p.amount
            FROM _musu_patch p
            WHERE a.id = p.id
              AND a.action_type = p.action_type
              AND a.amount IS NULL
            """
        )
        conn.execute("DROP TABLE _musu_patch")
        pending_updates = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_fetch, tx): tx for tx in tx_list}
        for n, fut in enumerate(as_completed(futures), 1):
            tx_hash, drains, err = fut.result()
            if err is not None:
                receipt_failures += 1
                log.warning("receipt fetch failed for %s: %s", tx_hash, err)
                continue

            for sub_index, h_id, atype, meta_json in by_tx[tx_hash]:
                # Resolve harvest entity id: column first, then metadata
                # (older liquidate rows). Skip if neither.
                target = h_id
                if not target and meta_json:
                    try:
                        meta = json.loads(meta_json)
                    except (TypeError, ValueError):
                        meta = {}
                    target = meta.get("victim_harvest_id")
                if not target:
                    unpaired += 1
                    continue

                try:
                    target_int = int(target)
                except (TypeError, ValueError):
                    unpaired += 1
                    continue

                drained = drains.get(target_int)
                if drained is None:
                    # Real no-op — entity wasn't drained in this tx. Leave
                    # NULL; do not write 0. Counts toward "unpaired" so the
                    # final summary tells us how common no-op stops are.
                    unpaired += 1
                    continue

                pending_updates.append(
                    (str(drained), f"{tx_hash}:{sub_index}", atype)
                )
                paired += 1

            if args.batch and len(pending_updates) >= args.batch:
                _commit_batch()

            now = time.time()
            if now - last_progress >= 5.0:
                elapsed = now - start
                rate = n / elapsed if elapsed > 0 else 0.0
                log.info(
                    "progress: %d/%d tx (%.1f tx/s, paired=%d unpaired=%d "
                    "receipt_fail=%d elapsed=%.1fs)",
                    n, len(tx_list), rate, paired, unpaired,
                    receipt_failures, elapsed,
                )
                last_progress = now

    _commit_batch()
    conn.close()

    elapsed = time.time() - start
    log.info(
        "done: tx=%d paired=%d unpaired=%d receipt_fail=%d elapsed=%.1fs",
        len(tx_list), paired, unpaired, receipt_failures, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
