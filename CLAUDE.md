# CLAUDE.md — Operating Instructions for kami-oracle

> This file is read automatically by Claude Code at the start of every
> session. It tells you, the autonomous agent maintaining this repo,
> how to operate.

## What kami-oracle is

kami-oracle observes every on-chain action taken by every kami on
Yominet over a rolling window (Stage 1: 1 week, may extend to 28 days
later), decodes them against the Kamigotchi System ABIs, and stores
them in a DuckDB database for downstream analysis.

Purpose: surface **collective behavior** so agents don't have to
re-derive strategy from first principles. Top players have already
encoded months of optimization in their on-chain tx history — this
service makes that queryable.

## Current phase: Stage 1 + Phase D public query plane (authorized 2026-04-24 via session 5 prompt) — Ingest & store + public HTTPS

**In scope:**
- Continuous tail of Yominet, tx-level decoding against vendored ABIs.
- DuckDB file maintained at `db/kami-oracle.duckdb`.
- Four tables: `raw_tx`, `kami_action`, `kami_static`, `ingest_cursor`.
- Rolling 1-week window (Stage 1 investigation phase; retention-edge
  avoidance and clean sample before we commit to filtering decisions —
  see `memory/decoder-notes.md`), older rows pruned on a schedule.
  Window may extend to 28 days later.
- Idempotent: restart-safe, no duplicates.
- Validated against the founder's own accounts (caw, buzz, bpeon).

**Out of scope for Stage 1 — do NOT build any of these yet:**
- MCP server or any external query endpoint.
- Archetype classification.
- Derived metrics tables / rollups.
- ML / clustering / prediction.
- Any speculative query tools "for future agents."
- Any code that writes an on-chain tx. This is read-only.

## How to orient yourself at the start of a session

1. Read this file fully.
2. Read `README.md`.
3. Check `memory/next-steps.md` for what this session should pick up.
4. Check `memory/improvements.md` and `memory/decoder-notes.md` for
   what past sessions learned.
5. Check `memory/questions-for-human.md` — if it's non-empty, STOP
   and wait for human input rather than guessing.
6. Skim `kami_context/system-ids.md` + `kami_context/chain.md` if
   you're touching decoding logic.

## Schema

Canonical definitions live in `schema/schema.sql`. Summary:

- **`raw_tx`**: one row per tx that touches the Kamigotchi World
  contract. Includes `tx_hash` (PK), `block_number`, `timestamp`,
  `from_addr`, `to_addr`, `method_sig`, `raw_calldata`, `status`,
  `gas_used`, `gas_price_wei`.
- **`kami_action`**: decoded, one row per logical game action.
  Includes `id` (PK), `tx_hash` (FK), `kami_id`, `action_type`
  (enum), `timestamp`, `node_id`, `target_kami_id`, `amount`,
  `metadata_json`. `action_type` initial enum: `harvest_start`,
  `harvest_stop`, `harvest_collect`, `harvest_liquidate`, `feed`,
  `rest_start`, `rest_stop`, `move`, `lvlup`, `skill_upgrade`,
  `skill_respec`, `equip`, `unequip`, `revive`, `die`, `quest_accept`,
  `quest_complete`, `quest_drop`, `trade_create`, `trade_execute`,
  `trade_complete`, `trade_cancel`, `item_use`, `item_craft`.
- **`kami_static`**: per-kami traits + operator + build snapshot.
  `kami_id` (PK), `owner_address`, `account_index`, `account_name`
  (in-game operator label, Session 9), `body`, `hand`, `face`,
  `background`, `color`, `affinities`, `base_*` stats,
  `first_seen_ts`, `last_refreshed_ts`. Session 10 build columns:
  `level`, `xp`, `total_health`, `total_power`, `total_violence`,
  `total_harmony`, `total_slots` (effective scalars resolved via the
  canonical game formula `floor((1000+boost)*(base+shift)/1000)`),
  `skills_json` (`[{index, points}, ...]`), `equipment_json`
  (`[item_index, ...]`), `build_refreshed_ts`. Build fields read
  via `getKami` + `world.components()` getters (slots / skills /
  equipment); refreshed daily on the same sweep, latest snapshot
  only. In-game equipment capacity is `1 + total_slots`.
  Modifiers (Session 11) — 12 INTEGER columns, catalog-derived sum
  over skills × equipment, percent values ×1000, refreshed alongside
  build columns on the same `build_refreshed_ts`. SHS/SPS/SVS/SYS NOT
  re-emitted (already in `total_*`).
  - `strain_boost` — `SB`, ×1000 (negative = less strain)
  - `harvest_fertility_boost` — `HFB`, ×1000
  - `harvest_intensity_boost` — `HIB`, Musu/hr (no ×1000)
  - `harvest_bounty_boost` — `HBB`, ×1000
  - `rest_recovery_boost` — `RMB`, ×1000
  - `cooldown_shift` — `CS`, signed seconds (no ×1000)
  - `attack_threshold_shift` — `ATS`, ×1000
  - `attack_threshold_ratio` — `ATR`, ×1000
  - `attack_spoils_ratio` — `ASR`, ×1000
  - `defense_threshold_shift` — `DTS`, ×1000
  - `defense_threshold_ratio` — `DTR`, ×1000
  - `defense_salvage_ratio` — `DSR`, ×1000
- **`ingest_cursor`**: ops state. Last committed block, vendor
  version, schema version.

Extend the `action_type` enum as you discover new system calls. Never
silently drop an unknown tx — log it to `memory/unknown-systems.md`
for human review.

