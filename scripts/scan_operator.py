"""Targeted back-scan for a single operator wallet.

Walks head..head-N blocks, filters ``tx.from == OPERATOR``, decodes matched
txs and prints a per-kami activity summary. Writes nothing to DuckDB.

Usage:
    python scripts/scan_operator.py --operator 0x... --blocks 20000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web3 import Web3  # noqa: E402

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import configure_logging, load_config  # noqa: E402
from ingester.decoder import Decoder  # noqa: E402
from ingester.system_registry import resolve_systems  # noqa: E402

log = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--operator", required=True, help="0x-prefixed wallet")
    ap.add_argument("--blocks", type=int, default=20_000)
    ap.add_argument("--to-block", type=int, default=None)
    args = ap.parse_args()

    op = Web3.to_checksum_address(args.operator)
    cfg = load_config()
    configure_logging(cfg.log_level)

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("scan: RPC not reachable")
        return 1

    head = args.to_block or client.block_number()
    start = max(1, head - args.blocks + 1)
    log.info("scan: operator=%s  range=%d..%d  (%d blocks)", op, start, head, head - start + 1)

    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    decoder = Decoder(cfg.abi_dir, registry)
    known_addrs = registry.known_addresses()

    action_counts: Counter[str] = Counter()
    by_kami: defaultdict[str, Counter[str]] = defaultdict(Counter)
    by_kami_ts: defaultdict[str, list[int]] = defaultdict(list)
    node_counts: Counter[str] = Counter()
    examples: list[str] = []
    first_ts = last_ts = None
    matched_tx = 0

    for n in range(head, start - 1, -1):
        block = client.get_block(n, full=True)
        ts = int(block["timestamp"])
        for tx in block.get("transactions", []):
            try:
                frm = Web3.to_checksum_address(tx["from"])
            except (ValueError, KeyError):
                continue
            if frm != op:
                continue
            to = tx.get("to")
            if to is None:
                continue
            try:
                to_cs = Web3.to_checksum_address(to)
            except ValueError:
                continue
            if to_cs not in known_addrs:
                continue
            calldata = tx["input"]
            if isinstance(calldata, str):
                calldata = bytes.fromhex(calldata[2:] if calldata.startswith("0x") else calldata)
            else:
                calldata = bytes(calldata)
            tx_hash_hex = tx["hash"].hex() if hasattr(tx["hash"], "hex") else str(tx["hash"])
            if not tx_hash_hex.startswith("0x"):
                tx_hash_hex = "0x" + tx_hash_hex
            r = decoder.decode_tx(
                tx_hash=tx_hash_hex,
                from_addr=frm,
                to_addr=to_cs,
                calldata=calldata,
                block_number=n,
                block_timestamp=ts,
                status=1,
            )
            matched_tx += 1
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            for a in r.actions:
                action_counts[a.action_type] += 1
                if a.kami_id:
                    by_kami[a.kami_id][a.action_type] += 1
                    by_kami_ts[a.kami_id].append(ts)
                if a.node_id:
                    node_counts[a.node_id] += 1
                if len(examples) < 8:
                    examples.append(json.dumps({
                        "tx": a.tx_hash, "block": n, "ts": ts,
                        "action": a.action_type,
                        "kami_id": a.kami_id, "node_id": a.node_id,
                        "amount": a.amount, "meta": a.metadata,
                    }, default=str))

    print("=" * 72)
    print(f"OPERATOR SCAN — {op}")
    print(f"  block range   : {start}..{head}")
    if first_ts and last_ts:
        span_s = last_ts - first_ts
        print(f"  time span     : {first_ts} .. {last_ts}  ({span_s/3600:.1f}h)")
    print(f"  matched txs   : {matched_tx}")
    print(f"  total actions : {sum(action_counts.values())}")
    print()
    print("action_type counts:")
    for at, c in action_counts.most_common():
        print(f"  {at:24s} {c}")
    print()
    print("node_id counts (top 10):")
    for nid, c in node_counts.most_common(10):
        print(f"  {nid:10s} {c}")
    print()
    print(f"unique kami_ids seen: {len(by_kami)}")
    print("top 5 kami by action count:")
    top = sorted(by_kami.items(), key=lambda kv: -sum(kv[1].values()))[:5]
    for k, c in top:
        shape = ", ".join(f"{at}={n}" for at, n in c.most_common())
        ts_list = sorted(by_kami_ts[k])
        if len(ts_list) > 1:
            gaps = [ts_list[i+1]-ts_list[i] for i in range(len(ts_list)-1)]
            med = sorted(gaps)[len(gaps)//2]
            print(f"  {k[:16]}... [{shape}] median gap {med//60}min")
        else:
            print(f"  {k[:16]}... [{shape}]")
    print()
    print("examples (up to 8):")
    for e in examples:
        print("  " + e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
