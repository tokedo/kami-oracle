# Session 10 — Verification Report

## Service health (live now)

- `systemctl is-active kami-oracle`: active
- Public-URL probe: `curl -s https://136-112-224-147.sslip.io/health | jq .`
  - cursor lag: 8358s at probe time — service was stopped 2026-04-27
    14:28:57 → 16:48:39 UTC (verification restart, ~2h20m gap); cursor
    is unwinding, advancing on each probe. No data loss; the chain
    tail is replayed idempotently from the last committed block.
  - last block: 28141490 (advancing)
  - total_actions (`kami_action`): 355,525
  - schema version: 4

## Part 1 — Discovery

- [x] `memory/decoder-notes.md` has "Session 10 — build fields on chain"
- [x] Each field listed with its chain source (level/xp from `getKami`,
  total_* via the canonical formula on `getKami.stats.*`, slots from
  `SlotsComponent.safeGet`, skills via IDOwnsSkill + IndexSkill +
  SkillPoint, equipment via IDOwnsEquipment + IndexItem)
- [x] Bpeon fixture kami's raw build dump captured
  (`memory/session-10-discovery.txt`, also reproduced inline in
  decoder-notes)
- [x] Any field that couldn't be read from chain documented + handled
  (`component.for.string` for slot-name resolution did not resolve;
  equipment_json stores item indices only — explicit deferral noted)
- [x] Commits: `cf6ae43` (docs: session 10 discovery)

## Part 2 — Schema migration

- [x] Migration file added: `migrations/004_add_build_columns.py`
- [x] `schema/schema.sql` reflects post-migration state with comment block
- [x] `SCHEMA_VERSION` bumped 3 → 4
- [x] Migration applied cleanly on service start (no errors in serve.log;
  log line: `migration 004 complete: {'columns_added': 10,
  'columns_total': 10}`)
- [x] Final column list (29 total in `kami_static`, 10 new):
  level, xp, total_health, total_power, total_violence, total_harmony,
  total_slots, skills_json, equipment_json, build_refreshed_ts
- [x] Commits: `7bd3d13` (harness: schema migration v4)

## Part 3 — Populator + backfill

### 3a. Populator
- [x] `ingester/kami_static.py` fetches build fields per kami via the
  new `KamiStaticReader._fetch_build_extras` path; level/xp + effective
  totals collapsed in `_kami_shape_to_static` directly from getKami
- [x] No silent recomputation — totals come from chain getters and the
  canonical game formula `floor((1000+boost)*(base+shift)/1000)`
  documented in kamigotchi-context state-reading.md
- [x] Tests pass — `101 passed, 12 skipped, 1 warning in 19.82s`
  (4 new tests in `tests/test_build_fetch.py`)
- [x] Bpeon fixture asserts match Part 1 raw dump (Zephyr round-trip:
  level=37, total_health=230, total_power=16, total_harmony=19,
  total_violence=17 — verified in
  `test_kami_shape_round_trip_zephyr_fixture`, and re-confirmed
  post-backfill by direct DB read: kami_index=43, name=Zephyr,
  account=bpeon, level=37, xp=136367, hp=230, pw=16, vio=17, harm=19,
  slots=0)
- [x] Commits: `8d40896` (harness: kami_static populator — fetch
  level/xp/total_*/skills/equipment)

### 3b. Backfill
- [x] Pre-backfill NULL `build_refreshed_ts` count: 6,600
- [x] Post-backfill NULL `build_refreshed_ts` count: 0
- [x] Failed-fetch count: 0
- [x] Anomaly count (e.g. total < base): 0 (verified
  `SELECT COUNT(*) FROM kami_static WHERE total_health < base_health`
  returns 0)
- [x] Distribution sanity-check query result (harmony bucketed by 20):

  | harmony_bucket_low | kamis | avg_harmony |
  |---|---|---|
  | 0  | 1,231 | 18 |
  | 20 | 5,789 | 23 |

  Range: total_harmony min=10, max=37, avg=22. Level: min=1, max=56,
  avg=33. total_health: min=50, max=420, avg=198. total_power:
  min=10, max=38. Non-degenerate; values track expected mid-game
  population.

