# Decoder notes — Session 1 findings

Date: 2026-04-17 (session 1)
Vendor SHA: `332db78` (kami_context/UPSTREAM_SHA)

## Architecture observation: txs target individual System contracts

The World (`0x2729174c265dbBd8416C6449E0E813E88f43D0E7`) is the MUD registry.
Player-facing actions send transactions to the **individual System contracts**
(e.g., HarvestStartSystem at `0x0777687Ec9FEB7349c23a19Ba7D11a1fe8cd35F1`) —
not to World. Our ingester therefore:

1. Resolves all player-facing System IDs → addresses at startup via
   `world.systems()` → registry `getEntitiesWithValue(keccak256(systemID))`.
2. Filters each block's txs by `to ∈ {registered system addresses}`.
3. Decodes `tx.input` with the matching System ABI.

`raw_tx.to_addr` is the system contract address, not World. The `system_id`
column on `raw_tx` carries the human-readable ID (e.g.
`system.harvest.start`).

## Coverage on a 500-block sample (head-ish of chain, 2026-04-17)

Scan of blocks 27,778,327 .. 27,778,826:

| metric              | count | note |
|---------------------|-------|------|
| txs seen            | 265   | all txs in the window |
| matched to system   | 265   | 100% of txs hit a Kamigotchi system (this chain is dedicated) |
| successfully decoded| 259   | **97.7% coverage** |
| unknown selector    | 6     | all `system.craft` selector `0x5c817c70` |
| decode errors       | 0     | — |
| actions produced    | 512   | (fan-out from batched calls) |

Action-type breakdown (dominant categories):

- `harvest_stop` — 245 actions (~48%)
- `harvest_start` — 186 actions (~36%)
- `feed` (kami.use.item) — 51
- `skill_upgrade` — 15
- `lvlup` — 14
- `harvest_collect` — 1

## ABI divergence from vendored JSON

The vendored `kami_context/abi/*.json` set is **partial** relative to
what's live on-chain. Three specific gaps found:

### 1. `system.harvest.start`

Vendored JSON declares only `executeTyped(uint256 kamiID, uint256 nodeID)`.
The chain overwhelmingly uses the 4-arg form from the official docs
(`kami_context/system-ids.md` "Non-Standard Entry Points" table):

- `executeTyped(uint256 kamiID, uint32 nodeIndex, uint256 taxerID, uint256 taxAmt)` — selector `0xc8372a87`
- `executeBatched(uint256[] kamiIDs, uint32 nodeIndex, uint256 taxerID, uint256 taxAmt)` — selector `0x68f37c94`

Fix: both signatures added to `decoder.SYSTEM_ABI_OVERLAY` with citations
back to system-ids.md.

### 2. `system.harvest.stop` / `system.harvest.collect`

Vendored JSON has only the singular `executeTyped(uint256 id)`. Chain uses
`executeBatched(uint256[] ids)` (selector `0x01929a9f`) and
`executeBatchedAllowFailure(uint256[] ids)` (selector `0xb0fa4458`), both
documented in system-ids.md. Both added to overlay.

### 3. `system.craft` — RESOLVED (session 2, 2026-04-17)

Vendored JSON + system-ids.md both declare
`executeTyped(uint256 assignerID, uint32 index, uint256 amt)`
(selector `0xa693008c`). Chain txs use the **2-arg** selector
`0x5c817c70 = executeTyped(uint32, uint256)`.

Confirmed against deployed `CraftSystem` bytecode at
`0xd5dDd9102900cbF6277e16D3eECa9686F2531951`: contains `0x5c817c70`,
does **not** contain `0xa693008c`. Vendored `CraftSystem.json` is stale.

Fix: `SYSTEM_ABI_OVERLAY["system.craft"]` adds `executeTyped(uint32 index,
uint256 amt)`. `SYSTEM_FIELD_MAP["system.craft"]` already mapped
`index → metadata.recipe_index` and `amt → amount`. Test coverage:
`tests/test_decoder.py::test_craft_executeTyped_2arg_overlay`.

