# Questions for human

## Open

(none)

## Resolved

- **Session 6 — MUSU amount NULL on harvest_stop / harvest_collect**
  (resolved Session 7, 2026-04-25). Closed via receipt-log decoding —
  but with one important correction: MUSU is **not** an ERC-20.
  `0xE1Ff7038e…` is WETH; MUSU is in-game item index 1 tracked by
  MUDS `ValueComponent` updates emitted as `ComponentValueSet`
  events on World. Live decoder + 107,040-row historical backfill
  populated `kami_action.amount` on harvest_collect / stop /
  liquidate. Acceptance, cross-check, and golden-query top-5 in
  `memory/decoder-notes.md` "Session 7 acceptance".

- **Session 5 — VM service-account scope blocks GCS uploads** (resolved
  2026-04-24). Scope widened to `devstorage.read_write` (Option A);
  nightly cron now uploads cleanly to `gs://kami-oracle-backups/`.
  Verified by 2026-04-25T04:15Z run in `logs/backup.log`.
- Session 1's three questions were addressed in Session 2's resume brief
  (craft sig, overlay policy, bpeon operator wallet). One Session 2
  finding is non-blocking and lives in `memory/next-steps.md` under
  "Session 2 finding — RPC retention limit".
