"""Migration 007: add items_catalog table.

Per Session 13 — see ``memory/decoder-notes.md`` "Session 13 —
items_catalog + kami_equipment view". Mirrors the static
``kami_context/catalogs/items.csv`` into DuckDB so the
``kami_equipment`` view (migration 008) can resolve equipment slot
labels without a chain registry call. The chain doesn't return slot
identity per equipped item (Session 10 noted as "chain registry
quirk"); items.csv carries the canonical "For" column which is the
source of slot truth.

Loader: ``ingester/items_catalog.py``. Reload trigger is *only* on
``kami_context`` re-vendor (or service startup if the table is empty)
— catalog data is static between vendor refreshes; reloading every
poll would burn IO for zero benefit.

Idempotent: CREATE TABLE IF NOT EXISTS. The actual rows are inserted
by the loader, not by the migration — keeping schema and data
concerns separate.

Bumps schema_version 6 → 7.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 7

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS items_catalog (
    item_index   INTEGER  PRIMARY KEY,
    name         VARCHAR  NOT NULL,
    type         VARCHAR,
    rarity       VARCHAR,
    slot_type    VARCHAR,        -- chain "For" value when slot-equippable; NULL for non-equipment
    effect       VARCHAR,        -- raw "Effects" cell (e.g. "E_POWER+3")
    description  VARCHAR,
    loaded_ts    TIMESTAMP NOT NULL
)
"""


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchall()
    return bool(rows)


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
    """Apply migration 007 in-place. Returns counters for logging."""
    created = 0 if _table_exists(conn, "items_catalog") else 1
    conn.execute(CREATE_SQL)
    _bump_schema_version(conn)
    return {"tables_created": created}


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
    log.info("migration 007 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
