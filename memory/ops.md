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

## Poller — NOT started this session

Do **not** run the poller in parallel with the backfill: DuckDB does not
support concurrent writers, and `kami_action.id` idempotency won't save
us if both processes hold the DB file open. Start the poller only after
the backfill finishes (or is explicitly stopped). When ready:

```
screen -dmS kami-poller -L -Logfile ~/kami-oracle/logs/poller.log \
  bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.poller'
```

The poller reads the cursor at startup and continues from
`cursor + 1`, so any gap between backfill-complete and poller-start is
automatically filled.

## Prune — defer to Phase A tail

`python -m ingester.prune` enforces the `KAMI_ORACLE_WINDOW_DAYS`
rolling window (Stage 1: 7 days). Run daily once the backfill+poller
are steady-state. Not needed during initial population (we have
≤1 week of data by definition).
