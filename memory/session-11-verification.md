# Session 11 — Verification Report

## Service health (live now)

- `systemctl is-active kami-oracle`: `active`
- `curl -s https://136-112-224-147.sslip.io/health | jq .`
  - cursor lag: 3.6 s
  - last block: 28,146,361
  - total_actions: 363,799
  - schema version: 5  ✓ (post-migration)

## Part 1 — Discovery

- [x] `memory/decoder-notes.md` has "Session 11 — skill-effect
      modifiers on chain"
- [x] Chain source documented for each of the 12 modifiers
      (catalog walk; chain pre-aggregated components do not exist —
      probed 43 candidate names, 0 hits)
- [x] Bpeon Zephyr fixture raw dump captured
      (`memory/session-11-discovery.txt`) — 12 expected values
      computed from skills.csv × Zephyr's `skills_json`
- [x] Any modifier that couldn't be read → documented + handled
      (none unresolved; catalog walk covers all 12)
- [x] Commits: `4f63f10`

## Part 2 — Schema migration

- [x] Migration file added: `migrations/005_add_modifier_columns.py`
- [x] `schema/schema.sql` reflects post-migration state with comment
      block (sat above the 12 INTEGER column declarations)
- [x] `SCHEMA_VERSION` bumped 4 → 5
- [x] Migration applied cleanly (`storage: migration 005 complete:
      {'columns_added': 12, 'columns_total': 12}` in serve.log)
- [x] Final new column list: `strain_boost`,
      `harvest_fertility_boost`, `harvest_intensity_boost`,
      `harvest_bounty_boost`, `rest_recovery_boost`,
      `cooldown_shift`, `attack_threshold_shift`,
      `attack_threshold_ratio`, `attack_spoils_ratio`,
      `defense_threshold_shift`, `defense_threshold_ratio`,
      `defense_salvage_ratio`
- [x] Commits: `1fdf686`

## Part 3 — Populator + backfill

### 3a. Populator
- [x] `ingester/kami_static.py` produces all 12 modifiers per kami
      (via the new `ingester/skill_catalog.py` module — single CSV
      load at startup, per-kami arithmetic only)
- [x] No silent recomputation — values come from the upstream
      `kamigotchi-context/catalogs/{skills,items}.csv`, vendored
      into `kami_context/catalogs/` so the populator runs without
      external deps. Catalog → chain pipeline validated on Zephyr
      (catalog SHS sum 50+50+40 = 140 = chain `health.shift`;
      catalog SYS sum 5+3 = 8 = chain `harmony.shift`).
- [x] Tests pass — `8 passed` in `tests/test_build_fetch.py`,
      full suite `105 passed, 12 skipped, 1 warning in 22.14s`
- [x] Bpeon Zephyr fixture asserts match Part 1 raw dump
      (`test_modifiers_zephyr_catalog_walk` — exact dict equality
      on all 12 expected values)
- [x] Commits: `dc452fd`

### 3b. Backfill
- [x] Pre-backfill rows with any NULL modifier column: 7,021
- [x] Post-backfill rows with any NULL modifier column: 0
- [x] Failed-fetch count: 0
- [x] Anomaly counts:
  - `anomaly_negative_unexpected`: 0 (no non-{SB,CS,ATS,DTS}
    column went negative)
  - `anomaly_uniform_zero`: none — every column had at least one
    nonzero row across the population
- [x] strain_boost distribution: populated=7,021, negative=1,683
      (24%), min=-325, max=0, zero=5,338
- [x] Top-20 by strain_boost (most-negative SB) — pasted below
- [x] Commits: `cc83c3d`

```
idx=7773  name=Kamigotchi 7773      acct=0xRobster       lvl=38  harm=19  SB=-325
idx=15480 name=Kamigotchi 15480     acct=0xRobster       lvl=37  harm=20  SB=-325
idx=353   name=Kamigotchi 353       acct=0xRobster       lvl=49  harm=31  SB=-325
idx=9555  name=Kamigotchi 9555      acct=dmi             lvl=40  harm=22  SB=-325
idx=6251  name=Kamigotchi 6251      acct=𝄠𝄻𝄇             lvl=38  harm=25  SB=-325
idx=427   name=Kamigotchi 427       acct=𝄠𝄻𝄇             lvl=38  harm=25  SB=-325
idx=12037 name=Kamigotchi 12037     acct=0xRobster       lvl=41  harm=24  SB=-300
idx=5600  name=Kamigotchi 5600      acct=0xRobster       lvl=41  harm=27  SB=-300
idx=2170  name=Kamigotchi 2170      acct=0xRobster       lvl=40  harm=26  SB=-275
idx=2989  name=Kamigotchi 2989      acct=0xRobster       lvl=37  harm=23  SB=-275
idx=1352  name=Blissey              acct=wassa           lvl=40  harm=26  SB=-275
idx=199   name=199                  acct=wassa           lvl=40  harm=28  SB=-275
idx=5017  name=Kamigotchi 5017      acct=wassa           lvl=40  harm=28  SB=-275
idx=11465 name=Kamigotchi 11465     acct=0xRobster       lvl=40  harm=20  SB=-275
idx=2571  name=Kamigotchi 2571      acct=wassa           lvl=40  harm=25  SB=-275
idx=3254  name=Kamigotchi 3254      acct=wassa           lvl=40  harm=29  SB=-275
idx=6091  name=Kamigotchi 6091      acct=wassa           lvl=40  harm=29  SB=-275
idx=3346  name=Kamigotchi 3346      acct=wassa           lvl=40  harm=25  SB=-275
idx=3124  name=Kamigotchi 3124      acct=wassa           lvl=40  harm=25  SB=-275
idx=1717  name=brokele              acct=wassa           lvl=40  harm=26  SB=-275
```

