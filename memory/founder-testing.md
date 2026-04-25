# Founder Testing Guide — kami-oracle public plane (Session 5 hand-off)

The oracle is now publicly reachable over HTTPS. Phase D was
authorized 2026-04-24; see `memory/phase-d-transition.md` for the
decision record.

## Reach the API from anywhere

- **Public URL**: `https://136-112-224-147.sslip.io`
- **Auth**: `Authorization: Bearer <token>` on every route except
  `/health`. Token is in the VM's `~/kami-oracle/.env` under
  `KAMI_ORACLE_API_TOKEN`. Pull it with:
  ```bash
  gcloud compute ssh kami-oracle --zone=us-central1-a \
      --project=kami-agent-prod \
      --command "grep '^KAMI_ORACLE_API_TOKEN=' ~/kami-oracle/.env | cut -d= -f2-"
  ```
- **Rate limit**: 60 req/min per token, fixed window. `/health`
  is exempt. 429s come back with a `Retry-After` header.

No SSH tunnel needed anymore — just `curl https://…` from your
laptop.

```bash
curl -s https://136-112-224-147.sslip.io/health | jq .
```

## Colab

Starter notebook: `scripts/colab_starter.ipynb` in the repo. Open it
in Colab, then:

1. Tools → Secrets → add `KAMI_ORACLE_TOKEN`, paste the bearer.
2. Toggle "Notebook access" so the cell can read it.
3. Run cells top-to-bottom.

The notebook ships a `sql(query, limit=1000)` helper that returns a
`pandas.DataFrame`. Three worked examples:
- top 20 harvesters (count of `harvest_start` per kami) over 7d
- action-type histogram cross-checked against `/actions/types`
- bpeon operator timeline (the founder's reference account)

## Endpoint catalog

All endpoints take `Authorization: Bearer <token>` unless noted. JSON
responses; `uint256` values (`kami_id`, `amount`) are decimal strings.

| Method | Path | Notes |
| --- | --- | --- |
| GET  | `/health` | Liveness + cursor + row counts. **No auth.** |
| GET  | `/actions/types?since_days=N` | Action-type histogram. `since_days ∈ [1, 28]`. |
| GET  | `/actions/recent?limit=N` | Most recent decoded actions. `limit ∈ [1, 2000]`. |
| GET  | `/nodes/top?since_days=N&limit=M` | Top nodes by `harvest_start` count. |
| GET  | `/operator/{addr}/summary?since_days=N` | All actions by an operator address. |
| GET  | `/kami/{kami_id}/summary?since_days=N` | Per-kami activity summary. |
| GET  | `/kami/{kami_id}/actions?since_days=N&limit=M` | Per-kami raw action list. |
| GET  | `/registry/snapshot` | System-contract address history. |
| POST | `/sql` | Bounded read-only SQL. SELECT-only, 10 s timeout, 10 k row cap. |
| POST | `/backup` | Trigger `EXPORT DATABASE`. **Loopback-only** (cron uses it). |

`POST /sql` body:
```json
{"q": "SELECT … FROM kami_action …", "limit": 1000}
```
Response: `{"columns": [...], "rows": [[...], ...], "row_count": N,
"truncated": bool, "latency_ms": N}`.

## Service control (on the VM)

```bash
sudo systemctl status   kami-oracle    # ingester + FastAPI
sudo systemctl restart  kami-oracle
sudo systemctl reload   caddy          # after editing /etc/caddy/Caddyfile
sudo systemctl status   caddy
```

Both units are enabled at boot.

## Logs

- `logs/serve.log` — ingester poller + uvicorn + /sql audit
- `logs/backup.log` — nightly cron output
- `/var/log/caddy/kami-oracle.log` — JSON access log (HTTP path,
  status, client IP, latency)
- `sudo journalctl -u kami-oracle -f` — same as serve.log via systemd
- `sudo journalctl -u caddy -f` — Caddy itself (TLS issuance, etc.)

## Backups

- Bucket: `gs://kami-oracle-backups/`
- Schedule: cron `15 4 * * *` UTC nightly
- Path: `gs://kami-oracle-backups/export-<UTC-stamp>.tar.gz`
- Retention: last 14 objects auto-pruned by the script
- Hot export: uses `EXPORT DATABASE … (FORMAT PARQUET)` via the
  loopback `POST /backup` endpoint, so the live ingester is not
  interrupted.

**Currently blocked**: VM service-account scope is
`devstorage.read_only`, so the upload step 403s. See
`memory/questions-for-human.md` for the one-time fix (widen scopes).
Until then the EXPORT runs cleanly but no off-machine copy is made.

## Token rotation

```bash
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
sed -i "s|^KAMI_ORACLE_API_TOKEN=.*|KAMI_ORACLE_API_TOKEN=$NEW|" \
    ~/kami-oracle/.env
sudo systemctl restart kami-oracle
echo "$NEW"  # then update Colab secret + kami-zero config
```

## Known data caveats (as of 2026-04-25, post-Session 6)

- **Cursor at chain head**: the ~36h lag from Session 5 closed before
  Session 6 opened — the single-threaded poller is keeping pace.
  Queries with `now() - INTERVAL 1 DAY` work. The fallback anchor
  (`MAX(block_timestamp)` instead of `now()`) is still a fine
  defensive pattern but no longer required. Check
  `chain_head_lag_seconds` on `/health` before running tight time
  windows.
- **Rolling window**: 7 days. The prune thread sweeps every 3600 s.
  Window extension to 28 days remains a Session 7+ topic.
- **`harvest_stop` and `harvest_collect` `kami_id` is now populated**
  for new rows AND every historical row in the window (98.5% stitched
  via in-window harvest_start join; +1.6% stitched via the wider
  kami_id universe; ~0.02% orphans remain — stops whose starts predate
  the 7-day window and whose kami_id never reappears in any in-window
  action).
- **`kami_static` is now populated** with name / owner_address /
  traits / base stats. Refresh sweep runs every 6 h; first sweep
  populates all kamis observed in `kami_action`, ongoing sweeps
  refresh rows older than 24 h. New kamis are picked up automatically.
- **Tier-B selector `0x09c90324`** (~1.1k+ txs against
  `system.quest.accept`) is still skipped pending signature
  confirmation; counts will rise when the overlay lands.
