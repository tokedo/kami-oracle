# kami-oracle

> Open-source analytics pipeline for [Kamigotchi World](https://kamigotchi.io)
> on Yominet. Observes every on-chain action of every kami, decodes it,
> and stores it in a queryable analytics DB — so agents don't have to
> re-derive strategy from first principles.

**Status**: Stage 1 — ingest & store. Not yet exposing a query API.

## What it is

Every action every kami takes — harvest start/stop, feed, rest, move,
level-up, skill allocation, equip, liquidate, quest, trade, item
craft — is permanently recorded on Yominet. kami-oracle continuously
tails the chain, decodes these txs against the Kamigotchi System
ABIs, and maintains a rolling 28-day window of all kami activity in a
local DuckDB database.

Downstream phases will unlock questions like:

- "Top 20 Musu earners of the last 14 days."
- "How do the top efficient harvesters rotate between nodes?"
- "Which base-stat combinations do the most gas-efficient kamis
  share?"
- "Predator scan on node 47 over the last 48 hours."

## Architecture

```
Yominet RPC (public)
    │
    ▼
ingester/   (continuous tail, tx-level decode, idempotent upsert)
    │
    ▼
db/kami-oracle.duckdb   (rolling 28 days, Stage 1)
    │
    ▼
(future, Phase D) MCP server  →  playing agents
```

- **No writes to chain.** Read-only.
- **No private keys needed.** RPC URL only.
- **DuckDB file-backed**, local to the host.

## Design principles

- **Do one thing well.** Ingest and store decoded chain history.
  Nothing else.
- **Open source, no moat.** The repo is MIT. The data it reads is
  public. Anyone can run their own instance.
- **Read-only.** Oracle cannot influence the game.
- **Rolling window, not full history.** Stage 1 runs at 28 days
  (extended from 7 in Session 8; the window fills in over ~3 weeks
  as the chain is ingested). Bounded DB, signal fresh to the
  current meta.

## Operator labels in `kami_static` (Session 9)

Every `kami_static` row carries the in-game **Account** that owns
the kami via two columns: `account_index` (the small 1..N ordinal)
and `account_name` (the human display name — "bpeon",
"ray charles"). Both come from `GetterSystem.getAccount(accountId)`
and are refreshed on the same cadence as the rest of `kami_static`.
For kami-centric queries that join through `kami_static`, prefer
`account_name` as the operator label. The `owner_address` column is
still the canonical EOA wallet; `kami_action.from_addr` is the
*signer* (often a kamibots automation key, which can differ from
the owning account under automation).

## Build snapshot in `kami_static` (Session 10)

Every `kami_static` row now carries the kami's current **build** —
its effective stats, level, skills, and equipment — alongside the
trait + operator fields. New columns: `level`, `xp`, `total_health`,
`total_power`, `total_violence`, `total_harmony`, `total_slots`,
`skills_json`, `equipment_json`, `build_refreshed_ts`.

The four `total_*` stats are read directly from the chain via
`GetterSystem.getKami(...)` and resolved through the canonical game
formula `floor((1000 + boost) * (base + shift) / 1000)` documented
in `kamigotchi-context/systems/state-reading.md`. They are the same
effective scalars the in-game UI shows — not local recomputations
from base + first-principles. `total_slots` comes from
`SlotsComponent.safeGet(kamiId)` via the same formula; the in-game
equipment capacity is `1 + total_slots`. `skills_json` is a JSON
array of `{index, points}` per upgraded skill; `equipment_json` is a
JSON array of equipped item indices. Latest snapshot only — refreshed
on the daily `kami_static` sweep, not event-triggered. See
`schema/schema.sql` for the full per-column comment block and
`memory/decoder-notes.md` "Session 10 — build fields on chain" for
on-chain sources, the bpeon fixture cross-check, and resolved
component addresses.

## Skill-effect modifiers in `kami_static` (Session 11)

On top of the four `total_*` stats, every `kami_static` row also
carries the 12 non-stat skill-effect modifiers from the upstream
`kamigotchi-context/systems/leveling.md` "Skill Effects" table —
the values that make sustain-vs-combat-vs-yield builds visible.
New columns: `strain_boost` (`SB`), `harvest_fertility_boost` (`HFB`),
`harvest_intensity_boost` (`HIB`), `harvest_bounty_boost` (`HBB`),
`rest_recovery_boost` (`RMB`), `cooldown_shift` (`CS`),
`attack_threshold_shift` (`ATS`), `attack_threshold_ratio` (`ATR`),
`attack_spoils_ratio` (`ASR`), `defense_threshold_shift` (`DTS`),
`defense_threshold_ratio` (`DTR`), `defense_salvage_ratio` (`DSR`).

All 12 are summed across a kami's owned skills (per-point catalog
value × points spent) plus equipped items (catalog value × 1) using
the upstream `skills.csv` + `items.csv` catalogs. The catalog → chain
pipeline is faithful — round-tripped on bpeon's Zephyr (kami #43)
where catalog SHS sums (50+50+40 = 140) and SYS sums (5+3 = 8) match
chain `health.shift` and `harmony.shift` exactly. So the catalog walk
produces the resolved totals the game itself uses.

