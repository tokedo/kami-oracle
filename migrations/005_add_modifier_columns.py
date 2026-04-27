"""Migration 005: add the 12 skill-effect modifier columns to kami_static.

Per Session 11 — see ``memory/decoder-notes.md`` "Session 11 — skill-effect
modifiers on chain". The 12 non-stat skill effect types from
``kamigotchi-context/systems/leveling.md`` "Skill Effects" table:

    strain_boost              INTEGER  -- SB, ×1000 (negative = less strain)
    harvest_fertility_boost   INTEGER  -- HFB, ×1000
    harvest_intensity_boost   INTEGER  -- HIB, Musu/hr (no ×1000)
    harvest_bounty_boost      INTEGER  -- HBB, ×1000
    rest_recovery_boost       INTEGER  -- RMB, ×1000
    cooldown_shift            INTEGER  -- CS, seconds (signed; no ×1000)
    attack_threshold_shift    INTEGER  -- ATS, ×1000
    attack_threshold_ratio    INTEGER  -- ATR, ×1000
    attack_spoils_ratio       INTEGER  -- ASR, ×1000
    defense_threshold_shift   INTEGER  -- DTS, ×1000
    defense_threshold_ratio   INTEGER  -- DTR, ×1000
    defense_salvage_ratio     INTEGER  -- DSR, ×1000

These are catalog-derived sums (per-kami): for each owned skill,
``points × per-point catalog value``; for each equipped item, the
equipment-row catalog value × 1. The catalog → chain pipeline is
faithful (round-tripped via SHS/SYS sums vs Zephyr's chain stat
shifts) so the catalog walk produces the resolved totals the game
itself uses.

The four stat-shift effects (SHS/SPS/SVS/SYS) are NOT new columns —
they are already folded into Session 10's total_health/power/
violence/harmony via getKami(id).stats.

Reuses ``build_refreshed_ts`` from migration 004 — no new timestamp.

Idempotent: ALTER ... ADD COLUMN guarded by an information_schema check.
The actual hydration is done by the populator on the next refresh and
by ``scripts/backfill_kami_modifiers.py`` for existing rows.

Bumps schema_version 4 → 5.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 5

NEW_COLUMNS: list[tuple[str, str]] = [
    ("strain_boost", "INTEGER"),
    ("harvest_fertility_boost", "INTEGER"),
    ("harvest_intensity_boost", "INTEGER"),
    ("harvest_bounty_boost", "INTEGER"),
    ("rest_recovery_boost", "INTEGER"),
    ("cooldown_shift", "INTEGER"),
    ("attack_threshold_shift", "INTEGER"),
    ("attack_threshold_ratio", "INTEGER"),
    ("attack_spoils_ratio", "INTEGER"),
    ("defense_threshold_shift", "INTEGER"),
    ("defense_threshold_ratio", "INTEGER"),
    ("defense_salvage_ratio", "INTEGER"),
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
    """Apply migration 005 in-place. Returns counters for logging."""
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
    log.info("migration 005 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
