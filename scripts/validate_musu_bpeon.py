#!/usr/bin/env python
"""Cross-check kami_action.amount against on-chain ValueComponent state.

For Session 7 acceptance (memory/decoder-notes.md "Session 7"). The
prompt's original "compare to on-chain ERC-20 Transfer events" plan
doesn't apply — MUSU is not an ERC-20 — so this script does the
equivalent independent check: for a small bpeon-owned sample, query
the chain's ``ValueComponent.getValue(MUSU_inv_entity)`` at
``block-1`` and ``block`` and verify the delta matches the oracle's
stored amount.

The script reads the oracle DB **read-only** (so it can run while the
service holds the write lock).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb
from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import load_config  # noqa: E402
from ingester.musu import VALUE_COMPONENT_ID, decode_musu_drains  # noqa: E402
from ingester.system_registry import SystemRegistry  # noqa: E402

log = logging.getLogger("validate_musu_bpeon")

# Bpeon's manager-operator address — first defined in
# memory/decoder-notes.md "Spot-validation: bpeon operator (session 2)".
BPEON_OPERATOR = "0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2"


def musu_inventory_entity(account_id: int) -> int:
    """Compute MUSU inv entity: keccak256(abi.encodePacked(
    "inventory.instance", uint256 accountId, uint32 1)) — per
    kami_context/system-ids.md "Reading Inventory Balance"."""
    packed = b"inventory.instance" + account_id.to_bytes(32, "big") + (1).to_bytes(4, "big")
    return int.from_bytes(Web3.keccak(packed), "big")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(REPO_ROOT / "db" / "kami-oracle.duckdb"),
    )
    ap.add_argument(
        "--n", type=int, default=8,
        help="number of harvest_collect/stop sample txs to cross-check",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    cfg = load_config()
    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("rpc not reachable")
        return 1

    # Resolve the live ValueComponent address. Component lookup is via the
    # World's components registry — same path documented in
    # kami_context/system-ids.md "Resolving Component Addresses".
    components_registry_addr = client.w3.eth.call({
        "to": Web3.to_checksum_address(cfg.world_address),
        "data": Web3.keccak(text="components()")[:4],
    })
    components_registry_addr = Web3.to_checksum_address(
        "0x" + components_registry_addr.hex()[-40:]
    )
    log.info("components registry: %s", components_registry_addr)

    # Resolve component.value -> address via getEntitiesWithValue(componentId)
    # The componentId is the *uint256* of keccak256("component.value"),
    # which is VALUE_COMPONENT_ID.
    sel = Web3.keccak(text="getEntitiesWithValue(uint256)")[:4]
    call_data = sel + VALUE_COMPONENT_ID.to_bytes(32, "big")
    raw = client.w3.eth.call({
        "to": components_registry_addr,
        "data": call_data,
    })
    # Returns uint256[]: offset, length, then values. Take entities[0].
    if len(raw) < 96:
        log.error("getEntitiesWithValue returned unexpected payload")
        return 1
    n = int.from_bytes(raw[32:64], "big")
    if n == 0:
        log.error("no ValueComponent registered")
        return 1
    vc_addr_int = int.from_bytes(raw[64:96], "big")
    value_component_addr = Web3.to_checksum_address(
        "0x" + vc_addr_int.to_bytes(20, "big").hex()
    )
    log.info("ValueComponent address: %s", value_component_addr)

    # Pull bpeon's harvest_collect/stop sample with non-NULL amount.
    conn = duckdb.connect(args.db, read_only=True)
    rows = conn.execute(
        f"""
        SELECT a.tx_hash, a.sub_index, a.block_number, a.action_type,
               a.harvest_id, a.amount, a.kami_id, s.owner_address, s.account_id
        FROM kami_action a
        LEFT JOIN kami_static s USING (kami_id)
        WHERE a.from_addr ILIKE ?
          AND a.action_type IN ('harvest_collect', 'harvest_stop')
          AND a.amount IS NOT NULL
          AND a.harvest_id IS NOT NULL
          AND s.account_id IS NOT NULL
          AND a.block_timestamp > now() - INTERVAL 24 HOUR
        ORDER BY a.block_timestamp DESC
        LIMIT ?
        """,
        [BPEON_OPERATOR, args.n],
    ).fetchall()
    conn.close()

    if not rows:
        log.error("no sample rows found for bpeon — aborting")
        return 1

    log.info("cross-checking %d bpeon harvest rows", len(rows))
    log.info("%-18s %-12s %-12s %s", "tx_prefix", "oracle_amt",
             "chain_delta", "match")

    matches = 0
    mismatches = 0
    skipped = 0

    get_value_sel = Web3.keccak(text="getValue(uint256)")[:4]

    for (tx_hash, sub_index, block, atype, h_id, amt,
         kami_id, owner, account_id) in rows:
        try:
            account_id_int = int(account_id)
        except (TypeError, ValueError):
            skipped += 1
            continue
        inv_id = musu_inventory_entity(account_id_int)

        def _get_value_at(blk: int) -> int:
            """Read ValueComponent.getValue(inv_id) at the given block.

            Returns 0 if the entity hasn't been written yet (revert →
            common for fresh accounts at very early blocks)."""
            data = get_value_sel + inv_id.to_bytes(32, "big")
            try:
                raw = client.w3.eth.call({
                    "to": value_component_addr,
                    "data": data,
                }, block_identifier=blk)
            except Exception:  # noqa: BLE001
                return 0
            return int.from_bytes(raw, "big") if raw else 0

        before = _get_value_at(block - 1)
        after = _get_value_at(block)
        chain_delta = after - before
        oracle_amt = int(amt) if amt else 0

        # The block may contain MULTIPLE bpeon harvests (batched stop)
        # so the chain delta is the *batch sum*, not the per-row drain.
        # Aggregate the oracle's amounts across all bpeon harvests sharing
        # this tx, owner, and block — and compare those.
        # (Pull again with a tight filter.)
        conn = duckdb.connect(args.db, read_only=True)
        sib = conn.execute(
            """
            SELECT SUM(CAST(amount AS HUGEINT)) AS total
            FROM kami_action
            WHERE tx_hash = ?
              AND action_type IN ('harvest_collect', 'harvest_stop')
              AND amount IS NOT NULL
              AND kami_id IN (
                  SELECT kami_id FROM kami_static WHERE account_id = ?
              )
            """,
            [tx_hash, account_id],
        ).fetchone()
        conn.close()
        oracle_tx_sum = int(sib[0]) if sib and sib[0] is not None else oracle_amt

        ok = chain_delta == oracle_tx_sum
        log.info(
            "%-18s %-12s %-12s %s  block=%d before=%d after=%d (atype=%s, %s)",
            tx_hash[:18], oracle_tx_sum, chain_delta,
            "MATCH" if ok else "MISMATCH",
            block, before, after, atype, owner,
        )
        if ok:
            matches += 1
        else:
            mismatches += 1

    log.info(
        "summary: %d matched, %d mismatched, %d skipped",
        matches, mismatches, skipped,
    )
    return 0 if mismatches == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
