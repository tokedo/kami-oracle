"""Tests for the kami_skills view (Session 14).

Validates the migration's view definition end-to-end against an
in-memory DuckDB seeded with a small kami_static + skills_catalog
fixture:

1. A kami with ``skills_json=[{index:212,points:5}]`` yields one
   row with skill_name="Cardio" (joined from skills_catalog),
   tree="Enlightened", tier=1, effect="SHS".
2. A kami with ``skills_json=[]`` is dropped (WHERE filter).
3. ``skills_json IS NULL`` rows are dropped.
4. ``freshness_seconds`` and ``is_stale`` derive from
   ``build_refreshed_ts``: a row whose ``build_refreshed_ts`` is
   "now" returns ``is_stale=FALSE``; a row 40h old returns
   ``is_stale=TRUE``. Threshold is 36h (129600s).
5. A kami with multiple invested skills produces multiple rows
   (UNNEST fan-out).
6. A skills_json entry referencing a skill_index missing from
   skills_catalog yields skill_name=NULL via LEFT JOIN — surfaces
   catalog drift, doesn't silently drop.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb


def _load_migration_module(rel: str, mod_name: str):
    """Mirror Storage._apply_migrations' loader so tests use the same
    code path as production."""
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(mod_name, repo_root / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_db() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the minimal schema the view needs."""
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
        "INSERT INTO ingest_cursor (id, last_block_scanned, schema_version) VALUES (1, 0, 9)"
    )
    conn.execute(
        """
        CREATE TABLE kami_static (
            kami_id            VARCHAR PRIMARY KEY,
            kami_index         INTEGER,
            name               VARCHAR,
            account_name       VARCHAR,
            skills_json        VARCHAR,
            build_refreshed_ts TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE skills_catalog (
            skill_index  INTEGER PRIMARY KEY,
            name         VARCHAR NOT NULL,
            tree         VARCHAR NOT NULL,
            tier         INTEGER,
            tree_req     INTEGER,
            max_rank     INTEGER,
            cost         INTEGER,
            effect       VARCHAR,
            value        VARCHAR,
            units        VARCHAR,
            exclusion    VARCHAR,
            description  VARCHAR,
            loaded_ts    TIMESTAMP NOT NULL
        )
        """
    )
    return conn


def _seed(conn: duckdb.DuckDBPyConnection):
    now = dt.datetime.utcnow()
    fresh = now
    stale = now - dt.timedelta(hours=40)  # > 36h threshold

    conn.executemany(
        """
        INSERT INTO kami_static
            (kami_id, kami_index, name, account_name, skills_json, build_refreshed_ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("k_solo",      1186, "solo",   "fey", '[{"index":212,"points":5}]',                                fresh),
            ("k_two",       1745, "two",    "fey", '[{"index":212,"points":3},{"index":222,"points":1}]',       fresh),
            ("k_stale",     2418, "stale",  "caw", '[{"index":222,"points":2}]',                                stale),
            ("k_empty",     2465, "empty",  "caw", "[]",                                                         fresh),
            ("k_null",      9999, "null",   "caw", None,                                                         fresh),
            ("k_unknown",   9000, "drift",  "caw", '[{"index":99999,"points":1}]',                              fresh),
        ],
    )
    now_ts = dt.datetime.utcnow()
    conn.executemany(
        """
        INSERT INTO skills_catalog
            (skill_index, name, tree, tier, tree_req, max_rank, cost,
             effect, value, units, exclusion, description, loaded_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (212, "Cardio",               "Enlightened", 1, 0, 5, 1, "SHS", "10",   "Stat",    None, "", now_ts),
            (222, "Meditative Breathing", "Enlightened", 2, 5, 5, 1, "DTS", "0.02", "Percent", None, "", now_ts),
        ],
    )


def _create_view(conn: duckdb.DuckDBPyConnection):
    m010 = _load_migration_module("migrations/010_add_kami_skills_view.py", "migration_010_test")
    m010.run(conn)


def test_view_resolves_single_skill_with_freshness():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, skill_index, skill_name, tree, tier, points,
               effect, value, units, is_stale
        FROM kami_skills
        WHERE kami_id = 'k_solo'
        """
    ).fetchall()
    assert rows == [(1186, 212, "Cardio", "Enlightened", 1, 5, "SHS", "10", "Stat", False)]


def test_view_flags_stale_kami():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, skill_name, is_stale,
               freshness_seconds > 129600 AS over_threshold
        FROM kami_skills
        WHERE kami_id = 'k_stale'
        """
    ).fetchall()
    assert rows[0][0] == 2418
    assert rows[0][1] == "Meditative Breathing"
    assert rows[0][2] is True
    assert rows[0][3] is True


def test_view_unnests_multi_skill_kami():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT skill_index, points FROM kami_skills
        WHERE kami_id = 'k_two'
        ORDER BY skill_index
        """
    ).fetchall()
    assert rows == [(212, 3), (222, 1)]


def test_view_excludes_empty_and_null_skills():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_id FROM kami_skills
        WHERE kami_id IN ('k_empty', 'k_null')
        """
    ).fetchall()
    assert rows == []


def test_view_unresolved_skill_yields_null_join():
    """A skills_json entry referencing a skill_index missing from
    skills_catalog yields skill_name=NULL via LEFT JOIN — surfaces
    drift between snapshot and catalog (the founder should re-vendor)."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        "SELECT skill_index, skill_name, tree, points "
        "FROM kami_skills WHERE kami_id='k_unknown'"
    ).fetchall()
    assert rows == [(99999, None, None, 1)]


def test_view_total_row_count():
    """k_solo (1) + k_two (2) + k_stale (1) + k_unknown (1) = 5 rows.
    k_empty + k_null contribute 0."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    n = conn.execute("SELECT COUNT(*) FROM kami_skills").fetchone()[0]
    assert n == 5