The sustain-meta cluster the founder mentioned is visible: `0xRobster`
and `wassa` operators dominate the top 20, with `dmi` and `𝄠𝄻𝄇`
also in the deepest reductions. Most of these are level-40 builds
with high harmony — exactly the "long-session, gas-efficient
harvester" profile the founder asked to surface.

Coverage cross-reference:
- 1,683 of 7,021 (24%) carry `strain_boost < 0` → Enlightened-tree
  / Harvester-tree investment is non-degenerate across the fleet
- 2,573 carry `harvest_intensity_boost > 0` (HIB skills are common)
- 737 carry `harvest_bounty_boost > 0` (HBB is rarer; tier-3+ pick)
- 72 carry `attack_spoils_ratio > 0` (ASR; Predator-tier specialists)

## Part 4 — Documentation

- [x] README `kami_static` section updated — new
      "Skill-effect modifiers in `kami_static` (Session 11)"
      paragraph added below the Session 10 build snapshot section
- [x] decoder-notes "Session 11 — skill-effect modifiers" promoted
      (full taxonomy + storage convention + Zephyr round-trip)
- [x] CLAUDE.md schema summary updated — 12 modifier columns listed
      under "Modifiers (Session 11)" with one line each
- [x] Commits: `b7fb50d`

## Part 5 — Hand-off diff

- [x] `memory/next-steps.md` has "Hand-off to human (blocklife-ai)"
      with two diffs:
      A) `kami-agent/integration/oracle.md` schema cheat-sheet
         update — the 12 modifier columns block
      B) `blocklife-ai/context/kami-oracle-bootstrap/colab-setup.md`
         — schema cheat-sheet entry + sustain-harvester example
         query keyed off `strain_boost`

## Coverage check (regression guard)

Re-ran harvest_* coverage and account_name coverage.

| action_type | total | null_amount |
|---|---|---|
| harvest_collect   | 4,756   | 35      |
| harvest_stop      | 160,095 | 25,500  |
| harvest_liquidate | 1,251   | 186     |
| harvest_start     | 136,588 | 136,588 |

(`harvest_start` carries no MUSU by design — every row is null on
amount; the other three match Session 10 baseline within
poller-delta noise.)

- distinct_accounts: 151
- with_account_name: 151

Session 10 build columns regression — all still 100%, no row
went non-NULL → NULL:

- `build_refreshed_ts NOT NULL`: 7,021 ✓
- `total_health NOT NULL`: 7,021 ✓
- `total_power NOT NULL`: 7,021 ✓

`kami_static.row_count` is 7,021 (one new kami appeared post-Session
10 baseline of 7,020 — picked up cleanly by the
`account_id`-NULL/`build_refreshed_ts`-NULL backfill predicate).

## Known issues

- The chain DOES NOT carry pre-aggregated per-kami modifier scalars
  — Stage 1 stores the catalog-walk sum instead. Equivalent to "the
  game's own client-side aggregation," not "what `LibBonus`'s solidity
  loop would return at the next block." For permanent bonuses
  (which is what these 12 modifiers are) the two are identical
  by construction; for any temporary/passive bonus the populator
  would miss it. No such effects are wired up in the current
  catalog, so this is theoretical for now — but it's a known boundary
  (see "Source of truth: the upstream catalogs" in
  `memory/decoder-notes.md` for the discussion).
- Daily-sweep cost note from the prompt ("+30–60 min on the daily
  sweep") was conservative — actual added cost is ~1 minute for
  the modifier compute (zero new chain calls; pure CSV walk + per-kami
  arithmetic). Daily sweep stays well within the Stage-1 ceiling.

## Status

✅ **Session 11 complete. All 12 skill-effect modifiers populated
across kami_static; founder can now cluster by sustain (strain
reduction, 1,683 kamis with SB<0), income (HFB / HIB / HBB), and
combat threshold profiles. No regression on Session 10 build
columns or Session 9 coverage.**
