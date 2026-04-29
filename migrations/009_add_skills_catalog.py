"""Migration 009: add skills_catalog table.

Per Session 14 — see ``memory/decoder-notes.md`` "Session 14 —
skills_catalog + nodes_catalog + views". Mirrors the static
``kami_context/catalogs/skills.csv`` into DuckDB so the
``kami_skills`` view (migration 010) can resolve per-skill
effect / tree / tier details without the agent re-deriving them
from skills.csv inline.

Loader: ``ingester/skills_catalog.py``. Reload trigger is *only*
on ``kami_context`` re-vendor (or service startup if the table is
empty) — catalog data is static between vendor refreshes;
reloading every poll would burn IO for zero benefit. Same shape
as Session 13's items_catalog.

The CSV's first column is a leading blank cell (header is
``"﻿."`` — UTF-8 BOM + dot). The loader skips it via
DictReader keyed on the named columns (Index/Name/Tree/...), so
the BOM column never reaches the table.

``value`` is VARCHAR (not numeric) because skill effects mix
integer counts, signed percent values (``0.02``), and decimals.
Keeping the chain string verbatim avoids parse loss on edge cases
and lets the agent / view consumers cast as needed.

Idempotent: CREATE TABLE IF NOT EXISTS. Rows are inserted by the
loader, not by the migration. Bumps schema_version 8 → 9.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 9

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS skills_catalog (
    skill_index  INTEGER PRIMARY KEY,
    name         VARCHAR NOT NULL,
    tree         VARCHAR NOT NULL,   -- Predator | Guardian | Harvester | Enlightened
    tier         INTEGER,
    tree_req     INTEGER,            -- prereq points in tree
    max_rank     INTEGER,
    cost         INTEGER,            -- skill point cost per rank
    effect       VARCHAR,            -- effect key: SHS / HFB / SB / ...
    value        VARCHAR,            -- per-rank value (string — signed/decimal possible)
    units        VARCHAR,            -- Stat | Percent | Sec | Musu/hr | ...
    exclusion    VARCHAR,            -- mutually-exclusive sibling skill_index list, if any
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
    """Apply migration 009 in-place. Returns counters for logging."""
    created = 0 if _table_exists(conn, "skills_catalog") else 1
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
    log.info("migration 009 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
