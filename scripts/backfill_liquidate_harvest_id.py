#!/usr/bin/env python
"""Backfill ``harvest_id`` on historical ``harvest_liquidate`` rows.

The ``victim_harvest_id`` field-map fix landed after most historical
``harvest_liquidate`` rows were already decoded, so they carry the
value in ``metadata_json.victim_harvest_id`` but have NULL
``harvest_id`` on the column itself. As of Session 8 baseline that's
~99.7% of liquidation rows. This script copies the metadata field
into the column so historical liquidation joins (e.g. against the
attacker's matching ``harvest_start``) work in raw SQL.

Idempotent — only touches rows where ``harvest_id IS NULL`` and the
metadata field is set.

The DuckDB file is held under exclusive lock by the running service —
**stop the service first**:

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_liquidate_harvest_id.py
    sudo systemctl start kami-oracle
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("backfill_liquidate_harvest_id")


CANDIDATE_COUNT_SQL = """
SELECT COUNT(*) FROM kami_action
WHERE action_type = 'harvest_liquidate'
  AND harvest_id IS NULL
  AND json_extract_string(metadata_json, '$.victim_harvest_id') IS NOT NULL
"""

NULL_LIQUIDATE_SQL = """
SELECT COUNT(*) FROM kami_action
WHERE action_type = 'harvest_liquidate'
  AND harvest_id IS NULL
"""

UPDATE_SQL = """
UPDATE kami_action
SET harvest_id = json_extract_string(metadata_json, '$.victim_harvest_id')
WHERE action_type = 'harvest_liquidate'
  AND harvest_id IS NULL
  AND json_extract_string(metadata_json, '$.victim_harvest_id') IS NOT NULL
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(REPO_ROOT / "db" / "kami-oracle.duckdb"),
        help="path to DuckDB file (default: db/kami-oracle.duckdb)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("db file not found: %s", db_path)
        return 1

    conn = duckdb.connect(str(db_path))
    try:
        candidates = conn.execute(CANDIDATE_COUNT_SQL).fetchone()[0]
        before_null = conn.execute(NULL_LIQUIDATE_SQL).fetchone()[0]
        log.info(
            "before: %d harvest_liquidate rows with NULL harvest_id; "
            "%d candidates have victim_harvest_id in metadata",
            before_null,
            candidates,
        )
        if candidates == 0:
            log.info("nothing to backfill — exiting clean")
            return 0
        conn.execute(UPDATE_SQL)
        after_null = conn.execute(NULL_LIQUIDATE_SQL).fetchone()[0]
        log.info(
            "after:  %d harvest_liquidate rows with NULL harvest_id "
            "(updated %d rows)",
            after_null,
            before_null - after_null,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
