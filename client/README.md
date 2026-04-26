# kami-oracle client

Stable Python consumer surface for the kami-oracle public query plane.
Vendored into kami-zero, Colab notebooks, and any future agent that
needs to read what the oracle sees.

## Install

This package is vendored, not published to PyPI. Either:

1. Run scripts from inside the `kami-oracle` repo (the package is
   importable as `client`).
2. Or vendor it into another repo with
   `bash scripts/vendor-client.sh /path/to/target-repo` — the script
   copies `client/` to `<target>/kami_oracle_client/`.

Only dependency: `requests`.

## Quick start

```python
import os
from client import OracleClient

oc = OracleClient(
    base_url="https://136-112-224-147.sslip.io",
    token=os.environ["KAMI_ORACLE_TOKEN"],
)

# Service liveness — no token needed.
h = oc.health()
print(h.total_actions, "actions, lag", h.chain_head_lag_seconds, "s")

# Founder's golden query: kamis ranked by gross MUSU harvested.
for row in oc.harvest_leaderboard(since_days=7, limit=10):
    print(f"{row.musu_gross:>10}  {row.name or row.kami_id}")

# Raw SQL escape hatch for ad-hoc work.
result = oc.sql("SELECT action_type, COUNT(*) FROM kami_action GROUP BY 1")
for action_type, n in result.rows:
    print(action_type, n)
```

## API

| Method | Returns | Notes |
|---|---|---|
| `health()` | `HealthStatus` | Unauthenticated. |
| `kami_actions(kami_id, since_days=7, limit=100)` | `list[KamiAction]` | Recent actions for one kami. |
| `kami_summary(kami_id, since_days=7)` | `KamiSummary` | Per-type counts. |
| `operator_summary(addr, since_days=7)` | `OperatorSummary` | Per-type counts for an operator wallet. |
| `action_types(since_days=7)` | `list[ActionTypeCount]` | Global histogram. |
| `nodes_top(since_days=7, limit=20)` | `list[NodeStat]` | Top harvest nodes. |
| `actions_recent(limit=100)` | `list[KamiAction]` | Most-recent stream. |
| `registry_snapshot()` | `RegistrySnapshot` | (system_id, address) snapshot. |
| `sql(q, limit=1000)` | `SqlResult` | Raw read-only SQL. |
| `harvest_leaderboard(since_days=7, limit=20)` | `list[HarvestLeaderRow]` | Founder's golden query. |

### MUSU semantics — read once

Every MUSU-bearing field returned by this client is named
`musu_gross`. Gross MUSU is the integer item-count drained from the
harvest entity *before* the on-chain tax split.

For **kami productivity** (leaderboards, comparisons), always use
gross — tax is a node-config artifact, not a kami stat. A medium kami
on a 0%-tax node and a strong kami on a 12%-tax node would invert in
*net* rankings even though productivity is identical.

For **operator economics** (how much MUSU landed in your inventory),
derive net by joining the matching `harvest_start` row's
`metadata.taxAmt`:

```
net = gross - gross * taxAmt / 1e4
```

(`taxAmt` is in basis points: `600` = 6%.) See the repo README's
"MUSU semantics" section for the full derivation.

MUSU is an integer item count — **never divide by 1e18**. Cast as
`CAST(amount AS HUGEINT)` in raw SQL.

### Pagination & windows

- `limit` defaults to a per-method sane value (100 for actions, 20 for
  leaderboards), maxes out at 1000–2000 depending on the route.
- `since_days` is the canonical time-window knob (1–28). `start_ts` /
  `end_ts` aren't exposed in v1; keep the API narrow.
- For stream-shaped endpoints, monotonic `block_number` on the
  returned rows lets callers iterate forward without cursor tokens.

### Errors

- 401 → `OracleAuthError`
- other 4xx/5xx → `OracleHTTPError` (`.status_code` distinguishes)
- network errors → `requests.RequestException` (bubbled unchanged)

No retries in v1. Wrap if you need them.

### Timeouts

Defaults: 15s connect, 60s read. Override per-call with `timeout=...`.
