# Session 12 — Verification Report

## Service health (live now)

- `systemctl is-active kami-oracle`: `active`
- `/health` schema version: **6** ✓ (was 5 going in; migration 006
  applied cleanly on first restart, "storage: migration 006 complete:
  {'columns_added': 2, 'columns_total': 2}").
- cursor lag: 2,928 s (~49 min) at write-time. The service was stopped
  for ~58 min total during this session — first for the chain-fetch
  backfill attempt (option (a), ran ~50 min before founder authorized
  the switch), then briefly for the SQL backfill. Poller is catching
  up at ~3–4× real-time; expected to clear under 60 s within ~10–15
  min of report write-time. Cursor is making forward progress
  (28148031 → 28148315 over the last 90 s, ≈ 3 blocks/s).
- last block: 28,148,315.
- kami_static rows: 7,021.

## Part 1 — Discovery

- [x] `decoder-notes.md` "Session 12 — affinities" section present
  (commit 1eba9bf). Documents `[0]=body, [1]=hand` ordering,
  `{EERIE, NORMAL, SCRAP, INSECT}` value bucket, no-normalization
  rule, integer-vs-affinity column relationship.
- [x] Zephyr + 2 sample kamis raw chain dump captured in
  `memory/session-12-discovery.txt`, fetched live at block 28147942
  via `getKami(kamiId)`:
  - kami_index=43 ("Zephyr"): `['NORMAL', 'EERIE']` → body=NORMAL, hand=EERIE
  - kami_index=5121: `['EERIE', 'EERIE']` → body=EERIE, hand=EERIE
  - kami_index=12404: `['SCRAP', 'EERIE']` → body=SCRAP, hand=EERIE
- [x] Element ordering verified ([0]=body, [1]=hand). Cross-checked
  against `kamigotchi-context/systems/state-reading.md:101` —
  `kami.affinities // string[] — e.g. ["EERIE", "SCRAP"] (body, hand)`.
  Sample diversity (bodies 0, 11, 14 → SCRAP, EERIE, NORMAL) confirms
  body→affinity is a real lookup, not a single-affinity world.
- [x] Commits: 1eba9bf

## Part 2 — Schema migration

- [x] `migrations/006_add_affinity_columns.py` added; idempotent
  ALTER ... ADD COLUMN guarded by information_schema check; bumps
  `TARGET_SCHEMA_VERSION` to 6.
- [x] `schema/schema.sql` updated with the prescribed comment block
  ("Affinity (Session 12): each kami has a body affinity and a hand
  affinity drawn from {EERIE, NORMAL, SCRAP, INSECT}…") and
  `body_affinity VARCHAR` / `hand_affinity VARCHAR` placed alongside
  the integer body/hand columns and the existing JSON `affinities`
  column.
- [x] `SCHEMA_VERSION` 5 → 6 in `ingester/storage.py`; m006 wired into
  `_apply_migrations`.
- [x] Migration applied cleanly (no errors in `serve.log`):
  `2026-04-27 19:42:04,978 INFO ingester.storage: storage: applying
  migration 006 (add affinity columns)` →
  `2026-04-27 19:42:05,246 INFO ingester.storage: storage: migration
  006 complete: {'columns_added': 2, 'columns_total': 2}`.
- [x] Commits: fb9cb8c

## Part 3 — Populator + backfill

### 3a. Populator

- [x] `ingester/kami_static.py` extracts `body_affinity` /
  `hand_affinity` in `_kami_shape_to_static` from the same
  `affinities` array Session 9/10/11 already pulls off the
  `getKami(kamiId)` response. **Zero new chain calls.**
  `KamiStatic` dataclass extended with two `str | None` fields;
  `upsert_kami_static` SQL extended to write both on every populator
  pass (covers the daily sweep + new-kami first-seen path).
- [x] Tests pass — `9 passed, 1 warning in 0.93s` for
  `tests/test_build_fetch.py`. Full suite: `106 passed, 12 skipped,
  1 warning in 25.04s` (the 12 skipped are `test_client.py` cases
  that need an external endpoint).
- [x] Bpeon Zephyr fixture asserts match Part 1 raw dump:
  `body_affinity == "NORMAL"` and `hand_affinity == "EERIE"`. New
  defensive test `test_kami_shape_affinity_defensive_on_unexpected_length`
  confirms unexpected-length affinities array NULLs both columns and
  logs the kami_id.
- [x] Live populator round-trip confirmed end-to-end against Zephyr
  on chain (`reader.fetch(zephyr_id)` returns `body_affinity=NORMAL`,
  `hand_affinity=EERIE`).
- [x] Commits: 4798dce

### 3b. Backfill

- **Approach changed mid-session** from the prompt's option (a)
  "reuse the populator's per-kami fetch path" to option (c) "pure SQL
  split of the existing `affinities` JSON column", authorized by the
  founder on 2026-04-27. Rationale: option (a) ran at 0.5 kami/s
  against the public RPC (prompt estimated 50–100 min for the full
  7,016 rows; actual was tracking ~3.5 hours). Affinity is an
  immutable per-kami attribute, so the cached JSON in the existing
  `affinities` column (populated by the same getKami call Sessions
  9/10/11 already run) is exact-equal to a fresh chain fetch.
  Splitting in SQL is correct without a chain round-trip and finished
  in <100 ms. The first 1,600 rows committed via the chain-fetch
  path before the switch are skipped by the SQL UPDATE's WHERE
  clause. Documented in the script header.
- [x] Pre-backfill NULL count: **7,021** (immediately post-migration).
- [x] Post-backfill NULL count: **0** on both columns.
- [x] Failed-fetch count: **0** (chain-fetch phase committed 1,600
  rows with `fail=0 null_aff=0` before kill; SQL phase reported
  `eligible=5416 of 5416 pending` and `post_pending=0`).
- [x] body_affinity coverage: **7021/7021 (100%)**.
- [x] hand_affinity coverage: **7021/7021 (100%)**.
- [x] body_affinity distribution:

  | body_affinity | n     |
  |---------------|-------|
  | NORMAL        | 2,108 |
  | EERIE         | 1,691 |
  | SCRAP         | 1,660 |
  | INSECT        | 1,562 |

  Sum = 7,021 ✓. Exactly four distinct values, all from
  `{EERIE, NORMAL, SCRAP, INSECT}`. No empty strings, no surprise values.

- [x] hand_affinity distribution:

  | hand_affinity | n     |
  |---------------|-------|
  | NORMAL        | 2,159 |
  | INSECT        | 1,635 |
  | EERIE         | 1,618 |
  | SCRAP         | 1,609 |

  Sum = 7,021 ✓. Exactly four distinct values, all from
  `{EERIE, NORMAL, SCRAP, INSECT}`.

- [x] body × hand cross-tab — **all 16 cells populated** (no
  degenerate single-cell concentration that would suggest the columns
  got swapped):

  | body \ hand | NORMAL | INSECT | EERIE | SCRAP |
  |-------------|--------|--------|-------|-------|
  | NORMAL      | 639    | 508    | 492   | 469   |
  | EERIE       | 482    | 405    | 427   | 377   |
  | SCRAP       | 520    | 390    | 374   | 376   |
  | INSECT      | 518    | 332    | 325   | 387   |

  Most-populated cell is `(NORMAL, NORMAL)` at 639; least is
  `(INSECT, EERIE)` at 325. ~2× spread between most and least; no
  cell is missing.

- [x] Founder spot-check — **deferred to functional check** (the
  prompt expected a hardcoded `body_index → affinity` map from the
  agent's workaround query; that map was not pasted into this
  session's prompt). Substitute checks all pass:
  - **Part 1 sample re-verification.** Zephyr (#43): stored
    `(NORMAL, EERIE)` matches Part 1's chain dump exactly. #5121:
    stored `(EERIE, EERIE)` matches. #12404: stored `(SCRAP, EERIE)`
    matches.
  - **Functional determinism check.** Every body trait index in the
    population maps to **exactly one** body_affinity value
    (`SELECT body, COUNT(DISTINCT body_affinity) FROM kami_static
    GROUP BY 1 HAVING COUNT(DISTINCT body_affinity) > 1` returns 0
    rows). Same shape as the agent's hardcoded map: a body_index
    pins down a body_affinity. 30 distinct body indices, 4 distinct
    body affinities.
  - **JSON-vs-scalar consistency.** `7021/7021` rows have
    `body_affinity = json_extract_string(affinities, '$[0]')` AND
    `hand_affinity = json_extract_string(affinities, '$[1]')`.
    Proves the SQL split is faithful to the cached chain data.
- [x] Commits: 5ce596d

## Part 4 — Documentation

- [x] README updated with new "Affinity columns in `kami_static`
  (Session 12)" section, placed between Session 11 and "MUSU
  semantics".
- [x] `decoder-notes.md` "Session 12 — affinities" section already
  promoted from Part 1's discovery write-up (commit 1eba9bf, see
  Part 1).
- [x] CLAUDE.md schema summary updated — added `body_affinity` and
  `hand_affinity` lines under the `kami_static` block, with the
  `{EERIE, NORMAL, SCRAP, INSECT}` reference and many-to-one note.
- [x] Commits: 2ed3618 (README + CLAUDE.md), 1eba9bf (decoder-notes)

## Part 5 — Hand-off diff

- [x] `memory/next-steps.md` rewritten with a fresh "Hand-off to human
  (blocklife-ai) — Session 12" section: the
  `kami-agent/integration/oracle.md` schema cheat sheet update
  (one-line entry for `kami_static.{body_affinity, hand_affinity}`)
  plus an explicit note that the agent's hardcoded
  `body_index → affinity` VALUES tables can be dropped from
  in-flight queries (cross-references the agent's workaround pattern
  by example). Closing line per the Session 12 prompt: "Oracle is
  data-complete; future sessions reactive to real agent gaps." No
  Session 13 candidate list drafted.
- [x] Commits: afd0eb6

## Coverage check (regression guard)

- [x] **Session 11 modifier columns still 100% populated.** Sampled
  three: `strain_boost` 7021/7021, `harvest_intensity_boost`
  7021/7021, `defense_threshold_shift` 7021/7021. No regression.
- [x] **Session 10 build columns still 100% populated.** `level`
  7021/7021, `total_health` 7021/7021, `skills_json` 7021/7021. No
  regression.
- [x] **Session 9 `account_name` still 100% populated.** 7021/7021.
  No regression.
- [x] **harvest_id coverage on harvest_* actions unchanged or
  better.** 100.00% across all four action types
  (`harvest_start` 137,925/137,925; `harvest_collect` 4,854/4,854;
  `harvest_stop` 161,349/161,349; `harvest_liquidate` 1,251/1,251).

## Known issues

- **Cursor lag at write-time.** ~49 min behind chain head, expected
  to clear under 60 s within ~10–15 min as the poller catches up at
  ~3–4× real-time. The lag is from the kami-oracle service being
  stopped during the chain-fetch backfill attempt + SQL backfill
  window, not a regression. Forward progress confirmed
  (last_block 28,148,031 → 28,148,315 over the last 90 s).
- **Backfill approach diverged from prompt instruction.** Prompt said
  "Pick (a)" — we ran (a) for ~1,600 rows then switched to (c) on
  founder authorization. The script's docstring documents the
  rationale; behaviour-wise the two paths produce identical results
  (affinities are immutable, so cached JSON == fresh chain fetch).
- **Founder spot-check shape.** The prompt cited an agent-supplied
  hardcoded `body_index → affinity` map for the cross-check. That
  map wasn't in this session's prompt; substitute checks (Part 1
  sample re-verification, functional determinism, JSON↔scalar
  consistency) all pass. If the founder pastes the agent's map in a
  future session, a direct value-by-value check is a one-line query.

## Status

- ✅ **Session 12 complete. body_affinity + hand_affinity populated
  across kami_static; agents can now JOIN on affinity directly
  instead of hardcoding body_index → affinity tables. No regression
  on Sessions 9 / 10 / 11. Service active on schema_version 6;
  cursor catching up to chain head as expected.**
