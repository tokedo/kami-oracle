"""Migration 003: add account_index + account_name columns to kami_static.

Both columns come from ``GetterSystem.getAccount(uint256 accountId)`` —
documented in ``kami_context/system-ids.md`` Getter System section as

    function getAccount(uint256 accountId) view returns
        (tuple(uint32 index, string name, int32 currStamina, uint32 room))

We persist ``index`` and ``name`` so kami-centric queries through
``kami_static`` can display the human-readable operator label
("bpeon", "ray charles") and the small 1..N ordinal alongside
``kami_index``.

Idempotent: ALTER ... ADD COLUMN guarded by an information_schema check.
The actual hydration is done by the populator on the next refresh and by
``scripts/backfill_account_names.py`` for existing rows.

Bumps schema_version 2 → 3.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 3


def _table_has_column(conn: duckdb.DuckDBPyConnection, table: str, col: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        [table, col],
    ).fetchall()
    return bool(rows)


def _add_column_if_missing(conn: duckdb.DuckDBPyConnection, col: str, ddl_type: str) -> bool:
    if _table_has_column(conn, "kami_static", col):
        return False
    conn.execute(f"ALTER TABLE kami_static ADD COLUMN {col} {ddl_type}")
    return True


def _bump_schema_version(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        UPDATE ingest_cursor
        SET schema_version = ?, updated_at = now()
        WHERE id = 1 AND schema_version < ?
        """,
        [TARGET_SCHEMA_VERSION, TARGET_SCHEMA_VERSION],
    )


def run(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Apply migration 003 in-place. Returns counters for logging."""
    added_index = _add_column_if_missing(conn, "account_index", "INTEGER")
    added_name = _add_column_if_missing(conn, "account_name", "VARCHAR")
    _bump_schema_version(conn)
    return {
        "account_index_added": int(added_index),
        "account_name_added": int(added_name),
    }


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="db/kami-oracle.duckdb")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("db file not found: %s", db_path)
        return 1
    conn = duckdb.connect(str(db_path))
    try:
        stats = run(conn)
    finally:
        conn.close()
    log.info("migration 003 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