Note: `_typed_fn` lookup still resolves to the stale 3-arg JSON form for
craft, which would fail on any hypothetical `execute(bytes)` craft call.
All craft txs observed so far go through the direct selector path, so
this is theoretical — revisit if an `execute(bytes)` craft decode error
shows up in `unknown-systems.md`.

## Semantic quirks worth knowing

- **Harvest `id` is the harvest instance ID, not the kami.**
  HarvestStop/HarvestCollect take the entity ID of an active harvest record,
  not the kami being harvested with. Our `kami_action` rows therefore have
  `kami_id = NULL` and `metadata.harvest_id` set. To recover `kami_id`, a
  future phase can join against prior `harvest_start` rows where the harvest
  instance was created.
- **SkillReset arg name is `targetID` but means the kami being respec'd** —
  not a PvP target. Explicit field map (`SYSTEM_FIELD_MAP`) overrides the
  name-based inference.
- **SkillUpgrade arg is `holderID`, not `kamiID`** — same semantic (the
  kami holding the skill). Mapped to `kami_id`.
- **Batched calls fan out**: `executeBatched(uint256[] kamiIDs, ...)` with
  N kami IDs produces N separate `kami_action` rows, sharing `tx_hash` and
  scalar args, indexed by `sub_index=0..N-1`. This is why the action count
  (512) exceeds the matched-tx count (265).

## Missing system ABIs in this vendored snapshot

These were skipped at registry-resolution time because no ABI file was
found under `kami_context/abi/`:

- `system.account.fund` — AccountFundSystem.json exists but the system ID
  is not resolved on-chain (warning at startup). Possibly inactive.
- Entire families: equip/unequip, sacrifice, kami.send, kami.onyx.revive,
  trade (all 4), kamimarket (all 6), newbievendor, auction.buy,
  kami721.stake/unstake/transfer, erc20.portal, gacha.reveal,
  buy.gacha.ticket (deprecated).

None of these were seen on the current chain in the sample window — so
their absence doesn't hurt Stage-1 coverage today. But re-vendoring
`kami_context` after upstream publishes them would close the gap.

## Session 2 re-validation (2026-04-17, blocks 27,778,645 .. 27,780,644)

After the craft overlay landed (`harness: add craft executeTyped...`):

| metric              | count | note |
|---------------------|-------|------|
| txs seen            | 1123  | all txs in the window |
| matched to system   | 1116  | 99.4% — non-match is cross-module noise |
| successfully decoded| 1116  | **100% coverage** of matched txs |
| unknown selector    | 0     | craft resolved |
| decode errors       | 0     | — |
| actions produced    | 2057  | batched fan-out still dominant |

Action-type breakdown:

- `harvest_start` — 949 (~46%)
- `harvest_stop`  — 761 (~37%)
- `feed`          — 187 (~9%)
- `item_craft`    — 47  (newly decoded this session)
- `skill_upgrade` — 40
- `lvlup`         — 38
- `harvest_collect` — 17
- `move`          — 15
- `droptable_reveal` — 2
- `scavenge_claim` — 1

Spot-check of the craft fan-out: 3 sample rows all have
`metadata.fn == "executeTyped"`, `recipe_index ∈ {6, 23, 29}`,
`amount ∈ {1, 2, 5}` — shape matches the hypothesis that confirmed the
overlay.

## Validation scripts

- `scripts/validate_decode.py` — dry-run over an arbitrary block window;
  summarizes counts, examples, and appends unknowns to
  `memory/unknown-systems.md`. No writes to the production DuckDB.
- `scripts/scan_operator.py` — targeted back-scan filtered to a single
  `tx.from` wallet. Prints a per-kami activity summary; writes nothing
  to DuckDB. Used for spot-validating decode against known operators.
- `tests/test_decoder.py` — 7 unit tests over canned on-chain calldata.
  Covers executeTyped, executeBatched fan-out, overlay signatures, and
  error paths. Runs offline.

## Receipts strategy (session 2, 2026-04-17)