Stored at the same precision as on chain: percent values are ×1000
(e.g. `strain_boost = -200` means -20%), `cooldown_shift` is signed
seconds, `harvest_intensity_boost` is Musu/hr. Refreshed on the same
daily sweep as the Session 10 build columns; same `build_refreshed_ts`
marks both. The four stat-shift effects (`SHS`/`SPS`/`SVS`/`SYS`) are
NOT new columns — they're already folded into `total_health` /
`total_power` / `total_violence` / `total_harmony` via
`getKami(id).stats`. See `memory/decoder-notes.md` "Session 11 —
skill-effect modifiers on chain" for the catalog walk derivation,
storage convention, and Zephyr round-trip.

## Affinity columns in `kami_static` (Session 12)

Every `kami_static` row now carries the kami's body and hand
affinities as scalar columns: `body_affinity` and `hand_affinity`,
both VARCHAR drawn from `{EERIE, NORMAL, SCRAP, INSECT}` (uppercase
on chain). Extracted from `getKami(kamiId).affinities` (the
`[body, hand]` 2-string array, per
`kamigotchi-context/systems/state-reading.md`) on the daily sweep —
zero new chain calls, the same struct already feeds level/xp/stats.
Values are stored verbatim; no case normalization. The integer
`body` / `hand` columns (~30 / ~27 distinct trait indices) remain
alongside — body→affinity is many-to-one. Use the affinity columns
when grouping or joining by elemental type; use the integer columns
for trait-pose specificity. See `memory/decoder-notes.md` "Session 12
— affinities" for ordering verification and the chain dump.

## Slot-resolved equipment: `items_catalog` + `kami_equipment` (Session 13)

`equipment_json` on `kami_static` is a JSON array of raw `item_index`
values (e.g. `[30011]`) — chain-faithful but slot-anonymous.
`items_catalog` mirrors `kami_context/catalogs/items.csv` into DuckDB
so the `kami_equipment` view can resolve every equipped item to its
slot kind, name, and effect without a per-row chain getter.

- **`items_catalog`** — table. One row per item in `items.csv`. Columns:
  `item_index` (PK), `name`, `type`, `rarity`, `slot_type`, `effect`,
  `description`, `loaded_ts`. `slot_type` is non-NULL only when the
  catalog "For" cell matches `*_[Ss]lot` (today: `Kami_Pet_Slot`,
  `Passport_slot`); other "For" values (Account / Kami / Enemy_Kami /
  empty) collapse to NULL. Reload: `python -m ingester.items_catalog
  --reload` after re-vendoring `kami_context`.
- **`kami_equipment`** — view. One row per equipped item per kami.
  Joins `kami_static.equipment_json` against `items_catalog` to
  resolve `slot_type` / `item_name` / `item_effect`. Includes
  `freshness_seconds` and `is_stale` derived from `build_refreshed_ts`
  (threshold 36h — populator sweeps daily, so 36h gives one missed-
  sweep slack). `is_stale = TRUE` means **verify with live chain
  state via Kamibots before destructive ops** (unequip, transfer,
  liquidate). Snapshot lag means equipment can false-positive — an
  unequipped pet stays in the JSON until the next sweep.

```sql
-- All pet equips on a roster, ergonomic + freshness-aware.
SELECT kami_index, item_name, item_effect, freshness_seconds, is_stale
FROM kami_equipment
WHERE account_name = 'fey'
  AND slot_type = 'Kami_Pet_Slot';
```

See `memory/decoder-notes.md` "Session 13 — items_catalog +
kami_equipment view" for the slot-resolution rule and rationale.

## Resolved skills + current location: `skills_catalog` / `nodes_catalog` / views (Session 14)

`skills_json` on `kami_static` is a JSON array of raw
`{index, points}` entries — chain-faithful but effect-anonymous;
the agent had to re-derive "skill 212 = +10 HP/rank, skill 222 =
+2% DTS/rank" inline against `skills.csv` every session. Current
room had a parallel gap: agents fell back to
`harvest.node.roomIndex` from a live `get_kami_state` call, which
returns the *last-harvested* node, not the kami's actual current
room. Same Session 13 fix shape, twice: mirror a small static
catalog into DuckDB and expose a view that joins it.

