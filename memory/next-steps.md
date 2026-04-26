# Next Steps

## Session 10 — exploration-driven (founder picks lead)

Session 9 closed the operator-name ergonomics gap: every
`kami_static` row now carries `account_index` and `account_name`,
populated from `GetterSystem.getAccount`. 100% coverage post-backfill
(146/146 accounts, 6,923/6,923 rows). Bpeon cross-check passes — 20
kamis return `account_name='bpeon'`. Schema version is now 3.

Session 9 verification report: `memory/session-9-verification.md`.

### What Session 10 is for

**Stage 1 stays observe-the-chain.** Daily rollups and net-of-tax
views remain deferred until a consumer asks. Session 10's lead item
should come from continued Colab exploration — let the founder pick
based on what surfaces.

### Likely Session 10 candidates (founder picks priority)

These are *candidate* leads; do not pre-pick. Wait for the founder
to choose based on Colab usage + any new ergonomics gaps that
surface.

1. **Daily rollups** — only after Colab usage identifies a recurring
   query whose latency at full-scan (~150-300 ms on the public
   endpoint) is genuinely intolerable. Don't materialize speculatively.
2. **`kami-zero` wire-in (observation only)** — happens on the
   `kami-agent` VM, not here. The human sequences it through
   `blocklife-ai`. Lead query: top-20 MUSU leaderboard (7d), logged
   to kami-zero's perception loop with no effect on strategy.
3. **Tier-B `quest.accept` overlay batch** — selector `0x09c90324`
   against `system.quest.accept`. Sample list in
   `memory/unknown-systems.md` is now well over a thousand tx;
   useful for an upstream-PR-style reproduction once we have the
   signature.
4. **Net-of-tax MUSU view** — only when an operator-economics
   consumer asks. The schema records gross by design (kami
   leaderboards must use gross because tax is a node-config artifact).
5. **`kami_context` upstream PR — `getValue` typo.**
   `system-ids.md` documents the ValueComponent read fn as
   `getValue(uint256)`; the actual ABI declares only `get(uint256)`
   and `safeGet(uint256)`. Reproduction recipe in
   `memory/decoder-notes.md` "Session 7 acceptance — bpeon cross-check".
6. **Other ergonomics gaps** the founder surfaces during continued
   Colab exploration — easy to bundle into a small polish session
   like Session 9.

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
- **Typed account-centric client methods / derived account tables.**
  `kami_oracle_client` does not yet expose
  `account_summary(name=...)` or similar. Stage 1 stays "raw `/sql`
  through `kami_static` is sufficient." Add typed methods only when
  a downstream consumer asks for them.

### Hand-off to human (blocklife-ai)

The `blocklife-ai` repo's
`context/kami-oracle-bootstrap/colab-setup.md` should be updated to
lead all example queries with the new operator-name labels rather
than `owner_address`. The oracle VM cannot write to `blocklife-ai`,
so the human applies this diff separately.

**Diff to apply to
`blocklife-ai/context/kami-oracle-bootstrap/colab-setup.md`:**

For each of the four example queries plus the schema cheat sheet:

- Replace `a.kami_id` in SELECT with `s.kami_index`.
- Add `s.account_name AS operator` alongside the kami name.
- Drop `owner_address` from the SELECT (keep available for join,
  but de-emphasize in display).
- Update GROUP BY accordingly.

