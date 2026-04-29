"""Loader for ``nodes_catalog``: mirrors nodes.csv into DuckDB.

Per Session 14 — see ``memory/decoder-notes.md`` "Session 14 —
skills_catalog + nodes_catalog + views". Reads the vendored
``kami_context/catalogs/nodes.csv`` and upserts every row into
the ``nodes_catalog`` table created by migration 011.

Trigger points (the loader is **not** in the per-poll path):

- Service startup, if ``nodes_catalog`` is empty (handled by
  ``ensure_loaded`` from ``ingester.serve``).
- Explicit reload after re-vendoring ``kami_context``:
  ``python -m ingester.nodes_catalog --reload``.

room_index resolution: per Session 14 Part 1b discovery, every
node in the upstream nodes.csv has a same-Index, same-Name row
in rooms.csv (zero mismatches across all 64 in-game nodes). So
``room_index = node_index`` for every in-game node — no chain
call, no separate lookup, just identity. If upstream ever
introduces a node whose room differs, the loader can be extended
to consult rooms.csv as a verification step; today that would be
unnecessary work.

Failure mode: missing or unreadable CSV is loud + non-zero exit.
A silently stale catalog would let the kami_current_location
view serve unresolved node_id values.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)


def _opt_int(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_nodes_csv(csv_path: Path) -> list[dict]:
    """Parse nodes.csv into per-row dicts ready for upsert.

    Yields one dict per data row with keys matching ``nodes_catalog``
    columns. Rows without a parseable integer ``Index`` or empty
    ``Name`` are skipped (defensive — a real catalog shouldn't
    contain them).

    ``room_index`` is set to ``node_index`` per the Session 14
    discovery (Index identity between nodes.csv and rooms.csv for
    every in-game node).
    """
    rows: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row["Index"])
            except (KeyError, ValueError, TypeError):
                log.debug("nodes_catalog: skipping row with bad Index: %r", row)
                continue
            name = (row.get("Name") or "").strip()
            if not name:
                log.debug("nodes_catalog: skipping row %d with empty Name", idx)
                continue
            rows.append({
                "node_index": idx,
                "name": name,
                "status": (row.get("Status") or "").strip() or None,
                "drops": (row.get("Drops") or "").strip() or None,
                "affinity": (row.get("Affinity") or "").strip() or None,
                "level_limit": _opt_int(row.get("Level Limit")),
                "yield_index": _opt_int(row.get("YieldIndex")),
                "scav_cost": _opt_int(row.get("Scav Cost")),
                # Session 14: room_index = node_index for every in-game
                # node (verified zero-mismatch against rooms.csv). If
                # upstream ever breaks this, add a verifier here.
                "room_index": idx,
            })
    return rows


def load_nodes_catalog(
    conn: duckdb.DuckDBPyConnection,
    csv_path: Path,
    *,
    truncate: bool = True,
) -> dict[str, int]:
    """Load nodes.csv into ``nodes_catalog``.

    Truncates by default so a re-load reflects deletions in the CSV
    — the catalog is small (~70 rows) and the loader runs at most
    on every re-vendor, so the cost is trivial.
    """
    if not csv_path.exists():
        raise FileNotFoundError(
            f"nodes_catalog: CSV not found at {csv_path} — "
            "re-vendor kami_context (scripts/vendor-context.sh)?"
        )
    rows = parse_nodes_csv(csv_path)
    if not rows:
        raise ValueError(
            f"nodes_catalog: CSV at {csv_path} produced 0 rows — refusing to "
            "load (the existing table contents are preserved)."
        )

    now = dt.datetime.utcnow()
    if truncate:
        conn.execute("DELETE FROM nodes_catalog")

    conn.executemany(
        """
        INSERT INTO nodes_catalog
            (node_index, name, status, drops, affinity, level_limit,
             yield_index, scav_cost, room_index, loaded_ts)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (node_index) DO UPDATE SET
            name = excluded.name,
            status = excluded.status,
            drops = excluded.drops,
            affinity = excluded.affinity,
            level_limit = excluded.level_limit,
            yield_index = excluded.yield_index,
            scav_cost = excluded.scav_cost,
            room_index = excluded.room_index,
            loaded_ts = excluded.loaded_ts
        """,
        [
            (
                r["node_index"], r["name"], r["status"], r["drops"],
                r["affinity"], r["level_limit"], r["yield_index"],
                r["scav_cost"], r["room_index"], now,
            )
            for r in rows
        ],
    )

    in_game = sum(1 for r in rows if r["status"] == "In Game")
    with_room = sum(1 for r in rows if r["room_index"] is not None)
    log.info(
        "nodes_catalog: loaded %d rows (%d in game, %d with room) from %s",
        len(rows), in_game, with_room, csv_path,
    )
    return {"rows_loaded": len(rows), "rows_in_game": in_game, "rows_with_room": with_room}


def ensure_loaded(
    conn: duckdb.DuckDBPyConnection,
    catalogs_dir: Path,
) -> dict[str, int]:
    """Populate ``nodes_catalog`` if the table is empty.

    Called from ``ingester.serve`` startup. A no-op when rows are
    already present — re-loading is the founder's explicit
    ``--reload`` action, gated on a re-vendor.
    """
    n = conn.execute("SELECT COUNT(*) FROM nodes_catalog").fetchone()[0]
    if n > 0:
        log.info(
            "nodes_catalog: %d row(s) already present; skipping startup load", n,
        )
        return {"rows_loaded": 0, "rows_in_game": 0, "rows_with_room": 0, "skipped": 1}
    return load_nodes_catalog(conn, catalogs_dir / "nodes.csv")


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Load nodes.csv into nodes_catalog (Session 14)."
    )
    ap.add_argument("--db", default="db/kami-oracle.duckdb")
    ap.add_argument(
        "--catalogs-dir",
        default="kami_context/catalogs",
        help="Directory containing nodes.csv (default: kami_context/catalogs).",
    )
    ap.add_argument(
        "--reload",
        action="store_true",
        help="Force a truncate-and-reload even if the table is non-empty.",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("db file not found: %s", db_path)
        return 1
    csv_path = Path(args.catalogs_dir) / "nodes.csv"
    if not csv_path.exists():
        log.error("nodes.csv not found at %s", csv_path)
        return 1

    conn = duckdb.connect(str(db_path))
    try:
        if args.reload:
            stats = load_nodes_catalog(conn, csv_path, truncate=True)
        else:
            stats = ensure_loaded(conn, Path(args.catalogs_dir))
    finally:
        conn.close()
    log.info("nodes_catalog loader done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
