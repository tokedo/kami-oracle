"""Loader for ``items_catalog``: mirrors items.csv into DuckDB.

Per Session 13 — see ``memory/decoder-notes.md`` "Session 13 —
items_catalog + kami_equipment view". Reads the vendored
``kami_context/catalogs/items.csv`` and upserts every row into the
``items_catalog`` table created by migration 007.

Trigger points (the loader is **not** in the per-poll path):

- Service startup, if ``items_catalog`` is empty (handled by
  ``ensure_loaded`` from ``ingester.serve``).
- Explicit reload after re-vendoring ``kami_context``:
  ``python -m ingester.items_catalog --reload``.

slot_type convention: the CSV's ``For`` cell is treated as a slot
identity only when it matches ``*_[Ss]lot`` (today: ``Kami_Pet_Slot``,
``Passport_slot``; the rule is generic so any future ``Kami_Body_Slot``
or similar flows through without code change). All other ``For``
values (``Account``, ``Kami``, ``Enemy_Kami``, empty) map to
``slot_type = NULL`` — these are non-equippable scopes (consumables,
currency, target-scoped items).

Failure mode: missing or unreadable CSV is loud + non-zero exit. A
silently stale catalog would let the ``kami_equipment`` view serve
unresolved item indices.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from pathlib import Path
from typing import Iterable

import duckdb

log = logging.getLogger(__name__)

# Match e.g. "Kami_Pet_Slot", "Passport_slot", "Kami_Body_Slot".
_SLOT_RE = re.compile(r"_[Ss]lot$")


def _resolve_slot_type(for_value: str | None) -> str | None:
    """Return ``for_value`` when it names a slot, else None.

    Only ``*_[Ss]lot`` values are slot identities. Account / Kami /
    Enemy_Kami / empty all collapse to None.
    """
    if not for_value:
        return None
    v = for_value.strip()
    if not v:
        return None
    return v if _SLOT_RE.search(v) else None


def parse_items_csv(csv_path: Path) -> list[dict]:
    """Parse items.csv into per-row dicts ready for upsert.

    Yields one dict per data row with keys matching ``items_catalog``
    columns. Rows without a parseable integer ``Index`` are skipped
    (defensive — a real catalog won't contain them).
    """
    rows: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row["Index"])
            except (KeyError, ValueError):
                log.debug("items_catalog: skipping row with bad Index: %r", row)
                continue
            name = (row.get("Name") or "").strip()
            if not name:
                # name is NOT NULL in the table; defend the constraint.
                log.debug("items_catalog: skipping row %d with empty Name", idx)
                continue
            rows.append({
                "item_index": idx,
                "name": name,
                "type": (row.get("Type") or "").strip() or None,
                "rarity": (row.get("Rarity") or "").strip() or None,
                "slot_type": _resolve_slot_type(row.get("For")),
                "effect": (row.get("Effects") or "").strip() or None,
                "description": (row.get("Description") or "").strip() or None,
            })
    return rows


def load_items_catalog(
    conn: duckdb.DuckDBPyConnection,
    csv_path: Path,
    *,
    truncate: bool = True,
) -> dict[str, int]:
    """Load items.csv into ``items_catalog``.

    By default truncates the table first so a re-load reflects
    deletions in the CSV — items.csv is small (~200 rows) and the
    loader runs at most on every re-vendor, so the cost is trivial
    and the alternative (UPSERT-only) leaves stale rows around.
    """
    if not csv_path.exists():
        raise FileNotFoundError(
            f"items_catalog: CSV not found at {csv_path} — "
            "re-vendor kami_context (scripts/vendor-context.sh)?"
        )
    rows = parse_items_csv(csv_path)
    if not rows:
        raise ValueError(
            f"items_catalog: CSV at {csv_path} produced 0 rows — refusing to "
            "load (the existing table contents are preserved)."
        )

    now = dt.datetime.utcnow()
    if truncate:
        conn.execute("DELETE FROM items_catalog")

    conn.executemany(
        """
        INSERT INTO items_catalog
            (item_index, name, type, rarity, slot_type, effect, description, loaded_ts)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (item_index) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            rarity = excluded.rarity,
            slot_type = excluded.slot_type,
            effect = excluded.effect,
            description = excluded.description,
            loaded_ts = excluded.loaded_ts
        """,
        [
            (
                r["item_index"], r["name"], r["type"], r["rarity"],
                r["slot_type"], r["effect"], r["description"], now,
            )
            for r in rows
        ],
    )

    slotted = sum(1 for r in rows if r["slot_type"] is not None)
    log.info(
        "items_catalog: loaded %d rows (%d slot-bearing) from %s",
        len(rows), slotted, csv_path,
    )
    return {"rows_loaded": len(rows), "rows_slotted": slotted}


def ensure_loaded(
    conn: duckdb.DuckDBPyConnection,
    catalogs_dir: Path,
) -> dict[str, int]:
    """Populate ``items_catalog`` if the table is empty.

    Called from ``ingester.serve`` startup. A no-op when rows are
    already present — re-loading is the founder's explicit
    ``--reload`` action, gated on a re-vendor.
    """
    n = conn.execute("SELECT COUNT(*) FROM items_catalog").fetchone()[0]
    if n > 0:
        log.info("items_catalog: %d row(s) already present; skipping startup load", n)
        return {"rows_loaded": 0, "rows_slotted": 0, "skipped": 1}
    return load_items_catalog(conn, catalogs_dir / "items.csv")


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Load items.csv into items_catalog (Session 13)."
    )
    ap.add_argument("--db", default="db/kami-oracle.duckdb")
    ap.add_argument(
        "--catalogs-dir",
        default="kami_context/catalogs",
        help="Directory containing items.csv (default: kami_context/catalogs).",
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
    csv_path = Path(args.catalogs_dir) / "items.csv"
    if not csv_path.exists():
        log.error("items.csv not found at %s", csv_path)
        return 1

    conn = duckdb.connect(str(db_path))
    try:
        if args.reload:
            stats = load_items_catalog(conn, csv_path, truncate=True)
        else:
            stats = ensure_loaded(conn, Path(args.catalogs_dir))
    finally:
        conn.close()
    log.info("items_catalog loader done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
