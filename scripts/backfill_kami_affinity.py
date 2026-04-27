#!/usr/bin/env python
"""Backfill body_affinity / hand_affinity on existing kami_static rows.

Per Session 12 — see ``memory/decoder-notes.md`` "Session 12 — affinities".
Migration 006 added two VARCHAR columns to kami_static; the populator
(Session 12 / commit 4798dce) extracts them on every refresh going
forward. This script catches up the existing 7,021 rows that pre-date
the populator change.

Approach: pure SQL split of the existing `affinities` JSON column.

The Session 9 populator already stores the chain's
`getKami(kamiId).affinities` array as a JSON string in the
`affinities` column on every row. Affinity is an immutable per-kami
attribute — once a kami has been minted with `[body_aff, hand_aff]`,
that pair never changes. So the cached JSON in the `affinities`
column is exact-equal to what a fresh `getKami` call would return
today. Splitting the JSON into two scalar columns is correct
without re-fetching from chain.

This is **option (c)** in the Session 12 prompt's framing — the
prompt listed (a) "reuse the populator's per-kami fetch path" and
(b) "minimal getKami → affinities extractor", and instructed to
pick (a). We diverged after the (a) approach proved 4× slower than
the prompt's wall-time estimate (~50–100 min predicted, actual
~3.5 hours at 0.5 kami/s on the public RPC). Founder authorized
the switch on 2026-04-27 with the note that affinity immutability
makes the cached JSON authoritative — no chain round-trip needed.

Coverage of the JSON column was already 100% on the post-Session-11
state (every kami_static row has a non-NULL `affinities` value),
so this UPDATE hydrates every pending row in a single statement.
The Session 11 partial run had committed 1,600 rows via the
chain-fetch path before the switch; the WHERE clause skips those.

Failure handling: any row whose `affinities` JSON is malformed or
not a length-2 array is left with NULL `body_affinity` /
`hand_affinity` and reported in the post-run summary. New entries
inserted by the populator after Session 12 ships extract these
columns on the chain side (see ingester/kami_static.py
`_kami_shape_to_static`); the script is a one-shot for the legacy
backlog, not a routine job.

Stop the kami-oracle service before running (DuckDB exclusive lock):

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_kami_affinity.py
    sudo systemctl start kami-oracle
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.config import load_config  # noqa: E402
from ingester.storage import Storage, read_schema_sql  # noqa: E402

log = logging.getLogger("backfill_kami_affinity")

EXPECTED_AFFINITIES = {"EERIE", "NORMAL", "SCRAP", "INSECT"}


def _count_pending(storage: Storage) -> int:
    row = storage.fetchone(
        "SELECT COUNT(*) FROM kami_static "
        "WHERE body_affinity IS NULL OR hand_affinity IS NULL"
    )
    return int(row[0]) if row else 0


def _count_eligible(storage: Storage) -> int:
    """Pending rows whose `affinities` JSON is a length-2 array."""
    row = storage.fetchone(
        "SELECT COUNT(*) FROM kami_static "
        "WHERE (body_affinity IS NULL OR hand_affinity IS NULL) "
        "  AND affinities IS NOT NULL "
        "  AND json_array_length(affinities) = 2"
    )
    return int(row[0]) if row else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args(argv)  # noqa: F841 — kept for future flags

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    pre = _count_pending(storage)
    log.info(
        "backfill_kami_affinity: %d kami_static rows have NULL body_affinity or hand_affinity",
        pre,
    )
    if pre == 0:
        log.info("nothing to do")
        storage.close()
        return 0

    eligible = _count_eligible(storage)
    log.info(
        "backfill_kami_affinity: %d of %d pending rows have a length-2 `affinities` JSON (eligible for split)",
        eligible, pre,
    )
    if eligible < pre:
        log.warning(
            "backfill_kami_affinity: %d pending rows have no `affinities` JSON or wrong shape — they will stay NULL",
            pre - eligible,
        )

    # Single UPDATE: split the existing affinities JSON column into the
    # two scalar columns. WHERE clause is doubly defensive — only NULL
    # targets, only length-2 source, only non-NULL source. Bumps
    # last_refreshed_ts so the row's freshness signal moves forward.
    with storage.lock:
        storage.conn.execute(
            """
            UPDATE kami_static
            SET body_affinity = json_extract_string(affinities, '$[0]'),
                hand_affinity = json_extract_string(affinities, '$[1]'),
                last_refreshed_ts = now()
            WHERE (body_affinity IS NULL OR hand_affinity IS NULL)
              AND affinities IS NOT NULL
              AND json_array_length(affinities) = 2
            """
        )

    post = _count_pending(storage)
    log.info(
        "backfill_kami_affinity done: pre_pending=%d post_pending=%d (rows left NULL = `affinities` JSON missing or shape != 2)",
        pre, post,
    )

    log.info("=== body_affinity distribution ===")
    for r in storage.fetchall(
        "SELECT body_affinity, COUNT(*) AS n FROM kami_static "
        "GROUP BY 1 ORDER BY n DESC"
    ):
        label = r[0] if r[0] is not None else "NULL"
        flag = "" if r[0] is None or r[0] in EXPECTED_AFFINITIES else "  <-- UNEXPECTED"
        log.info("  %-7s n=%d%s", label, int(r[1]), flag)

    log.info("=== hand_affinity distribution ===")
    for r in storage.fetchall(
        "SELECT hand_affinity, COUNT(*) AS n FROM kami_static "
        "GROUP BY 1 ORDER BY n DESC"
    ):
        label = r[0] if r[0] is not None else "NULL"
        flag = "" if r[0] is None or r[0] in EXPECTED_AFFINITIES else "  <-- UNEXPECTED"
        log.info("  %-7s n=%d%s", label, int(r[1]), flag)

    log.info("=== body × hand cross-tab (16 cells expected for a fully-mixed 4×4) ===")
    for r in storage.fetchall(
        "SELECT body_affinity, hand_affinity, COUNT(*) AS n FROM kami_static "
        "GROUP BY 1, 2 ORDER BY n DESC"
    ):
        log.info(
            "  body=%-7s hand=%-7s n=%d",
            r[0] if r[0] is not None else "NULL",
            r[1] if r[1] is not None else "NULL",
            int(r[2]),
        )

    # Functional check: body_index should map to exactly one body_affinity
    # (affinity is a derived attribute of the body trait). Same for hand.
    nondet = storage.fetchall(
        """
        SELECT body, COUNT(DISTINCT body_affinity) AS n_distinct
        FROM kami_static
        WHERE body_affinity IS NOT NULL
        GROUP BY 1
        HAVING COUNT(DISTINCT body_affinity) > 1
        """
    )
    if nondet:
        log.warning(
            "anomaly: %d body_index values map to >1 distinct body_affinity — affinity may NOT be a pure function of body trait",
            len(nondet),
        )
        for r in nondet:
            log.warning("  body=%s n_distinct=%d", r[0], int(r[1]))
    else:
        log.info(
            "functional check: body_index -> body_affinity is deterministic (each body trait maps to exactly one affinity)"
        )

    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
