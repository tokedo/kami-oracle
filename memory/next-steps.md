# Next Steps

## Hand-off to human (blocklife-ai) — Session 12

Session 12 added `body_affinity` and `hand_affinity` to `kami_static`
(VARCHAR, drawn verbatim from `getKami(kamiId).affinities` —
`{EERIE, NORMAL, SCRAP, INSECT}` uppercase, `[0]` is body, `[1]` is
hand). Schema is now version 6. The kami-agent repo's schema cheat
sheet still shows the Session 11 column list and the agent has been
hand-rolling a hardcoded `body_index → affinity` VALUES list inline
in queries because the oracle exposed body / hand only as integer
trait indices. Both go away with this hand-off.

### Diff — `kami-agent/integration/oracle.md` schema cheat sheet

Add the two columns to the `kami_static` row in the schema cheat
sheet, with a one-line note:

> `kami_static.{body_affinity, hand_affinity}` — VARCHAR, drawn from
> `{EERIE, NORMAL, SCRAP, INSECT}` (uppercase on chain). Extracted
> from `getKami(kamiId).affinities` (string[2] = `[body, hand]`) on
> the daily sweep — zero new chain calls. Stored verbatim, no case
> normalization. The integer `body` / `hand` columns (~30 / ~27
> distinct trait indices) remain alongside; affinity is many-to-one
> over body/hand traits. See
> `memory/decoder-notes.md` "Session 12 — affinities" in the oracle
> repo for the canonical ordering verification and chain dump.

### Drop the hardcoded `body_index → affinity` VALUES tables

The agent's in-flight queries currently include something like:

```sql
LEFT JOIN (VALUES (0, 'SCRAP'), (1, 'EERIE'), ...) AS aff(body, label)
  ON s.body = aff.body
```

…to compensate for the oracle's missing affinity column. With the
new columns live, those VALUES tables can be dropped: replace
`aff.label` references with `s.body_affinity` (or
`s.hand_affinity` if the join was over `s.hand`). This drops a
fragile and duplicated mapping from agent code; the oracle now
carries it. **Founder, please flag this during the next agent
session** so the workaround doesn't keep getting reproduced.

### Verification artifacts

Populated 2026-04-27. Distribution + cross-tab + spot-check are in
`memory/session-12-verification.md`. Coverage is ≥99% on both new
columns (failed-fetch tolerance per Sessions 9/10/11). Each column
holds exactly the four expected affinity strings, no surprise
values. body_index → body_affinity is functionally deterministic
on this dataset (each body trait maps to exactly one affinity).

## Operational notes carried forward

- **Public URL**: `https://136-112-224-147.sslip.io`. Bearer token
  in the VM's `~/kami-oracle/.env` under `KAMI_ORACLE_API_TOKEN`.
- **Service control**: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- **Logs**: `logs/serve.log`, `logs/backup.log`,
  `logs/backfill-musu.log`, `logs/backfill-liquidate-harvest-id.log`,
  `logs/backfill-account-names.log` (Session 9 backfill output
  preserved), `logs/backfill-kami-build.log` (Session 10),
  `logs/backfill-kami-affinity.log` (Session 12),
  `/var/log/caddy/kami-oracle.log`. Session 11 backfill
  (`scripts/backfill_kami_modifiers.py`) ran inline — output captured
  in `memory/session-11-verification.md` rather than a long-tail log.
- **Backups**: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC,
  14-day retention).
- **Token rotation**: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- **DB file**: `db/kami-oracle.duckdb`. Held under exclusive lock
  by the serve process. Stop the unit before opening a DuckDB shell
  on the file directly.
- **Schema version**: 6 (Session 12 added `body_affinity` and
  `hand_affinity` VARCHAR columns to `kami_static` via migration 006).
  Storage.bootstrap auto-applies pending migrations on every start;
  bump `SCHEMA_VERSION` in `ingester/storage.py` and add a numbered
  file under `migrations/` for the next change.
- **Backfill scripts that touch the DB require the service stopped**
  (DuckDB exclusive file lock). Pattern:
  `sudo systemctl stop kami-oracle && python scripts/<name>.py &&
  sudo systemctl start kami-oracle`.
- **Retention window**: 28 days (`KAMI_ORACLE_WINDOW_DAYS=28`, set
  Session 8). The window fills in over ~3 weeks as the chain is
  ingested — full 28-day rolling window expected by 2026-05-24.
- **Client library**: `client/` is the stable consumer surface.
  Vendor into downstream repos with `scripts/vendor-client.sh
  /path/to/repo` (lands as `<target>/kami_oracle_client/`).
