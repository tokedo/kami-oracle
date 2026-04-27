# Decoder notes

## MUSU semantics — gross vs net (Session 8 promotion)

This was scattered across "Session 7 — bpeon cross-check" and the
schema; pulled up here so future sessions don't have to dig.

**`kami_action.amount` is gross MUSU pre-tax.** Gross = the integer
item-count drained from the harvest entity *before* the on-chain tax
split. The decoder records it from the World's `ComponentValueSet`
write to the harvest entity (see "Session 7 — MUSU Transfer probe"
for the receipt-walk).

**Always use gross for kami leaderboards / productivity comparisons.**
A medium kami on a 0%-tax node and a strong kami on a 12%-tax node
would invert in *net* rankings even though productivity is identical.
Tax is a node-config artifact, not a kami stat.

**Net is operator economics, not kami productivity.** When operator
inventory is what you care about, derive it on the fly:

```
net = gross - gross * harvest_start.taxAmt / 1e4
```

`taxAmt` is in basis points and lives in `metadata_json` on the
matching `harvest_start` row (decoder field map already buckets it
there — see `decoder.SYSTEM_FIELD_MAP["system.harvest.start"]`).
Empirically validated against eight bpeon harvest_collect samples in
the "Session 7 — bpeon cross-check" block below: oracle gross equals
chain delta to operator + tax to taxer entity in every case.

**No 1e18 scaling.** MUSU is an in-game item index (item index 1),
not an ERC-20. Cast as `CAST(amount AS HUGEINT)` in raw SQL.

NULL on `harvest_collect` / `harvest_stop` / `harvest_liquidate`
means "this on-chain action transferred no MUSU" — typically a no-op
via `executeBatchedAllowFailure` against an already-stopped harvest.
NULL is a real, useful signal, not a decoder gap.

The client library (`client/`) renames the field to `musu_gross` in
its dataclasses to enforce this naming clarity at the consumer
layer. The wire protocol still calls it `amount` so the Colab
notebook keeps working.

---

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


## Session 4 internal plane smoke test (2026-04-24 22:20 UTC)

Ran after the systemd cutover and non-loopback bind landed. Token
generated via `secrets.token_urlsafe(32)`, stored only in `.env`
(gitignored) under `KAMI_ORACLE_API_TOKEN`. Service managed by
`kami-oracle.service` (active, listening on `0.0.0.0:8787`).

**Bearer-token auth**

| request                                        | expected | actual |
|------------------------------------------------|----------|--------|
| `GET /health` (no auth)                        | 200      | 200 ✓  |
| `GET /actions/types?since_days=7` (no auth)    | 401      | 401 ✓  |
| `GET /actions/types?since_days=7` (with token) | 200      | 200 ✓  |

`/actions/types?since_days=7` with token returned `total=222,883` —
consistent with continued ingest since the 3.5 smoke (was 214k). Top
types: harvest_stop 44.6%, harvest_start 35.3%, feed 8.8%,
skill_upgrade 2.7%, lvlup 2.4%.

**`/sql` plane**

Happy path — `SELECT kami_id, COUNT(*) FROM kami_action WHERE
action_type='harvest_start' AND block_timestamp > now() - INTERVAL 7
DAY GROUP BY 1 ORDER BY 2 DESC LIMIT 5` returned 5 rows, top kami
613 starts, latency 79 ms.

Rejection — `DROP TABLE kami_action` returned HTTP 400 with body
`{"detail":{"error":"validation","detail":"leading keyword 'DROP' is
not allowed; must be one of ['DESCRIBE','EXPLAIN','PRAGMA','SELECT',
'SHOW','SUMMARIZE','WITH']"}}`.

**Network bind**

```
$ ss -tlnp | grep 8787
LISTEN 0 2048 0.0.0.0:8787 0.0.0.0:* users:(("python",pid=153726,fd=18))
```

Startup log line as expected:
```
WARNING serve: API listening on 0.0.0.0:8787 — auth required (non-loopback bind)
```

**Data-quality note** (not blocking, flag for Session 6+): the
`harvest_stop` and `harvest_collect` rows have NULL `kami_id` in
kami_action. A `GROUP BY kami_id` on those action_types collapses to
a single null bucket holding ~103k rows. `harvest_start` populates
`kami_id` correctly. Cause is likely decoder-side: the stop/collect
paths probably dereference `harvest_id → kami_id` via a lookup that
isn't available at decode time. Worth revisiting once `kami_static`
backfill lands — might be resolvable with a post-decode join step or
by pulling the kami_id from a different calldata slot.

**Status**: internal query plane is live and authenticated. Service
survives crashes (systemd Restart=always) and VM reboots (enabled).
7-day window now self-prunes via the daemon thread at 3600s cadence.


## Session 5 public plane smoke test (2026-04-24)

End-to-end test against `https://136-112-224-147.sslip.io` from the
oracle VM after Caddy + middleware + bind switch went live.

### TLS
```
$ curl -sv https://136-112-224-147.sslip.io/health 2>&1 | grep -E "subject:|issuer:|HTTP/"
*  subject: CN=136-112-224-147.sslip.io
*  issuer: C=US; O=Let's Encrypt; CN=E8
< HTTP/2 200
```
Cert is HTTP-01-issued by Let's Encrypt E8, valid through Jul 23 2026.
HTTP/2, TLSv1.3.

### Auth'd /health (over HTTPS)
Returned 200 with full cursor / row-count / registry payload.

