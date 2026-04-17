# Next Steps

## Session 3 — pick up from here

### Current state at session 2 end (2026-04-17 21:16 UTC)

- All Session 1/2 ABI questions resolved; decode coverage 100% on
  recent blocks.
- 3-tier overlay policy codified in CLAUDE.md.
- **4-week backfill is running** in detached screen session
  `kami-backfill` (PID 7525 at launch). Log at `logs/backfill.log`.
  Expected runtime ~7.7 days over ~980k reachable blocks. First
  reattach command:
  ```
  screen -r kami-backfill        # Ctrl+a, d to detach again
  tail -f logs/backfill.log
  ```
- Poller is **not** running; do NOT start it until backfill is done
  (DuckDB single-writer constraint). See `memory/ops.md` for commands.
- `questions-for-human.md` is empty (no blockers).

### Session 2 finding — RPC retention limit (no action blocks Session 3)

Public Yominet RPC at
`jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz` retains **~22.7
days (~3.24 weeks)** of history, not the 28 days CLAUDE.md prescribes.
`backfill.py` probes the edge and clamps automatically; data reachable
is ~22 days plus whatever accumulates via the poller tail going
forward. Human decision (whenever — not session-3 blocking): accept the
shorter window, set up an archive RPC, or let the oracle grow locally
over months of poller uptime. Full discussion in
`memory/decoder-notes.md` under "RPC retention limit".

### Do in this order

1. **Check backfill health.**
   ```
   screen -ls                            # confirm kami-backfill alive
   tail -200 logs/backfill.log           # recent chunk messages
   .venv/bin/python -c "import duckdb; c=duckdb.connect('db/kami-oracle.duckdb', read_only=True); print(c.execute('SELECT last_block_scanned, last_block_timestamp FROM ingest_cursor').fetchone()); print('raw_tx:', c.execute('SELECT COUNT(*) FROM raw_tx').fetchone()); print('actions:', c.execute('SELECT COUNT(*) FROM kami_action').fetchone())"
   ```
   If it died mid-run, resume with
   `python -m ingester.backfill --from-block <cursor+1> --to-block <original_head>`.
   If cursor is near head, backfill is close to done.

2. **If backfill is complete (cursor reached head-at-launch):**
   - Capture summary: row counts, action-type distribution, unique
     kami count, unique operator count, block range, wall time.
     Commit as `data: 4-week backfill complete`.
   - Start the continuous poller in screen:
     ```
     screen -dmS kami-poller -L -Logfile logs/poller.log \
       bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.poller'
     ```
     Document in `memory/ops.md`.
   - Run the prune daily cron equivalent (or a one-shot test):
     `python -m ingester.prune`.

3. **If backfill is still running:**
   - Don't touch it. Don't start the poller.
   - Do one or more of the read-only tasks below; return to the
     backfill-health check at start of next session.

### Read-only analytics tasks (safe while backfill runs)

`duckdb.connect(..., read_only=True)` is safe to run concurrently with
the writer process. Useful sanity checks:

- Action-type distribution over last 24h of backfilled data.
- Top-20 operator wallets by action count.
- Harvest-cycle stitching: join `harvest_start` → `harvest_stop` /
  `harvest_collect` on `metadata.harvest_id` to recover the missing
  `kami_id` on stop/collect rows (prototype for the Phase B enrichment
  step).
- Any new unknown selectors in `memory/unknown-systems.md`? Apply
  Tier-A/B/C per CLAUDE.md overlay policy.

### Deferred (not Session 3 unless human asks)

- `kami_static` backfill worker (per-kami trait snapshots via
  GetterSystem) — waiting on steady-state ingest.
- Several system ABIs absent from the vendored snapshot (equip,
  trade, marketplace, kami721/portal). Wait for upstream re-vendoring
  unless they surface in `memory/unknown-systems.md`.
- Concurrency refactor for faster backfills. Current single-threaded
  pipeline is 2.4 blocks/s; a thread-pooled get_block / receipt fetch
  could 3–5x this. Not worth it for Stage 1 proof-of-concept.