- [x] Final coverage on `kami_static` (7,020 rows total):

  | column | populated |
  |---|---|
  | build_refreshed_ts | 7,020 (100%) |
  | total_health | 7,020 (100%) |
  | total_power | 7,020 (100%) |
  | skills_json | 7,020 (100%) |
  | equipment_json | 7,020 (100%) |

- [x] Backfill duration: 14:31:36 → 16:35:49 UTC (~107 min, ~1.0
  kami/s, 200-row commit batches). Survived the SSH disconnect at
  ~14:28; ran to completion in the background. Final log line:
  `backfill_kami_build done: pre_pending=6600 post_pending=0 ok=6600
   fail=0 partial=0 anomaly_total_lt_base=0`
- [x] Commits: `scripts/backfill_kami_build.py` was added in `0dcacfe`
  (folded into the docs commit rather than a standalone `data:`
  commit — minor prefix lapse, not worth amending).

## Part 4 — Documentation

- [x] README "Build snapshot in `kami_static` (Session 10)" section added
- [x] decoder-notes "Session 10 — build fields on chain" promoted to a
  permanent section
- [x] CLAUDE.md schema summary updated — `kami_static` row now lists
  the 10 new build columns + the canonical formula
- [x] Commits: `0dcacfe` (docs: README + CLAUDE.md + next-steps)

## Part 5 — Colab ride-along diff

- [x] `memory/next-steps.md` has "Hand-off to human (blocklife-ai)"
  section with: schema cheat-sheet update, top-earners × build query,
  meta-clustering starter query, build-vs-performance one-liners
  (harmony bracket avg MUSU, violence bracket liquidations, skill
  concentration by operator)

## Coverage check (regression guard)

Re-run of harvest_* coverage post-Session-10:

| action_type | total | null_amount |
|---|---|---|
| harvest_collect   | 4,586   | 25      |
| harvest_stop      | 156,528 | 25,296  |
| harvest_liquidate | 1,250   | 186     |
| harvest_start     | 133,215 | 133,215 |

Delta vs Session 9 baseline:
- harvest_collect: 4,084 → 4,586 (+502 rows, null unchanged at 25) — better
- harvest_stop: 138,756 → 156,528 (+17,772 rows, +1,767 nulls;
  83.0% → 83.8% populated) — better
- harvest_liquidate: 1,093 → 1,250 (+157 rows, +47 nulls; 87.3% → 85.1%
  populated) — slight pct dip from new live-tail rows not yet enriched;
  same pattern Session 9 noted, not a regression in already-populated
  rows
- harvest_start: 117,296 → 133,215 (+15,919, all null by design — no
  payout on start) — unchanged behavior

Account-name coverage (Session 9 regression):

- distinct_accounts: 150 (was 146 in Session 9)
- with_account_name: 150 (was 146 in Session 9)

Both 100% coverage. No regression.

## Known issues

- Equipment slot-name resolution deferred. `component.for.string` for
  slot-name lookup did not resolve in the current registry, so
  `equipment_json` stores item indices only (no slot label). Schema
  is forward-compatible — slot names can be backfilled later without
  a migration. Acceptable for Stage-1 meta clustering.
- `total_slots` resolves to 0 across the entire population. Confirmed
  expected per `kami_context/equipment.md`: no skill or item grants
  slots in the current game balance, so capacity = 1 implicitly. Not
  a fetch bug; column kept for forward compat.
- Daily-sweep cost: backfill ran at ~1.0 kami/s, so a full 7,020-kami
  sweep is ~117 minutes of RPC time. Not blocking but tracked for
  Session 11 — if the daily sweep becomes a contention point, switch
  to event-triggered refresh on `skill_upgrade` / `equip` / `lvlup`
  per the Session 11 candidate list in `next-steps.md`.
- Service was inactive when the audit started (SSH disconnect during
  Session 10 left the service stopped at 2026-04-27 14:28:57 UTC).
  Restarted cleanly at 16:48:39 UTC; cursor unwinding the ~2h20m
  gap. No data loss — replay is idempotent.

## Status

✅ **Session 10 complete. Build snapshot populated across `kami_static`
(7,020/7,020 = 100% coverage on every build column, 0 failed fetches,
0 anomalies); founder can now join performance × build for meta
clustering. Schema v4 live, service active, no regression on
Session-9 coverage. Colab notebook can refresh with the new example
queries from the `next-steps.md` hand-off.**
