# Session 4 Hand-off — Internal Query Plane

Session 4 closed on 2026-04-24 22:20 UTC. Status: **internal query
plane is live**. The service is token-authenticated, bound to
`0.0.0.0:8787` on the oracle VM, managed by systemd, and self-prunes
the 7-day window. Public HTTPS front is deferred to Session 5.

## Operations

### Service management

`screen` is no longer the supervisor. Use `systemd`:

```bash
# State
sudo systemctl status kami-oracle

# Stop / start / restart
sudo systemctl stop kami-oracle
sudo systemctl start kami-oracle
sudo systemctl restart kami-oracle

# Live tail
sudo journalctl -u kami-oracle -f
# OR (structured app logs, appended by the unit file)
tail -f ~/kami-oracle/logs/serve.log
```

`Restart=always` + `RestartSec=5` means the process comes back on
crash. The unit is `enabled`, so it survives reboot.

Graceful shutdown on SIGTERM: uvicorn drains in-flight HTTP, the
poller + prune threads see `stop_event`, then `storage.close()`
flushes DuckDB. `TimeoutStopSec=30` gives the drain 30s before
systemd falls back to SIGKILL.

### Token location

The bearer token lives in `~/kami-oracle/.env` under
`KAMI_ORACLE_API_TOKEN`. The .env file is gitignored — the token
never hits the repo.

Current value (paste this into Colab / kami-agent):

```
pV6WYI4HUSLWK95cSg_YJbDlD6rTdCDaYCCMqhQvTl8
```

To rotate, generate a new one and restart the service:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Edit ~/kami-oracle/.env, replace KAMI_ORACLE_API_TOKEN=...
sudo systemctl restart kami-oracle
```

## Endpoints

Base URL inside the VPC: `http://<oracle-vm-ip>:8787`. From the
oracle VM itself, loopback works too: `http://127.0.0.1:8787`.

**Open (no auth)**

| Method | Path      | Purpose                     |
|--------|-----------|-----------------------------|
| GET    | `/health` | Cursor + row counts + registry summary |

**Authenticated (require `Authorization: Bearer <token>`)**

| Method | Path                                 | Purpose                              |
|--------|--------------------------------------|--------------------------------------|
| GET    | `/kami/{kami_id}/actions`            | Per-kami action stream (since_days) |
| GET    | `/kami/{kami_id}/summary`            | Per-kami action_type breakdown       |
| GET    | `/operator/{addr}/summary`           | Per-operator action_type breakdown   |
| GET    | `/actions/types`                     | Aggregated action_type counts        |
| GET    | `/actions/recent`                    | Recent action rows (time-desc)       |
| GET    | `/nodes/top`                         | Top harvest_start node_ids           |
| GET    | `/registry/snapshot`                 | Deployed system→address map          |
| POST   | `/sql`                               | Ad-hoc read-only SELECT / PRAGMA     |

Clamps on the GET endpoints: `since_days ≤ 28`, `limit ≤ 2000`.

### `/sql` shape

```
POST /sql
Authorization: Bearer <token>
Content-Type: application/json
Body: {"q": "<sql>", "limit": <int optional>}

Response:
{
  "columns": [...],
  "rows":    [[...], ...],
  "row_count": N,
  "truncated": bool,
  "latency_ms": N
}
```

Errors: `400` on validation, `400` on DuckDB execution, `504` on
wall-clock timeout, `401` on bad / missing token, `422` on malformed
request body.

## Known limits

| Knob                     | Value    | Source                     |
|--------------------------|----------|----------------------------|
| `/sql` row cap           | 10,000   | `SQL_MAX_LIMIT` in api.py  |
| `/sql` wall-clock        | 10 s     | `SQL_TIMEOUT_S` in api.py  |
| `/sql` statement kinds   | SELECT, WITH, SHOW, DESCRIBE, EXPLAIN, PRAGMA, SUMMARIZE | `validate_readonly_sql` |
| `/sql` max query length  | 10,000 chars | `MAX_QUERY_CHARS` in sql.py |
| GET `since_days`         | 1..28    | `MAX_SINCE_DAYS` in api.py |
| GET `limit`              | 1..2000  | `MAX_LIMIT` in api.py      |
| Rolling retention        | 7 days   | `KAMI_ORACLE_WINDOW_DAYS`  |
| Prune cadence            | 1 h      | `KAMI_ORACLE_PRUNE_INTERVAL_S` |

