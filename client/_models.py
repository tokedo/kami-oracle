"""Typed response dataclasses for the kami-oracle client.

Stdlib only. ``uint256`` values (kami_id, amount, harvest_id) stay as
strings because they routinely overflow JS Number / int64; rely on
``int(s)`` or ``CAST(... AS HUGEINT)`` server-side when arithmetic is
needed.

Naming convention: every MUSU-bearing field is named ``musu_gross``,
even though the wire protocol still calls it ``amount``. The wire
protocol stays as-is to avoid breaking the Colab notebook; the
client is the layer that enforces the gross/net naming clarity.

Gross MUSU = the integer item-count drained from the harvest entity
before the on-chain tax split. For kami productivity rankings, always
use gross — see the package docstring or the repo README's "MUSU
semantics" section for the net derivation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthStatus:
    """Output of ``GET /health``. Service liveness + cursor + counts."""

    status: str
    last_block_scanned: int
    last_block_timestamp: str | None
    schema_version: int
    vendor_sha: str | None
    chain_head_lag_seconds: float | None
    total_actions: int
    total_raw_tx: int
    total_kami_static: int
    n_systems: int
    n_addresses: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class KamiAction:
    """One decoded action row.

    ``amount`` is **gross MUSU pre-tax** for harvest_collect /
    harvest_stop / harvest_liquidate (when populated). For kami
    productivity rankings use this (gross). NULL means the on-chain
    action transferred no MUSU — typically a no-op via
    executeBatchedAllowFailure against an already-stopped harvest.
    """

    id: str | None
    tx_hash: str | None
    sub_index: int
    block_number: int
    block_timestamp: str | None
    action_type: str | None
    system_id: str | None
    from_addr: str | None
    kami_id: str | None
    target_kami_id: str | None
    node_id: str | None
    amount: str | None  # gross MUSU pre-tax (when set); see class docstring
    item_index: int | None
    metadata: dict[str, Any]
    status: int


@dataclass
class ActionTypeCount:
    """One bucket of an action_type histogram."""

    action_type: str
    count: int
    pct: float | None = None


@dataclass
class KamiSummary:
    """Per-action-type counts for a single kami over a window."""

    kami_id: str
    since_days: int
    total_actions: int
    first_seen: str | None
    last_seen: str | None
    by_type: list[ActionTypeCount]


@dataclass
class OperatorSummary:
    """Per-action-type counts for a single operator wallet over a window.

    Operator-side economics is where *net* MUSU matters. This summary
    returns counts only; for net MUSU credited to the operator, query
    /sql joining ``harvest_start.metadata.taxAmt``.
    """

    operator: str
    since_days: int
    total_actions: int
    distinct_kami: int
    first_seen: str | None
    last_seen: str | None
    by_type: list[ActionTypeCount]


@dataclass
class NodeStat:
    """One row of /nodes/top — node ranked by harvest_start count."""

    node_id: str
    harvest_starts: int


@dataclass
class RegistrySnapshot:
    """Snapshot of (system_id, address) pairs the ingester has observed."""

    n_systems: int
    n_addresses: int
    by_system: dict[str, list[dict[str, Any]]]


@dataclass
class HarvestLeaderRow:
    """One row of the harvest leaderboard.

    ``musu_gross`` is the sum of gross MUSU pre-tax drained across the
    window. Use this for productivity ranking; tax is a node-config
    artifact and would distort rankings if folded in (see package
    docstring).
    """

    kami_id: str | None
    name: str | None
    owner_address: str | None
    collects: int
    stops: int
    musu_gross: int


@dataclass
class SqlResult:
    """Output of ``POST /sql``."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    latency_ms: int
