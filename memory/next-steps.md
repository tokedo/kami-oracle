# Next Steps

## Session 7 — daily rollups, client library, kami-zero wiring

Session 6 closed the core data-quality gaps:

- **`kami_static` is populated** (was empty since Stage 1 kickoff). Per-kami
  traits, base stats, name, and owner_address are read from the
  GetterSystem and refreshed every 6 hours. Owner address is recovered
  offline from `account_id` via the documented `uint256(uint160(addr))`
  cast — no extra eth_calls.
- **`kami_id` is now populated on `harvest_stop` and `harvest_collect`**
  for new rows AND every historical row in the rolling window. The
  decoder writes `harvest_id` as a first-class column for all
  `harvest_*` actions, and the in-process `HarvestResolver` stitches
  `kami_id` via the deterministic
  `keccak256(b"harvest" || uint256_be(kami_id))` mapping. No on-chain
  reads required.
- **The cursor is at chain head** and has been since before Session 6
  opened — single-threaded poller is keeping pace at ~2 blk/s steady
  state. The "~36 h behind" claim in the Session 6 prompt was stale
  from Session 5; concurrency refactor is deferred unless drift
  returns.
- Schema migration v1 → v2 (`harvest_id` column on `kami_action`) is
  wired into `Storage.bootstrap()` so service restarts auto-apply.
- `/health` now reports `kami_static` row count, `last_refreshed_ts`,
  and `chain_head_lag_seconds` for at-a-glance liveness checks.

Founder-facing acceptance: the golden query at the top of the Session 6
prompt now returns a real leaderboard with names, owners, harvest
counts, and MUSU totals. See `memory/decoder-notes.md` "Session 6
acceptance" for the top-5 rows.

### Pre-reqs (human-ops)

None blocking. The GCS scope was widened on 2026-04-24 and nightly
backups have been uploading cleanly since.

### Do in this order

**Part 1 — Receipt log decoding for MUSU amounts.** The Session 6
golden query asks for `SUM(amount)` MUSU collected, but `amount` is
NULL for every harvest_stop / harvest_collect — MUSU bounty doesn't
appear in calldata, it's an ERC20 Transfer event log on the MUSU token
(`0xE1Ff7038eAAAF027031688E1535a055B2Bac2546`) inside the tx receipt.
`process_block_range` already fetches receipts for status/gas; extend
it to walk `receipt.logs`, match the MUSU Transfer signature, and
populate `amount` on qualifying rows. Smallest delta that makes the
founder's golden query work end-to-end. Same plumbing unlocks
MUSU-denominated metrics for `kami-zero`.

**Part 2 — Daily rollup tables** (originally session-6-deferred).
`/actions/types` and `/nodes/top` full-scan `kami_action` per call.
Fine at 300k rows / sub-100ms latency, but the window will likely
extend to 28 days in a future session and `kami_action` is approaching
500k. Materialize:
- `daily_action_rollup(date, action_type, count)`
- `daily_node_rollup(date, node_id, harvest_starts)`

Recompute nightly (just before the prune sweep) for the trailing 7
days; the rest is immutable history. Update the two endpoints to
read from the rollup when `since_days >= some-threshold` and fall back
to live aggregation otherwise.

**Part 2 — Begin a Python client library at `client/`.**
`client/__init__.py` exporting an `OracleClient` class that wraps the
same shape as the Colab notebook's `sql()` helper plus typed methods
for the eight REST routes. Vendored into `kami-zero` (and future
agents) the same way `kami_context/` is vendored — by a sibling
script. This is the seed of a stable consumer surface, so take naming
and pagination seriously. Tests against the live loopback endpoint.

**Part 3 — Wire `kami-zero` to the oracle (low-stakes).**
On the `kami-agent` VM (not the oracle VM):
- import the new `client/` package (or vendor it).
- call it from kami-zero's perception loop, **observation only** — one
  or two queries that log what the oracle sees and don't yet affect
  strategy. The golden query (top earners 24 h) is a natural first
  observation. Goal is to prove the wire works under load before any
  decision-making depends on it.
- the public URL is `https://136-112-224-147.sslip.io`; token gets set
  in kami-agent's `.env` as `KAMI_ORACLE_TOKEN`.

### Deferred (not Session 7 unless human asks)

- **MCP server.** Still post-decision. Wait until kami-zero's `/sql`
  usage is stable and it's clear what shape an MCP wrapper should
  take.
- **Tier-B overlay batch** for selector `0x09c90324` against
  `system.quest.accept`. Still waiting on signature confirmation.
  Sample list in `memory/unknown-systems.md` keeps growing — useful
  for an upstream-PR-style reproduction case.
- **Poller concurrency refactor.** Cursor at chain head, so not
  blocking. Re-evaluate if drift returns post any large catchup
  (e.g. after a multi-hour outage or a large redeploy).
- **Window extension to 28 days.** Wait until rollups land so the
  histogram endpoints stay snappy.
- **Cross-action stitching for non-harvest actions.** `item_craft`,
  `move`, `scavenge_claim`, and ~5 other action_types still have NULL
  `kami_id` because their calldata genuinely doesn't carry one
  (operates on a recipe / account / scavenge entity). A future pass
  could resolve via `from_addr` (operator) → owner account → owned
  kamis, but only if the founder finds the data useful enough to
  justify the join cost.

### Operational notes carried forward

- Public URL: `https://136-112-224-147.sslip.io`. Bearer token in the
  VM's `~/kami-oracle/.env`.
- Service control: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- Logs: `logs/serve.log`, `logs/backup.log`,
  `/var/log/caddy/kami-oracle.log`.
- Backups: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC, 14-day
  retention) — uploading cleanly since the SA scope fix on 2026-04-24.
- Token rotation: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- DB file: `db/kami-oracle.duckdb`. Held under exclusive lock by the
  serve process. Stop the unit before opening a DuckDB shell on the
  file directly.
- Backup leftovers: `db/kami-oracle.duckdb.session2*.bak` can still be
  removed; preserved only because the disk has room.
- Schema version: 2 (added `kami_action.harvest_id` in Session 6).
  Storage.bootstrap auto-applies pending migrations on every start;
  bump `SCHEMA_VERSION` in `ingester/storage.py` and add a numbered
  file under `migrations/` for the next change.
