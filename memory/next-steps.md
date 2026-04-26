# Next Steps

## Session 9 — exploration-driven (founder runs Colab next)

Session 8 closed the foundation: `client/` Python library is published
and tested, MUSU gross-vs-net semantics are documented end-to-end
(schema → README → client docstrings → decoder notes), retention
window is 28 days, and `harvest_liquidate.harvest_id` was backfilled
from `metadata_json.victim_harvest_id` so historical liquidation joins
work.

The session-end report lives at `memory/session-8-verification.md`
with the live coverage numbers and the cursor / lag snapshot. Read it
first if you're picking up Session 9.

### What Session 9 is for

**Stage 1 stays observe-the-chain.** The previous hand-off proposed
daily rollup tables; the founder has explicitly deferred them until
exploration shows which queries warrant pre-computation. We don't
know which aggregations matter yet, and we won't until the founder
has run real queries against the new client library in Colab. Lead
items for Session 9 should come out of that exploration — not from a
speculative roadmap.

### Likely Session 9 candidates (founder picks priority)

These are *candidate* leads; do not pre-pick. Wait for the founder to
say which one Session 9 should run with based on what fell out of
their Colab exploration.

1. **`kami-zero` wire-in (observation only)** — happens on the
   `kami-agent` VM, not here. The human sequences it through
   `blocklife-ai`. Lead query: top-20 MUSU leaderboard (7d), logged
   to kami-zero's perception loop with no effect on strategy. Use the
   newly-vendored `kami_oracle_client` (see
   `scripts/vendor-client.sh`).
2. **Tier-B overlay batch** for selector `0x09c90324` against
   `system.quest.accept`. Sample list in `memory/unknown-systems.md`
   is now well over a thousand tx. Useful for an upstream-PR-style
   reproduction once we have the signature.
3. **`kami_context` upstream PR — `getValue` typo.** `system-ids.md`
   documents the ValueComponent read fn as `getValue(uint256)`. The
   actual ABI declares only `get(uint256)` and `safeGet(uint256)`;
   calls to `getValue` revert. Reproduction recipe in
   `memory/decoder-notes.md` "Session 7 acceptance — bpeon cross-check".

### Deferred-until-asked

Do not pull these forward without the founder confirming the demand.
Stage 1 is "make the raw data trustworthy and accessible" — not "ship
every plausible aggregation."

- **Daily rollup tables** (`daily_action_rollup`, `daily_node_rollup`,
  `daily_musu_rollup`). Materializing these *before* the founder has
  identified which queries actually warrant pre-computation would
  burn complexity on speculative work. Wait for Colab exploration to
  surface a specific query whose latency we can't tolerate at
  full-scan, then pre-compute *that* one.
- **Net-of-tax MUSU view / column.** The schema records gross by
  design — kami leaderboards must use gross because tax is a
  node-config artifact, not a kami stat (see "MUSU semantics" in
  `decoder-notes.md` and the README). Net is operator-side
  economics; derive on the fly via a join to `harvest_start.taxAmt`
  in `metadata_json` when an operator-economics consumer asks.
  Don't precompute until that consumer exists.
- **MCP server.** Still post-decision. Wait until kami-zero's
  client-library usage is stable and the shape an MCP wrapper should
  take is obvious.

### Hand-off to human (blocklife-ai)

The `blocklife-ai` repo (not this one) has a
`context/kami-oracle-bootstrap/colab-setup.md` doc that still divides
MUSU by `1e18`. That's wrong — MUSU is an integer item-count, not an
ERC-20 token. The oracle VM cannot write to `blocklife-ai`, so the
human applies this diff separately.

**Diff to apply to `blocklife-ai/context/kami-oracle-bootstrap/colab-setup.md`:**

1. Drop `/ 1e18` everywhere it appears in MUSU queries.
2. Cast as `CAST(amount AS HUGEINT)` (not `CAST(amount AS DECIMAL)`)
   — DuckDB's HUGEINT handles uint256 magnitudes; DECIMAL silently
   loses precision past 38 digits.
