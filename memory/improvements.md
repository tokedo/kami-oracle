# Improvements log

One-line entries per session describing what the harness learned about
itself. Commit hashes are filled in after the commit.

## Session 1 — 2026-04-17

- Built the Stage 1 skeleton: `ingester/{config,chain_client,system_registry,decoder,storage,ingest,poller,backfill,prune}.py`, `schema/schema.sql`, `scripts/validate_decode.py`, `tests/test_decoder.py`.
- Discovered vendored JSON ABIs are partial vs. live chain; added a small `SYSTEM_ABI_OVERLAY` in `decoder.py` carrying documented-but-missing signatures (cited against `kami_context/system-ids.md`) — got decode coverage from ~43% → ~98% on a 500-block sample.
- Decoder design: per-system **explicit field map** (`SYSTEM_FIELD_MAP`) rather than generic arg-name inference. Ambiguous names (`id`, `targetID`, `holderID`) mean different things per system; explicit mapping avoids mis-decodes.
- Decoded batched calls (`executeBatched(uint256[] ...)`) fan out to one `kami_action` row per array element, with `sub_index` as the row key suffix. A single tx commonly produces 2–8 rows.
- Left one ABI question open for human review: `system.craft` on-chain uses a 2-arg executeTyped that contradicts both the vendored JSON and the docs (see `memory/questions-for-human.md`).

## Session 2 — 2026-04-17

- Added craft `executeTyped(uint32,uint256)` overlay (selector `0x5c817c70`), confirmed against deployed CraftSystem bytecode — vendored JSON is stale. Decode coverage now **100%** over a 2000-block validation window (commit `bfbb526`, re-validation commit `6e2afd7`).
- Codified the 3-tier ABI overlay policy in CLAUDE.md (auto-extend / flag / never-invent) so future sessions don't ask the same question each time (`2c31066`).
- Added `scripts/scan_operator.py` for targeted operator spot-validation; walked founder's bpeon manager wallet `0x86aDb8…FAcEC2` — 100% decode coverage on 6 txs (`4236a86`, `276e9f4`).
- Probed `eth_getBlockReceipts` on the public Yominet RPC: supported but not a speedup, because blocks have ≤1 matched tx on average. Kept per-tx receipt; bumped `RetryPolicy` (attempts 5→8, max delay 30s→60s) for multi-day backfill resilience (`276e9f4`).
- **Public RPC retention is ~22.7 days, not 28** — a naive 4-week backfill burned retries on every historical fetch. `backfill.py` now binary-searches the earliest retained block at startup and clamps `start_block + 20k` to stay inside the load-balancer's fuzzy retention edge (`c87710f`). Documented for human decision: archive RPC vs shorter window.
- 4-week backfill launched in detached `screen` session `kami-backfill` (log at `logs/backfill.log`); projected ~7.7 days @ 2.4 blocks/sec over ~980k blocks.
