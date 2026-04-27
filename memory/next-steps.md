# Next Steps

## Session 11 — exploration-driven (founder picks lead)

Session 10 closed the build-snapshot ergonomics gap: every
`kami_static` row now carries the kami's current effective stats
(`level`, `xp`, `total_health`, `total_power`, `total_violence`,
`total_harmony`, `total_slots`), upgraded skills (`skills_json`),
and equipped items (`equipment_json`), with `build_refreshed_ts`
ticking on the daily sweep. The numbers come from the canonical
game formula `floor((1000 + boost) * (base + shift) / 1000)`
applied to the chain's `(base, shift, boost, sync)` Stat tuple — no
local recomputation. Schema version is now 4. Bpeon's Zephyr (kami
#43) round-trip cleanly: level=37, total_health=230, total_power=16,
total_harmony=19, total_violence=17, total_slots=0, 10 skills
(sum points = 37, matches level), 0 equipment.

Session 10 verification report: `memory/session-10-verification.md`.

### What Session 11 is for

**Stage 1 stays observe-the-chain.** Daily rollups, net-of-tax
views, and historical build journals remain deferred until a
consumer asks. Session 11's lead item should come from continued
Colab exploration and the founder's meta-clustering work — let the
founder pick based on what surfaces.

### Likely Session 11 candidates (founder picks priority)

These are *candidate* leads; do not pre-pick. Wait for the founder
to choose based on Colab usage + any new ergonomics gaps that
surface.

1. **Event-triggered build refresh** — refresh a kami's build
   columns on `skill_upgrade` / `skill_respec` / `equip` /
   `unequip` / `lvlup` rather than only on the daily sweep. Only
   worth doing if Session 10's daily sweep proves too coarse —
   meta and builds change much more slowly than the 28-day window,
   so daily is probably fine. Per-event refresh adds complexity and
   poller load; gate on actual staleness complaints.
2. **`kami_snapshot` (historical build journal)** — graduate from
   latest-snapshot to point-in-time. Only if a consumer asks for
   "what was kami X's build when it earned that MUSU on day Y".
   The latest-snapshot `kami_static` is sufficient for current-meta
   clustering; historical drift is a separable concern.
3. **Daily rollups** — still only after Colab usage identifies a
   recurring query whose latency at full-scan is genuinely
   intolerable. Don't materialize speculatively. Most build × perf
   queries currently land in <500 ms on the public endpoint, well
   under the threshold.
4. **`kami-zero` wire-in (observation only)** — happens on the
   `kami-agent` VM, not here. The human sequences it through
   `blocklife-ai`. Lead query: top-20 MUSU leaderboard (7d) joined
   to build columns, logged to kami-zero's perception loop with no
   effect on strategy.
5. **Tier-B `quest.accept` overlay batch** — selector `0x09c90324`
   against `system.quest.accept`. Sample list in
   `memory/unknown-systems.md` is now well over a thousand tx;
   useful for an upstream-PR-style reproduction once we have the
   signature.
6. **Net-of-tax MUSU view** — only when an operator-economics
   consumer asks. The schema records gross by design (kami
   leaderboards must use gross because tax is a node-config
   artifact).
7. **`kami_context` upstream PR — `getValue` typo.**
   `system-ids.md` documents the ValueComponent read fn as
   `getValue(uint256)`; the actual ABI declares only `get(uint256)`
   and `safeGet(uint256)`. Reproduction recipe in
   `memory/decoder-notes.md` "Session 7 acceptance — bpeon
   cross-check". Same flavour: `integration/ids/components.json`
   omits `component.id.equipment.owns` (Session 10 found it on
   chain via `world.components()` — the cheat sheet is incomplete).
8. **Skill-name resolution in `kami_static`** — currently
   `skills_json` stores raw skill indices. The catalog
   (`kamigotchi-context/catalogs/skills.csv`) maps index → named
   skill. Bundle into `kami_static` as a flat denormalised
   `skills_named_json` column if the founder finds Colab `index=212`
   readouts ergonomically painful.
9. **Equipment slot-name resolution** — Session 10 stores
   `equipment_json` as a flat list of item indices; slot-name
   resolution (`component.for.string`) does not resolve in the
   current registry. If a future re-vendor or registry update
   surfaces the component, switch `equipment_json` to a
   `{slot: item_index}` map.
10. **Other ergonomics gaps** the founder surfaces during continued
    Colab exploration — easy to bundle into a small polish session
    like Session 9 / 10.

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

The `blocklife-ai` repo's
`context/kami-oracle-bootstrap/colab-setup.md` should grow new
example queries that lean on the Session 10 build columns. The
oracle VM cannot write to `blocklife-ai`, so the human applies this
diff separately.

**Diff to apply to
`blocklife-ai/context/kami-oracle-bootstrap/colab-setup.md`:**

#### 1. Schema cheat-sheet update — `kami_static` row

Add the build columns to the `kami_static` row in the schema cheat
sheet, with a one-line note:

> `kami_static.{level, xp, total_health, total_power, total_violence,
> total_harmony, total_slots, skills_json, equipment_json,
> build_refreshed_ts}` — build snapshot, refreshed daily,
> chain-read effective totals via the canonical formula
> `floor((1000+boost)*(base+shift)/1000)`. In-game equipment
> capacity is `1 + total_slots`. `skills_json` stores
> `[{index, points}, ...]`; `equipment_json` stores
> `[item_index, ...]`. See `memory/decoder-notes.md` "Session 10 —
> build fields on chain" for sources.

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
  `/var/log/caddy/kami-oracle.log`.
- **Backups**: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC,
  14-day retention).
- **Token rotation**: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- **DB file**: `db/kami-oracle.duckdb`. Held under exclusive lock
  by the serve process. Stop the unit before opening a DuckDB shell
  on the file directly.
- **Schema version**: 4 (Session 10 added 10 build columns to
  `kami_static` via migration 004). Storage.bootstrap auto-applies
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