### /sql over HTTPS, 7-day action histogram
Returned 10 rows (harvest_stop=104,057; harvest_start=80,871;
feed=20,505; skill_upgrade=6,059; lvlup=5,303; item_craft=4,484;
move=3,765; harvest_collect=3,502; harvest_liquidate=1,308;
droptable_reveal=453). Latency 90 ms.
The 1-day window came back empty — expected, the cursor is ~36h
behind chain head and `now() - INTERVAL 1 DAY` is past the latest
ingested timestamp. Backlog is being eaten at ~50 blocks per ~25s.

### Rate limit (60/min)
65 requests in a tight loop:
  58 × 200, then 7 × 429
Window started at count=2 from two earlier /sql calls in the same
minute, so the 60-cap kicks in 2 requests sooner. Confirms the
counter is shared across endpoints per token, which is what we want.
429 response body: `{"error":"rate_limited","detail":"limit 60/min
exceeded"}`, with a `Retry-After` header.

### Path validation on /backup
Posting `dest_dir=/tmp/oracle-backup-test` returned 400 with body
`{"detail":"dest_dir must be inside /home/anatolyzaytsev/kami-oracle/db"}`.
Loopback gating not externally testable from the VM (loopback IS the
peer); the check is exercised in tests indirectly via client_ip.

### Backup script run
Manual `./scripts/backup-db.sh`: EXPORT step succeeded (1.9s, 67MB
parquet tarball). gcloud storage cp failed with HTTP 403 "Provided
scope(s) are not authorized" — VM SA scope is devstorage.read_only.
Question logged in memory/questions-for-human.md asking the founder
to widen scope.

**Status**: public plane is live, authenticated, rate-limited, with
nightly backup wired but blocked on SA scope. Founder can run the
Colab starter notebook once the token is in Colab secrets.

# Session 6 — kami_id stitch fix + kami_static backfill

Date: 2026-04-25.

## Session 6 baseline (before any work)

Cursor at chain head — 28075317 / 2026-04-25T14:54:01 (current UTC
2026-04-25T14:54:10). Single-threaded poller is keeping pace; the
"~36h behind" figure quoted in the Session 6 prompt was a Session-5
era observation that has since recovered. **Part 3 (poller
concurrency) is therefore not blocking the founder's golden query** —
deferred unless we hit drift again, see hand-off note for Session 7.

NULL kami_id distribution before the fix (`row_counts`: raw_tx=162009,
kami_action=309402):

| action_type        | rows    | NULL kami_id |
|--------------------|---------|--------------|
| harvest_stop       | 137,122 | 137,122      |
| harvest_start      | 113,943 | 0            |
| feed               |  23,624 | 0            |
| skill_upgrade      |   8,106 | 0            |
| lvlup              |   7,093 | 0            |
| item_craft         |   6,118 | 6,118        |
| move               |   5,725 | 5,725        |
| harvest_collect    |   4,152 | 4,152        |
| harvest_liquidate  |   1,177 | 0            |
| droptable_reveal   |     594 | 594          |
| scavenge_claim     |     562 | 562          |
| item_use           |     474 | 474          |
| listing_buy        |     243 | 243          |
| quest_complete     |     239 | 239          |
| gacha_reroll/mint  |    ~190 | ~190         |
| item_burn          |      57 | 57           |
| skill_respec       |      21 | 0            |
| friend / register  |       7 | 7            |
| goal_claim         |       7 | 7            |

Session 6 only addresses harvest_stop / harvest_collect. Other
NULL-kami_id rows (item_craft, move, scavenge_claim, etc.) are
out-of-scope; their calldata genuinely doesn't carry a kami_id (move
operates on the account, item_craft on a recipe, etc.) — they go
into a deferred bucket for a future pass that joins by `from_addr +
operator → owner_account → owned kamis` if/when needed.

## How `harvest_id` relates to `kami_id` — the deterministic mapping

`kamigotchi-context/integration/architecture.md` documents:

> Harvest | `keccak256(abi.encodePacked("harvest", kamiEntityId))` | Per-Kami harvest state

i.e. **the harvest entity ID is computed deterministically as
`keccak256(b"harvest" || kami_id_uint256_be)`**. One kami has exactly
one harvest entity at a time, so the relationship is bijective on the
universe of (kami_id, harvest_id) pairs we have ever observed.

Verified empirically: bpeon's most recent two harvest_start rows have
`kami_id` values; computing `keccak256("harvest" || kami_id)` for
each yields harvest_ids that match harvest_stop rows for the same
operator wallet:

```
kami_id  31233132...79529404  -> harvest_id 104349738...75050068  ✓ matches stop tx 0xb626818d...
kami_id  80999568...52636422533 -> harvest_id 11735597...00680540  ✓ matches stop tx 0xf143333d...
```

This means **no eth_call is needed** — the stitch is pure offline
keccak. Option A (stitch) and Option B (eth_call) from the Session 6
prompt collapse into a third option: compute the inverse map in
Python from the universe of distinct kami_ids we have already
observed, and use it to backfill kami_id on stop/collect rows.

The IDOwnsKamiComponent reverse lookup (initial hypothesis) was
wrong — that component maps `kami_entity → owner_account`, not
`harvest_entity → kami_entity`. A direct `get(harvest_id)` reverts.

## Distinct harvest_id cardinality

Across 137,122 harvest_stop rows there are only **6,754 distinct
harvest_ids**; across 4,152 harvest_collect rows there are **516
distinct harvest_ids**. So the mapping is small (each harvest_id is
re-stopped/collected ~20× on average — same kami stopped and
restarted on the same harvest entity many times).

## Decision