- **`skills_catalog`** — table. One row per skill in
  `skills.csv`. Columns: `skill_index` (PK), `name`, `tree`
  (Predator / Guardian / Harvester / Enlightened), `tier`,
  `tree_req`, `max_rank`, `cost`, `effect` (SHS / HFB / SB / ...),
  `value`, `units`, `exclusion`, `description`, `loaded_ts`.
  Reload: `python -m ingester.skills_catalog --reload` after
  re-vendoring `kami_context`.
- **`kami_skills`** — view. One row per (kami, invested skill)
  per `skills_json` entry. Joins `skills_catalog` to expose
  `skill_name` / `tree` / `tier` / `effect` / `value` / `units`.
  Includes `freshness_seconds` and `is_stale` derived from
  `build_refreshed_ts` (threshold 36h, same as `kami_equipment`).
  Per-tree point sums and archetype labels are intentionally NOT
  stored — derive sums via `GROUP BY tree`; archetype
  classification stays in agent code.
- **`nodes_catalog`** — table. Mirror of `nodes.csv` augmented
  with a `room_index` column resolved at load time (Session 14
  discovery: `room_index = node_index` for every in-game node).
  Reload: `python -m ingester.nodes_catalog --reload`.
- **`kami_current_location`** — view. Per-kami latest-known room
  derived from the most recent harvest_* action. Joins
  `nodes_catalog` for `room_index`. **Not live truth** — `move`
  actions are account-level on chain (kami_id NULL on the row)
  and excluded today; `is_stale = TRUE` (threshold 30 min) means
  verify against chain via Kamibots before any destructive op
  keyed on location. Cold-start kamis (no harvest in 28d) appear
  with NULL location columns.

```sql
-- Per-kami skill loadout, resolved.
SELECT skill_name, tree, tier, points, effect, value, units
FROM kami_skills
WHERE kami_index = 1186
ORDER BY tree, tier;

-- Current location of an entire roster.
SELECT kami_index, current_room_index, current_node_id,
       source_action_type, freshness_seconds, is_stale
FROM kami_current_location
WHERE kami_index IN (1186, 1745, 2418, 2465);
```

See `memory/decoder-notes.md` "Session 14 — skills_catalog +
nodes_catalog + views" for the discovery write-up (including the
move-attribution gap and node→room Index identity).

## MUSU semantics (read once)

`kami_action.amount` is **gross MUSU pre-tax** — the integer
item-count drained from the harvest entity *before* the on-chain tax
split. Always use gross for kami comparisons; tax varies by node
(0%, 6%, 12%, sometimes higher) and would distort productivity
rankings if folded in. Cast as `CAST(amount AS HUGEINT)` — never
divide by 1e18 (MUSU is an integer item index, not a token). For
operator-side economics, derive net by joining the matching
`harvest_start` row's `metadata.taxAmt`:
`net = gross - gross * taxAmt / 1e4`. Full derivation in
`memory/decoder-notes.md` under "MUSU semantics".

## Running your own instance

```bash
git clone https://github.com/tokedo/kami-oracle
cd kami-oracle
cp env.template .env      # set YOMINET_RPC_URL
pip install -r requirements.txt
bash scripts/vendor-context.sh ../kamigotchi-context    # vendors ABIs
python -m ingester.backfill --days 7                     # one-shot backfill
python -m ingester.poller                                # continuous tail
```

Prerequisites: Python 3.11+, ~10 GB disk (plenty of headroom),
[kamigotchi-context](https://github.com/tokedo/kamigotchi-context)
checked out next to this repo (source of the ABIs).

Public Yominet RPC is free to use.

## Development phases

- ✅ ADR-004 direction accepted.
- 🚧 **Stage 1: ingest & store.**
- ⏭  Stage 2: basic analytical queries.
- ⏭  Stage 3: archetype classification.
- ⏭  Stage 4: MCP server for agent consumption.

## License

MIT. See [LICENSE](LICENSE).

## Related

- [kamigotchi-context](https://github.com/tokedo/kamigotchi-context) —
  open-source Kamigotchi harness (ABIs, system IDs, game mechanics
  docs) that this repo vendors from.
- Kamigotchi World: https://kamigotchi.io

## Contributing

Issues and PRs welcome. This project is maintained primarily by
Claude Code running autonomously on a GCP VM under human review, so
response time on external contributions is best-effort.
