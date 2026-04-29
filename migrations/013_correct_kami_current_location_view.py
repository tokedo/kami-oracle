"""Migration 013: correct kami_current_location view (Session 14.5).

Re-defines kami_current_location to reflect the actual Kamigotchi
mechanic: kamis don't move; operators do. A kami is on a node iff
currently harvesting. The Session 14 view filtered only to
harvest_start and never checked for a subsequent end-of-harvest
action, so it conflated "last node placed on" with "current node."
~95% of Session 14 view rows were stale-by-construction (verified
in S14 verification report: 6,556 of 6,963 with_loc rows is_stale).

Adds column ``currently_harvesting`` (BOOLEAN). Sets
``current_node_id`` / ``current_room_index`` to NULL when the kami
isn't currently harvesting (resting kamis are in their operator's
pocket; their physical room is the operator's current room — a
separate join, not bundled here). Adds ``last_harvest_node_id`` /
``last_harvest_start_ts`` so the agent can still answer "where was
this kami last seen on a node" without overclaiming current state.

End-of-harvest action set discovered Session 14.5 Part 1:
- ``harvest_collect`` does NOT end harvesting (mid-session payout
  claim only — kami stays on node; chain spot-check confirmed
  ``getKami(...).state = HARVESTING``).
- ``harvest_stop`` ends harvest (kami_id is the actor).
- ``harvest_liquidate`` ends harvest **for the victim**, but the
  decoder stores the *killer* in ``kami_id`` (decoder.py:165-168
  maps killerID→kami_id, victimHarvID→harvest_id). The view
  resolves the victim via a self-join on harvest_id back to the
  victim's harvest_start row (harvest_id = keccak("harvest" ||
  kami_id_be), bijective).
- ``die`` / ``revive`` are NOT in the decoder's action_type set
  — known gap. A kami that died from non-liquidate causes (HP
  starvation outside a liquidate) won't have its harvest closed
  in our records, so currently_harvesting could read TRUE while
  the chain says DEAD. Out of scope to fix here.

Window-edge: a kami harvesting continuously for >7 days has no
harvest_start in our rolling window, only harvest_collect rows.
The view treats harvest_collect as evidence of active harvesting,
so currently_harvesting reads TRUE — but current_node_id is NULL
because we don't have the start row to read the node from. Agent
should treat (currently_harvesting=TRUE, current_node_id IS NULL)
as "harvesting on an unknown node — verify against chain via
Kamibots."

Renames are deferred — the corrected name reads accurately for
both states (harvesting → on a node; resting → NULL/in-pocket).

Idempotent: CREATE OR REPLACE VIEW. Bumps schema_version 12 → 13.
No backfill — the underlying ``kami_action`` rows are unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 13

# 30 minutes in seconds. Only meaningful when currently_harvesting:
# a still-presumed-active harvest signal older than 30 min is worth
# verifying for destructive ops keyed on location.
STALE_THRESHOLD_SECONDS = 30 * 60  # = 1800

CREATE_VIEW_SQL = f"""
CREATE OR REPLACE VIEW kami_current_location AS
WITH
-- Harvest-active events: latest signal that the kami is/was on a node.
-- harvest_start places the kami on a node. harvest_collect is mid-
-- session evidence (kami stays on node — verified S14.5 Part 1).
active_events AS (
    SELECT
        a.kami_id,
        a.action_type,
        a.block_timestamp AS ts
    FROM kami_action a
    WHERE a.kami_id IS NOT NULL
      AND a.action_type IN ('harvest_start', 'harvest_collect')
),
last_active AS (
    SELECT kami_id, action_type, ts
    FROM (
        SELECT kami_id, action_type, ts,
               ROW_NUMBER() OVER (PARTITION BY kami_id ORDER BY ts DESC) AS rn
        FROM active_events
    ) WHERE rn = 1
),
-- Latest harvest_start per kami: source of node_id / room.
last_start AS (
    SELECT kami_id, node_id, ts
    FROM (
        SELECT a.kami_id,
               CAST(a.node_id AS INTEGER) AS node_id,
               a.block_timestamp AS ts,
               ROW_NUMBER() OVER (
                   PARTITION BY a.kami_id ORDER BY a.block_timestamp DESC
               ) AS rn
        FROM kami_action a
        WHERE a.kami_id IS NOT NULL
          AND a.action_type = 'harvest_start'
          AND a.node_id IS NOT NULL
    ) WHERE rn = 1
),
-- End-of-harvest events with kami_id properly attributed.
-- harvest_stop: actor kami_id is direct.
-- harvest_liquidate: kami_id on the row is the killer; victim is
-- resolved via harvest_id self-join to a victim harvest_start.
end_events AS (
    SELECT a.kami_id, a.action_type, a.block_timestamp AS ts
    FROM kami_action a
    WHERE a.kami_id IS NOT NULL
      AND a.action_type = 'harvest_stop'
    UNION ALL
    SELECT DISTINCT hs.kami_id, lq.action_type, lq.block_timestamp AS ts
    FROM kami_action lq
    JOIN kami_action hs
      ON hs.action_type = 'harvest_start'
     AND hs.harvest_id = lq.harvest_id
    WHERE lq.action_type = 'harvest_liquidate'
),
last_end AS (
    SELECT kami_id, action_type, ts
    FROM (
        SELECT kami_id, action_type, ts,
               ROW_NUMBER() OVER (PARTITION BY kami_id ORDER BY ts DESC) AS rn
        FROM end_events
    ) WHERE rn = 1
)
SELECT
    s.kami_id,
    s.kami_index,
    s.name,
    s.account_name,
    -- currently_harvesting: have a harvest-active signal more recent
    -- than any end-of-harvest signal for this kami.
    CASE
        WHEN la.ts IS NULL THEN FALSE
        WHEN le.ts IS NULL THEN TRUE
        WHEN la.ts > le.ts THEN TRUE
        ELSE FALSE
    END AS currently_harvesting,
    -- current_node_id / current_room_index: only when currently
    -- harvesting AND we have a harvest_start in window. NULL when
    -- not harvesting (resting kami → operator's room) OR when the
    -- start is outside the rolling window (window-edge).
    CASE
        WHEN la.ts IS NOT NULL
         AND (le.ts IS NULL OR la.ts > le.ts)
        THEN ls.node_id
    END AS current_node_id,
    CASE
        WHEN la.ts IS NOT NULL
         AND (le.ts IS NULL OR la.ts > le.ts)
        THEN n.room_index
    END AS current_room_index,
    -- last_harvest_node_id / last_harvest_start_ts: from the latest
    -- harvest_start regardless of whether the kami is still on it.
    -- "Where was this kami last seen on a node?" answer.
    ls.node_id AS last_harvest_node_id,
    ls.ts AS last_harvest_start_ts,
    -- since_ts: timestamp of the latest action defining current
    -- state (start, collect, stop, or liquidate-victim).
    GREATEST(la.ts, le.ts) AS since_ts,
    CAST(EXTRACT(EPOCH FROM (now() - GREATEST(la.ts, le.ts))) AS INTEGER)
        AS freshness_seconds,
    -- is_stale: only meaningful when currently_harvesting. TRUE
    -- when the latest active signal is older than 30 min. NULL
    -- when not harvesting (no signal to be stale about).
    CASE
        WHEN la.ts IS NOT NULL
         AND (le.ts IS NULL OR la.ts > le.ts)
         AND EXTRACT(EPOCH FROM (now() - la.ts)) > {STALE_THRESHOLD_SECONDS}
            THEN TRUE
        WHEN la.ts IS NOT NULL
         AND (le.ts IS NULL OR la.ts > le.ts)
            THEN FALSE
        ELSE NULL
    END AS is_stale
FROM kami_static s
LEFT JOIN last_active la ON la.kami_id = s.kami_id
LEFT JOIN last_end le ON le.kami_id = s.kami_id
LEFT JOIN last_start ls ON ls.kami_id = s.kami_id
LEFT JOIN nodes_catalog n ON n.node_index = ls.node_id
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
    """Apply migration 013 in-place. Returns counters for logging."""
    conn.execute(CREATE_VIEW_SQL)
    _bump_schema_version(conn)
    return {"views_replaced": 1}


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
    log.info("migration 013 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
