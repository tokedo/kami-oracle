# Session 8 — Verification Report

Date: 2026-04-26.

## Service health (live now)

- `systemctl is-active kami-oracle`: **active**
- Public-URL probe: `curl -s https://136-112-224-147.sslip.io/health | jq .`
  - cursor lag: **15.3 s** (post-restart catch-up; was 3.1s pre-session)
  - last block: **28,111,490** (timestamp `2026-04-26T16:39:34`)
  - total_actions: **312,383**
  - schema version: **2**

## Part 1 — `client/` library

- [x] Package importable from a fresh Python venv. Verified via
      `bash scripts/vendor-client.sh /tmp/<scratch>` then
      `from kami_oracle_client import OracleClient` — `import OK`.
- [x] `tests/test_client.py` passes — `12 passed in 1.21s`
- [x] Vendor script `scripts/vendor-client.sh` runs against a tmp dir
      (writes `kami_oracle_client/` with 6 files + `ORACLE_SHA`).
- [x] Commits:
  - `1c2a7e6` feat: client/ — Python consumer library for the public query plane
  - `17061e1` test: client/ — integration tests against loopback endpoint
  - `84d30cf` docs: client/ README + scripts/vendor-client.sh

## Part 2 — Gross-vs-net MUSU clarity

- [x] `schema/schema.sql` has the gross-vs-net comment block on
      `kami_action.amount` (commit `4b427e5`).
- [x] `README.md` has the "MUSU semantics" section (commit `7fca4e3`).
- [x] Every client method returning a MUSU field has the gross/net
      docstring. `KamiAction.amount`, `HarvestLeaderRow.musu_gross`,
      and `OperatorSummary` all carry the gross/net callout in
      `client/_models.py`. Method docstrings on
      `harvest_leaderboard`, `kami_actions`, `actions_recent`, and
      `operator_summary` repeat the gross/net framing.
- [x] `harvest_leaderboard` docstring leads with "Ranks kamis by
      gross MUSU harvested. This is the right metric for productivity
      comparison; tax is a node-config artifact, not a kami stat."
- [x] `memory/decoder-notes.md` has a top-level "MUSU semantics"
      section pulled up from "Session 7 — bpeon cross-check".
- [x] `memory/next-steps.md` flags net-of-tax view + rollups as
      **deferred-until-asked** with rationale (commit `479651a`).
- [x] Commits: `4b427e5` (schema), `7fca4e3` (README + decoder-notes),
      `479651a` (next-steps cleanup).

## Part 3 — Colab ride-along diff

- [x] `memory/next-steps.md` has a "Hand-off to human (blocklife-ai)"
      section with the colab-setup.md diff specified — drops `/ 1e18`,
      uses `CAST(amount AS HUGEINT)`, includes the MUSU-semantics
      callout, and three example queries (top earners, liquidator
      leaderboard, bpeon operator summary). Commit `479651a`.

## Part 4 — Foundation completion

### 4a. Retention window
- [x] Prune-window config changed from 7 → 28 days
- [x] Where it lives: `KAMI_ORACLE_WINDOW_DAYS` in `.env` and
      `env.template` (env-driven via `ingester/config.py:61`)
- [x] Live `.env` updated on VM: **yes** (gitignored; edited
      directly).
- [x] README updated. The section "Rolling window, not full history"
      now says 28 days; the architecture diagram says "rolling 28
      days, Stage 1"; the intro sentence drops the "(Stage 1: 1 week,
      may extend to 28)" parenthetical.
- [x] No backfill performed (correct — see prompt).
- [x] Date when 28d window will be full: **~2026-05-24** (28 days
      from today, 2026-04-26).
- [x] Verification: serve.log line `2026-04-26 16:36:47,324 INFO
      __main__: serve/prune: starting — interval=3600s, window=28
      days` confirms the new value loaded after restart. The next
      prune sweep (in ~60 min from restart) will keep anything ≤28d
      old; everything in the DB is currently ≤7d so the next sweep
      is a no-op.
- [x] Commit `5eb3791` (env.template). README change folded into
      `7fca4e3` (Part 2 docs commit).

### 4b. harvest_liquidate.harvest_id backfill
- [x] Pre-backfill NULL count: **960** (out of 1,093 total — 87.8%
      NULL; some had already been populated by Session 7's live
      decoder, so the figure is lower than the prompt's quoted ~99.7%
      from the Session 7 hand-off).
- [x] Post-backfill NULL count: **0** (all 960 had
      `metadata.victim_harvest_id`).
- [x] Join-validity query result: **15,925 rows** for
      `harvest_liquidate ⨝ harvest_start ON harvest_id` over the
      7-day window. Sample rows show clean attacker / victim_kami /
      node_id tuples (e.g. attacker `1075673…651239` on victim
      `7017519…440824` at node `16`, MUSU 864).
- [x] Backfill log preserved at
      `logs/backfill-liquidate-harvest-id.log`.
- [x] Commit `c844da6`.

## Coverage check (regression guard)

Re-ran the Session 7 acceptance query post-restart, post-backfill:

| action_type        | total   | null_amount |
|--------------------|--------:|------------:|
| harvest_collect    |   4,065 |          25 |
| harvest_liquidate  |   1,093 |         139 |
| harvest_start      | 116,303 |     116,303 |
| harvest_stop       | 137,743 |      23,469 |

Compared to Session 7 baseline (Part 0 step 5 of this prompt):

| action_type        | baseline n | now n   | baseline null | now null | delta-populated |
|--------------------|-----------:|--------:|--------------:|---------:|-----------------|
| harvest_collect    |      4,061 |   4,065 |            25 |       25 | +4 / 0 → 99.4% (unchanged) |
| harvest_liquidate  |      1,093 |   1,093 |           139 |      139 | unchanged → 87.3% |
| harvest_start      |    116,192 | 116,303 |       116,192 |  116,303 | +111 / +111 → 100% NULL (by design, no payout) |
| harvest_stop       |    137,595 | 137,743 |        23,456 |   23,469 | +148 / +13 → 82.96% (was 82.95%) |

**Coverage: PASS — unchanged or marginally better** (the small NULL
deltas reflect new rows ingested in the ~30 min between baseline and
now, plus continued live decoding closing the gap on stop/collect).
No regression on `amount` populated rate.

## Known issues

**None blocking.** A few honest flags worth recording for Session 9:

- The 28-day rolling window currently holds only ~7 days of data
  (the prior retention window). Until 2026-05-24 the window is
  partially filled — any 28-day query result will be implicitly
  truncated to "what's accumulated since 2026-04-26 minus 7 days."
  This is the explicit deferred-fill design from the prompt; flag it
  if a Colab user is confused about why a 28d query has the same
  rows as a 14d one in early May.
- 23,469 NULL `amount` rows on `harvest_stop` (≈17% of the table)
  are real on-chain no-ops, not decoder gaps. Documented in Session
  7 acceptance; left in place because NULL is a useful "this call
  drained nothing" signal.
- 139 NULL `amount` rows on `harvest_liquidate` (≈12.7%) are real
  no-ops too (failed liquidation attempts via
  `executeBatchedAllowFailure` against an empty harvest entity). Same
  rationale as harvest_stop.
- Cursor lag was elevated to ~15s right after the second service
  restart of this session (post-backfill restart); will fall back to
  ~3s within a couple of poll cycles. Not a service-health concern,
  but worth checking on the next /health probe if Session 9 picks
  this up immediately.

## Status

✅ **Session 8 complete. kami-oracle running smoothly with no known
issues. Founder can begin Colab exploration whenever ready.**
