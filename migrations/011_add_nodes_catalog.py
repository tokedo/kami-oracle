"""Migration 011: add nodes_catalog table.

Per Session 14 — see ``memory/decoder-notes.md`` "Session 14 —
skills_catalog + nodes_catalog + views". Mirrors the static
``kami_context/catalogs/nodes.csv`` into DuckDB, augmented with a
``room_index`` resolved at load time. Used by the
``kami_current_location`` view (migration 012) to map an observed
``kami_action.node_id`` to a room.

Discovery (Session 14 Part 1b): every node in the upstream
nodes.csv has a same-Index, same-Name row in
``kami_context/catalogs/rooms.csv`` — zero mismatches across all
64 in-game nodes. So ``room_index = node_index`` for every
in-game node; the loader stores this directly without needing a
chain getter or a separate lookup file.

Loader: ``ingester/nodes_catalog.py``. Reload trigger is *only*
on ``kami_context`` re-vendor (or service startup if the table is
empty) — same shape as items_catalog / skills_catalog.

Idempotent: CREATE TABLE IF NOT EXISTS. Rows are inserted by the
loader, not by the migration. Bumps schema_version 10 → 11.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 11

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS nodes_catalog (
    node_index   INTEGER PRIMARY KEY,
    name         VARCHAR NOT NULL,
    status       VARCHAR,            -- 'In Game' or other (catalog-only / removed)
    drops        VARCHAR,
    affinity     VARCHAR,            -- Eerie | Normal | Scrap | Insect (or comma-list)
    level_limit  INTEGER,
    yield_index  INTEGER,
    scav_cost    INTEGER,
    room_index   INTEGER,            -- resolved at load (Session 14: room_index = node_index)
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
    """Apply migration 011 in-place. Returns counters for logging."""
    created = 0 if _table_exists(conn, "nodes_catalog") else 1
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
    log.info("migration 011 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
