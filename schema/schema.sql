-- kami-oracle Stage 1 schema (DuckDB)
--
-- Canonical schema definition. Applied via ingester.storage.bootstrap() on
-- first connection, or manually with:
--
--     duckdb db/kami-oracle.duckdb < schema/schema.sql
--
-- Schema version is tracked in ingest_cursor.schema_version. Bump it when
-- columns change and add a migration under migrations/.

-- ---------------------------------------------------------------------------
-- raw_tx: one row per observed transaction to a Kamigotchi system contract.
-- Kept so we can re-decode without re-hitting the RPC if decoder logic
-- changes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_tx (
    tx_hash          VARCHAR      PRIMARY KEY,  -- 0x-prefixed lower-hex
    block_number     BIGINT       NOT NULL,
    block_timestamp  TIMESTAMP    NOT NULL,     -- UTC, from block header
    tx_index         INTEGER      NOT NULL,
    from_addr        VARCHAR      NOT NULL,     -- 0x-prefixed checksum
    to_addr          VARCHAR      NOT NULL,     -- system contract address
    method_sig       VARCHAR      NOT NULL,     -- 4-byte selector, 0x-prefixed
    system_id        VARCHAR,                   -- e.g. "system.harvest.start"
    raw_calldata     BLOB         NOT NULL,
    status           INTEGER      NOT NULL,     -- 1 = success, 0 = reverted
    gas_used         BIGINT,
    gas_price_wei    BIGINT,
    inserted_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_raw_tx_block_number ON raw_tx(block_number);
CREATE INDEX IF NOT EXISTS idx_raw_tx_system_id    ON raw_tx(system_id);

-- ---------------------------------------------------------------------------
-- kami_action: decoded, one row per logical game action derived from a tx.
-- A single tx can produce zero-or-more rows (batched calls fan out to
-- multiple rows). `id` is a deterministic hash so re-decodes are idempotent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kami_action (
    id                VARCHAR      PRIMARY KEY,  -- "{tx_hash}:{sub_index}"
    tx_hash           VARCHAR      NOT NULL,     -- FK -> raw_tx.tx_hash
    sub_index         INTEGER      NOT NULL,     -- 0 for non-batched; 0..N-1 for batches
    block_number      BIGINT       NOT NULL,     -- denormalized for range queries
    block_timestamp   TIMESTAMP    NOT NULL,     -- denormalized
    action_type       VARCHAR      NOT NULL,     -- see action_type taxonomy
    system_id         VARCHAR      NOT NULL,
    from_addr         VARCHAR      NOT NULL,     -- signer (operator usually)
    kami_id           VARCHAR,                   -- uint256 as decimal string (nullable)
    target_kami_id    VARCHAR,                   -- for PvP / trade targets
    node_id           VARCHAR,                   -- harvest node, as decimal string
    -- amount: GROSS MUSU drained from the harvest entity. Integer
    -- item-count (item index 1 = MUSU); do NOT divide by 1e18.
    -- For harvest_collect / harvest_stop / harvest_liquidate this is
    -- the full amount removed from the harvest before the on-chain
    -- tax split. The operator's MUSU inventory receives
    --   amount - (amount * harvest_start.taxAmt / 1e4)
    -- with the remainder credited to the node's taxer entity.
    -- For kami leaderboards / productivity comparisons, ALWAYS use
    -- this column (gross). Tax is a node-config artifact, not a
    -- kami stat — using net would invert rankings for kamis on
    -- higher-tax nodes. For operator-side economics, derive net by
    -- joining to the matching harvest_start row's metadata_json.
    -- See memory/decoder-notes.md "Session 7 — bpeon cross-check"
    -- for the derivation.
    amount            VARCHAR,                   -- uint256 as decimal string (generic)
    item_index        INTEGER,                   -- for item_use / equip
    harvest_id        VARCHAR,                   -- entity id (uint256 decimal) for harvest_*
    metadata_json     VARCHAR,                   -- JSON blob for action-specific args
    status            INTEGER      NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kami_action_kami_ts     ON kami_action(kami_id, block_timestamp);
CREATE INDEX IF NOT EXISTS idx_kami_action_type_ts     ON kami_action(action_type, block_timestamp);
CREATE INDEX IF NOT EXISTS idx_kami_action_from_ts     ON kami_action(from_addr, block_timestamp);
CREATE INDEX IF NOT EXISTS idx_kami_action_block       ON kami_action(block_number);
CREATE INDEX IF NOT EXISTS idx_kami_action_harvest_id  ON kami_action(harvest_id);

-- ---------------------------------------------------------------------------
-- kami_static: per-kami traits. Populated lazily from the GetterSystem
-- when we first see a kami_id; refreshed periodically.
-- Stage 1 treats this as optional — the table exists but population is a
-- best-effort background task.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kami_static (
    kami_id           VARCHAR      PRIMARY KEY,  -- uint256 as decimal string
    kami_index        INTEGER,
    name              VARCHAR,
    owner_address     VARCHAR,
    account_id        VARCHAR,
    -- account_index, account_name: the in-game Account that owns this
    -- kami. account_name is the user-chosen display name (e.g.
    -- "bpeon"); account_index is the small 1..N ordinal. Both come
    -- from GetterSystem.getAccount(accountId). NULL if the account
    -- has not been hydrated yet, or if getAccount returns a default
    -- (anonymous) shape. The owner_address is still the canonical EOA
    -- wallet — account_name is the human label.
    account_index     INTEGER,
    account_name      VARCHAR,
    body              INTEGER,
    hand              INTEGER,
    face              INTEGER,
    background        INTEGER,
    color             INTEGER,
    affinities        VARCHAR,                  -- JSON array
    base_health       INTEGER,
    base_power        INTEGER,
    base_harmony      INTEGER,
    base_violence     INTEGER,
    -- Build snapshot fields (Session 10): the kami's current effective
    -- stats, level, skills, and equipment. Read directly from chain
    -- getters/components — the resolved totals come from the canonical
    -- game formula `floor((1000 + boost) * (base + shift) / 1000)`
    -- applied to the (base, shift, boost, sync) Stat tuple returned by
    -- GetterSystem.getKami / SlotsComponent.safeGet. Not a local
    -- recomputation from first principles — the formula is the one
    -- documented in kamigotchi-context (state-reading.md, health.md)
    -- and used by every other Kamigotchi client. Refreshed by the
    -- kami_static populator on a daily sweep; build_refreshed_ts is
    -- the per-kami refresh timestamp (distinct from last_refreshed_ts,
    -- which covers traits + account fields). NULL during initial
    -- backfill, populated for >=95% of active kamis post-Session 10.
    -- See memory/decoder-notes.md "Session 10 — build fields on chain"
    -- for per-field on-chain source, the bpeon fixture cross-check,
    -- and resolved component addresses.
    --
    -- total_slots stores the slots-stat resolved scalar; the in-game
    -- equipment capacity is `1 + total_slots` (per equipment.md, base
    -- capacity is implicit 1). Stored without the +1 to keep the
    -- column shape identical to total_health/total_power/etc.
    --
    -- skills_json: JSON array `[{"index": int, "points": int}, ...]`.
    -- Skill catalog (which index = which named skill) lives in
    -- kamigotchi-context/catalogs/skills.csv.
    --
    -- equipment_json: JSON array `[item_index, ...]`. Slot-name
    -- resolution deferred — component.for.string does not resolve in
    -- the current registry snapshot; capacity is 1 today so the loss
    -- is small.
    level             INTEGER,
    xp                BIGINT,
    total_health      INTEGER,
    total_power       INTEGER,
    total_violence    INTEGER,
    total_harmony     INTEGER,
    total_slots       INTEGER,
    skills_json       VARCHAR,                  -- JSON array of {index, points}
    equipment_json    VARCHAR,                  -- JSON array of item_index
    build_refreshed_ts TIMESTAMP,
    -- Skill-effect modifiers (Session 11): the 12 non-stat skill effect
    -- types from kami_context/systems/leveling.md. Resolved totals as the
    -- game uses them, summed across skills + equipment + passives. Stored
    -- at the same precision as on chain — percent values are ×1000 (e.g.
    -- strain_boost = -200 means -20%); CS is seconds; HIB is Musu/hr.
    -- Refreshed by the kami_static populator on the daily sweep alongside
    -- the Session 10 build columns; same build_refreshed_ts marks both.
    -- The four stat-shift effects (SHS/SPS/SVS/SYS) are NOT new columns —
    -- they're already folded into total_health/power/violence/harmony via
    -- getKami(id).stats. See memory/decoder-notes.md "Session 11 —
    -- skill-effect modifiers on chain" for the catalog-walk derivation
    -- and Zephyr round-trip.
    strain_boost              INTEGER,         -- SB,  ×1000 (negative = less strain)
    harvest_fertility_boost   INTEGER,         -- HFB, ×1000
    harvest_intensity_boost   INTEGER,         -- HIB, Musu/hr (no ×1000)
    harvest_bounty_boost      INTEGER,         -- HBB, ×1000
    rest_recovery_boost       INTEGER,         -- RMB, ×1000
    cooldown_shift            INTEGER,         -- CS,  seconds (signed; no ×1000)
    attack_threshold_shift    INTEGER,         -- ATS, ×1000
    attack_threshold_ratio    INTEGER,         -- ATR, ×1000
    attack_spoils_ratio       INTEGER,         -- ASR, ×1000
    defense_threshold_shift   INTEGER,         -- DTS, ×1000
    defense_threshold_ratio   INTEGER,         -- DTR, ×1000
    defense_salvage_ratio     INTEGER,         -- DSR, ×1000
    -- Affinity (Session 12): each kami has a body affinity and a hand
    -- affinity drawn from {EERIE, NORMAL, SCRAP, INSECT}. Read from
    -- getKami(kamiId).affinities (string[2]) — already fetched by the
    -- Session 10 build populator, so this is a free extraction. The
    -- existing integer body/hand columns are the trait indices; the new
    -- affinity columns are the on-chain affinity strings, stored in
    -- whatever case the chain returns (do not normalize).
    body_affinity     VARCHAR,
    hand_affinity     VARCHAR,
    first_seen_ts     TIMESTAMP    NOT NULL,
    last_refreshed_ts TIMESTAMP    NOT NULL
);

-- ---------------------------------------------------------------------------
-- ingest_cursor: ops state. Single row keyed by `id = 1`.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_cursor (
    id                   INTEGER    PRIMARY KEY,
    last_block_scanned   BIGINT     NOT NULL,  -- inclusive; next scan starts at +1
    last_block_timestamp TIMESTAMP,
    schema_version       INTEGER    NOT NULL,
    vendor_sha           VARCHAR,              -- kami_context UPSTREAM_SHA
    updated_at           TIMESTAMP  NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- system_address_snapshot: every (system_id, address) pair the ingester has
-- ever observed, with the earliest and latest probe blocks at which the
-- address resolved to that system_id.
--
-- Kamigotchi periodically redeploys system contracts (see decoder-notes
-- "action-mix divergence" investigation, session 2.5). Resolving the
-- registry once at head misses historical deployments, so any tx in the
-- backfill window that targeted a previous deployment gets silently
-- dropped at the match step. The ingester now probes the registry at
-- multiple block heights across the target window and unions the
-- resulting system-ID -> address sets into this table; every address
-- observed at any probe becomes a candidate in the match step, while
-- the decoder still dispatches on system_id (same ABI across
-- deployments).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_address_snapshot (
    system_id         VARCHAR   NOT NULL,   -- e.g. "system.harvest.start"
    address           VARCHAR   NOT NULL,   -- checksummed, 0x-prefixed
    abi_name          VARCHAR   NOT NULL,   -- filename under kami_context/abi/
    first_seen_block  BIGINT,               -- earliest probe block the pair was observed
    last_seen_block   BIGINT,               -- latest probe block the pair was observed
    ingested_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (system_id, address)
);

CREATE INDEX IF NOT EXISTS idx_system_snapshot_addr ON system_address_snapshot(address);

-- ---------------------------------------------------------------------------
-- items_catalog (Session 13): static catalog mirrored from
-- kami_context/catalogs/items.csv. Re-loaded only when kami_context is
-- re-vendored (or on service startup if the table is empty), not on
-- every poll. Used by the kami_equipment view to resolve slot labels
-- without a chain registry call (the chain doesn't return slot identity
-- per equipped item; the catalog's "For" column is the source of
-- truth). slot_type is non-NULL only when the catalog "For" cell
-- matches a slot kind (today: Kami_Pet_Slot, Passport_slot; rule
-- generalizes to any *_[Ss]lot value). slot_type NULL means the item
-- isn't slot-equippable (consumables, currency, target-scoped items).
-- Loader: ingester/items_catalog.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items_catalog (
    item_index   INTEGER  PRIMARY KEY,
    name         VARCHAR  NOT NULL,
    type         VARCHAR,
    rarity       VARCHAR,
    slot_type    VARCHAR,        -- chain "For" value when slot-equippable; NULL otherwise
    effect       VARCHAR,        -- raw "Effects" cell (e.g. "E_POWER+3")
    description  VARCHAR,
    loaded_ts    TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------------------------
-- kami_equipment (Session 13): one row per equipped item per kami.
-- equipment_json is UNNEST'd (DuckDB casts the VARCHAR JSON array
-- directly to INTEGER[]) and LEFT JOIN'd against items_catalog to
-- resolve slot_type, item_name, item_effect. freshness_seconds and
-- is_stale are derived from kami_static.build_refreshed_ts (snapshot
-- age, not on-chain truth) — not columns on kami_static. Threshold is
-- 36h (129600s); the populator sweeps daily, so 36h gives the agent
-- one missed-sweep slack before the row is flagged. is_stale = TRUE
-- means the agent should verify with live chain state via Kamibots
-- before destructive ops (unequip, transfer, liquidate). Snapshot lag
-- means equipment can false-positive: an unequipped pet stays in the
-- JSON until next sweep. Live-truth verification is on Kamibots, not
-- the oracle — see kami-agent/integration/oracle.md.
--
-- View definition lives in migrations/008_add_kami_equipment_view.py;
-- canonical shape:
--   SELECT s.kami_id, s.kami_index, s.name, s.account_name,
--          i.slot_type, je.value AS item_index,
--          i.name AS item_name, i.effect AS item_effect,
--          s.build_refreshed_ts,
--          CAST(EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) AS INTEGER)
--              AS freshness_seconds,
--          EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) > 129600
--              AS is_stale
--   FROM kami_static s,
--        UNNEST(CAST(s.equipment_json AS INTEGER[])) AS je(value)
--   LEFT JOIN items_catalog i ON i.item_index = je.value
--   WHERE s.equipment_json IS NOT NULL AND s.equipment_json != '[]';
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- skills_catalog (Session 14): static catalog mirrored from
-- kami_context/catalogs/skills.csv. Re-loaded only when kami_context is
-- re-vendored (or on service startup if the table is empty), not on
-- every poll. Used by the kami_skills view to resolve per-skill effect /
-- tree / tier details so the agent doesn't re-derive from skills.csv
-- inline. The first CSV column is a leading blank cell (UTF-8 BOM +
-- dot) — the loader skips it via DictReader keyed on named columns.
-- value is VARCHAR (not numeric) because skill effects mix integer
-- counts, signed percent values (e.g. "0.02"), and decimals — keeping
-- the chain string verbatim avoids parse loss on edge cases.
-- exclusion stores the raw CSV cell (e.g. "132, 133") for
-- mutually-exclusive sibling skill_index lists; enforcement lives
-- in chain logic.
-- Loader: ingester/skills_catalog.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skills_catalog (
    skill_index  INTEGER PRIMARY KEY,
    name         VARCHAR NOT NULL,
    tree         VARCHAR NOT NULL,   -- Predator | Guardian | Harvester | Enlightened
    tier         INTEGER,
    tree_req     INTEGER,            -- prereq points in tree
    max_rank     INTEGER,
    cost         INTEGER,            -- skill point cost per rank
    effect       VARCHAR,            -- effect key: SHS / HFB / SB / ...
    value        VARCHAR,            -- per-rank value (string — signed/decimal possible)
    units        VARCHAR,            -- Stat | Percent | Sec | Musu/hr | ...
    exclusion    VARCHAR,            -- mutually-exclusive sibling skill_index list, if any
    description  VARCHAR,
    loaded_ts    TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------------------------
-- kami_skills (Session 14): one row per (kami, invested skill) per
-- skills_json entry. skills_json is UNNEST'd (DuckDB casts the
-- VARCHAR JSON array directly to STRUCT("index" INTEGER, "points"
-- INTEGER)[]) and LEFT JOIN'd against skills_catalog to resolve
-- skill_name, tree, tier, effect, value, units. freshness_seconds
-- and is_stale derive from kami_static.build_refreshed_ts (snapshot
-- age, not on-chain truth) — not columns on kami_static. Threshold
-- is 36h (129600s); same as kami_equipment, populator sweeps daily.
-- Builds change rarely so the stale flag here is mostly defensive,
-- but the same verify-before-act discipline applies.
--
-- Per-tree point sums and archetype labels are intentionally NOT
-- stored: derive per-tree totals via
--   SELECT tree, SUM(points) FROM kami_skills WHERE kami_id = X GROUP BY tree;
-- archetype classification stays in agent code where the heuristic
-- is visible. Oracle exposes the components, not the label.
--
-- View definition lives in migrations/010_add_kami_skills_view.py;
-- canonical shape:
--   SELECT s.kami_id, s.kami_index, s.name, s.account_name,
--          je."index"  AS skill_index,
--          c.name      AS skill_name,
--          c.tree, c.tier,
--          je."points" AS points,
--          c.effect, c.value, c.units,
--          s.build_refreshed_ts,
--          CAST(EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) AS INTEGER)
--              AS freshness_seconds,
--          EXTRACT(EPOCH FROM (now() - s.build_refreshed_ts)) > 129600
--              AS is_stale
--   FROM kami_static s,
--        UNNEST(CAST(s.skills_json AS STRUCT("index" INTEGER, "points" INTEGER)[])) AS t(je)
--   LEFT JOIN skills_catalog c ON c.skill_index = je."index"
--   WHERE s.skills_json IS NOT NULL AND s.skills_json != '[]';
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- nodes_catalog (Session 14): static catalog mirrored from
-- kami_context/catalogs/nodes.csv, augmented with room_index resolved
-- at load time. Re-loaded only when kami_context is re-vendored (or
-- on service startup if the table is empty), not on every poll. Used
-- by the kami_current_location view to map an observed
-- kami_action.node_id (set by harvest_* actions) to a room. The CSV
-- itself doesn't carry a room column — Session 14 Part 1b discovery
-- found that every node in nodes.csv has a same-Index, same-Name row
-- in rooms.csv (zero mismatches across all 64 in-game nodes), so
-- room_index = node_index for every in-game node. The loader stores
-- this directly. If upstream ever introduces a node whose room
-- differs, add a verifier in the loader against rooms.csv.
-- Loader: ingester/nodes_catalog.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nodes_catalog (
    node_index   INTEGER PRIMARY KEY,
    name         VARCHAR NOT NULL,
    status       VARCHAR,            -- 'In Game' or other (catalog-only / removed)
    drops        VARCHAR,
    affinity     VARCHAR,            -- Eerie | Normal | Scrap | Insect (or comma-list)
    level_limit  INTEGER,
    yield_index  INTEGER,
    scav_cost    INTEGER,
    room_index   INTEGER,            -- resolved at load (Session 14: room_index = node_index)
    loaded_ts    TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------------------------
-- kami_current_location (Session 14): per-kami latest-known room
-- derived from the most recent harvest_start action and resolved
-- through nodes_catalog. Among the harvest action family, only
-- harvest_start carries node_id — harvest_stop / collect /
-- liquidate decode only harvest_id (the chain call references the
-- harvest entity, not the node), so on those rows node_id is NULL.
-- The semantic is honest: "latest node this kami was sent to
-- harvest." Kamis remain on their last-harvested node until an
-- untracked move, so this is the correct current-location signal
-- in the absence of move attribution. Cold-start kamis (no
-- harvest_start in the 28d window) appear with NULL location
-- columns.
--
-- move actions are NOT included. system.account.move is account-
-- level on chain — kami_action.move rows have kami_id NULL because
-- the chain call has no per-kami binding (the move applies to all
-- kamis under that account). Attributing to specific kamis
-- requires snapshot-time account-membership resolution and is a
-- future decoder concern.
--
-- is_stale threshold is 1800s (30 min). Kamis park on nodes for
-- hours; 30 min of fresh trust suffices for read-only ops; longer
-- means the agent should verify the kami's actual room against
-- chain via Kamibots before any destructive op keyed on location.
-- NOT live truth: an account-level move can shift a kami to a new
-- room without us attributing it to this specific kami.
--
-- View definition lives in
-- migrations/012_add_kami_current_location_view.py; canonical shape:
--   WITH last_loc_action AS (
--     SELECT a.kami_id, a.action_type, a.node_id, a.metadata_json,
--            a.block_timestamp,
--            ROW_NUMBER() OVER (PARTITION BY a.kami_id
--                               ORDER BY a.block_timestamp DESC) AS rn
--     FROM kami_action a
--     WHERE a.kami_id IS NOT NULL
--       AND a.action_type = 'harvest_start'
--       AND a.node_id IS NOT NULL
--   )
--   SELECT s.kami_id, s.kami_index, s.name, s.account_name,
--          n.room_index AS current_room_index,
--          CAST(la.node_id AS INTEGER) AS current_node_id,
--          la.action_type AS source_action_type,
--          la.block_timestamp AS since_ts,
--          CAST(EXTRACT(EPOCH FROM (now() - la.block_timestamp)) AS INTEGER)
--              AS freshness_seconds,
--          EXTRACT(EPOCH FROM (now() - la.block_timestamp)) > 1800
--              AS is_stale
--   FROM kami_static s
--   LEFT JOIN last_loc_action la ON la.kami_id = s.kami_id AND la.rn = 1
--   LEFT JOIN nodes_catalog n ON n.node_index = CAST(la.node_id AS INTEGER);
-- ---------------------------------------------------------------------------
