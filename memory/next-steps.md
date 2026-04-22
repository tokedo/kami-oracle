# Next Steps

## Session 3.5 — pick up from here

### Current state at session 3 end (2026-04-22 ~17:15 UTC)

- Session 3 shipped Parts 0–2 of the brief: registry-snapshot fix
  (Part 0 / prime), backfill relaunch (Part 1), and co-hosted FastAPI
  read-only query layer (Part 2). See `memory/improvements.md` for
  commit hashes.
- **1-week backfill is running** in detached screen session
  `kami-backfill`. Relaunched 2026-04-22 17:05 UTC after the
  registry-snapshot fix landed. 935 chunks done, 0 resume events at
  session 3 end. Projected ~2 days at 2.4 blocks/sec. Log at
  `logs/backfill.log`.
- DB file: `db/kami-oracle.duckdb` (fresh, bootstrapped by the
  backfill launch). Session-2 DB backed up at
  `db/kami-oracle.duckdb.session2.bak` and session-2.5 partial at
  `db/kami-oracle.duckdb.session2p5.bak`. Keep both until session
  3.5 confirms clean data, then delete the partials.
- Co-hosted service (`ingester.serve`) is built and tested but **not
  launched** — DuckDB per-process lock means only one of
  `{serve, poller, backfill}` may hold the DB at a time, and backfill
  is holding it. Part 3 of the session 3 brief is deferred to 3.5.
- All 10 `tests/test_api.py` + 10 `tests/test_system_registry.py`
  tests pass.
- `questions-for-human.md` is empty.

### Headline from session 3 — registry snapshot fix landed

The session 2.5 hypothesis (H1) was correct: Kamigotchi redeploys
system contracts periodically and the old "resolve once at head"
registry dropped txs targeting older addresses at the match step.
Live probe of Yominet over the target backfill window confirmed 6
systems with ≥2 addresses, including 2 for `system.harvest.start`
(`0x0777687Ec...` current + `0xA0b4E6F3...` older). Fix unions the
probed address sets per `system_id` and dispatches decode on
`system_id` rather than address. Persisted to new
`system_address_snapshot` table so restarts rebuild the union
without re-probing.

Expected effect on the relaunched backfill: harvest action mix
should now match the head-validation sample (~65-75% of decoded
actions), recovering the ~2/3 of historical harvest txs the
session-2 backfill silently dropped.

### Headline from session 3 — FastAPI read-only query layer

`ingester.serve` co-hosts the poller thread and a FastAPI app in
one process sharing a single `Storage` whose methods serialize on
`threading.Lock`. Endpoints: `/health`, `/kami/{id}/actions`,
`/kami/{id}/summary`, `/operator/{addr}/summary`, `/actions/types`,
`/nodes/top`, `/actions/recent`, `/registry/snapshot`. Bind defaults
to `127.0.0.1:8787`; `_parse_bind` refuses `0.0.0.0`/`::`/wildcards
— public exposure is a Phase-D + ADR decision. No auth layer yet.
Graceful shutdown via SIGTERM/SIGINT: stop_event → poller drains →
uvicorn stops → DB closes (30s join timeout).

Full launch command in `memory/ops.md` under "Co-hosted serve
(session 3 — poller + read-only HTTP in one process)".

### Do in this order

1. **Check backfill health.** Resume-loop should keep it alive but
   verify.
   ```
   screen -ls                                        # kami-backfill alive?
   tail -200 logs/backfill.log                       # recent chunks + resumes
   grep -c "done (actions" logs/backfill.log         # chunk count
   grep "backfill: unhandled" logs/backfill.log      # any resume events?
   ```
   Don't open the DuckDB file while backfill writes — per-process
   exclusive lock. Infer progress from chunk logs only.

2. **If backfill is complete (cursor reached head-at-launch):**
   - Capture row counts, action-type distribution (harvest %),
     unique kami count, unique operator count, block range, wall
     time. Commit as
     `data: 7-day backfill complete — N raw_tx, M actions,
     harvest coverage X%`.
   - Harvest-coverage sanity check: bpeon wallet
     `0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2` should show many
     `harvest_start`/`harvest_collect`/`harvest_stop` actions on
     node 47 (Kamibots auto_v2). If harvest ~0 on bpeon, something
     is still wrong — STOP and investigate before launching serve.
   - Re-run head-validation via `scripts/validate_decode.py` on the
     last 2000 blocks and confirm the action mix matches the DB
     slice closely. Mismatch would indicate the registry snapshot
     still missed something.
   - Delete partial backups `db/kami-oracle.duckdb.session2.bak`
     and `db/kami-oracle.duckdb.session2p5.bak` once coverage is
     confirmed healthy.

3. **Launch co-hosted serve (Part 3 of session 3 brief, deferred):**
   Only after step 2's acceptance gate passes.
   ```
   screen -dmS kami-oracle -L -Logfile ~/kami-oracle/logs/serve.log \
     bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.serve'
   ```
   Verify (from a second SSH):
   ```
   curl -s http://127.0.0.1:8787/health | jq .
   curl -s http://127.0.0.1:8787/registry/snapshot | jq '.n_addresses'
   curl -s 'http://127.0.0.1:8787/operator/0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2/summary?since_days=7' | jq .
   ```
   Watch `cursor.last_block_scanned` advance across successive
   `/health` calls to confirm the poller thread is alive. Commit
   health snapshot + bpeon-op summary to
   `memory/decoder-notes.md` under a new "Session 3.5 — serve
   launch" section.

4. **Start the daily prune:** `python -m ingester.prune` enforces
   the 7-day rolling window. Add to a cron / schedule. Not needed
   immediately (≤1 week of data by definition at backfill end).

### Deferred (not session 3.5 unless human asks)

- `kami_static` backfill worker (per-kami trait snapshots via
  GetterSystem). Waiting on steady-state ingest.
- Upstream ABI re-vendoring (equip, trade, marketplace, kami721).
  Wait for a kamigotchi-context release unless something critical
  surfaces in `memory/unknown-systems.md`.
- Concurrency refactor for faster backfills. Single-threaded
  pipeline is ~2.4 blocks/s; a thread-pooled get_block + receipt
  could 3–5x this. Not worth it for Stage 1.
- ~400 `system.quest.accept` selector `0x09c90324` entries in
  `memory/unknown-systems.md` (appended during session 3 backfill).
  Tier B per overlay policy — needs either a `system-ids.md`
  update from upstream or deployed-bytecode confirmation of the
  signature before we can add an overlay.
- Phase D (hosted endpoint + auth layer + MCP). The co-hosted
  serve is the Stage-1 stand-in bound to loopback; promoting it
  to a public endpoint is an ADR-gated decision, not a mechanical
  change.
