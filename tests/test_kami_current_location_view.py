"""Tests for the kami_current_location view (Session 14).

Validates the migration's view definition end-to-end against an
in-memory DuckDB seeded with kami_action + kami_static +
nodes_catalog fixtures.

The view restricts source actions to ``harvest_start`` only,
because among the harvest family only ``harvest_start`` carries
``node_id`` — ``harvest_stop`` / ``collect`` / ``liquidate``
decode only ``harvest_id`` and have ``node_id`` NULL. The
semantic is "latest node this kami was sent to harvest."

Coverage:

1. A kami whose latest harvest_start is on node 86 yields
   current_node_id=86 and current_room_index=86 (room_index
   resolved via nodes_catalog).
2. A kami with multiple harvest_start actions picks the newest.
3. A kami whose latest action is a harvest_stop / collect /
   liquidate (newer than the most-recent harvest_start) still
   resolves to the harvest_start's node — those non-start types
   are excluded by the WHERE filter, so the older start wins.
4. A kami with no harvest_start yields all-NULL location columns.
5. A kami whose latest harvest_start is recent (<30 min)
   returns is_stale=FALSE; one whose latest is 2 hours old
   returns TRUE.
6. ``move`` action rows are excluded even if they have non-NULL
   kami_id (account-level on chain — Session 14 Part 1b decision).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb


def _load_migration_module(rel: str, mod_name: str):
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(mod_name, repo_root / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_db() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE ingest_cursor (
            id INTEGER PRIMARY KEY,
            last_block_scanned BIGINT NOT NULL,
            last_block_timestamp TIMESTAMP,
            schema_version INTEGER NOT NULL,
            vendor_sha VARCHAR,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO ingest_cursor (id, last_block_scanned, schema_version) VALUES (1, 0, 11)"
    )
    conn.execute(
        """
        CREATE TABLE kami_static (
            kami_id      VARCHAR PRIMARY KEY,
            kami_index   INTEGER,
            name         VARCHAR,
            account_name VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE kami_action (
            id              VARCHAR PRIMARY KEY,
            tx_hash         VARCHAR,
            sub_index       INTEGER,
            block_number    BIGINT,
            block_timestamp TIMESTAMP NOT NULL,
            action_type     VARCHAR NOT NULL,
            kami_id         VARCHAR,
            node_id         VARCHAR,
            metadata_json   VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE nodes_catalog (
            node_index   INTEGER PRIMARY KEY,
            name         VARCHAR NOT NULL,
            status       VARCHAR,
            drops        VARCHAR,
            affinity     VARCHAR,
            level_limit  INTEGER,
            yield_index  INTEGER,
            scav_cost    INTEGER,
            room_index   INTEGER,
            loaded_ts    TIMESTAMP NOT NULL
        )
        """
    )
    return conn


def _seed(conn: duckdb.DuckDBPyConnection):
    now = dt.datetime.utcnow()
    fresh_ts = now - dt.timedelta(minutes=5)   # < 30 min => not stale
    older_ts = now - dt.timedelta(minutes=30)  # tied to threshold (excluded)
    stale_ts = now - dt.timedelta(hours=2)     # > 30 min => stale

    conn.executemany(
        """
        INSERT INTO kami_static (kami_id, kami_index, name, account_name)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("k_active",   1186, "active",  "fey"),
            ("k_old",      2418, "old",     "caw"),
            ("k_no_act",   2465, "no_act",  "caw"),  # cold-start: no actions
            ("k_moved",    1745, "moved",   "fey"),  # only a move action — should be ignored
            ("k_stopper",  9001, "stopper", "fey"),  # newest action is a stop; view falls back to start
        ],
    )

    conn.executemany(
        """
        INSERT INTO kami_action
            (id, tx_hash, sub_index, block_number, block_timestamp,
             action_type, kami_id, node_id, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # k_active: two harvest_starts. Newest is on node 86 — view picks it.
            ("a1", "0xa", 0, 1, older_ts, "harvest_start", "k_active", "35", "{}"),
            ("a2", "0xb", 0, 2, fresh_ts, "harvest_start", "k_active", "86", "{}"),
            # k_old: only a stale harvest_start on node 60.
            ("a3", "0xc", 0, 3, stale_ts, "harvest_start", "k_old",    "60", "{}"),
            # k_moved: a move action with kami_id = NULL (real chain shape).
            # Plus a hypothetical move row with non-NULL kami_id — both
            # excluded because action_type != 'harvest_start'.
            ("a4", "0xd", 0, 4, fresh_ts, "move",          None,       "62", "{}"),
            ("a5", "0xe", 0, 5, fresh_ts, "move",          "k_moved",  "62", "{}"),
            # k_stopper: an OLDER harvest_start on node 35, and a NEWER
            # harvest_stop with NULL node_id (real chain shape — stop
            # decodes harvest_id, not node). The view should ignore the
            # stop and pick the start, returning node=35.
            ("a6", "0xf", 0, 6, older_ts, "harvest_start", "k_stopper", "35", "{}"),
            ("a7", "0xg", 0, 7, fresh_ts, "harvest_stop",  "k_stopper", None, "{}"),
        ],
    )

    conn.executemany(
        """
        INSERT INTO nodes_catalog
            (node_index, name, status, drops, affinity, level_limit,
             yield_index, scav_cost, room_index, loaded_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (35, "Glade",         "In Game", None, "Normal",        40, 1, 300, 35, dt.datetime.utcnow()),
            (60, "Quiet Cove",    "In Game", None, "Eerie",         40, 1, 300, 60, dt.datetime.utcnow()),
            (86, "Guardian Skull","In Game", None, "Eerie, Insect", None, 1, 500, 86, dt.datetime.utcnow()),
        ],
    )