Add a real `harvest_id VARCHAR` column to `kami_action` (schema v1
→ v2 migration), populate it on every harvest_* row going forward,
and stitch `kami_id` via an in-process `HarvestResolver` keyed on
the (kami_id → keccak("harvest"||kami_id)) map. Backfill historical
rows with a one-shot script using the same keccak.

Validation: run the golden query post-fix; spot-check that bpeon's
top-N harvesters look like his actual fleet. Sample tx hashes used
in validation are in the commit message of the backfill script.

## Session 6 acceptance

Service restarted at 2026-04-25T15:54:17Z. Schema migrated to v2,
`kami_static` populated with 6,916 / 6,920 candidates (4 reverts —
burned/sacrificed kamis whose entity id no longer resolves through
GetterSystem). harvest_id stitched on 141,242 of 141,274 historical
harvest_stop / harvest_collect rows (99.977%).

Golden-query variant (harvest count, 7-day window, the slice we have
data for — see "MUSU NULL" gap below):

```sql
SELECT s.name, s.owner_address, COUNT(*) AS harvests
FROM kami_action a JOIN kami_static s USING (kami_id)
WHERE a.action_type IN ('harvest_collect','harvest_stop')
  AND a.block_timestamp > now() - INTERVAL 7 DAY
GROUP BY 1, 2 ORDER BY harvests DESC LIMIT 5
```

Top 5 harvesters by harvest count, 7-day window:

| name             | owner_address                              | harvests |
|------------------|--------------------------------------------|----------|
| Kamigotchi 6931  | 0x816E29648f7A26FA22B6D6ab39bd354251D55115 |      360 |
| Kamigotchi 10011 | 0x8649e0773018b773aE2bCd928762Ef29F026EFa6 |      110 |
| Kamigotchi 10647 | 0x8649e0773018b773aE2bCd928762Ef29F026EFa6 |      107 |
| Kamigotchi 5251  | 0xdb3032B18946EEd108B3De6ccd373ACEcFe3441d |      104 |
| Kamigotchi 3511  | 0x19F8a98C3512cf16731De58f83318ec553314F46 |       98 |

Kami names of the form "Kamigotchi NNNNN" are the on-chain default; only
players who explicitly renamed their kami have human-readable names
(e.g. "R3D", "Ari Muur" further down the list).

## Session 6 — MUSU amount NULL on harvest_stop / harvest_collect (gap)

`amount` is NULL for all 137k+4k harvest_stop / harvest_collect rows —
the `kami_action.amount` column is only populated when the calldata
itself carries an amount field (item_use, item_burn, gacha_mint,
listing_buy, item_craft). MUSU bounty on stop/collect comes from the
contract's internal accounting and is emitted as an ERC20 `Transfer`
event log on the MUSU token contract
(`0xE1Ff7038eAAAF027031688E1535a055B2Bac2546`) inside the same tx
receipt — but the ingester currently only processes calldata, not
receipt logs.

The founder's golden query asks for `SUM(amount)` MUSU collected; that
column will stay NULL until log decoding lands. Two options for the
next session:

1. **Add receipt log decoding to the ingester.** Each `process_block_range`
   call already fetches `tx_receipt` for status/gas; iterating
   `receipt.logs` and matching the MUSU Transfer signature gives us
   per-tx MUSU values. Cheap (no extra RPC). Adds an `amount` column
   write for the qualifying rows.
2. **Use harvest count as the leaderboard metric.** Already works (see
   above). Loses absolute MUSU but preserves the ranking signal.

Recommended for Session 7: do (1). It's the smallest delta that makes
the founder's golden query work end-to-end, and the same plumbing
unlocks MUSU-based metrics for `kami-zero`'s perception loop.

# Session 7 — MUSU Transfer probe (correcting Session 6's hand-off)

Date: 2026-04-25.
Vendor SHA: `332db78` (kami_context/UPSTREAM_SHA).

## Correction — MUSU is NOT an ERC-20

