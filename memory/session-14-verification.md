# Session 14 — Verification Report

## Service health (live now, 2026-04-29 20:33 UTC)

- `systemctl is-active kami-oracle`: **active**
- `/health` schema version: **12** (target was 12 ✅)
- cursor lag: **10.0s**
- last block: **28219081**
- kami_static rows: **7110**
- items_catalog rows: **177** (Session 13)
- skills_catalog rows: **72** (new — Session 14)
- nodes_catalog rows: **64** (new — Session 14)

## Part 0 — Orient + baseline

- [x] Session 13 commits in (`6fe08b3 docs: session 13 verification report`,
      `2987177 docs: next-steps`, `c35c8c6 docs: README + CLAUDE.md`,
      `5921333 harness: schema migration v8 + kami_equipment view`,
      `6d4a8bc harness: schema migration v7 + items_catalog table + loader`,
      `ea96f5e docs: session 13 discovery`).
- [x] Service active, schema_version 8 going in.
- [x] Cursor lag baseline: 16.9s.
- [x] Baseline counts captured: total=7110, with_skills=7108,
      kamis_with_action=7103.

## Part 1 — Discovery

- [x] `memory/decoder-notes.md` "Session 14 — skills_catalog +
      nodes_catalog + views" promoted (commit `385b487`).
- [x] **skills.csv shape**: 72 data rows, 4 trees (Predator,
      Guardian, Harvester, Enlightened — 18 each), 16 effect keys
      (all in documented taxonomy: SHS/SPS/SVS/SYS/HFB/HIB/HBB/
      ATS/ATR/ASR/DTS/DTR/DSR/RMB/SB/CS), 24 mutually-exclusive
      tier-3/tier-6 entries. Leading blank column is BOM + dot —
      `csv.DictReader` with `utf-8-sig` handles it.
- [x] **nodes.csv shape**: 64 in-game rows (all `Status='In Game'`
      in this snapshot). Indices sparse 1..90. Columns: Index,
      Name, Status, Drops, Affinity, Level Limit, YieldIndex,
      Scav Cost — no room column.
- [x] **node→room discovery**: path (b) — every node in nodes.csv
      has a same-Index, same-Name row in
      `kami_context/catalogs/rooms.csv`. Verified across all 64
      in-game nodes: zero name mismatches, zero index mismatches.
      → `room_index = node_index` for every in-game node. No
      chain getter needed.
- [x] **move-attribution gap surfaced**: `kami_action.move` rows
      have `kami_id = NULL` because `system.account.move` is
      account-level on chain. ~10k move rows vs ~390k harvest_*
      in 28d window. Decision: drop `move` from
      `kami_current_location` source action types; rely on
      harvest_* only. Documented in decoder-notes Session 14
      section.
- [x] **vendor-context.sh fix**: pulls all of `catalogs/` now
      (catch-up for nodes.csv / rooms.csv / recipes.csv /
      scavenge-droptables.csv / shop-listings.csv).
- [x] Commits: `a0dfe30 harness: vendor-context.sh — pull all
      of catalogs/ (Session 14 catch-up)`, `385b487 docs: session
      14 discovery — skills.csv + nodes.csv shape +
      move-attribution gap`.

## Part 2 — skills_catalog

- [x] `migrations/009_add_skills_catalog.py` added.
- [x] `schema/schema.sql` updated with `skills_catalog` table block.
- [x] `SCHEMA_VERSION` 8 → 9 (in `ingester/storage.py`).
- [x] Loader `ingester/skills_catalog.py` added (plural to match
      table name; doesn't collide with existing in-memory
      `skill_catalog.py` singular). Wired into `ingester/serve.py`
      startup (idempotent skip when populated).
- [x] Loader test passes — `tests/test_skills_catalog.py`:
      `6 passed in 0.75s` (test_parse_skills_csv,
      test_load_skills_catalog_into_db, test_load_is_idempotent,
      test_ensure_loaded_skips_when_populated,
      test_ensure_loaded_loads_when_empty, test_missing_csv_raises).
