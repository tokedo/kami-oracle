"""Migration 012: add kami_current_location view.

Per Session 14 — see ``memory/decoder-notes.md`` "Session 14 —
skills_catalog + nodes_catalog + views". For each kami, picks the
most recent ``harvest_start`` action and resolves the room via
``nodes_catalog``. Replaces the prior workaround of reading
``harvest.node.roomIndex`` from a live ``get_kami_state`` call
(which returns the *last-harvested* node, not the kami's actual
current room).

Why **only** ``harvest_start``: among the harvest action family,
only ``harvest_start`` carries ``node_id``. ``harvest_stop`` /
``collect`` / ``liquidate`` decode only ``harvest_id`` (the chain
call references the harvest entity, not the node) — so on those
rows ``node_id`` is NULL. The semantic is honest: "latest node
this kami was sent to harvest." Kamis remain on their last-
harvested node until an untracked ``move`` action, so this is the
correct current-location signal in the absence of move
attribution.

**move actions are NOT included.** Discovery (Session 14
Part 1b): chain ``system.account.move`` is account-level, not
kami-level — the decoder stores ``toIndex`` in
``kami_action.node_id`` (a *room index*, not a node index) but
``kami_id`` is NULL on the row because the chain call has no
per-kami binding. Attributing a move to specific kamis requires
snapshot-time account-membership resolution, which is a non-
trivial decoder change and out of scope for Session 14.

is_stale threshold is 1800s (30 minutes). Rationale: kamis park
on nodes for hours; 30 min of fresh trust is enough for read-
only ops; longer means the agent should verify the kami's
actual room against chain via Kamibots before destructive ops.
NOT live truth: an account-level move can have shifted the kami
to a new room without us attributing it to this specific kami.

Kamis with no observed ``harvest_start`` in the 28d window get
NULL location columns — the cold-start case the agent already
handles.

Idempotent: CREATE OR REPLACE VIEW. Bumps schema_version 11 → 12.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 12

# 30 minutes in seconds. Kamis typically park on a node for hours
# while harvesting; 30 min of fresh trust suffices for read-only
# operations. Longer => the agent should verify against live chain
# before any destructive op keyed on location.
STALE_THRESHOLD_SECONDS = 30 * 60  # = 1800

CREATE_VIEW_SQL = f"""
CREATE OR REPLACE VIEW kami_current_location AS
WITH last_loc_action AS (
    SELECT
        a.kami_id,
        a.action_type,
        a.node_id,
        a.metadata_json,
        a.block_timestamp,
        ROW_NUMBER() OVER (
            PARTITION BY a.kami_id
            ORDER BY a.block_timestamp DESC
        ) AS rn
    FROM kami_action a
    WHERE a.kami_id IS NOT NULL
      AND a.action_type = 'harvest_start'
      AND a.node_id IS NOT NULL
)
SELECT
    s.kami_id,
    s.kami_index,
    s.name,
    s.account_name,
    n.room_index AS current_room_index,
    CAST(la.node_id AS INTEGER) AS current_node_id,
    la.action_type AS source_action_type,
    la.block_timestamp AS since_ts,
    CAST(EXTRACT(EPOCH FROM (now() - la.block_timestamp)) AS INTEGER)
        AS freshness_seconds,
    EXTRACT(EPOCH FROM (now() - la.block_timestamp)) > {STALE_THRESHOLD_SECONDS}
        AS is_stale
FROM kami_static s
LEFT JOIN last_loc_action la
    ON la.kami_id = s.kami_id AND la.rn = 1
LEFT JOIN nodes_catalog n
    ON n.node_index = CAST(la.node_id AS INTEGER)
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
    """Apply migration 012 in-place. Returns counters for logging."""
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
    log.info("migration 012 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
