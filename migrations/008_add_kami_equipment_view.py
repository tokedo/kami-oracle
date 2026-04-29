"""Migration 008: add kami_equipment view.

Per Session 13 — see ``memory/decoder-notes.md`` "Session 13 —
items_catalog + kami_equipment view". UNNESTs
``kami_static.equipment_json`` and joins ``items_catalog`` on
``item_index`` to expose slot-resolved equipment as one row per
equipped item per kami. Replaces the previous workaround of joining
``equipment_json`` against items.csv inline on the agent side.

DuckDB note: ``equipment_json`` is stored as VARCHAR holding a JSON
array of integers (e.g. ``"[30031]"``). DuckDB v1.5+ casts that
shape directly via ``CAST(equipment_json AS INTEGER[])``, which is
cleaner than ``json_extract`` + per-element CAST. ``UNNEST`` in the
FROM clause is implicitly correlated in DuckDB (no LATERAL keyword
required), so the comma-join shape produces the expected one-row-
per-array-element fan-out.

Freshness: ``freshness_seconds`` and ``is_stale`` are *computed at
query time* from ``kami_static.build_refreshed_ts``; they are not
columns on ``kami_static``. Threshold is 36h (129600s) — populator
sweeps daily, 36h gives one missed-sweep slack. ``is_stale = TRUE``
means the agent must verify with live chain state via Kamibots
before destructive ops; snapshot lag means equipment can false-
positive (an unequipped pet stays in the JSON until next sweep).

Idempotent: CREATE OR REPLACE VIEW. Bumps schema_version 7 → 8.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 8

# 36 hours in seconds. The populator runs a daily sweep; this
# threshold gives the agent one missed sweep before flagging the
# row as stale.
STALE_THRESHOLD_SECONDS = 36 * 3600  # = 129600

CREATE_VIEW_SQL = f"""
CREATE OR REPLACE VIEW kami_equipment AS
SELECT
    s.kami_id,
    s.kami_index,
    s.name,
    s.account_name,
    i.slot_type,
    je.value AS item_index,
    i.name   AS item_name,
    i.effect AS item_effect,
    s.build_refreshed_ts,
    CAST(EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) AS INTEGER)
        AS freshness_seconds,
    EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) > {STALE_THRESHOLD_SECONDS}
        AS is_stale
FROM kami_static s,
     UNNEST(CAST(s.equipment_json AS INTEGER[])) AS je(value)
LEFT JOIN items_catalog i
    ON i.item_index = je.value
WHERE s.equipment_json IS NOT NULL
  AND s.equipment_json != '[]'
"""


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
    """Apply migration 008 in-place. Returns counters for logging."""
    conn.execute(CREATE_VIEW_SQL)
    _bump_schema_version(conn)
    return {"views_created": 1}


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
    log.info("migration 008 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
