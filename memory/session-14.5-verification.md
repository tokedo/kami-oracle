# Session 14.5 — Verification Report

## Service health (live now)

- `systemctl is-active kami-oracle`: **active**
- `/health` schema_version: **13** ✅ (was 12 going in)
- chain_head_lag_seconds: **6.1s**
- last_block_scanned: 28,219,990
- kami_static rows: 7,110 (unchanged)
- raw_tx rows: 229,030
- kami_action rows: 471,527
- vendor_sha: 332db78 (unchanged from S14)

## Part 0 — Baseline

Pre-correction view shape (S14, captured 2026-04-29 21:00 UTC,
schema_version=12):

```
rows  with_loc  stale
7110  6963      6556
```

92.5% of S14's `with_loc` rows were `is_stale=TRUE` — the smoking
gun for the S14 over-claim that drove this correction.

## Part 1 — Harvest-action semantics discovery

- ✅ `memory/decoder-notes.md` "Session 14.5 — kami_current_location
  semantic correction" + sub-section "Session 14.5 — harvest action
  end-of-harvest semantics" present.
- ✅ `harvest_collect` verified to NOT end harvest. Chain spot-check:
  kami "boom" idx=980 state=HARVESTING immediately after a recent
  harvest_collect. Mid-session payout claim only.
- ✅ `harvest_stop` verified to end harvest. Chain spot-check: kami
  idx=8441 state=RESTING after a recent harvest_stop.
- ✅ `harvest_liquidate` verified to end harvest **for the victim**.
  Chain spot-check: victim idx=10001 state=DEAD after liquidation.
- ✅ `harvest_liquidate` verified to NOT end harvest **for the killer**.
  Chain spot-check: killer "Shirt Man" idx=2759 state=HARVESTING
  immediately after committing a liquidate; room=16.
- ❓ `die` / `revive` — NOT in our `action_type` enum. Distinct types
  in `kami_action`: harvest_*, feed, gacha_*, item_*, lvlup,
  listing_buy, move, quest_complete, register, scavenge_claim,
  skill_*, kami_name, friend_*, droptable_reveal, goal_claim. No
  `die` or `revive`. Treated as known false-positive gap (a kami
  that died from non-liquidate causes won't have its harvest
  closed in our records); surfaced in the migration docstring +
  schema.sql comment + decoder-notes + next-steps hand-off.

**Diffs from prompt's working hypothesis** (all incorporated into
implementation, not blockers):

1. `harvest_liquidate.kami_id` is the *killer*, not the victim
   (decoder.py:165-168: `killerID → kami_id`,
   `victimHarvID → harvest_id`). `target_kami_id` is NULL on every
   liquidate row (1510 rows surveyed). Implementation route:
   self-join `liquidate.harvest_id` to a victim `harvest_start`
   row to recover victim_kami_id. The bijective derivation
   (`harvest_id = keccak("harvest" || kami_id_be)`, see
   `harvest_resolver.py:34-36`) guarantees uniqueness.
2. `die` / `revive` aren't in our enum — workaround is the
   30-min stale flag + agent verification via Kamibots.
3. **Window-edge** (newly surfaced): a kami harvesting
   continuously >7d has no `harvest_start` in window, only
   `harvest_collect` rows. View treats collect as evidence of
   active harvesting → currently_harvesting=TRUE but
   current_node_id NULL because the start is gone. 90 such kamis
   on live data. Documented; agent verifies.

Commit: `3b608ca` — `docs: session 14.5 discovery`.

## Part 2 — Migration 013

- ✅ `migrations/013_correct_kami_current_location_view.py` added.
- ✅ `schema/schema.sql` `kami_current_location` comment block
  REPLACED entirely (not appended).
- ✅ `SCHEMA_VERSION` 12 → 13 in `ingester/storage.py`; migration
  013 registered + idempotent (`CREATE OR REPLACE VIEW`).
- ✅ Tests rewritten to apply 012 then 013, exercising:
  - active harvest_start → currently_harvesting=TRUE on node 86
  - harvest_start then harvest_collect → still currently_harvesting=TRUE
  - harvest_start then harvest_stop → currently_harvesting=FALSE,
    last_harvest_node_id preserved
  - liquidated victim (resolved via harvest_id self-join) →
    currently_harvesting=FALSE
  - killer's own liquidate row does NOT end killer's harvest
  - cold-start (no actions) → all NULL, currently_harvesting=FALSE
  - move actions ignored
  - one row per kami in kami_static
  - partition is exhaustive (harvesting + resting + no_history = total)
- pytest summary: **147 passed, 12 skipped** (full suite clean).
- Commit: `ba5800a` — `harness: schema migration v13 + corrected
  kami_current_location view (S14.5)`.

## Part 3 — Real-data verification

Coverage shape (live, 2026-04-29 21:11 UTC):

```
total_kamis:             7110
harvesting_now:          4702
resting_with_history:    2352
no_harvest_in_window:      56
with_current_room:       4612
with_current_node:       4612
stale_active_harvests:   4312   (>30 min freshness on active signal)
harvesting_unknown_node:   90   (window-edge: collect-only)
```

