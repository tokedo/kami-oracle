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
