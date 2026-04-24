# Next Steps

## Session 6 — `kami_static` backfill, daily rollups, kami-zero wiring

Session 5 closed the public plane: Caddy terminates TLS at
`https://136-112-224-147.sslip.io`, FastAPI sits on `127.0.0.1:8787`
behind it, every non-`/health` route requires a bearer token,
per-token rate limit defaults to 60/min, nightly `EXPORT DATABASE`
backups ship to GCS via cron + a loopback `/backup` endpoint, and
the founder-facing Colab notebook is committed at
`scripts/colab_starter.ipynb`. Phase D is recorded in
`memory/phase-d-transition.md` and CLAUDE.md was updated to lift the
"never expose an external endpoint" rule. Full hand-off:
`memory/founder-testing.md`.

Session 6 should pick up the deferred-from-Session-4 backfill work,
add precomputed rollups, and wire `kami-zero` to the oracle.

### Pre-reqs (human-ops)

Open question in `memory/questions-for-human.md` — VM service-account
scope is `devstorage.read_only`, which blocks the nightly GCS upload.
Founder needs to widen scope (Option A in the question is preferred).
Not blocking Session 6 work, but the cron will keep failing at the
upload step until it's fixed.

### Do in this order

**Part 1 — `kami_static` backfill worker.**
The table has been empty since Stage 1 kickoff. Read per-kami traits
via the appropriate getter system at head, upsert keyed on
`kami_id`. One-shot script that walks every distinct `kami_id` seen
in `kami_action` over the rolling window, then a periodic sweep for
new kamis. Body / hand / face / background + base stats per the
schema in `schema/schema.sql`. Don't block ingest.

**Part 2 — Daily rollup tables.**
`/actions/types` and `/nodes/top` full-scan `kami_action` per call.
Fine at 500k rows / 90 ms latency, but the window will likely
extend to 28 days in a future session. Materialize:
- `daily_action_rollup(date, action_type, count)`
- `daily_node_rollup(date, node_id, harvest_starts)`

Recompute nightly (just before the prune sweep) for the trailing 7
days; the rest is immutable history. Update the two endpoints to
read from the rollup when `since_days >= some-threshold` and fall
back to live aggregation otherwise.

**Part 3 — Wire `kami-zero` to the oracle (low-stakes).**
On the `kami-agent` VM (not the oracle VM):
- add a small client module `kami_oracle_client.py` that wraps
  `requests.post(/sql)` and `requests.get(...)` with the bearer
  token loaded from kami-agent's own `.env`
- call it from kami-zero's perception loop, **observation only** —
  one or two queries that log what the oracle sees and don't yet
  affect strategy. Goal is to prove the wire works under load
  before any decision-making depends on it.
- the public URL is `https://136-112-224-147.sslip.io`; token gets
  set in kami-agent's `.env` as `KAMI_ORACLE_TOKEN`.

**Part 4 — Begin a Python client library at `client/`.**
`client/__init__.py` exporting an `OracleClient` class that wraps
the same shape as the Colab notebook's `sql()` helper plus typed
methods for the eight REST routes. Vendored into `kami-zero` (and
future agents) the same way `kami_context/` is vendored — by a
sibling script. This is the seed of a stable consumer surface, so
take naming and pagination seriously. Tests against the live
loopback endpoint.

### Deferred (not Session 6 unless human asks)

- **MCP server.** Still post-decision. Wait until kami-zero's `/sql`
  usage is stable and it's clear what shape an MCP wrapper should
  take.
- **Decoder fix for `harvest_stop` / `harvest_collect` `kami_id`.**
  Currently NULL for those action_types. Likely a post-decode
  lookup via `harvest_id → kami_id` or a different calldata slot.
  Cross-action stitching (Part below) probably fixes this naturally.
- **Cross-action stitching.** Materialize harvest_start →
  harvest_stop → harvest_collect chains keyed on `harvest_id`.
- **Tier-B overlay batch** for selector `0x09c90324` against
  `system.quest.accept`. Still waiting on signature confirmation.
- **Concurrency refactor** for faster backfills. Single-thread
  pipeline ~2.35 blocks/s; current cursor lag (~36 h, recovering at
  ~2 blocks/s net) is tolerable but will become annoying on each
  redeploy.
- **Window extension to 28 days.** Wait until rollups land so the
  histogram endpoints stay snappy.

### Operational notes carried forward

- Public URL: `https://136-112-224-147.sslip.io`. Bearer token in
  the VM's `~/kami-oracle/.env`.
- Service control: `sudo systemctl (status|restart) kami-oracle`,
  `sudo systemctl reload caddy` after `/etc/caddy/Caddyfile` edits.
- Logs: `logs/serve.log`, `logs/backup.log`,
  `/var/log/caddy/kami-oracle.log`.
- Backups: `gs://kami-oracle-backups/` (cron `15 4 * * *` UTC,
  14-day retention) — currently blocked on SA scope.
- Token rotation: regenerate via `python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`, update `.env`, restart, then
  redistribute to Colab secrets and kami-agent's `.env`.
- DB file: `db/kami-oracle.duckdb`. Held under exclusive lock by
  the serve process. Stop the unit before opening a DuckDB shell
  on the file directly.
- Backup leftovers: `db/kami-oracle.duckdb.session2*.bak` can still
  be removed; preserved only because the disk has room.