**Decision: keep per-tx `eth_getTransactionReceipt`** in
`ingester/ingest.py`. `eth_getBlockReceipts` is supported by the
Yominet RPC (probe latency ~240ms), but Yominet block density is low
— ~0.5–0.6 matched txs per block in the validated window — so one
`get_tx_receipt` per matched tx and one `eth_getBlockReceipts` per
block are equivalent in RPC count, and the block-scoped call is
*slower* on the common case of 0–1 tx blocks. A head-to-head probe
on a 1-tx block showed `get_tx_receipt`=255ms vs
`eth_getBlockReceipts`=750ms; net speedup is negative.

Instead, `RetryPolicy` is bumped (attempts 5→8, max delay 30s→60s) to
ride out transient outages across the multi-day 4-week backfill. No
schema or decode change; block-fetch itself (~250ms) remains the real
bottleneck — addressing it would require concurrent workers, which is
out of scope for Stage 1.

## RPC retention limit (session 2, 2026-04-17)

The public Yominet RPC at
`https://jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz` retains
**only ~22.7 days (3.24 weeks)** of block history, measured by
binary-searching which historical `eth_getBlockByNumber` requests return
"block not found." At 2026-04-17 21:12 UTC, head=27,783,030 and the
earliest retained block was 26,769,364 — 1,013,670 blocks deep.

Implications:

- A full 4-week (28-day) backfill as prescribed by CLAUDE.md is
  **not achievable** against this endpoint. The backfill can cover at
  most ~22 days.
- The retention edge is **fuzzy**: requests for blocks a few thousand
  blocks past the probed earliest value still hit intermittent "not
  found" errors, consistent with a load-balanced RPC whose backends
  have slightly different retention depths. `backfill.py` now probes
  the earliest retained block at startup and clamps `start_block` to
  `earliest + RETENTION_BUFFER_BLOCKS` (default 20,000).
- Transient retries on edge-of-retention blocks resolve on their own
  (the load balancer routes a retry to a backend that still has the
  block), so the backfill still succeeds — just slower near the start.

Long-term options for a full 4-week window (owner decision, not
automated): (a) set up a private archive-node RPC, (b) accept a ~22-day
rolling window in practice, (c) let the oracle accumulate history
locally over time (the poller tails forward indefinitely).

## Spot-validation: bpeon operator (session 2, 2026-04-17)

Operator wallet: `0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2` (founder's
bpeon manager). Scanned 2000 blocks ending at 27,782,694
(~1.2h time span) with `scripts/scan_operator.py`.

| metric               | value |
|----------------------|-------|
| matched txs          | 6     |
| actions produced     | 7     |
| decode coverage      | 100%  |
| action_type          | `harvest_stop` × 7 |
| `executeTyped` txs   | 5 (1 action each) |
| `executeBatchedAllowFailure` txs | 1 (fan-out to 2 actions) |

Interpretation: the operator is actively running a harvest-stop loop
(6 txs in 72min). All 6 txs decode cleanly; the fan-out case correctly
produces 2 separate `kami_action` rows sharing `tx_hash`. No unknown
selectors, no decode errors, no surprises.

`kami_id`/`node_id` are `NULL` on every row — **expected**. Per
existing decoder-notes, `harvest_stop` takes the harvest *instance* ID
(stored in `metadata.harvest_id`), not the kami. Recovering `kami_id`
requires joining against the prior `harvest_start` row where the
instance was created — a Phase B/derived-metrics task, not Stage 1.

What this validation does NOT confirm (documented for future sessions):

- The CLAUDE.md note that bpeons harvest node 47 cannot be checked
  from stop txs alone (no `node_id` in calldata). A longer back-scan
  reaching the original `harvest_start` txs would close this gap.
- 1.2h is too short to see a full harvest cycle. No `harvest_collect`
  txs landed in the window; the operator likely collects less
  frequently. Widen to 20k+ blocks in a future scan if needed.

Conclusion: decode quality against this known-good operator is clean.
Safe to proceed to 4-week backfill.

## Investigation: action-mix divergence (session 2.5, 2026-04-18)