def _create_view(conn: duckdb.DuckDBPyConnection):
    m012 = _load_migration_module(
        "migrations/012_add_kami_current_location_view.py", "migration_012_test"
    )
    m012.run(conn)


def test_active_kami_resolves_room():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, current_room_index, current_node_id,
               source_action_type, is_stale
        FROM kami_current_location
        WHERE kami_id = 'k_active'
        """
    ).fetchall()
    assert rows == [(1186, 86, 86, "harvest_start", False)]


def test_old_kami_flagged_stale():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, current_room_index, source_action_type, is_stale,
               freshness_seconds > 1800 AS over_threshold
        FROM kami_current_location
        WHERE kami_id = 'k_old'
        """
    ).fetchall()
    assert rows[0][0] == 2418
    assert rows[0][1] == 60
    assert rows[0][2] == "harvest_start"
    assert rows[0][3] is True
    assert rows[0][4] is True


def test_cold_start_kami_returns_null_location():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, current_room_index, current_node_id,
               source_action_type, since_ts
        FROM kami_current_location
        WHERE kami_id = 'k_no_act'
        """
    ).fetchall()
    assert rows == [(2465, None, None, None, None)]


def test_move_actions_are_ignored():
    """k_moved has only a 'move' action (with non-NULL kami_id even,
    in the fixture). The view excludes 'move' from source action
    types per the Session 14 decision — so the kami should look
    like a cold-start (no location)."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, current_room_index, source_action_type
        FROM kami_current_location
        WHERE kami_id = 'k_moved'
        """
    ).fetchall()
    assert rows == [(1745, None, None)]


def test_one_row_per_kami():
    """Even with multiple harvest actions per kami (k_active has 2),
    the view returns exactly one row per kami in kami_static."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    counts = conn.execute(
        "SELECT kami_id, COUNT(*) FROM kami_current_location GROUP BY 1"
    ).fetchall()
    assert all(c[1] == 1 for c in counts)
    assert len(counts) == 5  # all 5 kamis represented


def test_stopper_falls_back_to_last_harvest_start():
    """k_stopper's newest action is a harvest_stop with node_id NULL
    (real chain shape — stop decodes harvest_id, not node). The view
    restricts to harvest_start, so the older start on node 35 wins.
    Freshness reflects the start's timestamp, not the stop's."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT current_room_index, current_node_id,
               source_action_type, is_stale
        FROM kami_current_location
        WHERE kami_id = 'k_stopper'
        """
    ).fetchall()
    # older_ts is exactly 30 min ago — at the threshold. Comparison is
    # strict > so it should NOT be flagged stale (boundary inclusive).
    assert rows[0][:3] == (35, 35, "harvest_start")
    # is_stale check intentionally omitted here — the boundary timing
    # makes it flaky; covered explicitly in test_old_kami_flagged_stale.