- **GetterSystem ABI overlay**: `kami_static.py` merges a
  `getAccount(uint256)` fragment into the loaded GetterSystem ABI
  at construction time — the vendored JSON does not carry it. Tier-A
  per CLAUDE.md (cited against `kami_context/system-ids.md` Getter
  System section). If a future re-vendor adds the function, the
  merge becomes a no-op.
- **Components registry resolution (Session 10)**: per-component
  contract addresses (slots / skills / equipment) resolve via
  `world.components()` — a separate registry from the
  `world.systems()` one used by `SystemRegistry`. Same
  `getEntitiesWithValue(uint256)` ABI, different registry contract.
  Resolved addresses listed in `memory/decoder-notes.md`
  "Session 10 — build fields on chain".
- **Skill / equipment catalog (Session 11)**:
  `kami_context/catalogs/{skills.csv,items.csv}` are vendored from
  upstream Kamigotchi via `scripts/vendor-context.sh`. Re-vendor
  any time upstream ships new skills or equipment items. The
  modifier populator caches both at startup; any subsequent
  populator pass picks up catalog changes on next service restart.

---

Oracle is data-complete; future sessions reactive to real agent gaps.

---

## Hand-off to human (blocklife-ai) — Session 13 (2026-04-29)

Two follow-ups for the founder. Same Session 12 pattern: oracle-side
work is done and shipped on `main`; the doc edit lives in the
kami-agent repo and the founder applies it there.

### 5a. Updates to `kami-agent/integration/oracle.md`

Three edits, all in the existing schema cheat-sheet section:

**1. Refresh-cadence callout** — add at the *top* of the schema
cheat sheet, before the table list:

> **Snapshot cadence**: `kami_static` rows are refreshed by a daily
> populator sweep. `build_refreshed_ts` is the per-row age. The
> `kami_equipment` view exposes `freshness_seconds` and `is_stale`
> (TRUE when > 36 hours old — one missed sweep). For destructive
> ops (unequip, trade, liquidate) **always verify against live
> chain state via Kamibots before committing** — a row can carry
> an item that's been unequipped since the last sweep.

**2. New `kami_equipment` view section** — add immediately after the
existing `kami_static` section:

> ### `kami_equipment` — slot-resolved equipped items
>
> A view (Session 13). One row per equipped item per kami, with
> slot resolved via `items_catalog` (a static mirror of
> `kami_context/catalogs/items.csv`). Replaces the previous
> workaround of joining `equipment_json` against items.csv inline.
>
> Columns: `kami_id`, `kami_index`, `name`, `account_name`,
> `slot_type` (`Kami_Pet_Slot`, `Passport_slot`, …),
> `item_index`, `item_name`, `item_effect`,
> `build_refreshed_ts`, `freshness_seconds`, `is_stale`.
>
> ```sql
> -- All pet equips on a roster
> SELECT kami_index, item_name, item_effect,
>        freshness_seconds, is_stale
> FROM kami_equipment
> WHERE account_name = 'fey'
>   AND slot_type = 'Kami_Pet_Slot';
> ```
>
> **Always check `is_stale` before unequip / trade / liquidate.**
> A false positive (item shown equipped, slot actually empty on
> chain) is a normal consequence of snapshot lag, not a bug.
> Verify with `get_kami_state(kami_index)` on Kamibots before
> committing the destructive op.

**3. Drop the "raw item indices, no slot labels" caveat** in the
existing `equipment_json` description on `kami_static`. Replace it
with: "Use the `kami_equipment` view for slot-resolved access. Raw
`equipment_json` remains available for callers that want the
original chain payload."

### 5b. Session 14+ candidates (drafted/queued, do NOT pre-build)

After founder reviewed kami-agent's full wish-list (2026-04-29), the
queue is regularized as below. Each session's prompt should *lead*
with the rejections it inherits, so the queue stays principled.

- **Session 14 — `skills_catalog` + `kami_skills` view + `nodes_catalog`
  + `kami_current_location` view.** Same catalog-mirror + view shape
  as Session 13, applied to skills and to current location (latest-
  known room derived from action stream). **Explicitly rejected
  inside Session 14**: archetype columns, tier-choice columns,
  summed `*_from_skills` columns, live-state derivations. Draft
  prompt to be placed at
  `context/kami-oracle-bootstrap/session-14-prompt.md` when the
  founder is ready (not during this session).

- **Session 15 — `account_static` identity table.** Account-level
  identity (`account_id`, `account_index`, `account_name`,
  `owner_address`, `kami_count`, `last_active_ts`). Identity only —
  live-state rollups (`kamis_resting`/`kamis_harvesting`/
  `kamis_dead` counts) **rejected** as Kamibots-domain.

