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

## Session 2.5 — 2026-04-18

- Diagnosed the "action-mix divergence" in the partial session-2 DB: harvest system contracts are redeployed periodically (4 distinct addresses across 22 days of probes), but `resolve_systems()` only looks up head addresses — so historical harvest txs were silently dropped at the match step (never even reaching the decoder). H1 confirmed; H2/H3/H4 ruled out. Fix (registry-snapshot) deferred to session 3+ (`076196a`).
- Widened `TRANSIENT_EXC` to catch `requests.RequestException` + `socket.error` — session-2 backfill died ~6h in on a bare `requests.ConnectionError` that slipped past the previous narrower tuple. Added 11 parametrized tests covering ConnectionError, ChunkedEncodingError, ReadTimeout, SSLError, and the generic parent (`88c9024`).
- Added outer survival loops to `backfill.main()` and `poller.main()`: any unhandled exception logs + sleeps 60s + refreshes cursor from DB + resumes. No more "dead at 2am, human notices at 8am" (`88c9024`).
- Shrank rolling window 28 → 7 days during the investigation phase: stays clear of the ~22.7-day RPC retention edge, gets a clean sample fast, makes the registry-snapshot fix a smaller problem. Backfill CLI gains `--days` flag preferred over `--weeks`. Session-2 DB backed up to `db/kami-oracle.duckdb.session2.bak` (`4316470`).
- Relaunched backfill in `kami-backfill` screen session — 420k blocks, ~2 days projected.
