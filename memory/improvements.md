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

## Session 3 — 2026-04-22

- **Registry snapshot (prime task)**: `system_registry.SystemRegistry` now holds `set[str]` addresses per `system_id` with `first_seen_block`/`last_seen_block` seen-block bounds. Added `probe_historical_systems()` + `evenly_spaced_probes()`, `block_identifier` kwarg on `ChainClient.call_contract_fn`, and a new `system_address_snapshot` DuckDB table that persists the union across restarts. `backfill.main` probes at 10 evenly-spaced heights across the target window and writes the union snapshot; `poller` re-probes every 6h and merges. Decoder dispatches by `system_id` so the same ABI routes regardless of which historical address matched. Live probe confirmed 6 systems with ≥2 deployments, including 2 for `system.harvest.start` (`38a7f89`).
- Relaunched 7-day backfill with the fix in place, 420k blocks projected ~2 days. Session-2.5 partial DB backed up to `db/kami-oracle.duckdb.session2p5.bak`.
- **Co-hosted FastAPI read-only query layer**: new `ingester/api.py` with 8 endpoints (`/health`, `/kami/{id}/actions`, `/kami/{id}/summary`, `/operator/{addr}/summary`, `/actions/types`, `/nodes/top`, `/actions/recent`, `/registry/snapshot`). `since_days` clamped at 28d, `limit` at 2000 (both enforced by FastAPI `Query` validators → 422 on overflow). Parameter-bound SQL throughout (`629cab3`).
- **Co-hosted service**: new `ingester/serve.py` runs the poller thread + uvicorn in one process sharing a single `Storage` (DuckDB has a per-process exclusive file lock, which rules out separate reader processes). `threading.Lock` serializes Storage method calls. `_parse_bind` refuses `0.0.0.0`/`::`/wildcards; default bind `127.0.0.1:8787`. Graceful shutdown via signal handler that flips `stop_event` + `server.should_exit`, with 30s poller-join timeout on exit (`2f2b6c8`).
- Added `tests/test_api.py` (10 tests, FastAPI TestClient against scratch DuckDB) and `tests/test_system_registry.py` (10 tests covering the extend/union/probe/snapshot-roundtrip paths).
- New runtime deps: `fastapi`, `uvicorn[standard]`, `httpx` (test-only). All pinned with a comment explaining Stage-1 loopback-only bind (`629cab3`).
- Ops runbook updated for the co-hosted service launch command + health-check pattern (`9a99bad`).
- **Not done this session**: launch serve (Part 3 of session 3 brief). Backfill still holds DuckDB lock; deferred to 3.5 once backfill completes. See `memory/next-steps.md`.

## Session 6 — 2026-04-25

- **Decoder fix (`harvest_stop` / `harvest_collect` `kami_id`)**: `harvest_id`
  now a first-class column on `kami_action`. `HarvestResolver` stitches
  `kami_id` via the deterministic
  `keccak256(b"harvest" || uint256_be(kami_id))` mapping documented in
  `kamigotchi-context/integration/architecture.md` — no eth_call per
  tx, the inverse map is computed offline from every kami_id ever
  observed in any action_type. First commit: `f2b2eae` (decoder +
  resolver). Empirical: 141,274 historical stop+collect rows started
  with NULL `kami_id`; 98.5% stitched via in-window harvest_starts +
  another 1.6% via the wider kami_id universe; 32 orphans (0.023%)
  remain — stops whose start predates the 7-day window and whose
  kami_id never reappears.
- **Schema migration v1 → v2**: added `migrations/002_add_harvest_id_column.py`
  and a migration runner in `Storage.bootstrap()` so service restarts
  auto-apply pending schema changes. The migration is idempotent and
  bulk-driven (a per-row `executemany` UPDATE was unworkable at 113k
  rows; replaced with a temp-table JOIN that finishes in seconds).
  Commit `9c208b3`.
- **kami_static backfill + refresh worker**: `ingester/kami_static.py`
  reads `getKami(uint256)` from the GetterSystem, mapping the
  `KamiShape` tuple into the schema's columns. Owner address recovered
  offline via the address-cast pattern (`address = account_id mod
  2^160`). Daemon thread sweeps every 6 h, refreshing kamis missing
  from the table or older than 24 h. ThreadPoolExecutor at 8 workers
  for the RPC-latency-bound eth_calls; DB writes single-writer behind
  `Storage.lock`. First-run backfill TODO row count (logged inline in
  next-steps post-run). Commit `1a50a19`.
- **Poller concurrency refactor (deferred)**: cursor was already at
  chain head when Session 6 opened (Session-5-era ~36 h lag had since
  recovered). Single-threaded poller is keeping pace at ~2 blk/s
  steady state. Re-evaluate if drift returns after a major outage or
  redeploy.
- **`/health` payload extras**: `kami_static` row count + last refresh
  timestamp; `chain_head_lag_seconds` (`now - last_block_timestamp`).
  Cheaper than EWMA throughput tracking and more directly useful for
  spotting drift. Commit `9d43b6e`.
- **`questions-for-human.md` cleared**: GCS service-account scope was
  widened on 2026-04-24; nightly backups uploading cleanly.
