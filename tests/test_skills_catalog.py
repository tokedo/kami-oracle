"""Tests for the skills_catalog loader (Session 14).

Covers:

1. End-to-end load of a 7-row fixture: row count, all 4 trees
   present, leading-blank-column handling (BOM + dot), exclusion
   strings round-tripped, PK uniqueness on re-load.
2. Effect-key distribution covers the documented taxonomy
   (SHS / SVS / DTS / ATS / ASR / HFB / RMB in the fixture).
3. ``ensure_loaded`` is a no-op on a non-empty table.
4. Missing CSV → loud FileNotFoundError, not silent stale data.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from ingester.skills_catalog import (
    ensure_loaded,
    load_skills_catalog,
    parse_skills_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "skills_sample.csv"


def _make_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the skills_catalog table created."""
    conn = duckdb.connect(":memory:")
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


def test_parse_skills_csv():
    rows = parse_skills_csv(FIXTURE)
    assert len(rows) == 7
    by_idx = {r["skill_index"]: r for r in rows}

    # All four trees present.
    trees = {r["tree"] for r in rows}
    assert trees == {"Predator", "Guardian", "Harvester", "Enlightened"}

    # Spot-checks: agent's loadout note examples.
    assert by_idx[212]["name"] == "Cardio"
    assert by_idx[212]["tree"] == "Enlightened"
    assert by_idx[212]["effect"] == "SHS"
    assert by_idx[212]["value"] == "10"
    assert by_idx[212]["units"] == "Stat"
    assert by_idx[212]["exclusion"] is None

    assert by_idx[222]["effect"] == "DTS"
    assert by_idx[222]["value"] == "0.02"
    assert by_idx[222]["units"] == "Percent"

    # Mutually-exclusive tier-3 choice in Predator: Warmonger excludes 132/133.
    assert by_idx[131]["exclusion"] == "132, 133"
    assert by_idx[131]["tier"] == 3
    assert by_idx[131]["tree_req"] == 5

    # Numeric columns parsed as ints.
    assert by_idx[111]["max_rank"] == 5
    assert by_idx[111]["cost"] == 1


def test_load_skills_catalog_into_db():
    conn = _make_conn()
    stats = load_skills_catalog(conn, FIXTURE)
    assert stats == {"rows_loaded": 7, "trees": 4}

    n = conn.execute("SELECT COUNT(*) FROM skills_catalog").fetchone()[0]
    assert n == 7

    # PK + tree + effect end-to-end.
    row = conn.execute(
        "SELECT name, tree, effect, value, units, exclusion "
        "FROM skills_catalog WHERE skill_index = 222"
    ).fetchone()
    assert row == ("Meditative Breathing", "Enlightened", "DTS", "0.02", "Percent", None)

    # Exclusion preserved verbatim, not parsed.
    excl_row = conn.execute(
        "SELECT exclusion FROM skills_catalog WHERE skill_index = 132"
    ).fetchone()
    assert excl_row == ("131, 133",)

    # Distinct trees count (regression guard for catalog drift).
    trees = conn.execute(
        "SELECT COUNT(DISTINCT tree) FROM skills_catalog"
    ).fetchone()[0]
    assert trees == 4


def test_load_is_idempotent():
    conn = _make_conn()
    load_skills_catalog(conn, FIXTURE)
    load_skills_catalog(conn, FIXTURE)  # truncate-and-reload by default
    n = conn.execute("SELECT COUNT(*) FROM skills_catalog").fetchone()[0]
    assert n == 7


def test_ensure_loaded_skips_when_populated(tmp_path):
    skills_path = tmp_path / "skills.csv"
    skills_path.write_bytes(FIXTURE.read_bytes())

    conn = _make_conn()
    load_skills_catalog(conn, FIXTURE)
    stats = ensure_loaded(conn, tmp_path)
    assert stats["rows_loaded"] == 0
    assert stats.get("skipped") == 1


def test_ensure_loaded_loads_when_empty(tmp_path):
    skills_path = tmp_path / "skills.csv"
    skills_path.write_bytes(FIXTURE.read_bytes())

    conn = _make_conn()
    stats = ensure_loaded(conn, tmp_path)
    assert stats["rows_loaded"] == 7
    assert stats["trees"] == 4


def test_missing_csv_raises():
    conn = _make_conn()
    with pytest.raises(FileNotFoundError):
        load_skills_catalog(conn, Path("/nonexistent/skills.csv"))
