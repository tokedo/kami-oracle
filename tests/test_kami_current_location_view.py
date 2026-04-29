"""Tests for the kami_current_location view (Session 14, corrected S14.5).

Validates the migration's view definition end-to-end against an
in-memory DuckDB seeded with kami_action + kami_static +
nodes_catalog fixtures.

Session 14.5 correction: the view now reflects whether a kami is
currently on a node (i.e. mid-harvest) rather than "last node
sent to harvest." The end-of-harvest action set is
{harvest_stop, harvest_liquidate (victim, resolved via harvest_id
self-join)}. ``harvest_collect`` does NOT end harvesting (mid-
session payout — kami stays on node).

Coverage:

1. A kami whose latest action is a harvest_start on node 86
   yields currently_harvesting=TRUE, current_node_id=86,
   current_room_index=86.
2. A kami with a harvest_start then a later harvest_collect
   stays currently_harvesting=TRUE (collect is mid-session
   evidence, not an end signal); current_node_id from the start.
3. A kami with a harvest_start then a later harvest_stop has
   currently_harvesting=FALSE, current_node_id=NULL,
   last_harvest_node_id populated.
4. A kami who was liquidated (their harvest_id appears as
   victimHarvID on a liquidate row keyed to a different
   killer kami_id) has currently_harvesting=FALSE,
   current_node_id=NULL, last_harvest_node_id populated.
5. A killer kami who liquidates someone but is themselves
   harvesting stays currently_harvesting=TRUE — their own
   liquidate row is NOT an end-of-harvest signal for them.
6. A cold-start kami (no harvest history) yields all-NULL
   location columns and currently_harvesting=FALSE.
7. A kami whose latest harvest_start is recent (<30 min)
   returns is_stale=FALSE; one whose latest is 2 hours old
   returns TRUE. is_stale is NULL when not currently harvesting.
8. ``move`` action rows are ignored (account-level on chain,
   not a kami-movement signal — Session 14 / S14.5 decision).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
from eth_utils import keccak


def _load_migration_module(rel: str, mod_name: str):
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(mod_name, repo_root / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harvest_id_for(kami_id_int: int) -> str:
    """Same derivation as ingester/harvest_resolver.py — the bijection
    that lets liquidate.harvest_id be self-joined back to victim."""
    digest = keccak(b"harvest" + kami_id_int.to_bytes(32, "big"))
    return str(int.from_bytes(digest, "big"))


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
            harvest_id      VARCHAR,
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


# Numeric kami_ids so harvest_id derivation is meaningful in fixtures.
KAMI_ACTIVE = "1001"           # currently_harvesting via fresh harvest_start on 86
KAMI_COLLECTOR = "1002"        # harvest_start then harvest_collect — still harvesting
KAMI_STOPPER = "1003"          # harvest_start then harvest_stop — not harvesting
KAMI_OLD = "1004"              # one stale harvest_start on 60
KAMI_NO_ACT = "1005"           # cold-start
KAMI_MOVED = "1006"            # only a move action
KAMI_VICTIM = "1007"           # got liquidated
KAMI_KILLER = "1008"           # liquidated KAMI_VICTIM, still harvesting on 35


def _seed(conn: duckdb.DuckDBPyConnection):
    now = dt.datetime.utcnow()
    fresh_ts = now - dt.timedelta(minutes=5)   # < 30 min => not stale
    older_ts = now - dt.timedelta(minutes=30)  # at threshold (excluded)
    stale_ts = now - dt.timedelta(hours=2)     # > 30 min => stale
    very_fresh_ts = now - dt.timedelta(minutes=1)

    victim_harvest_id = _harvest_id_for(int(KAMI_VICTIM))
    active_harvest_id = _harvest_id_for(int(KAMI_ACTIVE))
    collector_harvest_id = _harvest_id_for(int(KAMI_COLLECTOR))
    stopper_harvest_id = _harvest_id_for(int(KAMI_STOPPER))
    killer_harvest_id = _harvest_id_for(int(KAMI_KILLER))

    conn.executemany(
        """
        INSERT INTO kami_static (kami_id, kami_index, name, account_name)
        VALUES (?, ?, ?, ?)
        """,
        [
            (KAMI_ACTIVE,    1186, "active",    "fey"),
            (KAMI_COLLECTOR, 1200, "collector", "fey"),
            (KAMI_STOPPER,   9001, "stopper",   "fey"),
            (KAMI_OLD,       2418, "old",       "caw"),
            (KAMI_NO_ACT,    2465, "no_act",    "caw"),
            (KAMI_MOVED,     1745, "moved",     "fey"),
            (KAMI_VICTIM,    7777, "victim",    "fey"),
            (KAMI_KILLER,    2759, "killer",    "shirt"),
        ],
    )

    conn.executemany(
        """
        INSERT INTO kami_action
            (id, tx_hash, sub_index, block_number, block_timestamp,
             action_type, kami_id, node_id, harvest_id, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # KAMI_ACTIVE: two harvest_starts. Newest is on node 86.
            ("a1", "0xa", 0, 1, older_ts, "harvest_start", KAMI_ACTIVE, "35", active_harvest_id, "{}"),
            ("a2", "0xb", 0, 2, fresh_ts, "harvest_start", KAMI_ACTIVE, "86", active_harvest_id, "{}"),
            # KAMI_COLLECTOR: harvest_start on 86 then harvest_collect (no node).
            ("c1", "0xc1", 0, 3, older_ts, "harvest_start",  KAMI_COLLECTOR, "86", collector_harvest_id, "{}"),
            ("c2", "0xc2", 0, 4, fresh_ts, "harvest_collect", KAMI_COLLECTOR, None, collector_harvest_id, "{}"),
            # KAMI_STOPPER: harvest_start on 35 then harvest_stop.
            ("s1", "0xs1", 0, 5, older_ts, "harvest_start", KAMI_STOPPER, "35", stopper_harvest_id, "{}"),
            ("s2", "0xs2", 0, 6, fresh_ts, "harvest_stop",  KAMI_STOPPER, None, stopper_harvest_id, "{}"),
            # KAMI_OLD: only a stale harvest_start on node 60.
            ("a3", "0xc", 0, 7, stale_ts, "harvest_start", KAMI_OLD, "60", _harvest_id_for(int(KAMI_OLD)), "{}"),
            # KAMI_MOVED: a move action with kami_id NULL (real chain shape)
            # plus a hypothetical move with non-NULL kami_id — both ignored.
            ("a4", "0xd", 0, 8, fresh_ts, "move", None,        "62", None, "{}"),
            ("a5", "0xe", 0, 9, fresh_ts, "move", KAMI_MOVED,  "62", None, "{}"),
            # KAMI_VICTIM: harvest_start on 73 then was liquidated.
            ("v1", "0xv1", 0, 10, older_ts, "harvest_start",     KAMI_VICTIM, "73",  victim_harvest_id, "{}"),
            # liquidate row: kami_id is the KILLER, victim's harvest_id derives from KAMI_VICTIM.
            ("v2", "0xv2", 0, 11, very_fresh_ts, "harvest_liquidate", KAMI_KILLER, None, victim_harvest_id, "{}"),
            # KAMI_KILLER: their own harvest_start on 35 — they're still harvesting.
            ("k1", "0xk1", 0, 12, fresh_ts, "harvest_start", KAMI_KILLER, "35", killer_harvest_id, "{}"),
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
            (73, "Petal Pit",     "In Game", None, "Insect",        40, 1, 300, 73, dt.datetime.utcnow()),
            (86, "Guardian Skull","In Game", None, "Eerie, Insect", None, 1, 500, 86, dt.datetime.utcnow()),
        ],
    )