- [x] Initial load: **72 rows / 4 trees / 18 skills each**.
- [x] Effect distribution (16 keys):

      | effect | n |
      |--------|--:|
      | SHS | 8 |
      | SB  | 7 |
      | HFB | 6 |
      | HBB | 6 |
      | DTS | 6 |
      | HIB | 6 |
      | SVS | 5 |
      | DSR | 4 |
      | SPS | 4 |
      | SYS | 4 |
      | ATS | 3 |
      | ASR | 3 |
      | RMB | 3 |
      | CS  | 3 |
      | ATR | 2 |
      | DTR | 2 |

      All in the documented 16-key taxonomy (Session 11 reference).
- [x] Spot-check: skill 212 = Cardio (Enlightened tier 1, SHS,
      "10" Stat → "+10 HP/rank"); skill 222 = Meditative Breathing
      (Enlightened tier 2, DTS, "0.02" Percent → "+2% DTS/rank"). ✅
- [x] Commits: `72bc330 harness: schema migration v9 +
      skills_catalog table + loader`.

## Part 3 — kami_skills view

- [x] `migrations/010_add_kami_skills_view.py` added.
- [x] `schema/schema.sql` updated with `kami_skills` view comment
      block.
- [x] `SCHEMA_VERSION` 9 → 10.
- [x] DuckDB unnest path: `UNNEST(CAST(skills_json AS
      STRUCT("index" INTEGER, "points" INTEGER)[]))` —
      directly typed columns, no `json_extract` round-trip.
      Cleanest analog to Session 13's `INTEGER[]` cast.
- [x] View test passes — `tests/test_kami_skills_view.py`:
      `6 passed in 0.45s` (test_view_resolves_single_skill_with_freshness,
      test_view_flags_stale_kami,
      test_view_unnests_multi_skill_kami,
      test_view_excludes_empty_and_null_skills,
      test_view_unresolved_skill_yields_null_join,
      test_view_total_row_count).
- [x] Real-data coverage:
      - **rows = 59,614**
      - **kamis_with_skills = 7,108** (matches Part 0 baseline ✓)
      - **unresolved = 0** (every skill_index resolves through
        skills_catalog ✓)
- [x] Per-tree investment summary across the population:

      | tree        | total_points | kamis_in_tree |
      |-------------|-------------:|--------------:|
      | Guardian    |      189,452 |          7,044 |
      | Enlightened |       24,588 |          2,141 |
      | Harvester   |       17,283 |          2,580 |
      | Predator    |        4,600 |            196 |

      Guardian dominant (~99% of kamis have at least one Guardian
      skill); Predator is the rarest tree. Plausible cross-section.
