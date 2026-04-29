"""Loader for ``skills_catalog``: mirrors skills.csv into DuckDB.

Per Session 14 — see ``memory/decoder-notes.md`` "Session 14 —
skills_catalog + nodes_catalog + views". Reads the vendored
``kami_context/catalogs/skills.csv`` and upserts every row into the
``skills_catalog`` table created by migration 009.

Trigger points (the loader is **not** in the per-poll path):

- Service startup, if ``skills_catalog`` is empty (handled by
  ``ensure_loaded`` from ``ingester.serve``).
- Explicit reload after re-vendoring ``kami_context``:
  ``python -m ingester.skills_catalog --reload``.

Same shape as Session 13's ``items_catalog`` loader — chosen
deliberately so future catalog mirrors look the same.

CSV note: skills.csv has a leading blank column (header
``"﻿."`` — UTF-8 BOM + dot). We open with ``utf-8-sig`` to
strip the BOM and rely on ``csv.DictReader`` to key by named
column (``Index``, ``Name``, ``Tree``, ...) — the unnamed leading
column is ignored.

``value`` is stored verbatim from the CSV cell (VARCHAR). Skill
catalog values mix integer counts, signed decimal percents
(``0.02``), and other shapes; coercing to numeric here would lose
information. Consumers cast as needed.

Failure mode: missing or unreadable CSV is loud + non-zero exit.
A silently stale catalog would let the ``kami_skills`` view serve
unresolved skill indices.
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


def parse_skills_csv(csv_path: Path) -> list[dict]:
    """Parse skills.csv into per-row dicts ready for upsert.

    Yields one dict per data row with keys matching ``skills_catalog``
    columns. Rows without a parseable integer ``Index`` or empty
    ``Name`` / ``Tree`` are skipped (defensive — a real catalog
    shouldn't contain them).
    """
    rows: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row["Index"])
            except (KeyError, ValueError, TypeError):
                log.debug("skills_catalog: skipping row with bad Index: %r", row)
                continue
            name = (row.get("Name") or "").strip()
            tree = (row.get("Tree") or "").strip()
            if not name or not tree:
                log.debug(
                    "skills_catalog: skipping row %d with empty Name/Tree", idx,
                )
                continue
            rows.append({
                "skill_index": idx,
                "name": name,
                "tree": tree,
                "tier": _opt_int(row.get("Tier")),
                "tree_req": _opt_int(row.get("Tree req")),
                "max_rank": _opt_int(row.get("Max")),
                "cost": _opt_int(row.get("Cost")),
                "effect": (row.get("Effect") or "").strip() or None,
                "value": (row.get("Value") or "").strip() or None,
                "units": (row.get("Units") or "").strip() or None,
                "exclusion": (row.get("Exclusion") or "").strip() or None,
                "description": (row.get("Description") or "").strip() or None,
            })
    return rows


def load_skills_catalog(
    conn: duckdb.DuckDBPyConnection,
    csv_path: Path,
    *,
    truncate: bool = True,
) -> dict[str, int]:
    """Load skills.csv into ``skills_catalog``.

    Truncates by default so a re-load reflects deletions in the CSV
    — the catalog is small (~80 rows) and the loader runs at most
    on every re-vendor, so the cost is trivial; the alternative
    (UPSERT-only) would leave stale rows around.
    """
    if not csv_path.exists():
        raise FileNotFoundError(
            f"skills_catalog: CSV not found at {csv_path} — "
            "re-vendor kami_context (scripts/vendor-context.sh)?"
        )
    rows = parse_skills_csv(csv_path)
    if not rows:
        raise ValueError(
            f"skills_catalog: CSV at {csv_path} produced 0 rows — refusing to "
            "load (the existing table contents are preserved)."
        )

    now = dt.datetime.utcnow()
    if truncate:
        conn.execute("DELETE FROM skills_catalog")

    conn.executemany(
        """
        INSERT INTO skills_catalog
            (skill_index, name, tree, tier, tree_req, max_rank, cost,
             effect, value, units, exclusion, description, loaded_ts)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (skill_index) DO UPDATE SET
            name = excluded.name,
            tree = excluded.tree,
            tier = excluded.tier,
            tree_req = excluded.tree_req,
            max_rank = excluded.max_rank,
            cost = excluded.cost,
            effect = excluded.effect,
            value = excluded.value,
            units = excluded.units,
            exclusion = excluded.exclusion,
            description = excluded.description,
            loaded_ts = excluded.loaded_ts
        """,
        [
            (
                r["skill_index"], r["name"], r["tree"], r["tier"],
                r["tree_req"], r["max_rank"], r["cost"],
                r["effect"], r["value"], r["units"],
                r["exclusion"], r["description"], now,
            )
            for r in rows
        ],
    )

    trees = {r["tree"] for r in rows}
    log.info(
        "skills_catalog: loaded %d rows across %d trees from %s",
        len(rows), len(trees), csv_path,
    )
    return {"rows_loaded": len(rows), "trees": len(trees)}


def ensure_loaded(
    conn: duckdb.DuckDBPyConnection,
    catalogs_dir: Path,
) -> dict[str, int]:
    """Populate ``skills_catalog`` if the table is empty.

    Called from ``ingester.serve`` startup. A no-op when rows are
    already present — re-loading is the founder's explicit
    ``--reload`` action, gated on a re-vendor.
    """
    n = conn.execute("SELECT COUNT(*) FROM skills_catalog").fetchone()[0]
    if n > 0:
        log.info(
            "skills_catalog: %d row(s) already present; skipping startup load", n,
        )
        return {"rows_loaded": 0, "trees": 0, "skipped": 1}
    return load_skills_catalog(conn, catalogs_dir / "skills.csv")


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Load skills.csv into skills_catalog (Session 14)."
    )
    ap.add_argument("--db", default="db/kami-oracle.duckdb")
    ap.add_argument(
        "--catalogs-dir",
        default="kami_context/catalogs",
        help="Directory containing skills.csv (default: kami_context/catalogs).",
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
    csv_path = Path(args.catalogs_dir) / "skills.csv"
    if not csv_path.exists():
        log.error("skills.csv not found at %s", csv_path)
        return 1

    conn = duckdb.connect(str(db_path))
    try:
        if args.reload:
            stats = load_skills_catalog(conn, csv_path, truncate=True)
        else:
            stats = ensure_loaded(conn, Path(args.catalogs_dir))
    finally:
        conn.close()
    log.info("skills_catalog loader done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
