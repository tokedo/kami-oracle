# Next Steps

## Session 2 — after human review of Session 1 decode quality

**Blockers first.** Before running a full backfill, the human needs to
address the three items in `memory/questions-for-human.md`:

1. Confirm the `system.craft` 2-arg signature (or re-vendor
   `kami_context/`).
2. Provide bpeon kami IDs / operator wallets so we can spot-validate
   decode correctness against a known-good account.
3. OK the ABI-overlay policy (add documented-sig overlays freely vs.
   ask each time).

## Once the above is resolved

**Do in this order:**

1. **Re-run validation** (`scripts/validate_decode.py --blocks 2000`)
   and confirm coverage is still in the ~98% range. Log any new unknown
   selectors to `memory/unknown-systems.md`.

2. **Spot-validate against the bpeon accounts** — pull their recent
   harvest history, confirm the expected start/stop/collect shape, and
   write findings to `memory/decoder-notes.md` under a new
   "spot-validation: bpeon" section.

3. **Kick off the 4-week backfill**:
   ```
   python -m ingester.backfill --weeks 4
   ```
   Expect the DB to reach a few hundred MB; watch for rate-limit
   behavior on the public RPC. The chunk size is 500 blocks; the
   pipeline is idempotent, so aborting and resuming is safe.

4. **Start the continuous poller**:
   ```
   python -m ingester.poller
   ```
   Runs forever. Pairs with a cron-driven prune job:
   ```
   python -m ingester.prune          # daily is fine
   ```

5. **Add the `kami_static` backfill worker** — Stage 1's schema includes
   the table but population is deferred. A simple loop: for every
   distinct `kami_id` in `kami_action`, call the GetterSystem's
   `getKami(uint256)` once, upsert the row, refresh weekly.

## Known gaps to close later (not Session 2 work)

- Several system ABIs are absent from the vendored snapshot (equip,
  trade, marketplace, kami721/portal, etc.). Wait for upstream
  re-vendoring unless they start showing up in
  `memory/unknown-systems.md`.
- `raw_tx.to_addr` as a FK to the system registry is implicit — a
  future schema revision could add a `systems` dim table if it becomes
  useful for queries.