3. Add a one-paragraph "MUSU semantics" callout near the top so
   Colab explorers don't accidentally rank by net:

   > **MUSU semantics.** `kami_action.amount` is **gross** MUSU
   > pre-tax — the integer item-count drained from the harvest entity
   > before the on-chain tax split. Always use gross for kami
   > comparisons (leaderboards, productivity rankings); tax varies by
   > node and would distort rankings if folded in. For operator
   > economics (net-of-tax), join to the matching `harvest_start`
   > row's `metadata.taxAmt`: `net = gross - gross * taxAmt / 1e4`.
   > Cast as `CAST(amount AS HUGEINT)` — never divide by 1e18.

4. Add three example queries (verbatim from
   `memory/decoder-notes.md` "Session 7 acceptance — golden queries"
   and the bpeon cross-check):

   ```sql
   -- Top earners by gross MUSU (7d).
   SELECT a.kami_id, s.name, s.owner_address,
          SUM(CAST(a.amount AS HUGEINT)) AS musu_gross
   FROM kami_action a LEFT JOIN kami_static s USING (kami_id)
   WHERE a.action_type IN ('harvest_collect', 'harvest_stop')
     AND a.amount IS NOT NULL
     AND a.block_timestamp > now() - INTERVAL 7 DAY
   GROUP BY a.kami_id, s.name, s.owner_address
   ORDER BY musu_gross DESC NULLS LAST
   LIMIT 20;

   -- Liquidator leaderboard (7d).
   SELECT a.kami_id, s.name,
          COUNT(*) AS hits,
          SUM(CAST(a.amount AS HUGEINT)) AS musu_taken
   FROM kami_action a LEFT JOIN kami_static s USING (kami_id)
   WHERE a.action_type = 'harvest_liquidate'
     AND a.amount IS NOT NULL
     AND a.block_timestamp > now() - INTERVAL 7 DAY
   GROUP BY a.kami_id, s.name
   ORDER BY musu_taken DESC NULLS LAST
   LIMIT 20;

   -- bpeon operator summary (gross MUSU, 7d).
   SELECT action_type, COUNT(*) AS n,
          SUM(CAST(amount AS HUGEINT)) AS musu_gross
   FROM kami_action
   WHERE from_addr = '0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2'
     AND block_timestamp > now() - INTERVAL 7 DAY
   GROUP BY 1 ORDER BY n DESC;
   ```

5. (Optional) Mention the `client/` library as an alternative to raw
   `requests` calls — once vendored into Colab via
   `scripts/vendor-client.sh /content/`, `from kami_oracle_client
   import OracleClient` gives typed `harvest_leaderboard()`,
   `health()`, etc. The raw `sql()` helper in the existing notebook
   stays useful for ad-hoc work.

### Operational notes carried forward

- **Public URL**: `https://136-112-224-147.sslip.io`. Bearer token
  in the VM's `~/kami-oracle/.env` under `KAMI_ORACLE_API_TOKEN`.
- **Service control**: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- **Logs**: `logs/serve.log`, `logs/backup.log`,
  `logs/backfill-musu.log`, `logs/backfill-liquidate-harvest-id.log`
  (Session 8 backfill output preserved), `/var/log/caddy/kami-oracle.log`.
- **Backups**: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC,
  14-day retention).
- **Token rotation**: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- **DB file**: `db/kami-oracle.duckdb`. Held under exclusive lock by
  the serve process. Stop the unit before opening a DuckDB shell on
  the file directly.
- **Schema version**: 2 (added `kami_action.harvest_id` in Session 6;
  no schema change in Sessions 7 or 8). Storage.bootstrap auto-applies
  pending migrations on every start; bump `SCHEMA_VERSION` in
  `ingester/storage.py` and add a numbered file under `migrations/`
  for the next change.
- **Backfill scripts that touch the DB require the service stopped**
  (DuckDB exclusive file lock). Pattern:
  `sudo systemctl stop kami-oracle && python scripts/<name>.py &&
  sudo systemctl start kami-oracle`.
- **Retention window**: now 28 days (`KAMI_ORACLE_WINDOW_DAYS=28` in
  both `.env` and `env.template`, set Session 8). The window fills
  in over ~3 weeks as the chain is ingested — full 28-day rolling
  window expected by 2026-05-24.
- **Client library**: `client/` is the stable consumer surface.
  Vendor into downstream repos with `scripts/vendor-client.sh
  /path/to/repo` (lands as `<target>/kami_oracle_client/`).
