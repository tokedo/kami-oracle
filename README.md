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
ABIs, and maintains a rolling 4-week window of all kami activity in a
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
db/kami-oracle.duckdb   (rolling 4 weeks)
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
- **Rolling window, not full history.** 4 weeks keeps the DB bounded
  and signal fresh to the current meta.

## Running your own instance

```bash
git clone https://github.com/tokedo/kami-oracle
cd kami-oracle
cp env.template .env      # set YOMINET_RPC_URL
pip install -r requirements.txt
bash scripts/vendor-context.sh ../kamigotchi-context    # vendors ABIs
python -m ingester.backfill --weeks 4                    # one-shot backfill
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
