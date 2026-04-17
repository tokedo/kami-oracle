"""Window prune — delete rows older than ``KAMI_ORACLE_WINDOW_DAYS``.

Usage:
    python -m ingester.prune
    python -m ingester.prune --days 28
    python -m ingester.prune --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from pathlib import Path

from .config import configure_logging, load_config
from .storage import Storage, read_schema_sql

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="Override window size in days (default: config).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    configure_logging(cfg.log_level)

    window_days = args.days if args.days is not None else cfg.window_days
    cutoff_dt = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=window_days)
    cutoff_ts = int(cutoff_dt.timestamp())
    log.info("prune: window=%d days, cutoff=%s", window_days, cutoff_dt.isoformat())

    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    if args.dry_run:
        n_act = storage.conn.execute(
            "SELECT COUNT(*) FROM kami_action WHERE block_timestamp < ?",
            [cutoff_dt],
        ).fetchone()[0]
        n_raw = storage.conn.execute(
            "SELECT COUNT(*) FROM raw_tx WHERE block_timestamp < ?",
            [cutoff_dt],
        ).fetchone()[0]
        log.info("prune: DRY-RUN would delete %d kami_action, %d raw_tx rows", n_act, n_raw)
    else:
        n_act, n_raw = storage.prune_older_than(cutoff_ts)
        log.info("prune: deleted %d kami_action, %d raw_tx rows", n_act, n_raw)

    storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