def _create_view(conn: duckdb.DuckDBPyConnection):
    # Apply 012 then 013 — the corrected view replaces the original.
    m012 = _load_migration_module(
        "migrations/012_add_kami_current_location_view.py", "migration_012_test"
    )
    m012.run(conn)
    m013 = _load_migration_module(
        "migrations/013_correct_kami_current_location_view.py", "migration_013_test"
    )
    m013.run(conn)


def test_active_kami_resolves_room():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT kami_index, currently_harvesting, current_room_index,
               current_node_id, last_harvest_node_id, is_stale
        FROM kami_current_location
        WHERE kami_id = '{KAMI_ACTIVE}'
        """
    ).fetchall()
    assert rows == [(1186, True, 86, 86, 86, False)]


def test_collector_still_harvesting():
    """harvest_collect is mid-session — does NOT end harvesting."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT currently_harvesting, current_node_id, last_harvest_node_id
        FROM kami_current_location
        WHERE kami_id = '{KAMI_COLLECTOR}'
        """
    ).fetchall()
    assert rows == [(True, 86, 86)]


def test_stopper_not_harvesting_but_history_preserved():
    """harvest_stop ends harvest. current_node_id is NULL but
    last_harvest_node_id keeps the historical answer."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT currently_harvesting, current_node_id, current_room_index,
               last_harvest_node_id, is_stale
        FROM kami_current_location
        WHERE kami_id = '{KAMI_STOPPER}'
        """
    ).fetchall()
    # is_stale is NULL when not currently_harvesting.
    assert rows == [(False, None, None, 35, None)]


def test_liquidated_victim_not_harvesting():
    """Liquidate ends victim's harvest. The view resolves the victim
    via harvest_id self-join (kami_id on liquidate row is the killer,
    not the victim — decoder.py:165-168)."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT currently_harvesting, current_node_id, last_harvest_node_id
        FROM kami_current_location
        WHERE kami_id = '{KAMI_VICTIM}'
        """
    ).fetchall()
    assert rows == [(False, None, 73)]


def test_killer_still_harvesting_after_liquidating():
    """A kami's own liquidate row is NOT an end-of-harvest signal
    for them (chain spot-check S14.5: killer state=HARVESTING)."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT currently_harvesting, current_node_id, last_harvest_node_id
        FROM kami_current_location
        WHERE kami_id = '{KAMI_KILLER}'
        """
    ).fetchall()
    assert rows == [(True, 35, 35)]


def test_old_kami_flagged_stale():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT kami_index, currently_harvesting, current_room_index,
               is_stale, freshness_seconds > 1800 AS over_threshold
        FROM kami_current_location
        WHERE kami_id = '{KAMI_OLD}'
        """
    ).fetchall()
    assert rows[0][0] == 2418
    assert rows[0][1] is True
    assert rows[0][2] == 60
    assert rows[0][3] is True
    assert rows[0][4] is True


