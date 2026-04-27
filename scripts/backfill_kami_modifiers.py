#!/usr/bin/env python
"""Backfill the 12 Session 11 modifier columns on kami_static.

Pure-Python aggregation over the already-stored `skills_json` and
`equipment_json` columns × the upstream skill catalog. Zero chain
calls — fast (~seconds for ~7,020 rows). Failure handling matches the
populator: if the catalog walk throws on a row, that row gets NULL
modifier columns + still ticks build_refreshed_ts.

Stop the kami-oracle service before running (DuckDB exclusive lock):

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_kami_modifiers.py
    sudo systemctl start kami-oracle

Reports:
    pre_pending / post_pending — rows where any modifier column is NULL
    n_ok / n_fail              — kamis updated successfully / catalog throws
    anomaly_negative_unexpected — modifiers that went negative when they
                                  shouldn't have (HFB/HBB/HIB/RMB/ATR/ASR/
                                  DTR/DSR — only SB/CS/ATS/DTS may be
                                  legitimately negative).
    anomaly_uniform_zero       — columns where 100% of populated rows are
                                  exactly zero. Possible (e.g. ASR for a
                                  population without Predator-tree
                                  investment) but flagged for human review.
    strain_boost min/max/count_negative
    Top-20 by strain_boost (most negative).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.config import load_config  # noqa: E402
from ingester.skill_catalog import (  # noqa: E402
    ALL_MODIFIER_COLUMNS,
    load_skill_catalog,
)
from ingester.storage import Storage, read_schema_sql  # noqa: E402

log = logging.getLogger("backfill_kami_modifiers")


# Modifiers that may legitimately go negative.
NEGATIVE_OK = frozenset({
    "strain_boost",                # SB — Enlightened/Harvester investments
    "cooldown_shift",              # CS — Predator-tier cooldown reductions
    "attack_threshold_shift",      # ATS may technically debuff (no skills today)
    "defense_threshold_shift",     # DTS likewise
})


def _count_pending(storage: Storage) -> int:
    pred = " OR ".join(f"{c} IS NULL" for c in ALL_MODIFIER_COLUMNS)
    row = storage.fetchone(f"SELECT COUNT(*) FROM kami_static WHERE {pred}")
    return int(row[0]) if row else 0


def _all_rows(storage: Storage) -> list[tuple[str, str | None, str | None]]:
    return [
        (str(r[0]), r[1], r[2])
        for r in storage.fetchall(
            "SELECT kami_id, skills_json, equipment_json FROM kami_static "
            "ORDER BY kami_index NULLS LAST"
        )
    ]


def _apply_batch(
    storage: Storage,
    batch: list[tuple[str, dict[str, int] | None]],
) -> None:
    if not batch:
        return
    payload = []
    for kami_id, mods in batch:
        if mods is None:
            payload.append((None,) * len(ALL_MODIFIER_COLUMNS) + (kami_id,))
        else:
            payload.append(
                tuple(mods[c] for c in ALL_MODIFIER_COLUMNS) + (kami_id,)
            )
    set_clause = ", ".join(f"{c} = ?" for c in ALL_MODIFIER_COLUMNS)
    sql = (
        f"UPDATE kami_static SET {set_clause}, "
        "build_refreshed_ts = COALESCE(build_refreshed_ts, now()), "
        "last_refreshed_ts = now() "
        "WHERE kami_id = ?"
    )
    with storage.lock:
        storage.conn.executemany(sql, payload)


def _report_anomalies(storage: Storage) -> None:
    # Negative-unexpected: any row where a non-NEGATIVE_OK column went < 0.
    log.info("=== Anomaly report ===")
    for col in ALL_MODIFIER_COLUMNS:
        if col in NEGATIVE_OK:
            continue
        row = storage.fetchone(
            f"SELECT COUNT(*) FROM kami_static WHERE {col} < 0"
        )
        n = int(row[0]) if row else 0
        if n > 0:
            log.warning("  anomaly_negative_unexpected: %s < 0 in %d rows", col, n)

    # Uniform-zero: column populated, but every populated row = 0.
    uniform_zero_cols: list[tuple[str, int]] = []
    for col in ALL_MODIFIER_COLUMNS:
        row = storage.fetchone(
            f"SELECT COUNT(*) FILTER (WHERE {col} IS NOT NULL) AS n_pop, "
            f"       COUNT(*) FILTER (WHERE {col} != 0) AS n_nonzero "
            "FROM kami_static"
        )
        n_pop = int(row[0]) if row else 0
        n_nonzero = int(row[1]) if row else 0
        if n_pop > 0 and n_nonzero == 0:
            uniform_zero_cols.append((col, n_pop))
    if uniform_zero_cols:
        log.warning(
            "  anomaly_uniform_zero (column where every populated row = 0): %s",
            ", ".join(f"{c} (n={n})" for c, n in uniform_zero_cols),
        )

    # strain_boost distribution.
    row = storage.fetchone(
        "SELECT COUNT(*) FILTER (WHERE strain_boost IS NOT NULL),"
        "       COUNT(*) FILTER (WHERE strain_boost < 0),"
        "       MIN(strain_boost), MAX(strain_boost) "
        "FROM kami_static"
    )
    if row:
        log.info(
            "  strain_boost: populated=%d, negative=%d, min=%s, max=%s",
            int(row[0] or 0), int(row[1] or 0), row[2], row[3],
        )

    log.info("Top-20 by strain_boost (most-negative SB / sustain harvesters):")
    for r in storage.fetchall(
        "SELECT kami_index, name, account_name, level, total_harmony, strain_boost "
        "FROM kami_static "
        "WHERE strain_boost IS NOT NULL "
        "ORDER BY strain_boost ASC "
        "LIMIT 20"
    ):
        log.info(
            "  idx=%-5s name=%-20s acct=%-15s lvl=%-3s harm=%-3s SB=%s",
            r[0], (r[1] or "")[:20], (r[2] or "")[:15], r[3], r[4], r[5],
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--commit-every", type=int, default=200,
        help="Flush UPDATE batch every N kamis (default 200)",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Cap kamis processed (default 0 = no cap)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    pre = _count_pending(storage)
    log.info(
        "backfill_kami_modifiers: %d kami_static rows have at least one NULL modifier column",
        pre,
    )
    if pre == 0:
        log.info("nothing to do")
        storage.close()
        return 0

    catalogs_dir = Path(cfg.abi_dir).parent / "catalogs"
    cat = load_skill_catalog(catalogs_dir)

    rows = _all_rows(storage)
    if args.limit:
        rows = rows[: args.limit]
    log.info("backfill_kami_modifiers: walking %d rows", len(rows))

    n_ok = n_fail = 0
    batch: list[tuple[str, dict[str, int] | None]] = []
    t0 = time.monotonic()
    for kami_id, skills_json, equipment_json in rows:
        try:
            mods = cat.compute_modifiers(skills_json, equipment_json)
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            log.warning("compute_modifiers(%s) failed: %s", kami_id, e)
            batch.append((kami_id, None))
            continue
        n_ok += 1
        batch.append((kami_id, mods))
        if len(batch) >= args.commit_every:
            _apply_batch(storage, batch)
            batch.clear()
            elapsed = time.monotonic() - t0
            log.info(
                "progress: ok=%d fail=%d / %d in %.1fs",
                n_ok, n_fail, len(rows), elapsed,
            )
    if batch:
        _apply_batch(storage, batch)

    post = _count_pending(storage)
    log.info(
        "backfill_kami_modifiers done: pre_pending=%d post_pending=%d ok=%d fail=%d",
        pre, post, n_ok, n_fail,
    )

    _report_anomalies(storage)
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