The session-2 backfill crashed ~47k blocks in (cursor=26,850,306) with an
unhandled `requests.exceptions.ConnectionError`. The partial DB (3,688
actions, 3,757 raw_tx) shows an action mix nothing like the head
validation:

| rank | action_type      | count | head-validation % |
|------|------------------|-------|-------------------|
| 1    | `move`           | 972   | ~0.7%             |
| 2    | `item_craft`     | 944   | ~2.3%             |
| 3    | `skill_upgrade`  | 691   | ~1.9%             |
| 4    | `lvlup`          | 532   | ~1.8%             |
| 5    | `item_use`       | 178   | ~9% (feed)        |
| …    | `harvest_start`  | **1** | **~46%**          |
| …    | `harvest_stop`   | **1** | **~37%**          |

The partial DB spans blocks 26,803,238 .. 27,778,603 but 99.95% of rows
are in the early bucket 26,803,238 .. 26,850,244 (~47k blocks, ~17h of
chain time 22 days ago). The two near-head harvest rows are stray
artifacts from a Session-1 ingester test; unimportant.

### Root cause: H1 confirmed — harvest systems were redeployed

The MUD registry at `0x3B9B1223d876968B8Ba319CDdD4B4B6739B462AA` is
stable across the window, but individual system-ID → address mappings
are **not**. Probing the registry with `block_identifier=N` for
`system.harvest.start` yields:

| probe block | resolved harvest.start addr                   | note        |
|-------------|-----------------------------------------------|-------------|
| head        | `0x0777687Ec9FEB7349c23a19Ba7D11a1fe8cd35F1` | current     |
| head-100k   | `0xA0b4E6F34d639d11262A80dDdCc6d891F1eb80ed` | ~2.3d ago   |
| head-400k   | `0xA0b4E6F34d639d11262A80dDdCc6d891F1eb80ed` | ~9d ago     |
| head-600k   | `0x3288687Ec346c9cf8Adea8772530191C3f029C8A` | ~14d ago    |
| head-800k   | `0x04938bc48a11D38c4b6Fbb6BeE9FAAfbBb7A2208` | ~19d ago    |
| head-960k   | `0x04938bc48a11D38c4b6Fbb6BeE9FAAfbBb7A2208` | ~22d ago    |

Four distinct harvest.start deployments in 22 days. Similar picture for
harvest.stop/collect/liquidate. `eth_getCode` at the current
`0x0777687Ec…` address returns 14,562 bytes at head but **zero bytes**
at block 26,820,000 — the contract did not exist yet.

Our ingester resolves system addresses **once at startup against head**
(`system_registry.resolve_systems`), then filters each block's txs by
`to ∈ {registered addresses}`. Historical harvest txs targeted the
*old* deployments; their `to_addr` didn't match any current address,
so they were silently dropped at the match step — not even logged to
`memory/unknown-systems.md` (that path fires only for matched-but-
undecodable selectors).

Systems whose addresses happened to be stable across the window
(`system.account.move`, `system.craft`, `system.skill.upgrade`,
`system.kami.level`, …) were captured correctly, which is why the
captured mix is dominated by those.

### Hypotheses H2, H3, H4

- **H2 (partial blocks from RPC):** ruled out. Total tx throughput in
  the early bucket looks consistent; the hole is category-specific,
  not density-specific.
- **H3 (head-block injection):** 2 near-head rows exist (block
  27,778,601 and 27,778,603) but they target *current* addresses and
  are almost certainly from an ingester test run late in Session 1.
  Trivial artifact, will be wiped by the session-2.5 DB reset.
- **H4 (harvest really was ~0% 22 days ago):** ruled out. H1 fully
  explains the data and is confirmed by direct registry probes + code
  size checks.

### Implication for the 1-week window

The *most recent* harvest.start redeployment (head-100k → head, i.e.
current `0x0777687Ec…`) happened within the last ~2.3 days. A naive
7-day backfill would therefore **still miss ~2/3 of historical
harvest txs** — the older `0xA0b4E6F…` deployment covers roughly
day -2.3 through day -9. Session 2.5 ships the 1-week window anyway
(gives us a clean sample we can reason about and stays clear of the
retention edge), but harvest coverage in the historical portion of
that sample will be partial until the registry-snapshot fix lands.

