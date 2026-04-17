# Ops runbook

Long-running processes — commands, expected lifetime, resume steps.

## Detached 4-week backfill (session 2, started 2026-04-17)

Launched via `screen` so an SSH drop does not kill it. Screen is
durable across logouts; `screen -r kami-backfill` reattaches.

**Launch command:**

```
screen -dmS kami-backfill -L -Logfile ~/kami-oracle/logs/backfill.log \
  bash -c 'cd ~/kami-oracle && exec .venv/bin/python -m ingester.backfill --weeks 4'
```

Flags:
- `-dmS kami-backfill` — create detached session named `kami-backfill`.
- `-L -Logfile ...` — tee screen output to a file so session 3 can
  read progress without reattaching.

**Expected runtime:** ~7.7 days at measured 2.4 blocks/sec on the
public Yominet RPC (200-block dry-run, 2026-04-17 21:08). 1.6M blocks
total. DB grows to a few hundred MB–~GB (actions > raw_tx volume).

**Check progress:**

```
screen -r kami-backfill         # reattach (Ctrl+a, d to detach again)
screen -ls                      # list sessions
tail -f ~/kami-oracle/logs/backfill.log
```

Or query the cursor directly:

```python
import duckdb
c = duckdb.connect('db/kami-oracle.duckdb', read_only=True)
print(c.execute("SELECT last_block_scanned, last_block_timestamp FROM ingest_cursor").fetchone())
```

**If it dies mid-run**, resume is safe. The cursor advances monotonically;
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

`python -m ingester.prune` enforces the 28-day rolling window. Run
daily once the backfill+poller are steady-state. Not needed during
initial population (we have ≤4 weeks of data by definition).
