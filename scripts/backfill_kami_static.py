#!/usr/bin/env python
"""Walk every distinct kami_id in kami_action and fill kami_static.

Reads via the GetterSystem (``getKami(uint256)``); see
``ingester.kami_static`` for details. Idempotent — safe to re-run; later
runs upsert with the latest chain values.

Stop the kami-oracle service before running (DuckDB exclusive lock):

    sudo systemctl stop kami-oracle
    .venv/bin/python scripts/backfill_kami_static.py
    sudo systemctl start kami-oracle
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import load_config  # noqa: E402
from ingester.kami_static import KamiStaticReader, backfill_all  # noqa: E402
from ingester.storage import Storage, read_schema_sql  # noqa: E402
from ingester.system_registry import SystemRegistry, resolve_systems  # noqa: E402

log = logging.getLogger("backfill_kami_static")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--flush-every", type=int, default=200)
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

    # Resolve registry at head + extend with prior snapshot so we have the
    # latest GetterSystem address.
    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    prior = storage.load_system_address_snapshot()
    if prior:
        prior_reg = SystemRegistry.from_snapshot_rows(prior)
        registry.extend(prior_reg)
    storage.upsert_system_address_snapshot(registry.to_snapshot_rows())

    reader = KamiStaticReader(client, registry, cfg.abi_dir)
    stats = backfill_all(
        storage, reader,
        workers=args.workers,
        flush_every=args.flush_every,
    )
    log.info("backfill_kami_static: %s", stats)
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
