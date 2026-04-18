# Next Steps

## Session 3 — pick up from here

### Current state at session 2.5 end (2026-04-18 ~14:00 UTC)

- Session 2.5 wrapped three goals: investigation of the action-mix
  mystery, ingester hardening against transport faults, and shrinking
  the rolling window from 4 weeks to 1 week. See
  `memory/improvements.md` for commit hashes.
- **1-week backfill is running** in detached screen session
  `kami-backfill`. Log at `logs/backfill.log`. Target: 420k blocks
  (head-0..head-420k). Start block 27,391,858; projected ~2 days
  at 2.4 blocks/sec.
- DB file: `db/kami-oracle.duckdb` (fresh, bootstrapped by the
  backfill launch). Session-2 DB backed up at
  `db/kami-oracle.duckdb.session2.bak` — keep until session 3
  confirms healthy data, then delete.
- Poller NOT running. Do not start until backfill finishes.
- `questions-for-human.md` is empty.

### Headline finding from session 2.5 — registry-snapshot bug

The session-2 backfill's partial DB showed move/item_craft/skill_upgrade
dominating and harvest ~0% — completely different from head
validation. Root cause is NOT a decode bug: **harvest system
contracts are redeployed periodically** (at least 4 distinct
harvest.start addresses appeared across a 22-day probe). Our
ingester resolves system addresses once at startup against head, so
historical harvest txs targeting *older* deployments were silently
dropped at the match-to-system step (they never even reached the
decoder).

Most recent harvest.start redeployment was within the last ~2.3
days. Even the 1-week backfill will miss ~2/3 of historical harvest
txs until this is fixed.

Full analysis + proposed fix in `memory/decoder-notes.md` under
"Investigation: action-mix divergence (session 2.5)".

### Do in this order

1. **Check backfill health.** The session-2.5 outer survival loop
   should keep it alive through transient RPC faults, but verify.
   ```
   screen -ls                            # kami-backfill alive?
   tail -200 logs/backfill.log           # recent chunks + any resume logs
   grep -c "done (actions" logs/backfill.log   # chunk count
   grep "backfill: unhandled" logs/backfill.log  # any resume events?
   ```
   Don't open the DuckDB file while the writer is running — exclusive
   file lock. Infer progress from chunk logs.

2. **If backfill is complete (cursor reached head-at-launch):**
   - Capture summary: row counts, action-type distribution, unique
     kami count, unique operator count, block range, wall time.
     Commit as `data: 7-day backfill complete`.
   - **Validate against head**: scan last 2000 blocks with
     `scripts/validate_decode.py` and compare its action mix to the
     DB's last-2000-block slice. Should match closely; a mismatch
     suggests the registry-snapshot bug is still biting.
   - **Prime task: implement the registry-snapshot fix** (see below)
     *before* starting the poller. Otherwise the poller's live data
     is clean but the historical backfill portion will continue to
     underreport harvest.
   - Once fix is in, re-run backfill with `--days 7` (idempotent) to
     pick up missed harvest txs.

3. **If backfill is still running:**
   - Don't touch it. Don't start the poller.
   - Work on the registry-snapshot fix (design/sketch/tests) against
     a scratch DB. Keep hands off `db/kami-oracle.duckdb`.

### Session 3 prime task — registry-snapshot fix

**Problem**: `system_registry.resolve_systems()` runs once at
startup, at head. Historical backfill uses those head addresses to
filter `to_addr`, so any system redeployed inside the backfill
window leaks txs at the match step (silently — no entry in
`memory/unknown-systems.md`).

**Proposed fix (option 3 from decoder-notes)**: At backfill startup,
probe the registry at ~10 evenly-spaced block heights across the
target window. Union the resulting system-ID → address sets into a
multi-address-per-system registry. The filter becomes "match if
to_addr is in {current or any historical address for this system}";
the decoder selects the right ABI by system_id regardless of which
address was the match. One-time startup cost (~350 RPC calls, ~2
minutes), no mid-run overhead.

Sketch:
- Extend `SystemRegistry` to hold `addresses: set[str]` per system
  (currently one).
- Add `probe_historical_systems(client, world_address, abi_dir,
  block_heights: list[int])` that calls `resolve_systems` at each
  height (via `block_identifier=N`) and unions the results.
- Call it from `backfill.main` once `start` and `head` are computed,
  with `block_heights = linspace(start, head, 10)`.
- Poller path unchanged: poller tails forward from head so only the
  current registry matters (but the API is still safe to use —
  historical probing returns the current set unioned with nothing).

Write tests that exercise a two-address case: ensure decode still
routes by system_id and that the extra address shows up in
`known_addresses()`.

After landing, **re-run the backfill from scratch** on a wiped DB
to get a clean representative sample. Commit the rerun results as
`data: 7-day backfill complete (registry-snapshot fix)`.

### Deferred (not session 3 unless human asks)

- `kami_static` backfill worker (per-kami trait snapshots via
  GetterSystem). Waiting on steady-state ingest.
- Upstream ABI re-vendoring (equip, trade, marketplace, kami721).
  Wait for a kamigotchi-context release unless something critical
  surfaces in `memory/unknown-systems.md`.
- Concurrency refactor for faster backfills. Current single-threaded
  pipeline is ~2.4 blocks/s; a thread-pooled get_block + receipt
  could 3–5x this. Not worth it for Stage 1.
- ~80 `system.quest.accept` selector `0x09c90324` entries in
  `memory/unknown-systems.md`. Tier B per overlay policy — needs
  either a system-ids.md update from upstream or deployed-bytecode
  confirmation of the signature before we can add an overlay.