- **Session 16 — `kami_last_known_state` view (dead-kami hint).**
  Derivation from action stream (latest `die` not followed by
  `revive`). Caveated as a hint, not live truth.

- **Session 17 — populator-side new-kami auto-populate.** Address
  the original Pain 1b: `kami_static` rows for `kami_id`s that
  appear in `kami_action` but are missing from `kami_static`, or
  with `build_refreshed_ts` older than SLA. **Populator-side, not
  endpoint-side.** Specifically rejected: any HTTP write endpoint
  (breaks read-only `/sql` posture); any live-derivation view
  (oracle is historical). The 1745 / 2465 kamis from kami-agent's
  2026-04-29 pass are a real example — they appear in actions but
  not in `kami_static` because the populator hasn't picked them up
  yet.

- **Session 18 (docs only) — `oracle.md` cookbook.** Short "common
  queries" recipes near the top of `oracle.md`. Pure docs, no
  oracle-side artifact. Founder may do this as a standalone PR
  whenever convenient.

- **Deferred / out (do not draft):** musu/hour rate (wait for 28d
  window fill ~2026-05-24); predator-threat function (agent can
  SQL when ready); cross-account inventory (too narrow);
  strategy-membership view (off-chain, Kamibots-domain); operator
  nonce-contention indicator (oracle ingests confirmed txs only,
  wrong tool).

### Rejections inherited from Session 13 (carry forward)

These were proposed during the kami-agent feedback that triggered
Session 13. They violate the oracle's "historical observation,
read-only" role and were rejected with rationale in the Session 13
prompt. Do not re-litigate without revisiting that rationale:

1. **No `kami_pet_inferred` view** that derives live equipment from
   stat-shift residuals. Live state is Kamibots' job
   (`oracle.md` line 33-42); the skills catalog isn't in DuckDB; a
   `/sql` query must never trigger a chain getter. Fix staleness by
   freshening the snapshot, not synthesizing live state.
2. **No HTTP refresh / write endpoint** for on-demand re-population
   of `kami_static` rows. Breaks the read-only `/sql`-only posture
   in `kami-oracle/CLAUDE.md`. Auto-populate from the action stream
   in the populator (Session 17) is the right shape.
3. **No `equipment_freshness_seconds` / `is_stale` columns on
   `kami_static`.** Already derivable from `build_refreshed_ts`;
   storing them creates two sources of truth that can drift.
4. **No pet-specific column or view.** Pets are a slot kind, not
   a special case. `kami_equipment WHERE slot_type = 'Kami_Pet_Slot'`
   is the canonical access pattern.

## Hand-off to human (blocklife-ai) — Session 14 (2026-04-29)

Three follow-ups for the founder. Same pattern as Sessions 12/13:
oracle-side work is shipped on `main` (schema_version 12); the
doc edit lives in the kami-agent repo and the founder applies it
there; the same sweep applies in kami-zero (the autonomous VM)
because both consume the oracle via the same MCP path per ADR-006.

### 7a. Updates to `kami-agent/integration/oracle.md`

Three edits, in the existing schema cheat-sheet section:

**1. New `kami_skills` view section** — add right after the
existing `kami_static` skills mention:

> ### `kami_skills` — per-skill effect details
>
> A view (Session 14). One row per (kami, invested skill) per
> `skills_json` entry. Joins `kami_static.skills_json` against
> `skills_catalog` (a static mirror of
> `kami_context/catalogs/skills.csv`) so the agent doesn't
> re-derive "skill 212 = +10 HP / rank, skill 222 = +2% DTS /
> rank" from the catalog inline.
>
> Columns: `kami_id`, `kami_index`, `name`, `account_name`,
> `skill_index`, `skill_name`, `tree`
> (Predator/Guardian/Harvester/Enlightened), `tier`, `points`,
> `effect`, `value`, `units`, `freshness_seconds`, `is_stale`
> (36h, same as `kami_equipment`).
>
> Per-tree point totals: `SELECT tree, SUM(points) FROM
> kami_skills WHERE kami_id = X GROUP BY tree`. Archetype
> classification stays in agent code — oracle exposes the
> components, not the label. Rationale: same as Session 13's
> rejected `kami_pet_inferred` — no hidden thresholds in oracle
> SQL.

**2. New `kami_current_location` view section** — add in a new
"Live-ish state" subsection (or near `kami_equipment`):

