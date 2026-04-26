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
