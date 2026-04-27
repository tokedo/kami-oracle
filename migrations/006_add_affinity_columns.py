"""Migration 006: add body_affinity / hand_affinity columns to kami_static.

Per Session 12 — see ``memory/decoder-notes.md`` "Session 12 — affinities".
``getKami(kamiId).affinities`` is a fixed-length 2-element string array
drawn from ``{EERIE, NORMAL, SCRAP, INSECT}`` (uppercase on chain). The
canonical ordering (``kamigotchi-context/systems/state-reading.md`` line
101) is ``[body, hand]``: ``affinities[0]`` is the body affinity,
``affinities[1]`` is the hand affinity.

The Session 9 populator already stores the raw 2-string array as JSON
in the ``affinities`` column. Session 12 splits it into two scalar
columns so kami-agent can ``GROUP BY body_affinity`` / ``JOIN`` on
affinity directly without parsing JSON inline. The integer
``body`` / ``hand`` columns (~30 / ~27 distinct trait indices) stay
unchanged — the new affinity columns sit beside them, denormalized
for query ergonomics.

Reuses ``build_refreshed_ts`` from migration 004 — no new timestamp.

Idempotent: ALTER ... ADD COLUMN guarded by an information_schema check.
The actual hydration is done by the populator on the next refresh and
by ``scripts/backfill_kami_affinity.py`` for existing rows.

Bumps schema_version 5 → 6.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 6

NEW_COLUMNS: list[tuple[str, str]] = [
    ("body_affinity", "VARCHAR"),
    ("hand_affinity", "VARCHAR"),
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
    """Apply migration 006 in-place. Returns counters for logging."""
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
    log.info("migration 006 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