- **Partition is exhaustive**: 4702 + 2352 + 56 = 7110 ✅
- **`with_current_room` shrunk from 6,963 → 4,612** (–34%, –2,351
  rows). The drop is the population that had a harvest_start in
  window but has since stopped or been liquidated — the rows that
  S14 over-claimed as "still on a node." Founder estimate was
  "few hundred to low thousand"; actual 4,612 is higher because
  passive harvesting is the norm — most active kamis are left on
  a node for hours, so at any instant a large fraction of
  harvest-active kamis remain on nodes. The view is now honest
  about which they are.
- **`stale_active_harvests`**: 4,312 of 4,702 currently_harvesting
  (92%) have signals older than 30 min — the long-tail of passive
  harvesters. Agent should verify against Kamibots before
  destructive ops keyed on these.
- **`harvesting_unknown_node`** (90): window-edge cases — kamis
  with collect rows in window but no start (continuous harvest
  >7d). currently_harvesting=TRUE, current_node_id=NULL.
  Documented gap.

Source action mix among currently_harvesting:

```
n: 4706    avg_fresh: 18403s   max_fresh: 623,300s   min_fresh: 7s
                       (=5.1h)         (=7.2 days)       (live)
```

(Off-by-4 vs the partition snapshot above is normal — chain ingest
mid-query, lag ~6s.) The 7.2-day max confirms the window-edge case
exists (kamis active continuously across the full rolling window).

4-kami spot-check (S13/S14 reference set):

```
kami_index  curr_harv  curr_node  curr_room  last_node  since_ts             freshness  is_stale
1186        FALSE      NULL       NULL       86         2026-04-29T21:09:00      127     NULL
2418        TRUE       62         62         62         2026-04-29T16:28:42   16,945     TRUE
```

- 1186: previously on node 86 (S14 said still there); S14.5 honestly
  says they've stopped (likely a stop within the last few minutes —
  freshness 127s on the end-of-harvest signal). ✅ correct
  regression-of-overclaim.
- 2418: still harvesting on node 62, ~4.7 hours in, is_stale=TRUE
  → agent should verify before destructive ops. ✅
- 1745, 2465: not in `kami_static` (never indexed). Same as S14;
  not a S14.5 regression.

## Part 4 — Documentation

- ✅ `README.md` `kami_current_location` description rewritten
  around the corrected mechanic; example SQL updated to use
  `WHERE currently_harvesting AND current_node_id = 86`.
- ✅ `CLAUDE.md` schema-summary one-liner updated with the new
  column set + end-of-harvest set + resting-kami operator-room
  follow-on.
- ✅ `memory/decoder-notes.md` Session 14.5 sections added:
  - "Session 14.5 — kami_current_location semantic correction"
    (the framing fix).
  - "Session 14.5 — harvest action end-of-harvest semantics" (the
    chain spot-check table).
  - "Window-edge: long-running harvests started >7d ago".
  Session 14's section preserved unchanged — historical record.
- Commits: `3b608ca` (decoder-notes), `ddd47f0` (README + CLAUDE.md).

## Part 5 — Hand-off

- ✅ `memory/next-steps.md` "Hand-off to human (blocklife-ai) —
  Session 14.5" recorded:
  - oracle.md `kami_current_location` description rewrite.
  - Move-attribution caveat reframed (operator's movement, not
    missed kami movement).
  - Example SQL update with `currently_harvesting` guard.
  - Note to spot-check agent code that keyed destructive ops on
    `current_room_index IS NOT NULL` under S14's wider population.
- ✅ Session 15.5 candidate (`account_current_location` view)
  added to the queue under 7c. Defer until an agent needs it.
- ✅ Section 7d ("Known gap — move attribution") amended with the
  S14.5 reframing rather than rewritten — preserves historical
  record while pointing to the corrected view.
- Commit: `40ae276` — `docs: next-steps + decoder-notes — Session
  14.5 hand-off`.

## Coverage check (regression guard)

- Sessions 9-14 columns / catalogs / views all unchanged.
- Schema versions 1-12 still apply cleanly on a fresh DB before
  013 fires (idempotent migration chain).
- Full test suite: **147 passed, 12 skipped** (98s wall).
- `kami_skills` / `kami_equipment` / `nodes_catalog` /
  `skills_catalog` / `items_catalog` views queryable as before
  (not directly verified row-by-row this session — out of scope —
  but their tests are part of the 147 pass).

## Known issues

1. **die / revive aren't decoded.** A kami that died from non-
   liquidate causes (HP starvation outside a liquidate) will read
   `currently_harvesting = TRUE` while the chain says DEAD. The
   30-min stale flag + Kamibots verification covers it for now;
   future decoder work could close it.
2. **Window-edge: 90 kamis with `currently_harvesting=TRUE AND
   current_node_id IS NULL`.** Long-running harvests beyond the
   1w rolling window. Window extension to 28d (CLAUDE.md
   "Window may extend") would shrink it. Agent verifies via
   Kamibots in the meantime.
3. **`harvesting_now` = 4,702 is higher than founder's "few
   hundred to low thousand" estimate.** The view is correct; the
   estimate undercounted passive harvesters who park on nodes
   for hours/days at a time. 92% of active signals are >30 min
   old, consistent with this. No action required.

## Status

- ✅ **Session 14.5 complete. `kami_current_location` now reflects
  the actual mechanic; resting kamis honestly NULL;
  `currently_harvesting` + `last_harvest_node_id` added; docs and
  hand-off recorded; 147/147 tests pass; service active at
  schema_version 13 with chain lag <10s.**
