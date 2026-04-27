#!/usr/bin/env python
"""Backfill build-snapshot columns on existing kami_static rows.

Walks every kami in ``kami_static`` whose ``build_refreshed_ts`` is
NULL, calls ``KamiStaticReader.fetch`` (which already does the full
getKami + getAccount + slots/skills/equipment fan-out), and bulk-UPDATEs
the build columns. Rows hydrated by the Session 9 path keep their
account_index/account_name; this script only writes the Session 10
build columns + bumps last_refreshed_ts.

Idempotent — safe to re-run; only touches rows with NULL
``build_refreshed_ts``. A per-kami failure (revert / RPC fault on
``getKami`` itself) leaves the row untouched and is reported as a
counter rather than a propagating exception. A *partial* failure
(getKami ok, build extras revert) writes whatever did succeed and
sets ``build_refreshed_ts`` so the row isn't infinite-retried.

Stop the kami-oracle service before running (DuckDB exclusive lock):

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_kami_build.py
    sudo systemctl start kami-oracle
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import load_config  # noqa: E402
from ingester.kami_static import KamiStatic, KamiStaticReader  # noqa: E402
from ingester.storage import Storage, read_schema_sql  # noqa: E402
from ingester.system_registry import SystemRegistry, resolve_systems  # noqa: E402

log = logging.getLogger("backfill_kami_build")


def _count_pending(storage: Storage) -> int:
    row = storage.fetchone(
        "SELECT COUNT(*) FROM kami_static WHERE build_refreshed_ts IS NULL"
    )
    return int(row[0]) if row else 0


def _pending_kami_ids(storage: Storage) -> list[str]:
    rows = storage.fetchall(
        "SELECT kami_id FROM kami_static WHERE build_refreshed_ts IS NULL"
    )
    return [str(r[0]) for r in rows]


def _apply_batch(storage: Storage, batch: list[KamiStatic]) -> None:
    """Write build columns for a batch of kamis, leaving Session 9 columns intact."""
    if not batch:
        return
    payload = [
        (
            r.level, r.xp,
            r.total_health, r.total_power, r.total_violence, r.total_harmony,
            r.total_slots, r.skills_json, r.equipment_json,
            r.build_refreshed_ts,
            r.kami_id,
        )
        for r in batch
    ]
    with storage.lock:
        storage.conn.executemany(
            """
            UPDATE kami_static
            SET level = ?, xp = ?,
                total_health = ?, total_power = ?, total_violence = ?,
                total_harmony = ?, total_slots = ?,
                skills_json = ?, equipment_json = ?,
                build_refreshed_ts = ?,
                last_refreshed_ts = now()
            WHERE kami_id = ?
            """,
            payload,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--commit-every", type=int, default=200,
        help="Flush UPDATE batch every N kamis fetched (default 200)",
    )
    ap.add_argument(
        "--workers", type=int, default=8,
        help="Thread pool size for parallel fetches (default 8)",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Cap kamis processed (default 0 = no cap; useful for smoke runs)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("RPC not reachable: %s", cfg.rpc_url)
        return 1

    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    pre = _count_pending(storage)
    log.info("backfill_kami_build: %d kami_static rows have NULL build_refreshed_ts", pre)
    if pre == 0:
        log.info("nothing to do")
        storage.close()
        return 0

    # Resolve registry at head + extend with prior snapshot (so we have the
    # latest GetterSystem address and can match older redeployments too).
    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    prior = storage.load_system_address_snapshot()
    if prior:
        prior_reg = SystemRegistry.from_snapshot_rows(prior)
        registry.extend(prior_reg)
    storage.upsert_system_address_snapshot(registry.to_snapshot_rows())

    reader = KamiStaticReader(client, registry, cfg.abi_dir)

    kami_ids = _pending_kami_ids(storage)
    if args.limit:
        kami_ids = kami_ids[: args.limit]
    log.info("backfill_kami_build: processing %d kamis (workers=%d)", len(kami_ids), args.workers)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    n_ok = n_fail = 0
    n_partial = 0  # ok at top level but build_extras incomplete
    batch: list[KamiStatic] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(reader.fetch, kid): kid for kid in kami_ids}
        for fut in as_completed(futures):
            kid = futures[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                log.warning("backfill_kami_build: getKami(%s) failed: %s", kid, e)
                continue
            n_ok += 1
            # Track partial-build (getKami ok, but build extras returned None).
            if row.total_slots is None or row.skills_json is None:
                n_partial += 1
            batch.append(row)
            if len(batch) >= args.commit_every:
                _apply_batch(storage, batch)
                batch.clear()
                elapsed = time.monotonic() - t0
                log.info(
                    "progress: ok=%d fail=%d partial=%d / %d in %.1fs (%.1f kami/s)",
                    n_ok, n_fail, n_partial, len(kami_ids), elapsed,
                    n_ok / max(elapsed, 1e-6),
                )
    if batch:
        _apply_batch(storage, batch)

    post = _count_pending(storage)

    # Anomaly: any kami where total_health < base_health would suggest a
    # formula-resolution bug (effective should always be >= base when no
    # negative shifts/boosts are applied — and even then, the floor at 0
    # caps it). Surface as a counter for the verification report.
    anomaly_row = storage.fetchone(
        """
        SELECT COUNT(*) FROM kami_static
        WHERE total_health IS NOT NULL
          AND base_health IS NOT NULL
          AND total_health < base_health
        """
    )
    n_anomaly = int(anomaly_row[0]) if anomaly_row else 0

    log.info(
        "backfill_kami_build done: pre_pending=%d post_pending=%d ok=%d fail=%d partial=%d anomaly_total_lt_base=%d",
        pre, post, n_ok, n_fail, n_partial, n_anomaly,
    )
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
