# Session 9 — Verification Report

## Service health (live now)

- `systemctl is-active kami-oracle`: **active**
- Public-URL probe: `curl -s https://136-112-224-147.sslip.io/health | jq .`
  - cursor lag: **13.8s** (well under the 30s acceptance bar)
  - last block: **28,113,544**
  - last block timestamp: 2026-04-26T18:13:10 UTC
  - total_actions (kami_action): **314,963**
  - kami_static rows: **6,924** (one new kami hydrated post-restart;
    confirms the live populator path also writes account_name)
  - schema version: **3** ✅ post-migration

## Part 1 — Schema migration

- [x] Migration file added: `migrations/003_add_account_name_columns.py`
- [x] `schema/schema.sql` reflects post-migration state with comment
      block on `kami_static.account_index` / `account_name`
      (citing `GetterSystem.getAccount`)
- [x] `SCHEMA_VERSION` bumped 2 → 3 in `ingester/storage.py`
- [x] Migration applied cleanly on service start. From
      `logs/serve.log` after Session 9 restart:
      ```
      storage: applying migration 003 (add account_index, account_name)
      storage: migration 003 complete: {'account_index_added': 1, 'account_name_added': 1}
      ```
      No subsequent errors.
- [x] Commits: `8e5026d` (harness)

## Part 2 — Populator + backfill

### 2a. Populator
- [x] `ingester/kami_static.py` calls `getAccount(account_id)` via
      a doc-cited ABI fragment merged in at construction time
      (the vendored `GetterSystem.json` does not carry getAccount —
      Tier-A overlay per CLAUDE.md, cited against
      `kami_context/system-ids.md` Getter System).
- [x] Within-pass cache implemented: `KamiStaticReader._account_cache`
      keyed by `account_id`. 146 unique accounts across 6,923 kamis →
      ~47× call reduction. Both the success and failure paths cache
      so a reverting account never re-hits the RPC in the same pass.
- [x] Tests pass: `97 passed, 12 skipped, 1 warning in 18.55s`
- [x] Bpeon kami test: `tests/test_account_fetch.py` exercises the
      cache, revert→NULL, and empty-name→NULL paths against a stub
      contract — the live bpeon `account_name='bpeon'` assertion is
      a separate live-DB cross-check (recorded in 2b below) since
      the RPC is the source of truth for the empirical claim.
- [x] Commits: `8e5026d` (harness — populator + tests in same commit)

### 2b. Backfill
- [x] Pre-backfill NULL `account_name` count: **6,923**
- [x] Distinct accounts fetched: **146**
- [x] Post-backfill NULL `account_name` count: **0**
- [x] Anonymous accounts (came back with empty name): **0**
      (every owning account in our 28-day window has called
      `system.account.set.name` at some point — unusually clean
      coverage; future windows may surface anonymous accounts and
      the schema/populator handle that gracefully)
- [x] Top-20 operator-name leaderboard query returns names. Top 5:

      | account_name | kamis | distinct_kamis |
      |--------------|------:|---------------:|
      | lele         |   285 |            285 |
      | cherki       |   261 |            261 |
      | dark         |   247 |            247 |
      | aaron        |   233 |            233 |
      | spark        |   226 |            226 |

- [x] **Bpeon cross-check**: 20 kamis come back with
      `account_name='bpeon'`. `kami_index` values:
      `43 (Zephyr), 1064, 2553, 3874, 3983, 6096, 7722, 7803, 8745,
      10011, 10647, 11716, 12459, 13235, 13390, 13702, 13857, 13947,
      14286, 14306`.
- [x] **Founder's flagship query** (top earners with operator
      names) returns operator labels — top 3 are all "tokedo"
      (kami #2808, #11200, #4126).
- [x] Backfill log preserved at `logs/backfill-account-names.log`
      (gitignored).
- [x] Commits: `b524d18` (data — backfill execution + docs)

## Part 3 — Documentation

- [x] README kami_static section updated — added "Operator labels in
      `kami_static` (Session 9)" callout above the MUSU semantics
      section. Names the new columns, points at `account_name` as
      the right operator label, and calls out the
      `account_name` vs `kami_action.from_addr` distinction.
- [x] `memory/decoder-notes.md` has "Session 9 — account fetch"
      section with:
      - the `getAccount(uint256)` call shape
      - coverage table (146/146 accounts, 6923/6923 rows, 0 anon)
      - bpeon cross-check (all 20 kami_index values listed)
      - within-pass cache rationale (~47× call reduction)
      - operator name vs signer wallet section
- [x] Commits: `b524d18` (docs bundled with the data commit since
      the bpeon numbers come straight from the backfill output)

## Part 4 — Colab ride-along diff

- [x] `memory/next-steps.md` "Hand-off to human (blocklife-ai)"
      section has the four-query rewrite (top earners, liquidator
      leaderboard, bpeon operator summary in both signer-wallet and
      Account forms, liquidation pairing) and the operator-vs-signer
      callout for the schema cheat sheet.
- [x] Will be picked up by next-steps push (`docs:` commit below).

## Coverage check (regression guard)

`harvest_*` `amount` coverage post-restart, post-backfill:

| action_type        | total   | null_amount |
|--------------------|--------:|------------:|
| harvest_collect    |   4,084 |          25 |
| harvest_liquidate  |   1,093 |         139 |
| harvest_start      | 117,296 |     117,296 |
| harvest_stop       | 138,756 |      23,529 |

Compared to Session 8 baseline:

| action_type        | s8 total | now total | s8 null | now null | populated drift |
|--------------------|---------:|----------:|--------:|---------:|-----------------|
| harvest_collect    |    4,065 |     4,084 |      25 |       25 | +19 / 0 → 99.4% (unchanged) |
| harvest_liquidate  |    1,093 |     1,093 |     139 |      139 | unchanged → 87.3% |
| harvest_start      |  116,303 |   117,296 | 116,303 |  117,296 | +993 / +993 → 100% NULL by design (no payout) |
| harvest_stop       |  137,743 |   138,756 |  23,469 |   23,529 | +1,013 rows / +60 NULL → 83.04% (was 82.96%) |

**Coverage: PASS — unchanged or marginally better.** All deltas
are explained by new rows ingested in the ~24h between Session 8 and
Session 9 baselines plus continued live decoding closing the gap on
stop. No regression on `amount` populated rate.

## Known issues

None.

- No pattern of `getAccount` failures: 146/146 distinct accounts
  resolved cleanly, 0 reverts, 0 empty-name shapes.
- No kamis with unresolvable `account_id`: all 6,923 rows that had
  a non-zero `account_id` now have `account_name` populated.
- One new kami row was hydrated post-restart (6,923 → 6,924) and
  came back named, confirming the live populator path also writes
  the new columns end-to-end.

## Status

✅ **Session 9 complete. account_index + account_name populated
across kami_static; Colab queries can now display human-readable
operator names and small kami indices. Founder can refresh their
Colab notebook with the updated example queries from the
next-steps.md hand-off.**
