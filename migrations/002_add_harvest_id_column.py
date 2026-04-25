"""Migration 002: add harvest_id column to kami_action and backfill it.

Idempotent — safe to run on a fresh DB (where the column may already exist
from schema.sql) and on an existing v1 DB (where the column needs to be
added). Backfill populates harvest_id for every harvest_* row:

* harvest_start: harvest_id = keccak256(b"harvest" || uint256(kami_id)).
* harvest_stop / harvest_collect: harvest_id = json_extract(metadata_json,
  '$.harvest_id') (the decoder has been writing it into metadata since
  Session 1).

After this migration runs, the kami_id stitch (also in this script) sets
harvest_stop / harvest_collect rows' kami_id from the harvest_id ↔ kami_id
map computed offline. See ``ingester.harvest_resolver`` for the live-ingest
counterpart that handles new rows.

Bumps schema_version 1 → 2.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
from eth_utils import keccak

log = logging.getLogger(__name__)

TARGET_SCHEMA_VERSION = 2


def _harvest_id_uint(kami_id_int: int) -> int:
    return int.from_bytes(keccak(b"harvest" + kami_id_int.to_bytes(32, "big")), "big")


def _table_has_column(conn: duckdb.DuckDBPyConnection, table: str, col: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        [table, col],
    ).fetchall()
    return bool(rows)


def _add_column_if_missing(conn: duckdb.DuckDBPyConnection) -> bool:
    if _table_has_column(conn, "kami_action", "harvest_id"):
        return False
    conn.execute("ALTER TABLE kami_action ADD COLUMN harvest_id VARCHAR")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kami_action_harvest_id ON kami_action(harvest_id)"
    )
    return True


def _backfill_stop_collect_from_metadata(conn: duckdb.DuckDBPyConnection) -> int:
    """Pull harvest_id out of metadata_json for stop/collect rows."""
    res = conn.execute(
        """
        UPDATE kami_action
        SET harvest_id = json_extract_string(metadata_json, '$.harvest_id')
        WHERE action_type IN ('harvest_stop', 'harvest_collect')
          AND harvest_id IS NULL
          AND metadata_json IS NOT NULL
          AND json_extract_string(metadata_json, '$.harvest_id') IS NOT NULL
        """
    )
    return res.fetchone()[0] if res.description else _changed_rows(conn)


def _changed_rows(conn: duckdb.DuckDBPyConnection) -> int:
    # DuckDB UPDATE does not return rowcount via fetchone; SELECT changes() is
    # SQLite-specific. We don't rely on the count for correctness — the
    # logging is informational. Return -1 to indicate "unknown" and let the
    # caller log accordingly.
    return -1


def _backfill_start_from_kami_id(conn: duckdb.DuckDBPyConnection) -> int:
    """Compute harvest_id for harvest_start rows from their kami_id.

    Implementation note: a per-row Python loop with executemany ran ~10
    UPDATEs/s at session-6 cardinality (113k rows ≈ 3+ hours), unworkable
    for a service restart. Instead we materialise the (kami_id → harvest_id)
    map once via a DISTINCT scan, build a Python dict in memory, then
    register it as a DuckDB temp table and do a single bulk UPDATE with a
    JOIN. End-to-end < 5s on the same dataset.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT kami_id
        FROM kami_action
        WHERE action_type = 'harvest_start'
          AND harvest_id IS NULL
          AND kami_id IS NOT NULL
        """
    ).fetchall()
    if not rows:
        return 0

    pairs: list[tuple[str, str]] = []
    for (kami_id_str,) in rows:
        try:
            kid_int = int(kami_id_str)
        except (ValueError, TypeError):
            continue
        pairs.append((kami_id_str, str(_harvest_id_uint(kid_int))))

    if not pairs:
        return 0

    # Stage the (kami_id, harvest_id) map in a temp table; one bulk UPDATE
    # joins it back. DuckDB's executemany on an INSERT into a tiny temp
    # table is fast — the slowness was in UPDATE-per-row, not insert.
    conn.execute(
        "CREATE TEMP TABLE _hid_map(kami_id VARCHAR PRIMARY KEY, harvest_id VARCHAR)"
    )
    try:
        conn.executemany(
            "INSERT INTO _hid_map(kami_id, harvest_id) VALUES (?, ?)",
            pairs,
        )
        conn.execute(
            """
            UPDATE kami_action AS a
            SET harvest_id = m.harvest_id
            FROM _hid_map m
            WHERE a.action_type = 'harvest_start'
              AND a.harvest_id IS NULL
              AND a.kami_id = m.kami_id
            """
        )
    finally:
        conn.execute("DROP TABLE IF EXISTS _hid_map")
    return len(pairs)


def _stitch_kami_id_via_harvest_id(conn: duckdb.DuckDBPyConnection) -> int:
    """Set kami_id on stop/collect rows by joining on harvest_id.

    First pass: use harvest_start rows (already backfilled with computed
    harvest_id) as the (harvest_id → kami_id) source. Second pass: rebuild
    the map from EVERY distinct kami_id ever observed (feed, lvlup, etc.)
    so we can resolve stops whose starts pre-date the 7-day rolling
    window. Both passes are idempotent — they only touch rows whose
    kami_id is currently NULL.
    """
    before = conn.execute(
        """
        SELECT COUNT(*) FROM kami_action
        WHERE kami_id IS NULL
          AND action_type IN ('harvest_stop', 'harvest_collect')
          AND harvest_id IS NOT NULL
        """
    ).fetchone()[0]

    # First pass: harvest_start rows in-window.
    conn.execute(
        """
        UPDATE kami_action AS a
        SET kami_id = s.kami_id
        FROM kami_action AS s
        WHERE a.kami_id IS NULL
          AND a.action_type IN ('harvest_stop', 'harvest_collect')
          AND a.harvest_id IS NOT NULL
          AND s.action_type = 'harvest_start'
          AND s.harvest_id = a.harvest_id
          AND s.kami_id IS NOT NULL
        """
    )

    # Second pass: any kami_id we've ever seen (feed / lvlup / liquidate
    # / skill_upgrade etc. all carry kami_id directly). Compute harvest_id
    # for each and join.
    rows = conn.execute(
        """
        SELECT DISTINCT kami_id
        FROM kami_action
        WHERE kami_id IS NOT NULL
        """
    ).fetchall()
    pairs: list[tuple[str, str]] = []
    for (kami_id_str,) in rows:
        try:
            kid_int = int(kami_id_str)
        except (ValueError, TypeError):
            continue
        pairs.append((str(_harvest_id_uint(kid_int)), kami_id_str))
    if pairs:
        conn.execute(
            "CREATE TEMP TABLE _hid_universe(harvest_id VARCHAR PRIMARY KEY, kami_id VARCHAR)"
        )
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO _hid_universe(harvest_id, kami_id) VALUES (?, ?)",
                pairs,
            )
            conn.execute(
                """
                UPDATE kami_action AS a
                SET kami_id = u.kami_id
                FROM _hid_universe u
                WHERE a.kami_id IS NULL
                  AND a.action_type IN ('harvest_stop', 'harvest_collect')
                  AND a.harvest_id IS NOT NULL
                  AND a.harvest_id = u.harvest_id
                """
            )
        finally:
            conn.execute("DROP TABLE IF EXISTS _hid_universe")

    after = conn.execute(
        """
        SELECT COUNT(*) FROM kami_action
        WHERE kami_id IS NULL
          AND action_type IN ('harvest_stop', 'harvest_collect')
          AND harvest_id IS NOT NULL
        """
    ).fetchone()[0]
    return int(before - after)


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
    """Apply migration 002 in-place. Returns counters for logging."""
    added = _add_column_if_missing(conn)
    _backfill_stop_collect_from_metadata(conn)
    n_starts = _backfill_start_from_kami_id(conn)
    n_stitched = _stitch_kami_id_via_harvest_id(conn)
    _bump_schema_version(conn)
    return {
        "column_added": int(added),
        "starts_backfilled": int(n_starts),
        "stop_collect_stitched": int(n_stitched),
    }


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
    log.info("migration 002 done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
