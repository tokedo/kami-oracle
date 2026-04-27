# Next Steps

## Session 12 — exploration-driven (founder picks lead)

Session 11 closed the modifier-visibility gap: every `kami_static`
row now carries the 12 non-stat skill-effect modifiers
(`strain_boost`, `harvest_*_boost`, `rest_recovery_boost`,
`cooldown_shift`, `attack_*`, `defense_*`) on top of the Session 10
build columns. Sums are catalog-derived from `skills_json` ×
`equipment_json` × the upstream skill+item catalogs — zero new chain
calls; ~68 s for the 7,021-row backfill. Catalog → chain pipeline
validated on bpeon's Zephyr (catalog SHS sums = 140 = chain
`health.shift` exactly; catalog SYS sums = 8 = chain
`harmony.shift` exactly). Schema version is now 5. Sustain meta is
visible: 1,683 of 7,021 kamis carry `strain_boost < 0`, max sustain
build is `strain_boost = -325` (`0xRobster`'s top kamis).

Session 11 verification report: `memory/session-11-verification.md`.

### What Session 12 is for

**Stage 1 stays observe-the-chain.** Daily rollups, net-of-tax
views, and historical build journals remain deferred until a
consumer asks. Session 12's lead item should come from continued
Colab exploration and the founder's meta-clustering work — let the
founder pick based on what surfaces.

### Likely Session 12 candidates (founder picks priority)

These are *candidate* leads; do not pre-pick. Wait for the founder
to choose based on Colab usage + any new ergonomics gaps that
surface.

1. **Per-tree skill-point counters** — derive `harvester_points`,
   `predator_points`, `guardian_points`, `enlightened_points` from
   `skills_json` × `kami_context/catalogs/skills.csv` (Tree column).
   Pure aggregation, no new chain reads, no new chain math — just
   a second catalog field exposed as four columns or a SQL view.
   Founder picks columns vs view based on whether the
   `kami_context/` catalog reload cycle should reflow into stored
   data. (Same shape as Session 11 modifiers — extends the existing
   `compute_modifiers` path naturally.)
2. **Event-triggered build/modifier refresh** — refresh on
   `skill_upgrade` / `skill_respec` / `equip` / `unequip` / `lvlup`
   rather than only on the daily sweep. Worth doing only if the
   daily sweep cost (Session 10 ~117 min for build extras +
   Session 11 ~1 min for modifiers) becomes contention. Modifier
   add-on is essentially free, so the call is purely on Session 10
   build extras — gate on actual staleness complaints.
3. **Tier-B `quest.accept` overlay batch** — selector `0x09c90324`
   against `system.quest.accept`. Sample list in
   `memory/unknown-systems.md` is well over a thousand tx; useful
   for an upstream-PR-style reproduction once we have the
   signature.
4. **Net-of-tax MUSU view** — only when an operator-economics
   consumer asks. The schema records gross by design (kami
   leaderboards must use gross because tax is a node-config
   artifact).
5. **`kami-zero` wire-in (observation only)** — happens on the
   `kami-agent` VM, not here. The human sequences it through
   `blocklife-ai`. Lead query: top-20 MUSU leaderboard (7d) joined
   to build columns + modifiers, logged to kami-zero's perception
   loop with no effect on strategy.
6. **`kami_context` upstream PR — `getValue` typo.**
   `system-ids.md` documents the ValueComponent read fn as
   `getValue(uint256)`; the actual ABI declares only `get(uint256)`
   and `safeGet(uint256)`. Reproduction recipe in
   `memory/decoder-notes.md` "Session 7 acceptance — bpeon
   cross-check". Same flavour: `integration/ids/components.json`
   omits `component.id.equipment.owns` (Session 10 found it on
   chain via `world.components()` — the cheat sheet is incomplete).
   Add Session 11's finding too: the per-modifier components
   (`component.boost.harvest.fertility`, `component.shift.attack.threshold`,
   etc.) do NOT exist in the deployed components registry — every
   kami's modifier values are catalog-resolved by the LibBonus
   loop at action time, not stored as scalars. Worth flagging
   upstream so the docs match reality.
7. **Skill-name resolution in `kami_static`** — `skills_json`
   stores raw skill indices. The catalog (`skills.csv`) maps
   index → named skill; modifier columns already join through
   the catalog under the hood, so projecting names alongside is
   trivial.
8. **Equipment slot-name resolution** — `equipment_json` is a
   flat list of item indices. Slot-name resolution
   (`component.for.string`) does not resolve in the current
   registry. If a future re-vendor or registry update surfaces
   the component, switch `equipment_json` to a `{slot: item_index}`
   map.
9. **`kami_snapshot` (historical build journal)** — graduate from
   latest-snapshot to point-in-time. Only if a consumer asks for
   "what was kami X's build when it earned that MUSU on day Y".
10. **Daily rollups** — still only after Colab usage identifies a
    recurring query whose latency at full-scan is genuinely
    intolerable. Don't materialize speculatively. Most build × perf
    queries currently land in <500 ms on the public endpoint, well
    under the threshold.
11. **Other ergonomics gaps** the founder surfaces during continued
    Colab exploration — easy to bundle into a small polish session
    like Session 9 / 10 / 11.

### Deferred-until-asked

Do not pull these forward without the founder confirming the
demand. Stage 1 is "make the raw data trustworthy and accessible" —
not "ship every plausible aggregation."

- **Daily rollup tables** (`daily_action_rollup`, `daily_node_rollup`,
  `daily_musu_rollup`). Materializing these *before* the founder
  has identified which queries actually warrant pre-computation
  would burn complexity on speculative work. Wait for Colab
  exploration to surface a specific query whose latency we can't
  tolerate at full-scan, then pre-compute *that* one.
- **Net-of-tax MUSU view / column.** The schema records gross by
  design — kami leaderboards must use gross because tax is a
  node-config artifact, not a kami stat (see "MUSU semantics" in
  `decoder-notes.md` and the README). Net is operator-side
  economics; derive on the fly via a join to `harvest_start.taxAmt`
  in `metadata_json` when an operator-economics consumer asks.
  Don't precompute until that consumer exists.
- **MCP server.** Still post-decision. Wait until kami-zero's
  client-library usage is stable and the shape an MCP wrapper
  should take is obvious.
- **Typed account-centric / build-centric client methods.**
  `kami_oracle_client` does not yet expose `account_summary(...)`,
  `kami_build(...)`, or similar. Stage 1 stays "raw `/sql` through
  `kami_static` is sufficient." Add typed methods only when a
  downstream consumer asks for them.
- **`kami_snapshot` (historical build journal).** Latest-snapshot is
  enough for current-meta clustering. Graduate only if a consumer
  asks "what was kami X's build on day Y when it earned that MUSU?".

### Hand-off to human (blocklife-ai)

Two diffs that the oracle VM cannot apply because they target other
repos. The human applies both.

#### Diff A — `kami-agent/integration/oracle.md` schema cheat sheet

The kami-agent repo's `integration/oracle.md` already documents the
Session 10 build columns; add a `kami_static` modifier-columns block
mirroring the entries below. One-line each, same shape as the
Session 10 columns:

> `kami_static.{strain_boost, harvest_fertility_boost,
> harvest_intensity_boost, harvest_bounty_boost,
> rest_recovery_boost, cooldown_shift,
> attack_threshold_shift, attack_threshold_ratio,
> attack_spoils_ratio, defense_threshold_shift,
> defense_threshold_ratio, defense_salvage_ratio}` — 12 INTEGER
> columns, sums of skill+equipment effects from
> `kamigotchi-context/catalogs/{skills,items}.csv`. Percent values
> stored ×1000 (`strain_boost = -200` means -20%); `cooldown_shift`
> in signed seconds; `harvest_intensity_boost` in Musu/hr. Refreshed
> on the same `build_refreshed_ts` sweep as Session 10 build columns.
> SHS/SPS/SVS/SYS NOT new columns — already in `total_*`. See
> `memory/decoder-notes.md` "Session 11 — skill-effect modifiers on
> chain" for the catalog walk derivation and Zephyr round-trip.

#### Diff B — `blocklife-ai/context/kami-oracle-bootstrap/colab-setup.md`

The `colab-setup.md` should grow modifier-aware example queries on
top of the Session 10 build queries already there.

**1. Schema cheat-sheet update — `kami_static` row**

Add the modifier columns to the `kami_static` row in the schema
cheat sheet, with a one-line note:

> `kami_static.{strain_boost, harvest_fertility_boost,
> harvest_intensity_boost, harvest_bounty_boost,
> rest_recovery_boost, cooldown_shift,
> attack_threshold_shift, attack_threshold_ratio,
> attack_spoils_ratio, defense_threshold_shift,
> defense_threshold_ratio, defense_salvage_ratio}` — Session 11
> skill-effect modifier sums, INTEGER, percent values stored ×1000,
> `cooldown_shift` in signed seconds, `harvest_intensity_boost` in
> Musu/hr. Refreshed alongside Session 10 build columns.

**2. New example query — gas-efficient sustain harvesters**

The strain-reduction meta the founder surfaced — top-20 sustain
harvesters by absolute strain reduction with their 7d earnings:

```sql
-- Top-20 sustain-meta harvesters: most-negative strain_boost,
-- joined to gross MUSU/7d. -325 means -32.5% strain (longer harvests,
-- fewer feeds, lower gas).
SELECT s.kami_index, s.name, s.account_name AS operator,
       s.level, s.total_harmony, s.strain_boost,
       s.harvest_intensity_boost, s.harvest_bounty_boost,
       p.musu_gross_7d, p.payouts
FROM kami_static s
LEFT JOIN (
  SELECT kami_id,
         SUM(CAST(amount AS HUGEINT)) AS musu_gross_7d,
         COUNT(*) AS payouts
  FROM kami_action
  WHERE amount IS NOT NULL
    AND action_type IN ('harvest_collect','harvest_stop')
    AND block_timestamp > now() - INTERVAL 7 DAY
  GROUP BY 1
) p USING (kami_id)
WHERE s.strain_boost IS NOT NULL
ORDER BY s.strain_boost ASC
LIMIT 20;
```

Workflow note: pull the result into pandas, group `strain_boost` into
buckets (`(-400, -200]`, `(-200, -100]`, `(-100, 0)`, `[0]`), and
plot `musu_gross_7d / payouts` per bucket. A clean monotone shows
the meta-cluster narrative; flat means strain reduction isn't pulling
its weight in the current node mix.

#### 2. New example query — top earners × build (the founder's flagship)

The opening flagship query should now project build columns
alongside the operator label:

```sql
-- Top 20 earners by gross MUSU (7d), with current build snapshot.
SELECT s.kami_index, s.name AS kami_name,
       s.account_name AS operator,
       s.level, s.total_health, s.total_power, s.total_violence,
       s.total_harmony, s.total_slots,
       SUM(CAST(a.amount AS HUGEINT)) AS musu_gross_7d
FROM kami_action a LEFT JOIN kami_static s USING (kami_id)
WHERE a.action_type IN ('harvest_collect', 'harvest_stop')
  AND a.amount IS NOT NULL
  AND a.block_timestamp > now() - INTERVAL 7 DAY
GROUP BY s.kami_index, s.name, s.account_name, s.level,
         s.total_health, s.total_power, s.total_violence,
         s.total_harmony, s.total_slots
ORDER BY musu_gross_7d DESC NULLS LAST
LIMIT 20;
```

#### 3. New example query — meta clustering starter

The starter for meta clustering analysis the founder asked about:

```sql
-- Top 50 earners over 7d, with current build for clustering
WITH perf AS (
  SELECT kami_id,
         SUM(CAST(amount AS HUGEINT)) AS musu_gross_7d,
         COUNT(*) FILTER (WHERE action_type IN ('harvest_collect','harvest_stop')) AS payouts,
         COUNT(*) FILTER (WHERE action_type = 'harvest_start') AS starts
  FROM kami_action
  WHERE amount IS NOT NULL
    AND action_type IN ('harvest_collect','harvest_stop')
    AND block_timestamp > now() - INTERVAL 7 DAY
  GROUP BY 1
)
SELECT s.kami_index, s.name, s.account_name AS operator,
       s.level, s.total_health, s.total_power, s.total_violence,
       s.total_harmony, s.total_slots,
       p.musu_gross_7d, p.payouts, p.starts,
       p.musu_gross_7d / NULLIF(p.payouts, 0) AS musu_per_payout,
       s.skills_json, s.equipment_json
FROM perf p JOIN kami_static s USING (kami_id)
ORDER BY p.musu_gross_7d DESC NULLS LAST
LIMIT 50;
```

Workflow note in the prose: pull the result into a pandas
DataFrame, run k-means / DBSCAN on
`(total_harmony, total_power, total_violence, level)` to surface
emergent build groups, then look at the `musu_per_payout` and
`musu_gross_7d` distributions per cluster. `skills_json` /
`equipment_json` are JSON strings — `pd.json_normalize` /
`json.loads` on each row to expand if needed.

#### 4. New example queries — build-vs-performance one-liners

A handful of "is the meta actually X?" sanity queries the founder
can run as ad-hoc, included as a separate "build × performance
sanity checks" section after the leaderboards:

```sql
-- Average gross MUSU/7d by harmony bracket
SELECT width_bucket(s.total_harmony, 0, 200, 10) AS harmony_bucket,
       COUNT(DISTINCT s.kami_id) AS kamis,
       AVG(p.musu_gross_7d) AS avg_musu_7d
FROM kami_static s
LEFT JOIN (
  SELECT kami_id, SUM(CAST(amount AS HUGEINT)) AS musu_gross_7d
  FROM kami_action
  WHERE amount IS NOT NULL
    AND action_type IN ('harvest_collect','harvest_stop')
    AND block_timestamp > now() - INTERVAL 7 DAY
  GROUP BY 1
) p USING (kami_id)
WHERE s.total_harmony IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

```sql
-- Liquidations dealt vs total_violence bracket (7d)
SELECT width_bucket(s.total_violence, 0, 100, 10) AS violence_bucket,
       COUNT(DISTINCT s.kami_id) AS attackers,
       SUM(p.hits) AS total_hits,
       SUM(p.musu_taken) AS total_taken
FROM kami_static s
LEFT JOIN (
  SELECT kami_id, COUNT(*) AS hits,
         SUM(CAST(amount AS HUGEINT)) AS musu_taken
  FROM kami_action
  WHERE action_type = 'harvest_liquidate'
    AND amount IS NOT NULL
    AND block_timestamp > now() - INTERVAL 7 DAY
  GROUP BY 1
) p USING (kami_id)
WHERE s.total_violence IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

```sql
-- Skill-tree concentration by operator (top 5 most-spent skill indices)
SELECT s.account_name AS operator,
       sk.value->>'$.index' AS skill_index,
       SUM(CAST(sk.value->>'$.points' AS INTEGER)) AS total_points
FROM kami_static s,
     LATERAL (SELECT unnest(json_extract_array(s.skills_json)) AS value) sk
WHERE s.skills_json IS NOT NULL
GROUP BY s.account_name, skill_index
ORDER BY total_points DESC
LIMIT 25;
```

(DuckDB JSON extract syntax — verify in Colab; alternative is
`json_each` if the above doesn't bind. The point is the founder can
group by skill-index across kamis to find which skills cluster with
which operators.)

### Operational notes carried forward

- **Public URL**: `https://136-112-224-147.sslip.io`. Bearer token
  in the VM's `~/kami-oracle/.env` under `KAMI_ORACLE_API_TOKEN`.
- **Service control**: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- **Logs**: `logs/serve.log`, `logs/backup.log`,
  `logs/backfill-musu.log`, `logs/backfill-liquidate-harvest-id.log`,
  `logs/backfill-account-names.log` (Session 9 backfill output
  preserved), `logs/backfill-kami-build.log` (Session 10),
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
- **Schema version**: 5 (Session 11 added 12 modifier columns to
  `kami_static` via migration 005). Storage.bootstrap auto-applies
  pending migrations on every start; bump `SCHEMA_VERSION` in
  `ingester/storage.py` and add a numbered file under `migrations/`
  for the next change.
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
