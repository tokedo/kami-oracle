"""One-shot integration check for the registry-snapshot fix.

Probes the live Yominet MUD registry at N evenly-spaced block heights
across a configurable window and prints the resulting union of
(system_id -> address) mappings. Session 2.5 found 4 distinct
``system.harvest.start`` deployments across a 22-day window; a
7-day window should yield at least 2 given the most recent
redeployment was ~2.3 days before 2026-04-18.

Usage:
    python -m scripts.probe_registry_snapshot --days 7 --probes 10
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import configure_logging, load_config  # noqa: E402
from ingester.system_registry import (  # noqa: E402
    evenly_spaced_probes,
    probe_historical_systems,
)

log = logging.getLogger(__name__)

BLOCKS_PER_DAY = 60_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--probes", type=int, default=10)
    args = ap.parse_args()

    cfg = load_config()
    configure_logging(cfg.log_level)

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        print(f"RPC {cfg.rpc_url} not reachable", file=sys.stderr)
        return 1

    head = client.block_number()
    start = max(1, head - args.days * BLOCKS_PER_DAY)
    heights = evenly_spaced_probes(start, head, n=args.probes)

    print(f"head={head} start={start} probes={heights}")
    reg = probe_historical_systems(client, cfg.world_address, cfg.abi_dir, heights)

    print()
    print(f"systems resolved: {len(reg.system_ids())}")
    print(f"total addresses:  {len(reg)}")
    print()

    for sid in sorted(reg.system_ids()):
        addrs = sorted(reg.addresses_for_system(sid))
        marker = " <-- multiple deployments" if len(addrs) > 1 else ""
        print(f"  {sid}: {len(addrs)} address(es){marker}")
        for a in addrs:
            info = reg.get_by_address(a)
            print(
                f"    {a}  first={info.first_seen_block} last={info.last_seen_block}"
            )

    harvest_addrs = reg.addresses_for_system("system.harvest.start")
    print()
    print(
        f"system.harvest.start addresses in [{start}, {head}]: "
        f"{len(harvest_addrs)}"
    )
    if len(harvest_addrs) < 2:
        print(
            "WARNING: expected >=2 deployments of system.harvest.start "
            "across a 7-day window (session 2.5 found 4 across 22 days). "
            "Check the probe density or RPC retention."
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