### Fix (proposed, OUT OF SCOPE for session 2.5)

Three paths, in increasing complexity:

1. **Selector-based filtering.** Ignore `to_addr`; match by
   `tx.input[:4]` against a precomputed set of selectors across all
   ABIs. The selector is a stable function signature — redeployments
   don't change it. Downside: loses system-ID tagging at the match
   step (recoverable by selector→system_id reverse map, since
   selectors are effectively ABI-unique within the Kamigotchi universe).
2. **Per-chunk registry resolution.** Re-resolve the system registry
   at each backfill chunk boundary via `block_identifier`. Precise,
   costs ~35 RPC calls every 500 blocks.
3. **Historical address union.** At backfill startup, probe the
   registry at ~10 block heights across the target window and union
   the resulting system-ID → address sets. One-time cost, no mid-run
   RPC overhead. Best fit for Stage 1.

Recommend option 3 for session 3+. See
`memory/next-steps.md` for the action item.

---

## Session 3.5 backfill summary (2026-04-24)

Registry-snapshot backfill completed cleanly.

**Wall time & coverage**
- Start: 2026-04-22 17:05 UTC, end: 2026-04-24 18:56 UTC (~49.9 h)
- 420,001 blocks scanned, 0 survival-loop resume events in
  `logs/backfill.log` (zero unhandled exceptions across the full run).
- Block range in DB: 27,550,126..27,970,125
- Time range: 2026-04-12 03:02 UTC..2026-04-22 17:05 UTC (10.59 days)
  — wider than the nominal 7-day window; backfill seeded at head - 420k
  blocks (~2.35 blk/s × 2 days).

**Row counts**
- `raw_tx`: 260,088
- `kami_action`: 478,358
- `kami_static`: 0 (not yet backfilled — deferred, see next-steps.md)
- `system_address_snapshot`: 40 (34 unique system_ids, 6 with ≥2
  addresses)

**Action-type distribution** (top 10, 478,358 total)

| action_type       | count   | %     |
|-------------------|---------|-------|
| harvest_stop      | 209,893 | 43.88 |
| harvest_start     | 171,416 | 35.83 |
| feed              |  40,611 |  8.49 |
| skill_upgrade     |  12,343 |  2.58 |
| move              |  10,755 |  2.25 |
| lvlup             |  10,391 |  2.17 |
| item_craft        |   9,321 |  1.95 |
| harvest_collect   |   6,804 |  1.42 |
| harvest_liquidate |   2,394 |  0.50 |
| droptable_reveal  |     932 |  0.19 |

**Harvest coverage: 81.63%** of decoded actions
(`harvest_start + harvest_stop + harvest_collect + harvest_liquidate`
= 390,507). **7,019 unique kami_id** in harvest rows — the registry
fix is clearly working. Session 2.5 bugged DB showed ~0% harvest.

**Registry snapshot** — 6 systems with ≥2 addresses (the redeployment
set the fix was designed to recover):

- `system.harvest.collect` (2)
- `system.harvest.liquidate` (2)
- `system.harvest.start` (2)
- `system.harvest.stop` (2)
- `system.kami.gacha.reroll` (2)
- `system.kami.use.item` (2)

**bpeon operator spot-check** (`0x86aDb8…cEC2`, session-1 validator)
- 1,449 total actions in the 10.6-day window
- 1,108 harvest rows across all nodes (76.5% of bpeon activity)
- 20 harvest rows on node 47 specifically — session-3 prompt expected
  "many on node 47 (Scrap Paths)"; actual node distribution shows
  bpeon has shifted off node 47. Not a data-quality concern — other
  nodes are well-populated and harvest totals are healthy.
- Full type mix: harvest_stop 598, harvest_start 510, move 222,
  quest_complete 28, droptable_reveal 25, scavenge_claim 24, others.