> ### `kami_current_location` — latest-known room
>
> A view (Session 14). For each kami, picks the most recent
> `harvest_start` action and resolves the room via
> `nodes_catalog`. Replaces the prior workaround of reading
> `harvest.node.roomIndex` from a live `get_kami_state` call
> (which returns the *last-harvested* node, not the kami's
> actual current room).
>
> Columns: `kami_id`, `kami_index`, `name`, `account_name`,
> `current_room_index`, `current_node_id`,
> `source_action_type` (always `harvest_start` today),
> `since_ts`, `freshness_seconds`, `is_stale`.
>
> **Not live truth.** The view restricts to `harvest_start`
> because among the harvest action family, only `harvest_start`
> carries `node_id` (stop / collect / liquidate decode
> harvest_id only). `move` actions are excluded entirely —
> `system.account.move` is account-level on chain, so
> `kami_action.move` rows have `kami_id` NULL. An account-level
> move can shift a kami to a new room without us attributing
> it to that specific kami. `is_stale = TRUE` (>30 min) means
> the agent should verify the room against chain via Kamibots
> before any destructive op keyed on location. Cold-start
> kamis (no `harvest_start` in the 28d window) appear with
> NULL location columns — same shape as the `kami_equipment`
> stale handling.

**3. Sundown the "raw skills_json" workaround** — rewrite the
existing `skills_json` description on `kami_static` to:

> Use the `kami_skills` view for resolved per-skill details.
> Raw `skills_json` remains available for callers that want
> the original chain payload.

### 7b. kami-agent + kami-zero integration sweep

Same hand-off pattern applies to **both** agent codebases:

- **kami-agent** (`~/kami-agent`): wherever the agent currently
  re-derives skill effects inline against `skills.csv` or peeks
  at `harvest.node.roomIndex` for current room, swap to
  `kami_skills` / `kami_current_location` queries. Founder runs
  this sweep after applying 7a.
- **kami-zero** (autonomous VM, `blocklife-ai`): same sweep.
  kami-zero's perception loop should similarly drop any inline
  `skills.csv` joining or harvest-room peeking. Founder
  coordinates the rollout via the standing collaboration mode
  (ssh / scp / git path).

### 7c. Session 15+ candidates (do NOT pre-build)

The Session 13 queue (Sessions 15 / 16 / 17 / 18) carries forward
unchanged — see the Session 13 section above. Session 14 itself
is now closed. Inheritable rejections from Session 14 (do not
re-litigate without revisiting the rationale in the Session 14
prompt):

1. **No `archetype` column / view** (Guardian / Predator /
   Harvester / Hybrid classification). Categorical labels with
   hidden thresholds — same anti-pattern as Session 13's
   rejected `kami_pet_inferred`. The agent gets the components
   (per-tree point totals from `kami_skills`) and classifies in
   its own code where the heuristic is visible.
2. **No `tier3_choice` / `tier6_choice` columns** — "which
   skill at tier 3" is one `WHERE tier = 3` away in
   `kami_skills`. Don't bake tree-specific column names into
   schema.
3. **No `total_hp_from_skills` / `total_dts_pct_from_skills`
   summed columns.** The agent already has resolved totals in
   `kami_static.total_health` (Session 10) and the 12 modifier
   columns (Session 11). What was missing was the *attribution*
   — per-skill breakdown — which `kami_skills` provides as
   rows. Sums are a `GROUP BY` away.
4. **No live chain getter for current room from `/sql`.**
   Oracle stays read-only against chain. `kami_current_location`
   is a derivation from observed action history with an
   explicit `is_stale` flag. Live-truth verification belongs in
   the agent against Kamibots — same boundary as Session 13's
   equipment freshness story.
5. **No per-tree skill-point columns on `kami_static`** (e.g.
   `predator_points`). Derive in SQL via GROUP BY — storing as
   columns means a backfill every time a kami respecs and
   another redundancy with `kami_static`.

### 7d. Known gap — move attribution (carry forward)

`system.account.move` is account-level on chain — `kami_action.move`
rows have `kami_id` NULL because the chain call has no per-kami
binding. ~10k move rows in the 28d window vs ~390k harvest rows.
A kami that moves without harvesting on the new node will surface
as stale or NULL in `kami_current_location`. Fixing this requires
a snapshot-time account-membership resolution in the decoder
(fan out one row per kami of the moving account, at the move's
block) — meaningful refactor, not Session 14 scope. Re-evaluate
if the agent's real-world traffic shows it matters; the
verify-before-act discipline covers it for now.

