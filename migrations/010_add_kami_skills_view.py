"""Migration 010: add kami_skills view.

Per Session 14 — see ``memory/decoder-notes.md`` "Session 14 —
skills_catalog + nodes_catalog + views". UNNESTs
``kami_static.skills_json`` and joins ``skills_catalog`` on
``skill_index`` to expose per-skill effect / tree / tier details
as one row per (kami, invested skill). Replaces the prior
workaround of joining skills_json against skills.csv inline on
the agent side.

DuckDB note: ``skills_json`` is stored as VARCHAR holding a JSON
array of objects, e.g. ``[{"index": 212, "points": 5}, ...]``.
DuckDB v1.5+ casts that shape directly via
``CAST(skills_json AS STRUCT("index" INTEGER, "points" INTEGER)[])``,
which produces typed columns and avoids the older
``json_extract`` + per-field CAST shape. Mirrors the
``UNNEST(CAST(equipment_json AS INTEGER[]))`` pattern from
Session 13's kami_equipment view — same comma-join fan-out, no
LATERAL keyword required.

Freshness: ``freshness_seconds`` and ``is_stale`` are *computed
at query time* from ``kami_static.build_refreshed_ts``; they are
not columns on ``kami_static``. Threshold is 36h (129600s) — same
as kami_equipment, populator sweeps daily, gives one missed-
sweep slack. Kami builds change rarely, so a stale flag here is
mostly defensive — the snapshot lag still warrants the boundary.

Per-tree point sums and archetype labels are intentionally NOT
stored on the view. Compute per-tree totals with
``SELECT tree, SUM(points) FROM kami_skills WHERE kami_id = X
GROUP BY tree``. Archetype classification stays in agent code
where the heuristic is visible — oracle exposes the components.

Idempotent: CREATE OR REPLACE VIEW. Bumps schema_version 9 → 10.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 10

# 36 hours in seconds. Same threshold as kami_equipment (Session 13)
# — the populator runs a daily sweep; this gives one missed-sweep
# slack before the row is flagged stale. Kami builds change rarely,
# so this is mostly defensive — a stale row should still trigger
# the agent's verify-before-act discipline.
STALE_THRESHOLD_SECONDS = 36 * 3600  # = 129600

CREATE_VIEW_SQL = f"""
CREATE OR REPLACE VIEW kami_skills AS
SELECT
    s.kami_id,
    s.kami_index,
    s.name,
    s.account_name,
    je."index"   AS skill_index,
    c.name       AS skill_name,
    c.tree,
    c.tier,
    je."points"  AS points,
    c.effect,
    c.value,
    c.units,
    s.build_refreshed_ts,
    CAST(EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) AS INTEGER)
        AS freshness_seconds,
    EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) > {STALE_THRESHOLD_SECONDS}
        AS is_stale
FROM kami_static s,
     UNNEST(CAST(s.skills_json AS STRUCT("index" INTEGER, "points" INTEGER)[])) AS t(je)
LEFT JOIN skills_catalog c
    ON c.skill_index = je."index"
WHERE s.skills_json IS NOT NULL
  AND s.skills_json != '[]'
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
    """Apply migration 010 in-place. Returns counters for logging."""
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
    log.info("migration 010 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