**Validator false positive**: `/sql` queries that mention a blacklisted
token (DROP, INSERT, etc.) inside a string literal are rejected —
the validator is a whole-word regex, not a full tokenizer. Rewrite
the predicate if you hit this.

## What Session 5 does

Next session adds the **public plane**:

- **Caddy** on :443, auto-TLS via Let's Encrypt for
  `<ip-with-dashes>.sslip.io`. Upstream → `127.0.0.1:8787`.
- **Rate limit middleware** on the app (slowapi or equivalent).
- **Nightly GCS backup** of `db/kami-oracle.duckdb`.
- **Colab starter notebook** checked into repo.
- **CLAUDE.md Phase D note** transitioning from Stage 1 "local-only"
  to Phase D "hosted + auth'd".

The `kami_static` trait backfill and daily rollup materialization
are punted to Session 6 — they're build-out work, not public-plane
work.

## Human-ops steps (founder must run before Session 5)

These require GCP admin creds the agent doesn't have. Run from a
laptop authenticated to `kami-agent-prod`.

```bash
# 1. Tag the oracle VM so firewall rules can target it
gcloud compute instances add-tags kami-oracle \
  --tags=kami-oracle \
  --zone=us-central1-a \
  --project=kami-agent-prod

# 2. Tag the agent VM
gcloud compute instances add-tags kami-agent \
  --tags=kami-agent \
  --zone=us-central1-a \
  --project=kami-agent-prod

# 3. Allow kami-agent → kami-oracle:8787 over internal VPC
gcloud compute firewall-rules create allow-oracle-from-agent \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:8787 \
  --source-tags=kami-agent \
  --target-tags=kami-oracle \
  --project=kami-agent-prod

# 4. Reserve a STATIC external IP and attach to kami-oracle
#    (Required by Session 5 for stable sslip.io hostname.)
#    First: reserve the static IP.
gcloud compute addresses create kami-oracle-ip \
  --region=us-central1 \
  --project=kami-agent-prod
#    Note the reserved IP:
gcloud compute addresses describe kami-oracle-ip \
  --region=us-central1 --project=kami-agent-prod --format='value(address)'
#    Then reassign the VM's external interface to it (SSH drops — reconnect):
gcloud compute instances delete-access-config kami-oracle \
  --access-config-name='external-nat' \
  --zone=us-central1-a --project=kami-agent-prod
gcloud compute instances add-access-config kami-oracle \
  --access-config-name='external-nat' \
  --address=<STATIC_IP_FROM_ABOVE> \
  --zone=us-central1-a --project=kami-agent-prod

# 5. Open port 443 (and :80 for Let's Encrypt HTTP-01) publicly
gcloud compute firewall-rules create allow-oracle-https \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:443,tcp:80 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=kami-oracle \
  --project=kami-agent-prod
```

### Founder verification from laptop

While the public plane isn't up yet, the fastest way to hit the API
from a laptop is an IAP tunnel:

```bash
gcloud compute start-iap-tunnel kami-oracle 8787 \
  --local-host-port=localhost:8787 \
  --zone=us-central1-a --project=kami-agent-prod
# In another terminal:
TOKEN='pV6WYI4HUSLWK95cSg_YJbDlD6rTdCDaYCCMqhQvTl8'
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8787/health | jq .
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8787/actions/types?since_days=7" | jq '{total, by_type: .by_type[:5]}'
```

## Service state snapshot at hand-off

```
systemctl is-active kami-oracle   # active
systemctl is-enabled kami-oracle  # enabled
ss -tlnp | grep 8787              # 0.0.0.0:8787 (python)

/health returned last_block_scanned=27981158, row_counts.raw_tx=266435,
kami_action=489491, n_systems=34, n_addresses=40.
```

Poller is still in catchup from a pre-session gap — cursor was ~2
days behind head at session start and was narrowing at session end
(~9k blocks/h vs head ~3.5k blocks/h).