## Harness-as-raw-clay

The oracle evolves its own code. When you find:
- A decode bug → fix it in place.
- A missing `action_type` → add to the enum + decoder.
- A schema gap → add a migration in `migrations/` and bump the
  schema version.
- An ingest inefficiency → fix it.

Commit decoder / schema / ingest self-improvements with a `harness:`
prefix. Record notable changes in `memory/improvements.md` as a
one-liner with the commit hash.

## ABI overlay policy (3-tier)

The vendored `kami_context/abi/*.json` set can drift behind the live chain
(upstream publishes, we lag). When you find a selector missing from the
JSON ABI, apply this policy — do not ask each time.

- **Tier A — auto-extend.** If a selector is missing from the vendored
  JSON ABI *and* its signature is explicitly documented in
  `kami_context/system-ids.md`, add it to `SYSTEM_ABI_OVERLAY` without
  asking. Every overlay entry MUST carry a one-line comment citing the
  source (a `system-ids.md` section, or "deployed bytecode + docs" when
  both back the signature).
- **Tier B — flag.** If a selector's signature is inferred only from
  calldata shape (length, plausible decode), or if it *contradicts* the
  documented signature, do not add it. Log to
  `memory/unknown-systems.md` under "## Open" and raise in
  `memory/questions-for-human.md` with sample tx hashes.
- **Tier C — never invent.** Never add an overlay entry without either
  doc backing *or* deployed-bytecode confirmation of the selector.

Overlay entries are additive — never override a vendored JSON signature.
If a JSON signature is wrong (not just missing), re-vendor
`kami_context/` via `scripts/vendor-context.sh` instead of overlay-shadowing.

## Validation discipline

Before trusting a decoded action type:
1. Show 2–3 example tx hashes + the decoded row.
2. Check against a known account's activity. The founder's bpeon
   accounts on kami-zero are safe reference — they harvest node 47
   under Kamibots `auto_v2`, so we expect many `harvest_start` /
   `harvest_collect` / `harvest_stop` actions.
3. If decode yields a surprising result (one kami with 10k txs in an
   hour, negative musu amounts, tx counts that don't match chain
   reality), STOP and investigate before committing. Record findings
   in `memory/decoder-notes.md`.

## Guardrails — never do

- **Never sign or send an on-chain tx.** This is a read-only service.
- **Never commit secrets** (RPC API keys, private keys, tokens) to
  git. Everything via `.env` (gitignored). Only `env.template` is
  tracked.
- **Never add user tracking / telemetry / phone-home.** This is an
  open-source public-good service.
- **Public HTTPS endpoint is authorized** (Phase D transition,
  2026-04-24). The API listens on `127.0.0.1:8787` behind Caddy on
  :443 at `<ORACLE_HOST>` (currently `136-112-224-147.sslip.io`).
  Bearer-token auth on all non-`/health` routes. Rate limited per
  token (default 60/min). Never bind FastAPI directly to a public
  interface — always keep Caddy in the path. Never accept
  unauthenticated writes of any kind (the API is read-only; `/sql`
  rejects non-SELECT, `/backup` is loopback-only). Never log the
  full token in any file or log line.
- **Never extend the rolling window** (currently 1 week; cap 28 days)
  without human approval in `memory/next-steps.md`.
- **Never guess at an ABI.** If a system selector has no matching
  ABI, log it to `memory/unknown-systems.md` and skip the tx — don't
  mis-decode.
- **Never trust `git push --force`** or destructive git operations
  without explicit human instruction.

## Tech stack

- **Language**: Python 3.11+.
- **Chain**: web3.py, reading public Yominet RPC.
- **DB**: DuckDB, file-backed at `db/kami-oracle.duckdb`.
- **Testing**: pytest.
- **Config**: `.env` (gitignored). `env.template` tracked.
- **Dependency management**: pip + `requirements.txt`.

## Repo layout

- `ingester/` — chain tail, decoder, upsert, backfill, prune.
- `schema/schema.sql` — DuckDB schema (canonical).
- `migrations/` — schema migration scripts.
- `kami_context/` — vendored from kamigotchi-context (ABIs + system
  IDs + chain doc). Re-vendored via `scripts/vendor-context.sh`.
- `scripts/` — ops scripts (vendor-context, backfill, prune, etc.).
- `db/` — DuckDB file (gitignored).
- `memory/` — your self-improvement log + plan-for-next-session.
- `tests/` — pytest.

## Session protocol

1. Read CLAUDE.md, `memory/next-steps.md`, `memory/improvements.md`,
   `memory/questions-for-human.md`.
2. If `questions-for-human.md` is non-empty, STOP. Do not proceed
   until a human has addressed it.
3. Run `git status` / `git log -5` to orient on recent state.
4. Do the work in the next-steps plan. Commit early and often.
5. At end of session: update `memory/next-steps.md` with a clear
   plan for the following session. Update `memory/improvements.md`
   if anything notable was learned. Push to origin.

## Open-source posture

This repo is MIT licensed. Anyone can clone, run, and modify their
own oracle instance. Treat every file you write as code strangers
may read.

The specific VM running this service (the one you're in) is not a
public endpoint. No MCP server, no HTTP API, no exposed ports — yet.
That decision happens in Phase D.

## Commits & push

- `harness:` prefix for decode / ingest / schema self-improvement.
- Plain prefixes (`feat:`, `fix:`, `docs:`, `chore:`) for everything
  else.
- Commit early and often. Push to origin after each meaningful chunk.
- Never skip hooks, never force-push, never amend a pushed commit
  unless the human asks.
