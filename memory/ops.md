# Ops runbook

Long-running processes — commands, expected lifetime, resume steps.

## Detached 7-day backfill (session 2.5, started 2026-04-18)

Launched via `screen` so an SSH drop does not kill it. Screen is
durable across logouts; `screen -r kami-backfill` reattaches.

Window was 4 weeks in session 2 but the backfill died ~6h in on a
bare `requests.ConnectionError`, and the partial data showed an
action-mix that flagged a registry-snapshot bug (see
`memory/decoder-notes.md` → "action-mix divergence"). Session 2.5
shrank the window to 1 week, hardened the retry/survival loop, and
backed up the session-2 DB to `db/kami-oracle.duckdb.session2.bak`.

**Launch command:**

```
screen -dmS kami-backfill -L -Logfile ~/kami-oracle/logs/backfill.log \
  bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.backfill --days 7'
```

Flags:
- `-dmS kami-backfill` — create detached session named `kami-backfill`.
- `-L -Logfile ...` — tee screen output to a file so session 3 can
  read progress without reattaching.

**Expected runtime:** ~2 days at measured 2.4 blocks/sec on the
public Yominet RPC. 420k blocks total. DB grows to ~100-200 MB.

**Check progress:**

```
screen -r kami-backfill         # reattach (Ctrl+a, d to detach again)
screen -ls                      # list sessions
tail -f ~/kami-oracle/logs/backfill.log
```

Do **not** open the DuckDB file while the writer is running: DuckDB
holds an exclusive file lock even against `read_only=True` connections
(tested 2026-04-17 — raises `IOException: Conflicting lock is held`).
Read progress from the backfill log, not the DB. Each chunk emits
`backfill: START..END done (actions=N, running total=N)`.

**If it dies mid-run**, the outer survival loop (session 2.5) should
catch the exception, sleep 60s, refresh from the DB cursor, and
resume — no manual intervention needed in the common case. Check the
log for `backfill: unhandled exception, sleeping 60s before resume`.

If the process has actually exited (screen session gone), resume is
still safe manually: the cursor advances monotonically;
`--from-block <cursor+1> --to-block <original_head>` continues exactly
where it stopped. The pipeline is idempotent via `ON CONFLICT DO NOTHING`
on `raw_tx.tx_hash` and `kami_action.id`, so re-processing overlapping
ranges is harmless.

## Co-hosted serve (session 3 — poller + read-only HTTP in one process)

Session 3 introduces `ingester.serve`, which runs the ingest poller
and the FastAPI read-only query layer inside **one Python process**
sharing a single DuckDB connection. DuckDB holds a per-process
exclusive file lock, so two independent processes can't both open the
file; co-hosting sidesteps the issue and is the Stage-1 stand-in for
a Phase-D MCP server.

**Launch command:**

```
screen -dmS kami-oracle -L -Logfile ~/kami-oracle/logs/serve.log \
  bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.serve'
```

Only one of `{serve, poller, backfill}` may hold the DB at a time —
the standalone `ingester.poller` entrypoint is retained for use when
the query layer is not wanted, but **do not run it alongside serve**.

**HTTP bind:** `127.0.0.1:8787` by default. Override via
`KAMI_ORACLE_API_BIND=127.0.0.1:PORT`. The process refuses to bind to
`0.0.0.0` / `::` / wildcard hosts — there's no auth layer yet;
exposure is a Phase-D + ADR decision.

**Health check (from a second SSH session):**

```
curl -s http://127.0.0.1:8787/health | jq .
```

Expected shape:

```json
{
  "status": "ok",
  "cursor": {"last_block_scanned": 27970000, "last_block_timestamp": "..."},
  "row_counts": {"raw_tx": N, "kami_action": M, "system_address_snapshot": K},
  "registry": {"n_systems": 34, "n_addresses": 40}
}
```

Watch `last_block_scanned` advance across successive calls to confirm
the poller thread is alive. Other endpoints worth hitting for a
quick smoke test:

```
curl -s http://127.0.0.1:8787/actions/types?since_days=7 | jq .
curl -s http://127.0.0.1:8787/registry/snapshot | jq '.n_addresses'
curl -s 'http://127.0.0.1:8787/operator/0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2/summary?since_days=7' | jq .
```

**Graceful shutdown:** `SIGTERM`/`SIGINT` (Ctrl+C inside the screen)
flips a stop event that drains the poller and tells uvicorn to stop
accepting new requests. In-flight HTTP requests complete, then the
DB closes. 30s join timeout on the poller thread; if it doesn't exit
in time a warning is logged but the process still terminates.

## Standalone poller (legacy, retained for emergencies)

If the HTTP layer is broken or you just want ingest-only operation:

```
screen -dmS kami-poller -L -Logfile ~/kami-oracle/logs/poller.log \
  bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.poller'
```

Same cursor semantics as `serve`. **Do not run alongside `serve` or
`backfill`** — DuckDB per-process lock.

## Prune — defer to Phase A tail

`python -m ingester.prune` enforces the `KAMI_ORACLE_WINDOW_DAYS`
rolling window (Stage 1: 7 days). Run daily once the backfill+poller
are steady-state. Not needed during initial population (we have
≤1 week of data by definition).
