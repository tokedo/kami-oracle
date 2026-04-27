"""Migration 004: add build-snapshot columns to kami_static.

Per Session 10 — see ``memory/decoder-notes.md`` "Session 10 — build
fields on chain" for the per-field on-chain source. Columns added:

    level                 INTEGER   -- getKami(id).level
    xp                    BIGINT    -- getKami(id).xp
    total_health          INTEGER   -- formula(getKami(id).stats.health)
    total_power           INTEGER   -- formula(getKami(id).stats.power)
    total_violence        INTEGER   -- formula(getKami(id).stats.violence)
    total_harmony         INTEGER   -- formula(getKami(id).stats.harmony)
    total_slots           INTEGER   -- formula(SlotsComponent.safeGet(id))
                                       — in-game capacity = 1 + total_slots
    skills_json           VARCHAR   -- JSON array [{index, points}, ...]
    equipment_json        VARCHAR   -- JSON array [item_index, ...]
    build_refreshed_ts    TIMESTAMP -- per-kami build refresh time
                                       (distinct from last_refreshed_ts,
                                       which covers traits + account fields)

Effective stat formula (per kamigotchi-context state-reading.md):
    effective = max(0, floor((1000 + boost) * (base + shift) / 1000))

Idempotent: ALTER ... ADD COLUMN guarded by an information_schema check.
The actual hydration is done by the populator on the next refresh and
by ``scripts/backfill_kami_build.py`` for existing rows.

Bumps schema_version 3 → 4.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 4

NEW_COLUMNS: list[tuple[str, str]] = [
    ("level", "INTEGER"),
    ("xp", "BIGINT"),
    ("total_health", "INTEGER"),
    ("total_power", "INTEGER"),
    ("total_violence", "INTEGER"),
    ("total_harmony", "INTEGER"),
    ("total_slots", "INTEGER"),
    ("skills_json", "VARCHAR"),
    ("equipment_json", "VARCHAR"),
    ("build_refreshed_ts", "TIMESTAMP"),
]


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
    """Apply migration 004 in-place. Returns counters for logging."""
    added = 0
    for col, ddl in NEW_COLUMNS:
        if _add_column_if_missing(conn, col, ddl):
            added += 1
    _bump_schema_version(conn)
    return {"columns_added": added, "columns_total": len(NEW_COLUMNS)}


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
    log.info("migration 004 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