**Validator cross-check** — `scripts/validate_decode.py --blocks 2000`
ran live against head (blocks 28,044,476..28,046,475, 76k blocks
past the DB's max since backfill ended 2 days ago):

- 1,155 txs seen, 1,146 matched/decoded, **0 unknown selectors,
  0 decode errors**, 2,474 actions produced.
- Action mix: harvest_stop 48.0%, harvest_start 39.6%, feed 3.6%,
  item_craft 2.7%, lvlup 2.0%, item_use 1.7%. Harvest coverage
  88.2% — close to DB's 81.6% (difference consistent with normal
  activity drift and the longer 10.6-day window mixing in calmer
  periods).

**Acceptance gate: PASS.** Proceeding to co-hosted serve launch.

**Unknown-selector log** — 1,137 lines appended to
`memory/unknown-systems.md` during backfill, all selector `0x09c90324`
(1,134 against `system.quest.accept`, 3 against
`system.account.use.item`). Both systems are in the overlay registry;
the selector itself is not in the vendored ABI. Tier-B overlay
candidate pending upstream signature confirmation.


## Session 3.5 serve launch health check (2026-04-24 21:02 UTC)

Co-hosted `ingester.serve` launched via screen `kami-oracle`. Bound
to `127.0.0.1:8787`. Poller thread scanning forward from backfill
end (27,970,125) toward head (~28,046,946, ~76k-block catchup).

**Cursor advance** — sampled `/health.cursor.last_block_scanned`
every 30 s for 10 min:

- T0 (before window): 27,970,253
- t=30 s:  27,970,335  (+82)
- t=60 s:  27,970,408  (+73)
- t=300 s: 27,970,978  (+570 from t=30, avg 2.34 blk/s)
- t=600 s: 27,971,692  (+1,357 from t=30, avg 2.38 blk/s)

Total advance from T0 to t=600: **1,439 blocks in 603 s = 2.39
blocks/s**, matching the single-threaded backfill throughput. No
stalls, no regressions.

**Endpoint smoke + latencies** (`time curl`, localhost):

| endpoint                                      | status | latency |
|-----------------------------------------------|--------|---------|
| `/health`                                     | 200    |   62 ms |
| `/actions/types?since_days=7`                 | 200    |  115 ms |
| `/actions/recent?limit=5`                     | 200    |   40 ms |
| `/nodes/top?since_days=7&limit=10`            | 200    |  132 ms |
| `/operator/{bpeon}/summary?since_days=7`      | 200    |   73 ms |
| `/kami/{id}/summary?since_days=7`             | 200    |   93 ms |
| `/kami/{id}/actions?since_days=7&limit=10`    | 200    |  152 ms |
| `/registry/snapshot`                          | 200    |   43 ms |

**Bounds-cap validation** — `MAX_SINCE_DAYS=28`, `MAX_LIMIT=2000`
in `ingester/api.py`:

| request                                   | expected | actual |
|-------------------------------------------|----------|--------|
| `/actions/recent?limit=5000`              | 422      | 422 ✓  |
| `/actions/types?since_days=999`           | 422      | 422 ✓  |
| `/actions/recent?limit=2000` (at cap)     | 200      | 200 ✓  |
| `/actions/types?since_days=28` (at cap)   | 200      | 200 ✓  |

**Sample payloads**

- `/actions/types?since_days=7` returned 22 distinct action_types,
  total 214,033, harvest share ~82% — consistent with the backfill
  summary above.
- `/nodes/top?since_days=7&limit=10` top node is `86` with 27,688
  harvest_starts, then 16/18/9/53/35/73/65/72/75.
- bpeon (`0x86aDb8…cEC2`) 7-day summary: 321 total actions, 20
  distinct kami, 162 harvest_stop, 103 harvest_start, 34 move, 8
  scavenge_claim, 7 droptable_reveal, 6 quest_complete, 1 feed.
- `/registry/snapshot`: 34 systems, 40 addresses — matches
  `system_address_snapshot` row count.

**Status: serve launch successful**, founder-testing guide published
at `memory/founder-testing.md`.