**Worked example — top earners (the founder's flagship):**

```sql
-- Top earners by gross MUSU (7d).
SELECT s.kami_index, s.name AS kami_name,
       s.account_name AS operator,
       SUM(CAST(a.amount AS HUGEINT)) AS musu_gross
FROM kami_action a LEFT JOIN kami_static s USING (kami_id)
WHERE a.action_type IN ('harvest_collect', 'harvest_stop')
  AND a.amount IS NOT NULL
  AND a.block_timestamp > now() - INTERVAL 7 DAY
GROUP BY s.kami_index, s.name, s.account_name
ORDER BY musu_gross DESC NULLS LAST
LIMIT 20;
```

**Liquidator leaderboard (7d):**

```sql
SELECT s.kami_index, s.name AS kami_name,
       s.account_name AS operator,
       COUNT(*) AS hits,
       SUM(CAST(a.amount AS HUGEINT)) AS musu_taken
FROM kami_action a LEFT JOIN kami_static s USING (kami_id)
WHERE a.action_type = 'harvest_liquidate'
  AND a.amount IS NOT NULL
  AND a.block_timestamp > now() - INTERVAL 7 DAY
GROUP BY s.kami_index, s.name, s.account_name
ORDER BY musu_taken DESC NULLS LAST
LIMIT 20;
```

**Bpeon operator summary (gross MUSU, 7d):**

The "filter by from_addr" form still works for tracking a specific
*signer wallet* (e.g. an automation key). The kami-centric form
filters by Account name through `kami_static`:

```sql
-- By signer wallet (raw-tx perspective):
SELECT action_type, COUNT(*) AS n,
       SUM(CAST(amount AS HUGEINT)) AS musu_gross
FROM kami_action
WHERE from_addr = '0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2'
  AND block_timestamp > now() - INTERVAL 7 DAY
GROUP BY 1 ORDER BY n DESC;

-- By Account (kami-centric perspective via kami_static):
SELECT a.action_type, COUNT(*) AS n,
       SUM(CAST(a.amount AS HUGEINT)) AS musu_gross
FROM kami_action a JOIN kami_static s USING (kami_id)
WHERE s.account_name = 'bpeon'
  AND a.block_timestamp > now() - INTERVAL 7 DAY
GROUP BY 1 ORDER BY n DESC;
```

These can return different numbers under automation: the first
counts every tx the bpeon operator wallet signs (any kami it
operates); the second counts every action by a kami the bpeon
*Account* owns (regardless of signer). For ownership / fleet
analysis, prefer the second form.

**Liquidation pairing query** — show attacker + victim by Account:

```sql
SELECT
  ls.kami_index AS attacker_idx, ls.name AS attacker_name,
  ls.account_name AS attacker_op,
  vs.kami_index AS victim_idx, vs.name AS victim_name,
  vs.account_name AS victim_op,
  CAST(l.amount AS HUGEINT) AS musu_taken,
  l.node_id, l.block_timestamp
FROM kami_action l
LEFT JOIN kami_static ls ON l.kami_id = ls.kami_id
LEFT JOIN kami_action h ON l.harvest_id = h.harvest_id
                       AND h.action_type = 'harvest_start'
LEFT JOIN kami_static vs ON h.kami_id = vs.kami_id
WHERE l.action_type = 'harvest_liquidate'
  AND l.amount IS NOT NULL
  AND l.block_timestamp > now() - INTERVAL 7 DAY
ORDER BY l.block_timestamp DESC
LIMIT 50;
```

**Schema cheat-sheet callout — operator name vs signer wallet:**

> **Operator name vs signer wallet.** `kami_static.account_name` is
> the in-game Account display name ("bpeon", "ray charles"). It's
> the right label for kami-centric queries that join through
> `kami_static`. For raw-tx queries that filter on
> `kami_action.from_addr`, that's the signer wallet (could be a
> kamibots automation key, not the same as the kami's owning
> account). The two coincide for accounts that operate manually,
> but diverge under automation.

### Operational notes carried forward

- **Public URL**: `https://136-112-224-147.sslip.io`. Bearer token
  in the VM's `~/kami-oracle/.env` under `KAMI_ORACLE_API_TOKEN`.
- **Service control**: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- **Logs**: `logs/serve.log`, `logs/backup.log`,
  `logs/backfill-musu.log`, `logs/backfill-liquidate-harvest-id.log`,
  `logs/backfill-account-names.log` (Session 9 backfill output
  preserved), `/var/log/caddy/kami-oracle.log`.
- **Backups**: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC,
  14-day retention).
- **Token rotation**: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- **DB file**: `db/kami-oracle.duckdb`. Held under exclusive lock
  by the serve process. Stop the unit before opening a DuckDB shell
  on the file directly.
- **Schema version**: 3 (Session 9 added `kami_static.account_index`
  and `kami_static.account_name` via migration 003). Storage.bootstrap
  auto-applies pending migrations on every start; bump
  `SCHEMA_VERSION` in `ingester/storage.py` and add a numbered file
  under `migrations/` for the next change.
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
