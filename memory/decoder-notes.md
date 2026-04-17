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

### 3. `system.craft` — UNRESOLVED (flagged to human)

Vendored JSON + system-ids.md both declare
`executeTyped(uint256 assignerID, uint32 index, uint256 amt)`.
Chain txs use a **2-arg** selector `0x5c817c70`, which hashes to
`executeTyped(uint32, uint256)`. Decoding the 2 args on real craft txs
yields small, plausibly (recipe_index, amount)-shaped values (e.g.
`(34, 2)`, `(29, 1)`), but the field semantics are not documented
anywhere in kami_context. Flagged in `memory/questions-for-human.md` for
confirmation before adding to the overlay.

Impact at current volume: 6 unknown txs per 500-block sample (~2.3%).

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

## Validation scripts

- `scripts/validate_decode.py` — dry-run over an arbitrary block window;
  summarizes counts, examples, and appends unknowns to
  `memory/unknown-systems.md`. No writes to the production DuckDB.
- `tests/test_decoder.py` — 6 unit tests over canned on-chain calldata.
  Covers executeTyped, executeBatched fan-out, overlay signatures, and
  error paths. Runs offline.
