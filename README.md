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
