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
