"""Tests for the kami_equipment view (Session 13).

Validates the migration's view definition end-to-end against an
in-memory DuckDB seeded with a small kami_static + items_catalog
fixture:

1. A kami with ``equipment_json="[30011]"`` yields one row with
   slot_type="Kami_Pet_Slot" (joined from items_catalog).
2. A kami with ``equipment_json="[]"`` is dropped (WHERE filter).
3. ``equipment_json IS NULL`` rows are dropped.
4. ``freshness_seconds`` and ``is_stale`` derive from
   ``build_refreshed_ts``: a row whose ``build_refreshed_ts`` is
   "now" returns ``is_stale=FALSE``; a row 40h old returns
   ``is_stale=TRUE``. Threshold is 36h (129600s); we test on either
   side of it.
5. A kami with multiple equipped items produces multiple rows
   (UNNEST fan-out).
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
        "INSERT INTO ingest_cursor (id, last_block_scanned, schema_version) VALUES (1, 0, 7)"
    )
    # Mirror the kami_static columns the view references.
    conn.execute(
        """
        CREATE TABLE kami_static (
            kami_id            VARCHAR PRIMARY KEY,
            kami_index         INTEGER,
            name               VARCHAR,
            account_name       VARCHAR,
            equipment_json     VARCHAR,
            build_refreshed_ts TIMESTAMP
        )
        """
    )
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


def _seed(conn: duckdb.DuckDBPyConnection):
    now = dt.datetime.utcnow()
    fresh = now
    stale = now - dt.timedelta(hours=40)  # > 36h threshold

    conn.executemany(
        """
        INSERT INTO kami_static
            (kami_id, kami_index, name, account_name, equipment_json, build_refreshed_ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("k_pet_fresh",  1186, "fresh",        "fey", "[30011]",         fresh),
            ("k_pet_stale",  1745, "stale",        "fey", "[30031]",         stale),
            ("k_two_items",  2418, "twin",         "caw", "[30011, 30031]",  fresh),
            ("k_empty",      2465, "empty",        "caw", "[]",              fresh),
            ("k_null_equip", 9999, "null",         "caw", None,              fresh),
        ],
    )
    now_ts = dt.datetime.utcnow()
    conn.executemany(
        """
        INSERT INTO items_catalog
            (item_index, name, type, rarity, slot_type, effect, description, loaded_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (30011, "Wise Leafling",     "Equipment", "Uncommon", "Kami_Pet_Slot", "E_BOUNTY+16%", "", now_ts),
            (30031, "Antique Automata",  "Equipment", "Common",   "Kami_Pet_Slot", "E_HIB+15P",    "", now_ts),
        ],
    )


def _create_view(conn: duckdb.DuckDBPyConnection):
    m008 = _load_migration_module("migrations/008_add_kami_equipment_view.py", "migration_008_test")
    m008.run(conn)


def test_view_resolves_single_pet_with_freshness():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, slot_type, item_index, item_name, item_effect, is_stale
        FROM kami_equipment
        WHERE kami_id = 'k_pet_fresh'
        """
    ).fetchall()
    assert rows == [(1186, "Kami_Pet_Slot", 30011, "Wise Leafling", "E_BOUNTY+16%", False)]


def test_view_flags_stale_kami():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_index, slot_type, is_stale, freshness_seconds > 129600 AS over_threshold
        FROM kami_equipment
        WHERE kami_id = 'k_pet_stale'
        """
    ).fetchall()
    assert rows[0][0] == 1745
    assert rows[0][1] == "Kami_Pet_Slot"
    assert rows[0][2] is True
    assert rows[0][3] is True


def test_view_unnests_multi_item_kami():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT item_index, item_name FROM kami_equipment
        WHERE kami_id = 'k_two_items'
        ORDER BY item_index
        """
    ).fetchall()
    assert rows == [(30011, "Wise Leafling"), (30031, "Antique Automata")]


def test_view_excludes_empty_and_null_equipment():
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    rows = conn.execute(
        """
        SELECT kami_id FROM kami_equipment
        WHERE kami_id IN ('k_empty', 'k_null_equip')
        """
    ).fetchall()
    assert rows == []


def test_view_total_row_count():
    """k_pet_fresh (1) + k_pet_stale (1) + k_two_items (2) = 4 rows.
    k_empty and k_null_equip contribute 0."""
    conn = _setup_db()
    _seed(conn)
    _create_view(conn)
    n = conn.execute("SELECT COUNT(*) FROM kami_equipment").fetchone()[0]
    assert n == 4


def test_view_unresolved_items_get_null_slot():
    """An equipped item not in items_catalog yields slot_type=NULL via LEFT JOIN
    — preferable to silently dropping it. Surfaces drift between the snapshot
    and the catalog (the founder should re-vendor to fix)."""
    conn = _setup_db()
    _seed(conn)
    # Add a kami with an item not in items_catalog.
    conn.execute(
        """
        INSERT INTO kami_static
            (kami_id, kami_index, name, account_name, equipment_json, build_refreshed_ts)
        VALUES ('k_unknown', 9000, 'unknown', 'caw', '[99999]', now())
        """
    )
    _create_view(conn)
    rows = conn.execute(
        "SELECT slot_type, item_name, item_effect FROM kami_equipment WHERE kami_id='k_unknown'"
    ).fetchall()
    assert rows == [(None, None, None)]
