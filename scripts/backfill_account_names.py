#!/usr/bin/env python
"""Backfill account_index + account_name on existing kami_static rows.

Walks every distinct ``account_id`` in ``kami_static`` whose
``account_name`` is NULL, calls ``GetterSystem.getAccount(uint256)``
once per unique account, and bulk-UPDATEs the matching rows.

Idempotent — safe to re-run; only touches rows where
``account_name IS NULL``. A revert / empty name leaves the row NULL
(treated as anonymous) and is reported as a counter rather than a
failure.

Stop the kami-oracle service before running (DuckDB exclusive lock):

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_account_names.py
    sudo systemctl start kami-oracle
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import load_config  # noqa: E402
from ingester.kami_static import KamiStaticReader  # noqa: E402
from ingester.storage import Storage, read_schema_sql  # noqa: E402
from ingester.system_registry import SystemRegistry, resolve_systems  # noqa: E402

log = logging.getLogger("backfill_account_names")


def _count_null(storage: Storage) -> int:
    row = storage.fetchone(
        """
        SELECT COUNT(*) FROM kami_static
        WHERE account_name IS NULL AND account_id IS NOT NULL AND account_id <> '0'
        """
    )
    return int(row[0]) if row else 0


def _distinct_accounts(storage: Storage) -> list[str]:
    rows = storage.fetchall(
        """
        SELECT DISTINCT account_id FROM kami_static
        WHERE account_name IS NULL AND account_id IS NOT NULL AND account_id <> '0'
        """
    )
    return [str(r[0]) for r in rows]


def _apply_batch(
    storage: Storage,
    batch: list[tuple[int | None, str | None, str]],
) -> None:
    """Apply (account_index, account_name, account_id) tuples to kami_static.

    Uses a single bulk UPDATE per batch via a temp table — per-row UPDATE
    against a 7k-row table is acceptable but the temp-table join keeps us
    consistent with how migration 002 handled its kami_id stitch.
    """
    if not batch:
        return
    with storage.lock:
        storage.conn.execute("BEGIN TRANSACTION")
        try:
            storage.conn.execute(
                "CREATE TEMP TABLE _acct_map("
                "  account_id VARCHAR PRIMARY KEY,"
                "  account_index INTEGER,"
                "  account_name VARCHAR"
                ")"
            )
            storage.conn.executemany(
                "INSERT INTO _acct_map(account_id, account_index, account_name) "
                "VALUES (?, ?, ?)",
                [(acct_id, idx, name) for (idx, name, acct_id) in batch],
            )
            storage.conn.execute(
                """
                UPDATE kami_static AS s
                SET account_index = m.account_index,
                    account_name = m.account_name,
                    last_refreshed_ts = now()
                FROM _acct_map m
                WHERE s.account_id = m.account_id
                  AND s.account_name IS NULL
                """
            )
            storage.conn.execute("DROP TABLE IF EXISTS _acct_map")
            storage.conn.execute("COMMIT")
        except Exception:
            storage.conn.execute("ROLLBACK")
            raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--commit-every", type=int, default=50,
        help="Flush UPDATE batch every N accounts fetched (default 50)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("RPC not reachable: %s", cfg.rpc_url)
        return 1

    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    pre_null = _count_null(storage)
    log.info("backfill_account_names: %d kami_static rows have NULL account_name", pre_null)
    if pre_null == 0:
        log.info("nothing to do")
        storage.close()
        return 0

    accounts = _distinct_accounts(storage)
    log.info("backfill_account_names: %d distinct account_id to fetch", len(accounts))

    # Resolve registry at head + extend with prior snapshot so we have the
    # latest GetterSystem address.
    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    prior = storage.load_system_address_snapshot()
    if prior:
        prior_reg = SystemRegistry.from_snapshot_rows(prior)
        registry.extend(prior_reg)
    storage.upsert_system_address_snapshot(registry.to_snapshot_rows())

    reader = KamiStaticReader(client, registry, cfg.abi_dir)

    n_named = 0
    n_anon = 0
    batch: list[tuple[int | None, str | None, str]] = []
    t0 = time.monotonic()
    for i, acct_id in enumerate(accounts, 1):
        idx, name = reader.fetch_account(acct_id)
        if name is None:
            n_anon += 1
        else:
            n_named += 1
        batch.append((idx, name, acct_id))
        if len(batch) >= args.commit_every:
            _apply_batch(storage, batch)
            batch.clear()
            elapsed = time.monotonic() - t0
            log.info(
                "progress: %d/%d accounts fetched (named=%d anon=%d) in %.1fs",
                i, len(accounts), n_named, n_anon, elapsed,
            )
    if batch:
        _apply_batch(storage, batch)

    post_null = _count_null(storage)
    log.info(
        "backfill_account_names done: pre_null=%d post_null=%d named=%d anon=%d",
        pre_null, post_null, n_named, n_anon,
    )
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
