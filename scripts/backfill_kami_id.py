#!/usr/bin/env python
"""Replay the Session-6 kami_id stitch on the local DuckDB.

In normal operation this is unnecessary — ``Storage.bootstrap()`` invokes
migration 002 on every service start, so a service restart performs the
backfill exactly once. This script exists for ops scenarios where someone
wants to re-run the stitch standalone (e.g. after wiping the DB and
re-importing from a backup).

The DuckDB file is held under exclusive lock by the running service —
**stop the service first**:

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_kami_id.py
    sudo systemctl start kami-oracle

The migration is idempotent — a second run is a no-op.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("backfill_kami_id")


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_002",
        REPO_ROOT / "migrations" / "002_add_harvest_id_column.py",
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(REPO_ROOT / "db" / "kami-oracle.duckdb"),
        help="path to DuckDB file (default: db/kami-oracle.duckdb)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("db file not found: %s", db_path)
        return 1

    m = _load_migration()
    conn = duckdb.connect(str(db_path))
    try:
        # Don't summarize before — pre-migration the harvest_id column may
        # not exist. The migration itself logs nothing on the row counts;
        # report only the post-state.
        stats = m.run(conn)
        log.info("migration stats: %s", stats)
        after = _summary(conn)
        log.info("after:  %s", after)
    finally:
        conn.close()
    return 0


def _summary(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT
            SUM(CASE WHEN action_type = 'harvest_start' AND harvest_id IS NULL THEN 1 ELSE 0 END) AS start_null_hid,
            SUM(CASE WHEN action_type IN ('harvest_stop','harvest_collect') AND harvest_id IS NULL THEN 1 ELSE 0 END) AS stopcollect_null_hid,
            SUM(CASE WHEN action_type IN ('harvest_stop','harvest_collect') AND kami_id IS NULL THEN 1 ELSE 0 END) AS stopcollect_null_kid
        FROM kami_action
        """
    ).fetchone()
    return {
        "start_null_hid": int(rows[0] or 0),
        "stopcollect_null_hid": int(rows[1] or 0),
        "stopcollect_null_kid": int(rows[2] or 0),
    }


if __name__ == "__main__":
    sys.exit(main())
