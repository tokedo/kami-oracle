"""Tests for the nodes_catalog loader (Session 14).

Covers:

1. End-to-end load of a 5-row fixture: row count, room_index =
   node_index identity, status / affinity / drops round-trip, PK
   uniqueness on re-load.
2. ``ensure_loaded`` is a no-op on a non-empty table.
3. Missing CSV → loud FileNotFoundError, not silent stale data.
4. Spot-check node 86 (agent's example): name "Guardian Skull",
   room_index = 86 (Index identity), affinity carries the
   comma-list verbatim.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from ingester.nodes_catalog import (
    ensure_loaded,
    load_nodes_catalog,
    parse_nodes_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "nodes_sample.csv"


def _make_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the nodes_catalog table created."""
    conn = duckdb.connect(":memory:")
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


def test_parse_nodes_csv():
    rows = parse_nodes_csv(FIXTURE)
    assert len(rows) == 5
    by_idx = {r["node_index"]: r for r in rows}

    # Spot-check agent's example.
    assert by_idx[86]["name"] == "Guardian Skull"
    assert by_idx[86]["room_index"] == 86  # Session 14 Index identity
    assert by_idx[86]["affinity"] == "Eerie, Insect"
    assert by_idx[86]["status"] == "In Game"
    assert by_idx[86]["scav_cost"] == 500
    assert by_idx[86]["level_limit"] is None  # blank cell

    # Identity holds across the fixture.
    for r in rows:
        assert r["room_index"] == r["node_index"]


def test_load_nodes_catalog_into_db():
    conn = _make_conn()
    stats = load_nodes_catalog(conn, FIXTURE)
    assert stats == {"rows_loaded": 5, "rows_in_game": 5, "rows_with_room": 5}

    n = conn.execute("SELECT COUNT(*) FROM nodes_catalog").fetchone()[0]
    assert n == 5

    row = conn.execute(
        "SELECT name, room_index, affinity, status, scav_cost "
        "FROM nodes_catalog WHERE node_index = 86"
    ).fetchone()
    assert row == ("Guardian Skull", 86, "Eerie, Insect", "In Game", 500)


def test_load_is_idempotent():
    conn = _make_conn()
    load_nodes_catalog(conn, FIXTURE)
    load_nodes_catalog(conn, FIXTURE)  # truncate-and-reload by default
    n = conn.execute("SELECT COUNT(*) FROM nodes_catalog").fetchone()[0]
    assert n == 5


def test_ensure_loaded_skips_when_populated(tmp_path):
    nodes_path = tmp_path / "nodes.csv"
    nodes_path.write_bytes(FIXTURE.read_bytes())

    conn = _make_conn()
    load_nodes_catalog(conn, FIXTURE)
    stats = ensure_loaded(conn, tmp_path)
    assert stats["rows_loaded"] == 0
    assert stats.get("skipped") == 1


def test_ensure_loaded_loads_when_empty(tmp_path):
    nodes_path = tmp_path / "nodes.csv"
    nodes_path.write_bytes(FIXTURE.read_bytes())

    conn = _make_conn()
    stats = ensure_loaded(conn, tmp_path)
    assert stats["rows_loaded"] == 5
    assert stats["rows_in_game"] == 5


def test_missing_csv_raises():
    conn = _make_conn()
    with pytest.raises(FileNotFoundError):
        load_nodes_catalog(conn, Path("/nonexistent/nodes.csv"))