def test_cold_start_kami_returns_null_location():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT kami_index, currently_harvesting, current_room_index,
               current_node_id, last_harvest_node_id, since_ts, is_stale
        FROM kami_current_location
        WHERE kami_id = '{KAMI_NO_ACT}'
        """
    ).fetchall()
    assert rows == [(2465, False, None, None, None, None, None)]


def test_move_actions_are_ignored():
    """k_moved has only a 'move' action. The view excludes 'move'
    from source action types — so the kami looks like a cold-start."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        f"""
        SELECT kami_index, currently_harvesting, current_room_index,
               last_harvest_node_id
        FROM kami_current_location
        WHERE kami_id = '{KAMI_MOVED}'
        """
    ).fetchall()
    assert rows == [(1745, False, None, None)]


def test_one_row_per_kami():
    """Even with multiple harvest actions per kami, the view returns
    exactly one row per kami in kami_static."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    counts = conn.execute(
        "SELECT kami_id, COUNT(*) FROM kami_current_location GROUP BY 1"
    ).fetchall()
    assert all(c[1] == 1 for c in counts)
    assert len(counts) == 8  # all 8 kamis represented


def test_partition_is_exhaustive():
    """harvesting + (resting_with_history) + (no_history) covers
    every kami_static row exactly once."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE currently_harvesting) AS harvesting,
          COUNT(*) FILTER (WHERE NOT currently_harvesting AND last_harvest_node_id IS NOT NULL) AS resting,
          COUNT(*) FILTER (WHERE NOT currently_harvesting AND last_harvest_node_id IS NULL) AS no_history
        FROM kami_current_location
        """
    ).fetchone()
    total, harvesting, resting, no_history = row
    assert harvesting + resting + no_history == total
