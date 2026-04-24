# Founder Testing Guide — kami-oracle HTTP (Session 3.5 hand-off)

The co-hosted serve process runs on the oracle VM, bound to
`127.0.0.1:8787`. This guide is for testing it from your laptop.

## Reach the API from your laptop

The API is loopback-only on the VM. Open an SSH tunnel and it becomes
local to your laptop:

```bash
gcloud compute ssh kami-oracle --zone=us-central1-a \
  --project=kami-agent-prod -- -L 8787:127.0.0.1:8787 -N
```

Leave that running in one terminal. In another, hit the API as if it
were local:

```bash
curl -s http://127.0.0.1:8787/health | jq .
```

To stop the tunnel: Ctrl+C the `gcloud` process.

## Endpoint catalog

All responses are JSON. `uint256` values (`kami_id`, `amount`,
`harvest_id`) are decimal strings to stay safe for JS consumers.

### `GET /health`

Service liveness + cursor + row counts + registry summary.

```bash
curl -s http://127.0.0.1:8787/health | jq .
```

Watch `cursor.last_block_scanned` advance across successive calls to
confirm the poller is alive.

### `GET /actions/types?since_days=<N>`

Action-type histogram over the last N days. Caps: `since_days ∈ [1, 28]`.

```bash
curl -s "http://127.0.0.1:8787/actions/types?since_days=7" | jq .
```

### `GET /actions/recent?limit=<N>`

Most recent decoded actions. Caps: `limit ∈ [1, 2000]`. Returns
`{count, actions: [...]}`.

```bash
curl -s "http://127.0.0.1:8787/actions/recent?limit=20" | jq .
```

### `GET /nodes/top?since_days=<N>&limit=<M>`

Top nodes by `harvest_start` count. Caps: `since_days ∈ [1, 28]`,
`limit ∈ [1, 200]`.

```bash
curl -s "http://127.0.0.1:8787/nodes/top?since_days=7&limit=10" | jq .
```

### `GET /operator/{addr}/summary?since_days=<N>`

All actions by an operator address. `addr` is case-sensitive (checksum
preferred). Caps: `since_days ∈ [1, 28]`.

```bash
curl -s "http://127.0.0.1:8787/operator/0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2/summary?since_days=7" | jq .
```

### `GET /kami/{kami_id}/summary?since_days=<N>`

Per-kami activity summary. `kami_id` is a decimal string. Caps:
`since_days ∈ [1, 28]`.

```bash
curl -s "http://127.0.0.1:8787/kami/8985147110719105535652045351219036898186953673538840789086552104591862936658/summary?since_days=7" | jq .
```

### `GET /kami/{kami_id}/actions?since_days=<N>&limit=<M>`

Per-kami raw action list, newest first. Caps: `since_days ∈ [1, 28]`,
`limit ∈ [1, 2000]`.

```bash
curl -s "http://127.0.0.1:8787/kami/<id>/actions?since_days=7&limit=50" | jq .
```

### `GET /registry/snapshot`

Persisted union of system-contract addresses across the backfill
window. Shows which systems redeployed (≥2 addresses).

```bash
curl -s http://127.0.0.1:8787/registry/snapshot | jq '.by_system | to_entries | map(select(.value|length>1))'
```

## Known data caveats (as of 2026-04-24)

- **Data window**: Backfill covers 2026-04-12 03:02 UTC through
  2026-04-22 17:05 UTC (~10.6 days, 420k blocks, 478k actions). The
  live poller is catching up the ~76k blocks between backfill end and
  head — `cursor.last_block_scanned` in `/health` tells you where it
  is. At ~2.35 blocks/s the catchup takes ~9 h. `/actions/recent`
  queries trail the cursor, not head, until catchup completes.

- **Harvest coverage**: 81.6% of decoded actions are in the harvest
  family. The session 2.5 bug that made harvest near-invisible is
  fixed; registry snapshot shows 6 systems with ≥2 addresses, all 4
  harvest systems among them.

- **Tier-B selector deferred**: 1,137 rows in
  `memory/unknown-systems.md` for selector `0x09c90324`, against
  `system.quest.accept` (1134) and `system.account.use.item` (3).
  These txs are not counted in `kami_action` — they were skipped per
  the "never guess an ABI" policy. Actual game-level quest_accept /
  item_use counts will be higher once the selector is confirmed and
  an overlay lands.

- **Sparse action types**: Many action types appear <10 times in
  10.6 days (e.g. `friend_accept`, `kami_name`, `account_set_name`,
  `goal_claim`, `echo_kamis`, `echo_room`). These are genuinely rare
  on the chain, not decode failures — the validator shows 0 unknown
  selectors on current-head samples.

- **`kami_static` is empty**. Per-kami trait snapshots were deferred
  from Stage 1 kickoff — Session 4 candidate.

- **Address case**: operator/addr params should use checksummed
  casing when possible. The DB stores whatever the chain returned,
  which is mixed-case checksum.

## Stopping the service cleanly

```bash
screen -S kami-oracle -X quit
```

The serve process traps SIGTERM: stop_event → poller drains its
current chunk → uvicorn stops → DuckDB closes (30 s join timeout).
`logs/serve.log` records the shutdown sequence.

To restart:

```bash
cd ~/kami-oracle
screen -dmS kami-oracle -L -Logfile ~/kami-oracle/logs/serve.log \
  bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.serve'
```

Give it ~30 s to boot (registry probe + uvicorn bind); then
`curl http://127.0.0.1:8787/health` should reply.

## Security reminder

This API has no auth layer. Do **not** expose it beyond loopback
without an ADR-gated Phase D decision — the `_parse_bind` guard in
`ingester/serve.py` refuses any non-loopback bind as a defensive
backstop, but that's a backstop, not a policy. The correct
deployment model for public access is still TBD.
