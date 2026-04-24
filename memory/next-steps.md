# Next Steps

## Session 5 — public plane

Session 4 closed Stage 1's internal governance pass: the service is
managed by systemd (`kami-oracle.service`, auto-restart + auto-start
on reboot), bound to `0.0.0.0:8787` with bearer-token auth, the 7-day
window self-prunes on a 3600 s cadence, and `/sql` lets any
authenticated caller run bounded read-only queries. Token lives in
`~/kami-oracle/.env` as `KAMI_ORACLE_API_TOKEN`. Full hand-off at
`memory/session-4-handoff.md`.

Session 5 makes the service reachable from outside the VPC over
HTTPS, adds the supporting durability + developer ergonomics, and
updates CLAUDE.md to reflect the Phase-D transition.

### Pre-reqs (human-ops)

The gcloud commands listed at the bottom of
`memory/session-4-handoff.md` must run first. They:
- tag `kami-oracle` and `kami-agent` VMs
- open agent→oracle:8787 over internal VPC
- reserve a static external IP and attach it to `kami-oracle`
- open :443 and :80 on `kami-oracle` from 0.0.0.0/0

**Session 5 must not proceed past Part 2 until the static IP is
attached** — the sslip.io hostname depends on it.

### Do in this order

**Part 1 — Caddy on :443 with TLS via Let's Encrypt.**
Reverse-proxy from `<ip-with-dashes>.sslip.io` (443) to
`127.0.0.1:8787` (the co-hosted FastAPI). Caddy handles HTTP-01
challenge on :80 and auto-renews. Install via apt, run as its own
systemd service, config at `/etc/caddy/Caddyfile`. The Caddyfile
should also force-redirect :80 → :443 for human traffic.

Expose **only** the eight auth'd routes + `/health` through the
reverse proxy. Don't leak `/docs`, `/openapi.json`, or any FastAPI
default. Simplest is a Caddy `handle` list; otherwise set
`openapi_url=None` on the FastAPI app.

Check in a `ops/Caddyfile` template (tokenized — actual hostname
gets filled in on the VM).

**Part 2 — Rate limit middleware.**
Add `slowapi` (or similar lightweight rate-limiter) to
`ingester/api.py`. Cap: 60 req/min per IP on GETs, 10 req/min per
IP on `/sql`, no cap on `/health` (uptime probes). Limits should
key on `X-Forwarded-For` set by Caddy, not the direct peer (which
will always be 127.0.0.1). Return 429 on breach.

**Part 3 — Nightly GCS backup.**
Cron (systemd timer, not crontab) at 03:00 UTC daily. Flow:

1. `SIGTERM` the serve process so DuckDB flushes cleanly.
2. `gsutil cp db/kami-oracle.duckdb gs://<bucket>/kami-oracle/<date>.duckdb`
3. Restart serve via `systemctl start kami-oracle`.

Total downtime ~20 s. Keep the last 14 daily snapshots; lifecycle
rule on the bucket handles expiry. Bucket name goes in a new
`ops/gcs-backup.env` (gitignored) that the timer unit sources.

**Part 4 — Colab starter notebook.**
New file `notebooks/kami-oracle-colab.ipynb` with:
- auth cell that reads the token from a Colab secret
- health check
- 2-3 example `/sql` queries showing typical aggregations
- one example pull-and-plot using pandas

The notebook must not check in any token value.

**Part 5 — CLAUDE.md Phase D note.**
Rewrite the "Current phase" and "Guardrails — never do" sections in
`CLAUDE.md` to reflect that the oracle **is** now publicly reachable
(token-auth'd), so the blanket "never expose an external endpoint"
rule no longer applies. Keep the spirit: still no MCP, still
read-only chain, still no signing.

### Deferred (not Session 5 unless human asks)

- **`kami_static` trait backfill.** Still empty; will matter once
  queries want to segment by body / hand / face. One-shot via
  `GetterSystem` read at head, upsert keyed on `kami_id`. Ingest is
  happy without it.
- **Daily rollup job.** `/actions/types` full-scans kami_action
  on every call; fine at current volume (~490k rows, ~115 ms) but
  worth precomputing `daily_action_rollup(date, action_type,
  count)` before the window grows to 28 days.
- **Decoder fix for harvest_stop / harvest_collect kami_id.**
  Currently NULL for those action_types (harvest_start populates
  correctly). A `GROUP BY kami_id` on those types collapses to one
  null bucket. Likely requires a post-decode lookup via
  `harvest_id → kami_id`, or extracting kami_id from a different
  calldata slot. See decoder-notes.md "Session 4 internal plane
  smoke test" → "Data-quality note".
- **Tier-B overlay batch** for selector `0x09c90324` against
  `system.quest.accept`. Waiting on signature confirmation.
- **Cross-action stitching** — materialize harvest_start →
  harvest_stop → harvest_collect chains keyed by `harvest_id`.
- **Concurrency refactor** for faster backfills. Single-thread
  pipeline ~2.35 blocks/s.

### Operational notes carried forward

- DB file: `db/kami-oracle.duckdb`. DuckDB holds a per-process
  exclusive lock; the serve process holds it. Use
  `sudo systemctl stop kami-oracle` before running backfill /
  prune / ad-hoc DuckDB shells directly on the file.
- Backup files: `db/kami-oracle.duckdb.session2.bak` and
  `db/kami-oracle.duckdb.session2p5.bak` can still be deleted. Did
  not remove them in Session 4 because the service was live and the
  disk has room.
- Serve logs: `logs/serve.log` (unit-appended) or
  `sudo journalctl -u kami-oracle`.
- Token rotation: edit `.env`, `sudo systemctl restart kami-oracle`.
