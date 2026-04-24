# Next Steps

## Session 4 — pick up from here

### Current state at session 3.5 end (2026-04-24 21:xx UTC)

- **Acceptance gate passed.** 7-day backfill (actually 10.6 days,
  27,550,126..27,970,125) finished clean at 2026-04-24 18:56 UTC:
  420,001 blocks, 478,358 actions, **81.6% harvest coverage**, 7,019
  unique kami in harvest, 0 survival-loop resumes across the 49.9 h
  run. Validator cross-check (live last 2000 blocks) returned 0
  unknown selectors and 0 decode errors. See
  `memory/decoder-notes.md` → "Session 3.5 backfill summary".
- **Co-hosted serve is live** in screen `kami-oracle` on
  `127.0.0.1:8787`. All 8 endpoints verified; bounds caps
  (`MAX_SINCE_DAYS=28`, `MAX_LIMIT=2000`) reject with 422. Cursor
  was advancing normally at session end. See
  `memory/decoder-notes.md` → "Session 3.5 serve launch health
  check" for the 10-min cursor-advance snapshot.
- **Founder-testing guide** at `memory/founder-testing.md` —
  SSH-tunnel command, endpoint catalog, caveats, stop/restart.
- **Poller catchup**: at session-3.5 end the poller was catching up
  ~76k blocks between backfill end (27,970,125) and head
  (~28,046,946). At ~2.35 blk/s that's ~9 h of catchup. By the time
  Session 4 starts, cursor should be at or near head and `/health`
  will show `last_block_scanned` within a few blocks of chain head.
- **Unknown-systems.md**: 1,137 new rows appended during backfill,
  all selector `0x09c90324` (1,134 against `system.quest.accept`,
  3 against `system.account.use.item`). Tier-B candidate, pending
  signature confirmation. Committed in `b33501e`.
- `questions-for-human.md` is empty.

### Do in this order

**Priority 1 — `kami_static` backfill worker.** Deferred since
Stage 1 kickoff. `kami_static` is still empty (confirmed via
`/health`). Per-kami traits (body, hand, face, background, base
stats, owner) are needed for any analysis that groups by kami
archetype. The ingest side is harvest-dominated event data;
`kami_static` is the slowly-changing dimension that turns action
rows into meaningful segments. Approach: enumerate distinct
`kami_id`s from `kami_action`, read via `GetterSystem` at latest
head, upsert into `kami_static` with `last_refreshed_ts`.
Read-only, idempotent; should coexist with the live poller
(DuckDB single-writer → run inside the serve process as a third
thread, OR stop serve briefly and run as a one-shot). A one-shot
with serve stopped is simpler for first cut.

**Priority 2 — daily rollup job.** `/actions/types?since_days=N`
full-scans `kami_action` on every call; for 478k rows it's still
fast (<120 ms), but at steady-state with 2-4× that it'll matter.
Precompute a `daily_action_rollup(date, action_type, count)` table
and have `/actions/types` union recent rollup rows with a partial
scan of the current day. Similar shape for `/nodes/top`.
Materialize via a periodic job (the existing `ingester.prune`
hook point is a natural place).

### Deferred (not Session 4 unless human asks)

- **Tier-B overlay batch** for selector `0x09c90324`. Needs either
  a `kami_context/system-ids.md` bump that documents the signature,
  or a deployed-bytecode decompile to confirm. Do not guess.
- **Cross-action stitching** — materialize `harvest_start →
  harvest_stop → harvest_collect` chains keyed by `harvest_id` so
  founder queries don't need window-function joins. Nice-to-have;
  not blocking analysis.
- **Scheduled prune worker** for the 7-day rolling window.
  Currently manual (`python -m ingester.prune`). At 10.6 days of
  data it's not urgent but will become so within a week.
- **Concurrency refactor** for faster backfills. Single-threaded
  pipeline is ~2.35 blocks/s; a thread-pooled
  `get_block + receipt` could 3–5× this. Not worth it for Stage 1;
  backfill frequency is low.
- **Upstream ABI re-vendoring** (equip, trade, marketplace,
  kami721). Wait for a kamigotchi-context release unless a new
  critical selector shows up in `memory/unknown-systems.md`.
- **Phase D** (public endpoint + auth layer + MCP). ADR-gated,
  human-authored decision. `_parse_bind` in `ingester/serve.py`
  blocks non-loopback binds as a defensive backstop.

### Operational notes

- DB file: `db/kami-oracle.duckdb`. DuckDB holds a per-process
  exclusive lock — only one of `{serve, poller, backfill, prune}`
  may hold it. To work on the DB directly (read-only), the serve
  process must be stopped first.
- Backup files: `db/kami-oracle.duckdb.session2.bak` and
  `db/kami-oracle.duckdb.session2p5.bak` can be deleted now that
  Session 3.5 has confirmed clean data. The next session can safely
  `rm` both.
- Serve restart: see `memory/founder-testing.md` → "Stopping the
  service cleanly".