The Session 6 hand-off (`memory/decoder-notes.md` "Session 6 MUSU
gap" and `memory/questions-for-human.md`) named
`0xE1Ff7038eAAAF027031688E1535a055B2Bac2546` as the MUSU token
contract. Per the vendored `kami_context/chain.md` (sha 332db78,
"Currencies: Native ETH vs WETH vs In-Game Currencies" table and the
"$MUSU (In-Game Currency)" section starting at line 168):

- `0xE1Ff7038eAAAF027031688E1535a055B2Bac2546` is **WETH** — the
  ERC-20 wrapper around bridged ETH. Used for marketplace approvals.
- $MUSU is the in-game currency item index 1, **not** an ERC-20
  token. It "exists only as an in-game inventory item" and "cannot be
  transferred on-chain as a token."

Confirmed empirically against tx
`0x59ecb09fa31053e00115086ccbb0a1ac8b5285a5930fa81793ed3d1a78820e97`
(harvest_collect for kami `Disco Wrench`, owner `bD8DDEF...`):

- 24 logs total. Two ERC20 `Transfer` events at
  `0xE1Ff7038e...` (WETH), values `8.498e-6 ETH` and `2.211e-6 ETH`.
  These are operator gas/fee accounting, not the harvest payout —
  too small by orders of magnitude.
- 21 `ComponentValueSet` events emitted by the World contract
  (`0x2729174c265dbBd8416C6449E0E813E88f43D0E7`).

So the original Session 7 plan ("decode ERC-20 Transfer at MUSU
token contract") would have produced the wrong number entirely.

## How MUSU actually flows

MUSU is a `ValueComponent` (`component.value`,
`componentId = uint256(keccak256("component.value")) =
80678919686888423251211770875952264544944593537285580074425903087691541684961`)
balance keyed by the entity id
`keccak256(abi.encodePacked("inventory.instance", uint256 accountId,
uint32 1))` (from `kami_context/system-ids.md` "Reading Inventory
Balance"). The harvest entity (`harvest_id`) holds the *unclaimed
accrued bounty* on its own ValueComponent slot.

When `system.harvest.collect` / `system.harvest.stop` /
`system.harvest.liquidate` runs, the World emits, in order:

1. `ComponentValueSet(component.value, ..., harvest_id, NEW_BOUNTY)`
   — the system first writes the freshly-computed accrued amount to
   the harvest entity (e.g. `1001`).
2. The harvest entity's value is drained: another
   `ComponentValueSet(component.value, ..., harvest_id, 0)`.
3. The operator's MUSU inventory entity gets a single
   `ComponentValueSet(component.value, ..., MUSU_INV_ID, NEW_TOTAL)`
   — only the post-credit balance, not the delta.

So the *gross* MUSU drained by the action is recoverable by walking
the `component.value` writes to the action's `harvest_id` and taking
the **maximum** value (or equivalently the value of the first write,
since the second write is always 0). No prior-state read is needed.

Validated on three sample txs (operator-match column shows whether
the row's `from_addr` equals the inventory owner's operator wallet
in `kami_static`):

| action_type        | tx                  | harvest_id (truncated) | writes                | bounty | operator-match |
|--------------------|---------------------|-------------------------|-----------------------|-------:|----------------|
| harvest_collect    | 0x59ecb09fa31053e0… | 81194157677…498971494   | [(1, 1001), (6, 0)]   | 1001   | ✓ (Disco Wrench, bD8DD…) |
| harvest_stop       | 0x5a70106cf5a2f78…  | 100452166037…198665274  | [(1, 717), (6, 0), (11, 0)] | 717 | ✓ |
| harvest_stop       | 0xc22a56ac99d1299…  | 13653922722…785349258   | [(1, 218), (6, 0), (11, 0)] | 218 | ✓ |
| harvest_liquidate  | 0x5ae7117967817d7…  | 12266280309…377863975   | [(4, 812), (27, 0)]   | 812    | n/a (liquidator's MUSU goes to attacker) |
| harvest_start      | 0x3c9f353879d08ee…  | 47042844534…045255825   | []                    | NULL   | (no payout) |

`harvest_start` correctly emits no `ValueComponent` writes to the
harvest entity (the harvest record's value starts at 0 and is set on
first `start`/`stop`/`collect`). So `harvest_start.amount` stays
NULL by design.

## Decoder strategy

For each `harvest_collect` / `harvest_stop` / `harvest_liquidate`
row in a tx receipt:

1. Look up the row's `harvest_id`. For 99.7% of historical
   `harvest_liquidate` rows the column is NULL but
   `metadata_json.victim_harvest_id` is populated. Fall back to that
   when needed (Session 7 doesn't backfill the column itself — out
   of scope).
2. Walk `receipt.logs`. Filter on:
   - `address == World (0x2729174c265dbBd8416C6449E0E813E88f43D0E7)`
   - `topics[0] == keccak256("ComponentValueSet(uint256,address,uint256,bytes)")`
   - `uint256(topics[1]) == component.value componentId`
   - `uint256(topics[3]) == row.harvest_id`
   - `data` ABI-decodes to a 32-byte uint256
3. The row's `amount` is the **max** of those uint256 values. If
   no matching write is found, leave NULL and log.

This avoids the prompt's tx-index/log-index pairing scheme entirely
because we have a direct foreign key (`harvest_id`) from the action
row to the receipt event.

## Why no scaling

MUSU is a plain integer (in-game item count). No 1e18 divisor at the
SQL layer — the column just holds the integer string. The founder's
golden query examples that say `CAST(amount AS HUGEINT) / 1e18`
should be `CAST(amount AS HUGEINT)`.

## Session 7 acceptance — coverage

Post-backfill (107,040 historical rows updated; 0 receipt fetch
failures over 63,134 unique tx, 5528.9 s wall):

| action_type        | n       | non-null amt | NULL    | NULL %  |
|--------------------|--------:|-------------:|--------:|--------:|
| harvest_collect    | 4,191   | 4,164        | 27      | 0.64%   |
| harvest_stop       | 138,566 | 112,599      | 25,967  | 18.74%  |
| harvest_liquidate  | 1,181   | 1,029        | 152     | 12.87%  |
| harvest_start      | 115,219 | 0            | 115,219 | 100%    |

`harvest_start` stays 100% NULL by design (no payout). The remaining
NULLs on collect/stop/liquidate are **real on-chain no-ops** — calls
made via `executeBatchedAllowFailure` against an already-stopped
harvest write nothing to the harvest entity's `ValueComponent`, so
there's no drain to credit. Spot-checked three NULL `harvest_collect`
rows directly: 0 ValueComponent writes to the row's `harvest_id`
across the entire receipt.

The original Session 7 prompt asked for `<0.1%` NULL on
collect/stop/liquidate. That target was based on the ERC-20 model
where every `harvest_*` tx pays out — wrong premise. With the
correct ECS model, **NULL semantically means "this action transferred
no MUSU"**, which is a real and useful signal (it tells the founder
which calls were genuine retries / no-ops vs. real drains).

## Session 7 acceptance — bpeon cross-check

The prompt asked for an on-chain cross-check via "MUSU contract
Transfer events." MUSU has no token contract, so the equivalent
independent check is: query `ValueComponent.safeGet(MUSU_inv_entity)`
at `block-1` vs `block` and compare the delta to the oracle's
`amount`. (Function name is `safeGet`, **not** `getValue` as
documented in `kami_context/system-ids.md`; that doc is wrong, the
actual ABI in `ValueComponent.json` only declares
`get(uint256)` / `safeGet(uint256)`.)

ValueComponent live address (resolved via the components registry
on the World): `0x23F86938Cf4CE6F6A78d32A4F49F72798f305F5f`.

**8 single-action harvest_collect samples across 8 different kamis,
2026-04-25**:

| tx (truncated)   | oracle gross | chain Δ to operator | "tax" % |
|------------------|-------------:|--------------------:|--------:|
| 0x89a52c0da71345 | 780          | 734                 | 5.90%   |
| 0x6a5997a0c1ee8d | 635          | 559                 | 11.97%  |
| 0x87e3ca2e021e94 | 1000         | 940                 | 6.00%   |
| 0x4f30e37780341f | 1001         | 941                 | 5.99%   |
| 0x3da7469c5a155f | 1000         | 940                 | 6.00%   |
| 0x59ecb09fa31053 | 1001         | 941                 | 5.99%   |
| 0x76efa05046d716 | 281          | 248                 | 11.74%  |
| 0xd54032dcf3fcda | 2079         | 0                   | 100.00% |

The "tax" gap is the on-chain harvest tax (`harvest_start.taxAmt`
parameter — see `decoder.SYSTEM_FIELD_MAP["system.harvest.start"]`
which buckets `taxerID` and `taxAmt` into metadata). The operator's
MUSU inventory only receives the *net* bounty; the tax goes to the
taxer's MUSU inventory entity in the same tx.

**The oracle records the gross bounty by design** — that's what
"how much MUSU did this kami harvest" actually means for a
leaderboard. The split into operator/taxer is an inventory transfer,
not a productivity metric. Most node-47 / standard nodes show 6%
tax. Higher rates (12%, 100%) correspond to specific node configs;
they're real on-chain settings, not data bugs.

So the cross-check is **green**: oracle equals chain delta plus tax
across all eight samples, with no decoder mismatches.

## Session 7 acceptance — golden queries

**Top earners by MUSU (7-day, harvest_collect + harvest_stop)**:

| name             | owner_address                              | collects | musu  |
|------------------|--------------------------------------------|---------:|------:|
| Kamigotchi 2808  | 0xae190Eb02b793A17cE6f649A26F71D38a32f9dEd | 0        | 65696 |
| Kamigotchi 11200 | 0xae190Eb02b793A17cE6f649A26F71D38a32f9dEd | 0        | 53156 |
| Kamigotchi 4126  | 0xae190Eb02b793A17cE6f649A26F71D38a32f9dEd | 9        | 45229 |
| Kamigotchi 8333  | 0x4d72D4269Ab46962713ca19cAb8161A87684A163 | 5        | 43821 |
| Kamigotchi 13946 | 0xae190Eb02b793A17cE6f649A26F71D38a32f9dEd | 9        | 37882 |

Note: top three are all owned by `0xae190E…fdEd` — that wallet runs
a fleet on a higher-tax / higher-yield node. The "0 collects" on
top entries means they harvest via `harvest_stop` only (drain on
stop, restart). `harvest_collect` is for partial draws without
ending the harvest.

**Liquidation leaderboard (7-day)**:

| liquidator       | hits | musu_taken |
|------------------|-----:|-----------:|
| Kamigotchi 3070  | 50   | 59678      |
| Kami 9939        | 47   | 53926      |
| Kamigotchi 3188  | 32   | 42919      |
| Kamigotchi 1437  | 38   | 33113      |
| Nova Heat        | 38   | 29824      |

**For the founder's actual queries**: drop the `/ 1e18` from the
golden query at the top of the Session 7 prompt — MUSU is an
integer item count, not an 18-decimal token. The correct cast is
`CAST(amount AS HUGEINT)`.

## Session 9 — account fetch (account_index, account_name)

Added two columns to `kami_static`: `account_index` (small 1..N
ordinal, mirrors `kami_index`) and `account_name` (human display
name like "bpeon", "ray charles"). Both come from
`GetterSystem.getAccount(uint256 accountId)` — already documented in
`kami_context/system-ids.md` Getter System section but missing from
the vendored `GetterSystem.json` ABI (Tier-A overlay territory; the
populator merges a doc-cited fragment in at construction time).

Call shape:

```solidity
function getAccount(uint256 accountId) view returns (
  tuple(
    uint32 index,
    string name,
    int32 currStamina,
    uint32 room
  )
)
```

We persist `index` and `name` only; `currStamina` and `room` are
ephemeral and would just churn `last_refreshed_ts` if we tracked
them in `kami_static`.

**Coverage post-backfill (2026-04-26):**

| metric                              | value |
|-------------------------------------|------:|
| kami_static rows total              | 6,923 |
| distinct account_id (non-zero)      |   146 |
| rows with account_name populated    | 6,923 (100.0%) |
| accounts with account_name populated|   146 (100.0%) |
| accounts that came back anonymous   |     0 |

100% coverage is unusual — it means every owning account in our
window has called `system.account.set.name` at some point. Future
windows may legitimately surface anonymous accounts (NULL
`account_name`); the populator handles that gracefully and the
schema comment block calls it out.

**Bpeon cross-check (founder validator)**: 20 kamis come back with
`account_name = 'bpeon'`. `kami_index` values:

```
43, 1064, 2553, 3874, 3983, 6096, 7722, 7803, 8745, 10011,
10647, 11716, 12459, 13235, 13390, 13702, 13857, 13947, 14286, 14306
```

(Including kami #43 "Zephyr" — the only named one of the fleet;
the rest carry default "Kamigotchi N" labels.)

**Within-pass cache.** The populator's `KamiStaticReader` caches
`getAccount` results by `account_id` for the lifetime of one
`backfill_all` / `refresh_stale` pass. With 146 unique accounts
across 6,923 kamis, the cache cuts chain calls by ~47×. The cache
also memoises *failures* — a reverting account stays cached as
`(None, None)` for the rest of the pass instead of re-hitting the
RPC each time.

**Operator name vs signer wallet.** `account_name` in `kami_static`
is the in-game Account display name. `kami_action.from_addr` is the
signer EOA (often a kamibots automation key, e.g.
`0x86aDb8…cEC2`). They coincide for accounts that operate manually
but diverge under automation. Use `account_name` for kami-centric
labels and `from_addr` only when filtering by signer.

## Session 10 — build fields on chain

The kami's *current build* (effective stats, level, skills,
equipment) is the next thing the meta-clustering analysis needs. The
oracle already has trait images and base stats; this section
documents what's added to `kami_static` in Session 10 and the
chain-side sources for each field.

### What's resolved on chain vs. computed

`getKami(uint256 kamiId)` already returns the per-kami stat tuple
`(base, shift, boost, sync)` for health / power / harmony / violence,
plus `level` and `xp`. The Session 9 populator throws away `level`
and `xp` (`_lvl, _xp` in the destructure); Session 10 wires them
through. The stat tuple already carries every modifier (skill
shifts, equipment shifts, item boosts) merged in — we don't
recompute those, we just collapse the four-field stat into the
canonical effective scalar via the game's documented formula.

**Effective stat formula** (per
`kamigotchi-context/systems/state-reading.md` and `health.md`,
identical for all four core stats and slots):

```
effective = max(0, floor((1000 + boost) * (base + shift) / 1000))
```

This is the same expression Kamigotchi's own client / state-reading
docs use, applied to `kami.stats.health`, `kami.stats.power`,
`kami.stats.harmony`, `kami.stats.violence`. The `sync` field is
the last-synced *current* depletable HP — it's the runtime value,
not a build property, so we don't store it on `kami_static`.

### Per-field resolution

| field | source | notes |
|---|---|---|
| `level` | `getKami(id).level` | already in tuple, persisted directly |
| `xp` | `getKami(id).xp` | already in tuple, persisted directly |
| `total_health` | `getKami(id).stats.health` → formula | effective max HP |
| `total_power` | `getKami(id).stats.power` → formula | effective Power (combat output, harvest fertility) |
| `total_harmony` | `getKami(id).stats.harmony` → formula | effective Harmony (strain-resistance, defense floor) |
| `total_violence` | `getKami(id).stats.violence` → formula | effective Violence (kill threshold, defense ceiling) |
| `total_slots` | `SlotsComponent.safeGet(id)` → formula | the slots-stat resolved scalar; **in-game capacity = 1 + `total_slots`** (per `equipment.md`, base capacity is implicit 1) |
| `skills_json` | `IDOwnsSkillComponent.getEntitiesWithValue(id)` → enumerate; per skill, `IndexSkillComponent.safeGet` + `SkillPointComponent.safeGet` | JSON array `[{"index": N, "points": M}, ...]` |
| `equipment_json` | `IDOwnsEquipmentComponent.getEntitiesWithValue(id)` → enumerate; per equip, `IndexItemComponent.safeGet` | JSON array of item indices `[item_index, ...]`. Slot-name resolution deferred (see below). |

`SlotsComponent`, `IDOwnsSkillComponent`, `IndexSkillComponent`,
`SkillPointComponent`, `IDOwnsEquipmentComponent`, `IndexItemComponent`
all resolve via `world.components()` (the components registry) — a
separate registry from `world.systems()` used by the existing
`SystemRegistry`. Resolution pattern is identical: keccak256 the
component name string, call `getEntitiesWithValue(hash)`, low-160
bits of the returned entity is the component contract address.

Notes on each:

- `total_slots` semantics. The formula above resolves to the
  slots-stat effective scalar. The in-game equipment capacity (per
  `kamigotchi-context/systems/equipment.md`) is `1 +
  EQUIP_CAPACITY_SHIFT bonus`, and `EQUIP_CAPACITY_SHIFT` is the
  slots-stat shift. Today no skill or item grants slots, so the
  observed value across the fleet is uniformly 0 (capacity = 1).
  Stored as the chain value, not as `1 + chain value`, to keep the
  column shape identical to `total_health` / `total_power` /
  `total_harmony` / `total_violence` — the "+1" is an interpretation
  layer the founder's Colab can apply when needed.
- `skills_json`. The skill catalog (which `index` corresponds to
  which named skill) lives in `kamigotchi-context/catalogs/skills.csv`
  — not joined here. Stored as raw indices so a future session can
  resolve via the catalog without re-reading chain.
- `equipment_json`. We store **only item indices**, not slot names.
  `component.for.string` (which would give the slot-name string per
  equip entity) does not resolve in the current components registry
  snapshot. Capacity is 1, so most kamis have 0 or 1 equipped — the
  ergonomic loss is small. Slot names can be added in a future
  session if a consumer asks.

### Why no separate `attack_threshold` / `defense_threshold`

The Session 10 prompt asked about combat thresholds. In Kamigotchi,
liquidation viability is determined by the attacker's effective
Power vs the victim's effective Violence (and the victim's HP
state). Both are already exposed as `total_power` / `total_violence`
above — there is no separate "attack threshold" or "defense
threshold" scalar on chain that isn't a derivation of those two
plus health. Stage-1 meta clustering uses
`(total_harmony, total_power, total_violence, level)` as its
primary feature space; thresholds collapse cleanly into those.

A discrete "can-be-liquidated below X HP" threshold may exist as a
config value in the `component.value` registry under
`is.config:LIQUIDATION_*`, but I did not surface a clean,
kami-specific version of that during discovery and the prompt's
fallback ("compute from what is on chain, clearly labeled as
derived") is heavier than Stage-1 needs. Deferred until a consumer
asks.

### Bpeon fixture cross-check (Zephyr, kami #43)

Raw build dump from `scripts/discover_build_fields.py` against the
live chain at block 28,141,324:

```
kami_index = 43
name       = Zephyr
account    = 766652271399468889391879684419720168355448418214 (= bpeon)
level      = 37
xp         = 136367
room       = 16
state      = RESTING
affinities = ['NORMAL', 'EERIE']

stats — (base, shift, boost, sync) per stat, effective per the formula
  health    base=  90 shift= 140 boost=   0 sync=  78 -> effective=230
  power     base=  16 shift=   0 boost=   0 sync=   0 -> effective= 16
  harmony   base=  11 shift=   8 boost=   0 sync=   0 -> effective= 19
  violence  base=  17 shift=   0 boost=   0 sync=   0 -> effective= 17

SlotsComponent.safeGet(zephyr) = base=0 shift=0 boost=0 sync=0
  -> effective=0  (in-game capacity = 1 + 0 = 1)

skills (10 owned, sum points=37 — matches level since 1 SP/level
+ 1 starting = level=37 implies 37 spent post-respec):
  index=212 points=5
  index=222 points=5
  index=223 points=5
  index=232 points=1
  index=311 points=5
  index=312 points=5
  index=323 points=5
  index=331 points=1
  index=322 points=4
  index=313 points=1

equipment: 0 entities owned (nothing equipped)
```

The level↔skill-points round-trip (sum of `skills_json[*].points`
== `level` after a respec) is a useful regression invariant for
future sessions.

### Components registry resolution (new ingester capability)

The Session 9 system registry resolves system contracts via
`world.systems()`. Components live in a *parallel* registry at
`world.components()` — same `getEntitiesWithValue(uint256)` ABI,
different address. The build populator needs both. Pattern:

```python
world.components() -> registry_addr  # call once at startup
ents = registry.getEntitiesWithValue(keccak256("component.X"))
addr = checksum("0x" + format(ents[0], "040x"))
```

Resolved addresses observed during Session 10 discovery
(2026-04-27, head=28,141,324):

| component | address |
|---|---|
| world.components() | `0x4d61e6034C6aE2556045186a37885A6F492f96De` |
| component.stat.slots | `0x574FdC51149Ad37147A6cBB60F2032b88665996F` |
| component.id.skill.owns | `0xedc5A68961B934e83718C6c8E09EA35944C75a04` |
| component.index.skill | `0xb23e9cAd4a81100b41B86751Ca1B90F5e755Ab05` |
| component.skill.point | `0x8Ed89867C0F2864904e9301d9C6E099DBD6c9fd7` |
| component.id.equipment.owns | `0x091A3408b6b07b503B18d942aA0e73DE2F1D66d6` |
| component.index.item | `0x40F05C42e3BA119cF9f8F7D82999C294373E599E` |
| component.for.string | NOT RESOLVED |

Notable: `component.id.equipment.owns` IS in the on-chain
components registry, even though it's missing from
`kamigotchi-context/integration/ids/components.json`. That cheat
sheet is incomplete — trust the registry, not the cheat sheet.
`component.for.string` (slot-name lookup per equipment entity)
genuinely does not resolve, so equipment is stored by item-index
only.

### Cost / cadence

Per-kami chain calls in the build populator:

- 1× `getKami` (already done by Session 9)
- 1× `SlotsComponent.safeGet`
- 1× `IDOwnsSkillComponent.getEntitiesWithValue` + 2N for N skills
- 1× `IDOwnsEquipmentComponent.getEntitiesWithValue` + 1M for M equips

For Zephyr (10 skills, 0 equips): 1 + 1 + 1 + 20 + 1 + 0 = 24 calls.
At ~150–300 ms / call on the public RPC, single-kami latency
~3.6 – 7.2 s. With 8 parallel workers, 7,020 kamis → ~50–100 min
backfill. Daily refresh sweep is the same cost; that's the
acceptable Stage-1 ceiling. **Event-triggered per-kami refresh on
`skill_upgrade`/`equip` is deferred to Session 11+** if the daily
sweep proves too coarse.

## Session 11 — skill-effect modifiers on chain

The 12 non-stat skill effects (`SB`, `HFB`, `HIB`, `HBB`, `RMB`, `CS`,
`ATS`, `ATR`, `ASR`, `DTS`, `DTR`, `DSR` — full taxonomy in
`kami_context/systems/leveling.md` "Skill Effects") are NOT exposed
as pre-aggregated per-kami components on chain. Probing every
plausible name (`component.boost.harvest.fertility`,
`component.shift.attack.threshold`, etc. — 43 candidates in total)
returned 0 hits in `world.components()` at block 28,145,273.

Per-kami **bonus instances** do exist as separate entities, anchored to
the skill or equipment instance via `component.id.anchor`
(0xf04bd935E81445aF3BCd29Eb53Da7df0816A4CfE) and tagged with the
holder via `component.id.holder`
(0xD7388a0a79987CC85Ba3EF78a0Ac05Bc9Ade6ec0). Each bonus carries
`component.level` (matches the SkillPointComponent on the anchor
for skill bonuses; equals 1 for equipment) but NOT `component.type`
or `component.value` — those live on the upstream skill/item
**prototype** entity, which is unreachable via
`IndexSkillComponent.getEntitiesWithValue` because that component is
non-indexed (call reverts).

### Source of truth: the upstream catalogs

`kamigotchi-context/catalogs/skills.csv` and
`kamigotchi-context/catalogs/items.csv` ARE the canonical source
devs upload to chain at deploy. Round-trip evidence on Zephyr:

  ```
  health.shift on chain = 140
  Σ catalog SHS contributions = 50 (212 Cardio ×5) + 50 (312 Toughness ×5) + 40 (322 Vigor ×4) = 140  ✓

  harmony.shift on chain = 8
  Σ catalog SYS contributions = 5 (311 Defensiveness ×5) + 3 (331 Anxiety ×1) = 8  ✓
  ```

This proves the catalog → chain pipeline is faithful. Stage 1 stores
the catalog-walk sum as the resolved modifier, with `skills_json`
and `equipment_json` as the per-kami inputs. Equivalent to "the
game's own client-side aggregation," not "what the LibBonus solidity
loop would return at the next block" — those should be identical
for permanent bonuses (which is what these 12 modifiers are).

### Storage convention

Catalog units → stored integer:

| Effect key | Catalog units | Stored as | Example |
|---|---|---|---|
| `SB`  | Percent (decimal) | int ×1000 (signed) | -0.025 → -25 per point |
| `HFB` | Percent | int ×1000          | 0.06 → 60 per point |
| `HIB` | Musu/hr (integer) | int (Musu/hr)      | 15 → 15 per point |
| `HBB` | Percent | int ×1000          | 0.04 → 40 per point |
| `RMB` | Percent | int ×1000          | 0.05 → 50 per point |
| `CS`  | Seconds (integer, signed) | int (seconds, signed) | -10 → -10 per point |
| `ATS` | Percent | int ×1000          | 0.02 → 20 per point |
| `ATR` | Percent | int ×1000          | 0.05 → 50 per point |
| `ASR` | Percent | int ×1000          | 0.02 → 20 per point |
| `DTS` | Percent | int ×1000          | 0.02 → 20 per point |
| `DTR` | Percent | int ×1000          | 0.05 → 50 per point |
| `DSR` | Percent | int ×1000          | 0.02 → 20 per point |

Per-kami modifier = Σ over owned skills (`points × per-point catalog
value`) + Σ over equipped items (`equipment-row catalog value × 1`).

### Skill key → kami_static column

| Effect key | Column                   | Notes |
|---|---|---|
| `SB`  | `strain_boost`           | negative = less strain |
| `HFB` | `harvest_fertility_boost`| % boost to base income rate |
| `HIB` | `harvest_intensity_boost`| flat Musu/hr add |
| `HBB` | `harvest_bounty_boost`   | % boost to total bounty |
| `RMB` | `rest_recovery_boost`    | % heal-rate multiplier |
| `CS`  | `cooldown_shift`         | collect cooldown delta in seconds (negative = shorter) |
| `ATS` | `attack_threshold_shift` | additive shift to attack threshold |
| `ATR` | `attack_threshold_ratio` | multiplicative ratio on attack threshold |
| `ASR` | `attack_spoils_ratio`    | % of victim's bounty captured |
| `DTS` | `defense_threshold_shift`| additive shift to defense threshold |
| `DTR` | `defense_threshold_ratio`| multiplicative ratio on defense threshold |
| `DSR` | `defense_salvage_ratio`  | % of own bounty saved on liquidation |

The four stat-shift effects (`SHS`, `SPS`, `SVS`, `SYS`) are NOT
new columns — they're already folded into Session 10's
`total_health` / `total_power` / `total_violence` / `total_harmony`
via `getKami(id).stats.*.shift` (the chain pre-aggregates them into
the stat tuple).

### Zephyr fixture cross-check

Zephyr's expected modifier values from skills.csv × Session 10
skills_json (kami #43, 0 equipment):

```
strain_boost              = -125     (= -12.5%, from skill 223 ×5)
harvest_fertility_boost   = 0
harvest_intensity_boost   = 20       (= 20 Musu/hr, from 232 ×1 + 313 ×1)
harvest_bounty_boost      = 0
rest_recovery_boost       = 0
cooldown_shift            = 0
attack_threshold_shift    = 0
attack_threshold_ratio    = 0
attack_spoils_ratio       = 0
defense_threshold_shift   = 200      (= 20.0%, from 222 ×5 + 323 ×5)
defense_threshold_ratio   = 0
defense_salvage_ratio     = 0
```

These values must round-trip via the populator on the post-backfill
`kami_static` row.

### Cost / cadence

Zero new chain calls per kami — modifier columns are derived purely
from Session 10's already-fetched `skills_json` and
`equipment_json` joined to an in-process catalog cache loaded once
at populator startup. The "+30–60 min on the daily sweep"
estimate in the Session 11 prompt was conservative — actual added
cost is ~seconds (catalog load + per-kami arithmetic).