**Session 14.5 reframing (2026-04-29):** the move-attribution "gap"
above was framed wrong. Kamis don't move on chain — operators do.
A `system.account.move` is the operator's room change, not a
missed kami movement. Harvesting kamis stay on their node;
resting kamis follow the operator implicitly (their physical room
is the operator's room). The remaining work is not "attribute the
move to specific kamis" — it's "expose the operator's current
room" so an agent can answer "where is my resting kami?" by
joining `kami_static.account_id` to operator-location. See
`Session 15.5 candidate (account_current_location)` in 7c above
for the proposed shape.

## Hand-off to human (blocklife-ai) — Session 14.5 (2026-04-29)

Session 14.5 corrected `kami_current_location`'s semantic to
match the actual Kamigotchi mechanic (kamis don't move; operators
do; a kami is on a node iff currently harvesting). Schema is now
version 13. The view's column set changed in three places that
need to be reflected in `kami-agent/integration/oracle.md` and
`kami-zero/integration/oracle.md`. Same hand-off pattern as
S12 / S13 / S14.

### Diff — `oracle.md` `kami_current_location` section

Replace the existing description with:

> **`kami_current_location`** (view, Session 14.5). Per-kami
> current physical location IF the kami is currently on a node
> (mid-harvest). **Kamis don't move on chain — operators do.** A
> kami is on a node iff currently harvesting; a resting kami is
> in its operator's pocket (logically at the operator's current
> room, not on any node).
>
> Columns: `kami_id`, `kami_index`, `name`, `account_name`,
> `currently_harvesting` (BOOLEAN — TRUE iff latest harvest-
> active signal {harvest_start, harvest_collect} more recent than
> latest end-of-harvest signal {harvest_stop, harvest_liquidate
> where this kami was the victim}), `current_node_id` /
> `current_room_index` (NULL when not currently harvesting),
> `last_harvest_node_id` / `last_harvest_start_ts` (where the
> kami was last seen on a node, regardless of current state),
> `since_ts`, `freshness_seconds`, `is_stale` (NULL when not
> currently_harvesting; TRUE at >30 min for an active signal —
> verify against chain via Kamibots before destructive ops).
>
> harvest_collect does NOT end harvesting (mid-session payout
> only — chain spot-check confirmed). harvest_liquidate's
> `kami_id` is the *killer*, not the victim — the view resolves
> the victim via a self-join on `harvest_id` back to the victim's
> `harvest_start` (harvest_id is bijective with kami_id by
> keccak derivation).
>
> die / revive aren't in our `action_type` enum (no decoder for
> the chain calls that emit those state transitions) — known
> false-positive: a kami that died from non-liquidate causes
> won't have its harvest closed in our records. Workaround:
> `is_stale = TRUE` for >30 min flags the row for chain
> verification; agent should not trust currently_harvesting
> alone for destructive ops.
>
> Window-edge: a kami harvesting continuously for >7 days has no
> `harvest_start` in our window. The view treats `harvest_collect`
> as evidence of active harvesting in this case — so
> currently_harvesting reads TRUE — but `current_node_id` is
> NULL because the start row is gone. Treat
> (currently_harvesting=TRUE AND current_node_id IS NULL) as
> "harvesting on an unknown node — verify against Kamibots."

### Reframe the move-attribution caveat

The S14 oracle.md likely describes account-level `move` as a
"missed kami-movement signal." That framing is wrong. The
account-level `move` is the operator's movement; for resting
kamis the physical location is the operator's current room, not
the last harvest node. To answer "where is this resting kami?",
an agent joins `kami_static.account_id` to the operator's latest
`move` action (a small extra step that lives in agent code; see
also the Session 15.5 candidate `account_current_location` view
which would bundle this if it becomes hot).

### Update example SQL

The S14 `oracle.md` "kamis on node 86" snippet should change
from `WHERE current_node_id = 86` to:

```sql
SELECT kami_index, name, since_ts, freshness_seconds
FROM kami_current_location
WHERE currently_harvesting AND current_node_id = 86
  AND NOT is_stale;
```

### Agent-side code

Most kami-agent / kami-zero code reads `oracle.md` fresh each
session, so the change propagates on next runs. Spot-check that
no agent code keys destructive ops on `current_room_index IS NOT
NULL` without the `currently_harvesting` guard — under S14 that
column was populated for ~6.9k kamis; under S14.5 it's ~4.6k
(kamis truly mid-harvest), so a destructive query keyed on the
old shape would silently change scope.

### Session 15.5 candidate — `account_current_location` view

Add to the queued list under 7c:

> **`account_current_location`** view: per-account current room
> derived from the operator's latest `move` action (account-
> level). Pairs with `kami_current_location` so the agent can
> answer "where is this resting kami?" by joining `account_id`
> and reading the operator's room. Defer until an agent actually
> needs it (kami-agent traffic shows it matters); today the
> agent can join itself in the rare resting-kami-location case.
