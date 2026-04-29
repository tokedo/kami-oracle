"""Tests for the items_catalog loader (Session 13).

Covers:

1. End-to-end load of a 8-row fixture: row count, slot_type values,
   NULL handling for blank ``For`` and non-slot scopes (Account /
   etc.), PK uniqueness on re-load.
2. The slot-type rule: ``For`` matching ``*_[Ss]lot`` is preserved
   (Kami_Pet_Slot, Passport_slot, Kami_Body_Slot — last one is a
   forward-compat case for new slot kinds appearing in upstream
   without code changes here). All other ``For`` values resolve to
   NULL.
3. ``ensure_loaded`` is a no-op on a non-empty table.
4. Missing CSV → loud FileNotFoundError, not silent stale data.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from ingester.items_catalog import (
    ensure_loaded,
    load_items_catalog,
    parse_items_csv,
    _resolve_slot_type,
)

FIXTURE = Path(__file__).parent / "fixtures" / "items_sample.csv"


def _make_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the items_catalog table created."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE items_catalog (
            item_index   INTEGER PRIMARY KEY,
            name         VARCHAR NOT NULL,
            type         VARCHAR,
            rarity       VARCHAR,
            slot_type    VARCHAR,
            effect       VARCHAR,
            description  VARCHAR,
            loaded_ts    TIMESTAMP NOT NULL
        )
        """
    )
    return conn


def test_resolve_slot_type():
    assert _resolve_slot_type("Kami_Pet_Slot") == "Kami_Pet_Slot"
    assert _resolve_slot_type("Passport_slot") == "Passport_slot"
    assert _resolve_slot_type("Kami_Body_Slot") == "Kami_Body_Slot"  # forward-compat
    assert _resolve_slot_type("Account") is None
    assert _resolve_slot_type("Kami") is None
    assert _resolve_slot_type("Enemy_Kami") is None
    assert _resolve_slot_type("") is None
    assert _resolve_slot_type(None) is None
    assert _resolve_slot_type("  Kami_Pet_Slot  ") == "Kami_Pet_Slot"


def test_parse_items_csv():
    rows = parse_items_csv(FIXTURE)
    assert len(rows) == 8
    by_idx = {r["item_index"]: r for r in rows}

    # Slot-bearing rows.
    assert by_idx[20]["slot_type"] == "Passport_slot"
    assert by_idx[30001]["slot_type"] == "Kami_Pet_Slot"
    assert by_idx[30011]["slot_type"] == "Kami_Pet_Slot"
    assert by_idx[30031]["slot_type"] == "Kami_Pet_Slot"
    assert by_idx[50001]["slot_type"] == "Kami_Body_Slot"

    # Non-slot rows.
    assert by_idx[1]["slot_type"] is None        # MUSU, no For
    assert by_idx[2]["slot_type"] is None        # Account-scoped consumable
    assert by_idx[99001]["slot_type"] is None    # blank For

    # Effect carries through verbatim for slotted equipment.
    assert by_idx[30011]["effect"] == "E_BOUNTY+16%"
    assert by_idx[30031]["effect"] == "E_HIB+15P"

    # Names trimmed and not nulled.
    assert by_idx[1]["name"] == "MUSU"
    assert by_idx[30011]["name"] == "Wise Leafling"


def test_load_items_catalog_into_db():
    conn = _make_conn()
    stats = load_items_catalog(conn, FIXTURE)
    assert stats == {"rows_loaded": 8, "rows_slotted": 5}

    n = conn.execute("SELECT COUNT(*) FROM items_catalog").fetchone()[0]
    assert n == 8

    # PK + slot resolution end-to-end.
    row = conn.execute(
        "SELECT slot_type, name, effect FROM items_catalog WHERE item_index = 30031"
    ).fetchone()
    assert row == ("Kami_Pet_Slot", "Antique Automata", "E_HIB+15P")

    # Non-slot row's slot_type is genuinely NULL, not "".
    n_null = conn.execute(
        "SELECT COUNT(*) FROM items_catalog WHERE slot_type IS NULL"
    ).fetchone()[0]
    assert n_null == 3  # items 1, 2, 99001


def test_load_is_idempotent():
    conn = _make_conn()
    load_items_catalog(conn, FIXTURE)
    load_items_catalog(conn, FIXTURE)  # truncate-and-reload by default
    n = conn.execute("SELECT COUNT(*) FROM items_catalog").fetchone()[0]
    assert n == 8


def test_ensure_loaded_skips_when_populated(tmp_path):
    # ensure_loaded looks for <catalogs_dir>/items.csv, so we mirror
    # the fixture into a temp catalogs dir under that exact name.
    items_path = tmp_path / "items.csv"
    items_path.write_bytes(FIXTURE.read_bytes())

    conn = _make_conn()
    load_items_catalog(conn, FIXTURE)
    stats = ensure_loaded(conn, tmp_path)
    assert stats["rows_loaded"] == 0
    assert stats.get("skipped") == 1


def test_ensure_loaded_loads_when_empty(tmp_path):
    items_path = tmp_path / "items.csv"
    items_path.write_bytes(FIXTURE.read_bytes())

    conn = _make_conn()
    stats = ensure_loaded(conn, tmp_path)
    assert stats["rows_loaded"] == 8
    assert stats["rows_slotted"] == 5


def test_missing_csv_raises():
    conn = _make_conn()
    with pytest.raises(FileNotFoundError):
        load_items_catalog(conn, Path("/nonexistent/items.csv"))
