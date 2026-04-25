# Next Steps

## Session 8 — client library, daily rollups, kami-zero wiring

Session 7 closed the last data-quality gap from Session 6:
**`kami_action.amount` is now populated for `harvest_collect`,
`harvest_stop`, and `harvest_liquidate`** (live decoder + 107,040
historical backfill). The founder's golden queries return real MUSU
totals; see `memory/decoder-notes.md` "Session 7 acceptance" for the
top-5 rows of each.

**One important framing correction from Session 6's hand-off**:
$MUSU is **not** an ERC-20 (the address `0xE1Ff7038e…` Session 6
named is actually WETH). MUSU is the in-game item index 1, tracked
by a MUDS `ValueComponent` write and decoded from the receipt's
`ComponentValueSet` events on the World contract. Full derivation
in `memory/decoder-notes.md` "Session 7 — MUSU Transfer probe".

### Pre-reqs (human-ops)

None blocking. Service running, cursor at chain head, backups
uploading nightly to GCS.

### Do in this order

**Part 1 — Python client library at `client/`.** Originally a
Session-7 deferred item; the prompt for Session 7 prioritized
MUSU instead, so this is the lead item now. Goals:

- `client/__init__.py` exporting an `OracleClient` class wrapping the
  same shape as the Colab notebook's `sql()` helper plus typed
  methods for the eight REST routes (`/health`, `/kami/{id}/actions`,
  `/kami/{id}/summary`, `/operator/{addr}/summary`,
  `/actions/types`, `/nodes/top`, `/actions/recent`,
  `/registry/snapshot`).
- The client should expose a `harvest_leaderboard(since_days=7)`
  convenience method that runs the founder's golden query (the
  one above with `SUM(CAST(amount AS HUGEINT))`). MUSU is integer
  item-count, **never** divide by 1e18.
- Vendored into `kami-zero` (and any future agent) the same way
  `kami_context/` is vendored — by a sibling script under
  `scripts/`.
- Tests against the live loopback endpoint (`tests/test_client.py`),
  similar shape to `tests/test_api.py`.

This is the seed of a stable consumer surface, so take naming and
pagination seriously.

**Part 2 — Daily rollup tables.** `/actions/types` and `/nodes/top`
full-scan `kami_action` per call. Fine at 312k rows / sub-100ms
latency, but `kami_action` is approaching 500k and the window will
likely extend to 28 days in a future session. Materialize:

- `daily_action_rollup(date, action_type, count)`
- `daily_node_rollup(date, node_id, harvest_starts)`
- `daily_musu_rollup(date, kami_id, musu_gross)` — new for
  Session 7, lets the leaderboard hit a 7-row scan instead of
  filtering 100k+ rows.

Recompute nightly (just before the prune sweep) for the trailing
7 days; the rest is immutable history. Update the relevant
endpoints to read from the rollup when `since_days >= some-threshold`
and fall back to live aggregation otherwise.

**Part 3 — Wire `kami-zero` to the oracle (low-stakes).**
On the `kami-agent` VM (not the oracle VM):

- import the new `client/` package (or vendor it).
- call it from kami-zero's perception loop, **observation only** —
  one or two queries that log what the oracle sees and don't yet
  affect strategy. The MUSU leaderboard (top 20 in 7d) is a
  natural first observation. Goal is to prove the wire works under
  load before any decision-making depends on it.
- the public URL is `https://136-112-224-147.sslip.io`; token gets
  set in kami-agent's `.env` as `KAMI_ORACLE_TOKEN`.

### Deferred (not Session 8 unless human asks)

- **Tier-B overlay batch** for selector `0x09c90324` against
  `system.quest.accept`. Sample list in `memory/unknown-systems.md`
  is large now (~1.1k+ tx); useful for an upstream-PR-style
  reproduction case once we have the signature.
- **Window extension to 28 days.** Wait until rollups land so the
  histogram endpoints stay snappy.
- **`kami_context` upstream PR — `getValue` typo.**
  `system-ids.md` documents the ValueComponent read fn as
  `getValue(uint256)`. The actual ABI declares only `get(uint256)`
  and `safeGet(uint256)`; calls to `getValue` revert. Caught
  during Session 7 cross-check; reproduction recipe is in the
  `Session 7 acceptance — bpeon cross-check` block of
  `memory/decoder-notes.md`. Easy upstream PR when we get to it.
- **MCP server.** Still post-decision. Wait until kami-zero's
  `/sql` usage is stable and it's clear what shape an MCP wrapper
  should take.
- **Net-of-tax MUSU column.** `amount` records the gross drained
  from the harvest entity. If a downstream consumer wants the
  *net* (operator-side) MUSU, the cleanest path is a derived view
  that joins to the `taxAmt` parameter on the matching
  `harvest_start` row. Skip until a consumer asks; Stage 1 is
  observe-the-chain, not pre-compute everything.
- **Liquidate-row `harvest_id` backfill.** ~99.7% of historical
  `harvest_liquidate` rows have NULL `harvest_id` (the field-map
  fix landed after they were decoded). The MUSU decoder falls
  back to `metadata_json.victim_harvest_id` so this didn't block
  Session 7, but it'd be a one-line `UPDATE` to copy the metadata
  field into the column for SQL ergonomics.

### Operational notes carried forward

- Public URL: `https://136-112-224-147.sslip.io`. Bearer token in
  the VM's `~/kami-oracle/.env`.
- Service control: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- Logs: `logs/serve.log`, `logs/backup.log`,
  `logs/backfill-musu.log` (Session 7 backfill output preserved
  for reference), `/var/log/caddy/kami-oracle.log`.
- Backups: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC,
  14-day retention). Uploading cleanly since the SA scope fix on
  2026-04-24.
- Token rotation: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- DB file: `db/kami-oracle.duckdb`. Held under exclusive lock by
  the serve process. Stop the unit before opening a DuckDB shell on
  the file directly.
- Schema version: 2 (added `kami_action.harvest_id` in Session 6;
  no schema change in Session 7). Storage.bootstrap auto-applies
  pending migrations on every start; bump `SCHEMA_VERSION` in
  `ingester/storage.py` and add a numbered file under
  `migrations/` for the next change.
- **Backfill scripts that touch the DB require the service stopped**
  (DuckDB exclusive file lock). Pattern:
  `sudo systemctl stop kami-oracle && python scripts/<name>.py &&
  sudo systemctl start kami-oracle`.
