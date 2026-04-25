# Questions for human

## Open

### Session 6 — MUSU amount NULL on harvest_stop / harvest_collect

The Session 6 golden query asks for `SUM(CAST(a.amount AS HUGEINT))`
on harvest_collect / harvest_stop, but `amount` is NULL for all
~141k of those rows. Reason: MUSU bounty isn't in the calldata —
it's emitted as an ERC20 Transfer event log on the MUSU token
contract (`0xE1Ff7038eAAAF027031688E1535a055B2Bac2546`) in the same
tx receipt. The ingester currently decodes calldata only.

Names + owners + harvest counts now work (top-5 by count is in
`memory/decoder-notes.md` "Session 6 acceptance"). The leaderboard
ranking by harvest count is a perfectly fine proxy for the founder's
"top earners" signal — but if the founder explicitly wants
MUSU-denominated totals, Session 7 needs to add receipt-log decoding.

Recommendation: do this in Session 7 — same plumbing also unlocks
MUSU-based metrics for `kami-zero`. Confirm priority before Session 7
opens; if it's already obvious, treat it as the lead item there and
no human action is required here.

## Resolved

- **Session 5 — VM service-account scope blocks GCS uploads** (resolved
  2026-04-24). Scope widened to `devstorage.read_write` (Option A);
  nightly cron now uploads cleanly to `gs://kami-oracle-backups/`.
  Verified by 2026-04-25T04:15Z run in `logs/backup.log`.
- Session 1's three questions were addressed in Session 2's resume brief
  (craft sig, overlay policy, bpeon operator wallet). One Session 2
  finding is non-blocking and lives in `memory/next-steps.md` under
  "Session 2 finding — RPC retention limit".