- [x] Spot-check kami 1186 (agent's pet allocation case): 9
      skills resolved — 7 Guardian (Patience, Toughness,
      Defensiveness, Armor, Vigor, Anxiety, Dedication = 30 pts)
      + 2 Enlightened (Cardio, Concentration = 7 pts). Recognizable
      Guardian-heavy hybrid build. ✅
- [x] Commits: `38093b2 harness: schema migration v10 +
      kami_skills view`.

## Part 4 — nodes_catalog

- [x] `migrations/011_add_nodes_catalog.py` added.
- [x] `schema/schema.sql` updated with `nodes_catalog` table block.
- [x] `SCHEMA_VERSION` 10 → 11.
- [x] Loader `ingester/nodes_catalog.py` added; wired into
      `ingester/serve.py` startup. `room_index` resolved at load
      time as `room_index = node_index` per Session 14 Part 1b
      discovery (Index identity).
- [x] Loader test passes — `tests/test_nodes_catalog.py`:
      `6 passed in 0.40s` (test_parse_nodes_csv,
      test_load_nodes_catalog_into_db, test_load_is_idempotent,
      test_ensure_loaded_skips_when_populated,
      test_ensure_loaded_loads_when_empty, test_missing_csv_raises).
- [x] Initial load: **64 rows / 64 in game / 64 with room**.
      All in-game rows have non-NULL `room_index`. ✅
- [x] Spot-check node 86 (agent's example): name="Guardian Skull",
      room_index=86, affinity="Eerie, Insect", status="In Game",
      scav_cost=500. ✅
- [x] Commits: `9014d8f harness: schema migration v11 +
      nodes_catalog table + loader`.

## Part 5 — kami_current_location view

- [x] `migrations/012_add_kami_current_location_view.py` added.
- [x] `schema/schema.sql` updated with `kami_current_location`
      view comment block.
- [x] `SCHEMA_VERSION` 11 → 12.
- [x] **Real-data verification surfaced a refinement**: initial
      view used all `harvest_*` types as source actions, but only
      `harvest_start` carries `node_id` — `harvest_stop` /
      `collect` / `liquidate` decode only `harvest_id` (the chain
      call references the harvest entity, not the node), so on
      those rows `node_id` is NULL. Without the refinement, the
      view returned `with_loc=4562` (a 2,508-kami gap below the
      `source_action IS NOT NULL=7070` count). Restricted
      source action set to `harvest_start` only — semantic is
      "latest node this kami was sent to harvest." Kamis remain
      on their last-harvested node until an untracked move, so
      this is the correct current-location signal. View
      re-applied via `python -m migrations.012_*` on a stopped
      service.
- [x] View test passes — `tests/test_kami_current_location_view.py`:
      `6 passed in 0.72s` (test_active_kami_resolves_room,
      test_old_kami_flagged_stale, test_cold_start_kami_returns_null_location,
      test_move_actions_are_ignored, test_one_row_per_kami,
      test_stopper_falls_back_to_last_harvest_start).
- [x] Real-data coverage:
      - **total_kamis = 7,110**
      - **with_loc = 6,962** (98% of 7,103 kamis-with-action
        baseline; cold-start kamis correctly NULL ✓)
      - **stale_loc = 6,629** (most kamis don't restart a harvest
        within 30 min — expected; the threshold flags kamis whose
        location confidence has decayed)
- [x] Source action mix: `harvest_start = 6962` (100% by
      construction — only source type allowed).
- [x] 4-kami roster check (agent's example):

      | kami_index | room | node | source         | freshness | stale |
      |-----------:|-----:|-----:|----------------|----------:|:-----:|
      |       1186 |   86 |   86 | harvest_start  |    28,682 |  TRUE |
      |       2418 |   62 |   62 | harvest_start  |    14,542 |  TRUE |
      |       1745 | NULL | NULL | NULL           |      NULL |       |
      |       2465 | NULL | NULL | NULL           |      NULL |       |

      1186 → Guardian Skull (room 86) matches the agent's example.
      2418 on node 62 (a real chain node). 1745 / 2465 are the
      Pain 1b cold-start kamis from Session 13 — same handling
      (NULL, not garbage). ✅
- [x] **Known gap documented**: move attribution. ~10k `move`
      rows in 28d window; chain `system.account.move` is account-
      level so `kami_id` is NULL on the row. A kami that just
      moved without harvesting on the new node will surface as
      stale or NULL. Fixing this requires a snapshot-time
      account-membership resolution in the decoder — meaningful
      refactor, not Session 14 scope. Surfaced in decoder-notes
      and next-steps hand-off as a known limitation.
- [x] Commits: `ebc6c34 harness: schema migration v12 +
      kami_current_location view`.

## Part 6 — Documentation

- [x] README updated — new "Resolved skills + current location"
      section walking through all 4 artifacts with example
      queries (skill loadout + roster location).
- [x] `memory/decoder-notes.md` Session 14 section promoted
      (skills.csv shape, nodes.csv shape, node→room discovery,
      move-attribution gap, harvest_start-only rationale).
- [x] `CLAUDE.md` schema summary lists `skills_catalog`,
      `kami_skills`, `nodes_catalog`, `kami_current_location`
      alongside Session 13's items_catalog / kami_equipment.
- [x] Commits: `9f84122 docs: README + CLAUDE.md — skills_catalog,
      kami_skills, nodes_catalog, kami_current_location (Session 14)`,
      `385b487 docs: session 14 discovery` (covered the
      decoder-notes promotion).

## Part 7 — Hand-off

- [x] `memory/next-steps.md` carries the `oracle.md` diff (7a) —
      new `kami_skills` view section, new `kami_current_location`
      view section with the move-attribution caveat and the
      live-truth boundary, replacement for the "raw skills_json"
      caveat.
- [x] `memory/next-steps.md` 7b — kami-agent + kami-zero
      integration sweep is itemized for both codebases (founder
      coordinates kami-zero rollout via standing collaboration
      mode).
- [x] `memory/next-steps.md` 7c — Session 15+ queue carries
      forward unchanged from Session 13; the five Session 14
      rejections (no archetype column, no tier-choice columns,
      no `*_from_skills` summed columns, no live-getter view, no
      per-tree skill-point columns on `kami_static`) are
      duplicated into the queue so future sessions inherit them.
- [x] `memory/next-steps.md` 7d — move-attribution gap surfaced
      explicitly as a known limitation for the agent.
- [x] Commits: `28e7b82 docs: next-steps — Session 14 hand-off`.

## Coverage check (regression guard)

`SELECT COUNT(...) FROM kami_static` per column, total = 7110:

| column                  | non-NULL | coverage |
|-------------------------|---------:|---------:|
| account_name (S9)       |     7110 |   100.0% |
| level (S10)             |     7110 |   100.0% |
| skills_json (S10)       |     7110 |   100.0% |
| equipment_json (S10)    |     7110 |   100.0% |
| strain_boost (S11)      |     7110 |   100.0% |
| body_affinity (S12)     |     7110 |   100.0% |
| hand_affinity (S12)     |     7110 |   100.0% |

No regression on Sessions 9 / 10 / 11 / 12.

Catalog / view counts:

| object                | rows   |
|-----------------------|-------:|
| items_catalog (S13)   |    177 |
| skills_catalog (S14)  |     72 |
| nodes_catalog (S14)   |     64 |
| kami_equipment (S13)  |    405 |
| kami_skills (S14)     | 59,614 |
| kami_current_location (S14) |  7,110 |

Full test suite: `143 passed, 12 skipped, 1 warning in 25.79s`
(was 119 passed / 12 skipped at end of Session 13 — added 24
tests across the four new artifacts; no pre-existing tests broken).

## Known issues

**Move attribution gap.** `system.account.move` is account-level
on chain — `kami_action.move` rows have `kami_id` NULL. Kamis
that move without harvesting on the new node surface as stale
or NULL in `kami_current_location`. Fix requires a
snapshot-time decoder change to fan out one row per kami of the
moving account. Out of scope for Session 14; covered by the
verify-before-act discipline + `is_stale` flag for now.
Re-evaluate if real-world traffic shows it matters.

## Status

✅ **Session 14 complete.** All 12 acceptance gates pass:
schema_version 12; `skills_catalog` (72 rows / 4 trees, 16 effect
keys all in documented set); `kami_skills` view (59,614 rows / 0
unresolved / 7108 kamis = matches baseline); `nodes_catalog` (64
rows / all in-game / all with room_index); `kami_current_location`
view (6,962 of 7,103 kamis-with-action have a resolved room — 98%
coverage; cold-start kamis correctly NULL). No regression on
Sessions 9 / 10 / 11 / 12 / 13. Hand-off to founder in
`memory/next-steps.md`. The five scope rejections (no archetype
column, no tier-choice columns, no `*_from_skills` summed
columns, no live-getter view, no per-tree skill-point columns
on `kami_static`) held end-to-end. The move-attribution gap is
surfaced explicitly as a known limitation rather than papered over.
