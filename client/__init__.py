"""kami-oracle Python client.

Stable consumer surface for the kami-oracle public query plane. Wraps
the eight REST routes plus the ``/sql`` escape hatch and the founder's
golden ``harvest_leaderboard`` query in a single ``OracleClient`` class.

Stdlib + ``requests`` only — no Pydantic, no httpx, no async — so the
package vendors cleanly into kami-zero (and Colab) without dragging
extra dependencies along.

Quick start::

    from client import OracleClient
    import os

    oc = OracleClient(
        base_url="https://136-112-224-147.sslip.io",
        token=os.environ["KAMI_ORACLE_TOKEN"],
    )

    rows = oc.sql("SELECT count(*) FROM kami_action").rows
    print(oc.health().total_actions)
    for row in oc.harvest_leaderboard(since_days=7, limit=10):
        print(row.musu_gross, row.kami_id)

MUSU semantics — read once
--------------------------
Every MUSU-bearing field is named ``musu_gross`` to make the
distinction explicit. ``musu_gross`` is the **gross** MUSU drained from
the harvest entity, before the on-chain tax split. For kami-productivity
rankings (leaderboards, comparisons), always use gross — tax is a
node-config artifact, not a kami stat. For operator-side economics
(net-of-tax), join the matching ``harvest_start`` row's
``metadata.taxAmt``: ``net = gross - gross * taxAmt / 1e4``. See the
repo README "MUSU semantics" section for the full derivation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ._http import HttpSession
from ._models import (
    ActionTypeCount,
    HarvestLeaderRow,
    HealthStatus,
    KamiAction,
    KamiSummary,
    NodeStat,
    OperatorSummary,
    RegistrySnapshot,
    SqlResult,
)
from .exceptions import OracleAuthError, OracleError, OracleHTTPError

__all__ = [
    "OracleClient",
    "OracleError",
    "OracleAuthError",
    "OracleHTTPError",
    "HealthStatus",
    "KamiAction",
    "KamiSummary",
    "OperatorSummary",
    "ActionTypeCount",
    "NodeStat",
    "RegistrySnapshot",
    "HarvestLeaderRow",
    "SqlResult",
]


_DEFAULT_CONNECT_TIMEOUT = 15.0
_DEFAULT_READ_TIMEOUT = 60.0


def _ts(s: str | None):
    """Return the timestamp string unchanged.

    Kept as a hook in case a future version wants to parse to
    datetime; for now we hand back the raw ISO string the API emits,
    which is what every current consumer (Colab notebooks, kami-zero)
    expects.
    """
    return s


class OracleClient:
    """Read-only client for the kami-oracle public query plane.

    All MUSU-bearing fields returned by this client are named
    ``musu_gross``: gross MUSU pre-tax, the right metric for kami
    productivity rankings. See the package docstring for net derivation.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._http = HttpSession(
            base_url=base_url.rstrip("/"),
            token=token,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    # ---- raw escape hatch -------------------------------------------------

    def sql(
        self,
        q: str,
        *,
        limit: int = 1000,
        timeout: float | None = None,
    ) -> SqlResult:
        """Run a read-only SQL query against the oracle.

        Same shape as the Colab notebook's ``sql()`` helper — returns
        columns + rows + truncation flag. The server enforces SELECT-only
        and a row cap; this method does not retry.

        ``limit`` maps to the server's row cap (max 10_000).

        Note on MUSU: ``kami_action.amount`` is **gross** MUSU pre-tax.
        Cast as ``CAST(amount AS HUGEINT)`` — never divide by 1e18.
        """
        body = {"q": q, "limit": limit}
        data = self._http.post("/sql", json_body=body, timeout=timeout)
        return SqlResult(
            columns=list(data["columns"]),
            rows=[list(r) for r in data["rows"]],
            row_count=int(data["row_count"]),
            truncated=bool(data["truncated"]),
            latency_ms=int(data["latency_ms"]),
        )

    # ---- typed REST wrappers ----------------------------------------------

    def health(self, *, timeout: float | None = None) -> HealthStatus:
        """Fetch service health + cursor + row counts. Unauthenticated."""
        data = self._http.get("/health", timeout=timeout, auth_required=False)
        rc = data.get("row_counts", {}) or {}
        cursor = data.get("cursor", {}) or {}
        registry = data.get("registry", {}) or {}
        return HealthStatus(
            status=data.get("status", "unknown"),
            last_block_scanned=int(cursor.get("last_block_scanned") or 0),
            last_block_timestamp=cursor.get("last_block_timestamp"),
            schema_version=int(cursor.get("schema_version") or 0),
            vendor_sha=cursor.get("vendor_sha"),
            chain_head_lag_seconds=(
                float(data["chain_head_lag_seconds"])
                if data.get("chain_head_lag_seconds") is not None
                else None
            ),
            total_actions=int(rc.get("kami_action") or 0),
            total_raw_tx=int(rc.get("raw_tx") or 0),
            total_kami_static=int(rc.get("kami_static") or 0),
            n_systems=int(registry.get("n_systems") or 0),
            n_addresses=int(registry.get("n_addresses") or 0),
            raw=data,
        )

    def kami_actions(
        self,
        kami_id: str,
        *,
        since_days: int = 7,
        limit: int = 100,
        timeout: float | None = None,
    ) -> list[KamiAction]:
        """Recent actions for a single kami within a rolling window.

        ``amount`` on each row is gross MUSU pre-tax (when populated).
        Use it for productivity comparisons; see net derivation in the
        README MUSU semantics section.
        """
        params = {"since_days": since_days, "limit": limit}
        data = self._http.get(
            f"/kami/{kami_id}/actions", params=params, timeout=timeout
        )
        return [_action_from_dict(a) for a in data.get("actions", [])]

    def kami_summary(
        self,
        kami_id: str,
        *,
        since_days: int = 7,
        timeout: float | None = None,
    ) -> KamiSummary:
        """Per-action-type counts for one kami over a rolling window."""
        data = self._http.get(
            f"/kami/{kami_id}/summary",
            params={"since_days": since_days},
            timeout=timeout,
        )
        by_type = [
            ActionTypeCount(
                action_type=row["action_type"],
                count=int(row["count"]),
                pct=float(row["pct"]) if "pct" in row else None,
            )
            for row in data.get("by_type", [])
        ]
        return KamiSummary(
            kami_id=str(data["kami_id"]),
            since_days=int(data["since_days"]),
            total_actions=int(data["total_actions"]),
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            by_type=by_type,
        )

    def operator_summary(
        self,
        addr: str,
        *,
        since_days: int = 7,
        timeout: float | None = None,
    ) -> OperatorSummary:
        """Per-action-type counts for one operator wallet (``from_addr``).

        Operator economics is where *net* MUSU matters. To derive net
        per-tx, query ``/sql`` and join to the matching
        ``harvest_start`` row's ``metadata.taxAmt``: this endpoint
        returns counts only.
        """
        data = self._http.get(
            f"/operator/{addr}/summary",
            params={"since_days": since_days},
            timeout=timeout,
        )
        by_type = [
            ActionTypeCount(
                action_type=row["action_type"],
                count=int(row["count"]),
                pct=float(row["pct"]) if "pct" in row else None,
            )
            for row in data.get("by_type", [])
        ]
        return OperatorSummary(
            operator=str(data["operator"]),
            since_days=int(data["since_days"]),
            total_actions=int(data["total_actions"]),
            distinct_kami=int(data["distinct_kami"]),
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            by_type=by_type,
        )

    def action_types(
        self,
        *,
        since_days: int = 7,
        timeout: float | None = None,
    ) -> list[ActionTypeCount]:
        """Histogram of action_type counts across all kamis."""
        data = self._http.get(
            "/actions/types",
            params={"since_days": since_days},
            timeout=timeout,
        )
        return [
            ActionTypeCount(
                action_type=row["action_type"],
                count=int(row["count"]),
                pct=float(row["pct"]) if "pct" in row else None,
            )
            for row in data.get("by_type", [])
        ]

    def nodes_top(
        self,
        *,
        since_days: int = 7,
        limit: int = 20,
        timeout: float | None = None,
    ) -> list[NodeStat]:
        """Top harvest nodes by ``harvest_start`` count."""
        data = self._http.get(
            "/nodes/top",
            params={"since_days": since_days, "limit": limit},
            timeout=timeout,
        )
        return [
            NodeStat(
                node_id=str(row["node_id"]),
                harvest_starts=int(row["harvest_starts"]),
            )
            for row in data.get("nodes", [])
        ]

    def actions_recent(
        self,
        *,
        limit: int = 100,
        timeout: float | None = None,
    ) -> list[KamiAction]:
        """Most recent actions across the whole table.

        Stream-shaped endpoint; use ``limit`` to cap the page. The
        server caps at 2000. ``amount`` is gross MUSU pre-tax (when
        populated); see README MUSU semantics for net derivation.
        """
        data = self._http.get(
            "/actions/recent",
            params={"limit": limit},
            timeout=timeout,
        )
        return [_action_from_dict(a) for a in data.get("actions", [])]

    def registry_snapshot(
        self, *, timeout: float | None = None
    ) -> RegistrySnapshot:
        """Snapshot of every (system_id, address) pair the ingester has seen."""
        data = self._http.get("/registry/snapshot", timeout=timeout)
        return RegistrySnapshot(
            n_systems=int(data["n_systems"]),
            n_addresses=int(data["n_addresses"]),
            by_system=dict(data.get("by_system", {})),
        )

    # ---- convenience: founder's golden query ------------------------------

    def harvest_leaderboard(
        self,
        *,
        since_days: int = 7,
        limit: int = 20,
        timeout: float | None = None,
    ) -> list[HarvestLeaderRow]:
        """Ranks kamis by gross MUSU harvested.

        This is the right metric for productivity comparison; tax is a
        node-config artifact, not a kami stat. A medium-strength kami on
        a 0%-tax node and a strong kami on a 12%-tax node would invert
        in *net* rankings even though productivity is identical — so
        leaderboards always use gross.

        Returns rows with ``kami_id``, ``owner_address`` (NULL if the
        kami has no kami_static row yet), ``name``, ``collects`` (count
        of harvest_collect rows), ``stops`` (count of harvest_stop), and
        ``musu_gross`` (sum of gross MUSU drained across both action
        types in the window).
        """
        q = (
            "SELECT a.kami_id, s.name, s.owner_address, "
            "       SUM(CASE WHEN a.action_type = 'harvest_collect' THEN 1 ELSE 0 END) AS collects, "
            "       SUM(CASE WHEN a.action_type = 'harvest_stop'    THEN 1 ELSE 0 END) AS stops, "
            "       SUM(CAST(a.amount AS HUGEINT)) AS musu_gross "
            "FROM kami_action a "
            "LEFT JOIN kami_static s USING (kami_id) "
            "WHERE a.action_type IN ('harvest_collect', 'harvest_stop') "
            "  AND a.amount IS NOT NULL "
            "  AND a.block_timestamp > now() - INTERVAL " + str(int(since_days)) + " DAY "
            "GROUP BY a.kami_id, s.name, s.owner_address "
            "ORDER BY musu_gross DESC NULLS LAST "
            "LIMIT " + str(int(limit))
        )
        result = self.sql(q, limit=limit, timeout=timeout)
        idx = {c: i for i, c in enumerate(result.columns)}
        out: list[HarvestLeaderRow] = []
        for row in result.rows:
            out.append(
                HarvestLeaderRow(
                    kami_id=str(row[idx["kami_id"]]) if row[idx["kami_id"]] is not None else None,
                    name=row[idx["name"]],
                    owner_address=row[idx["owner_address"]],
                    collects=int(row[idx["collects"]] or 0),
                    stops=int(row[idx["stops"]] or 0),
                    musu_gross=int(row[idx["musu_gross"]] or 0),
                )
            )
        return out


def _action_from_dict(a: dict[str, Any]) -> KamiAction:
    return KamiAction(
        id=a.get("id"),
        tx_hash=a.get("tx_hash"),
        sub_index=int(a["sub_index"]) if a.get("sub_index") is not None else 0,
        block_number=int(a["block_number"]) if a.get("block_number") is not None else 0,
        block_timestamp=a.get("block_timestamp"),
        action_type=a.get("action_type"),
        system_id=a.get("system_id"),
        from_addr=a.get("from_addr"),
        kami_id=a.get("kami_id"),
        target_kami_id=a.get("target_kami_id"),
        node_id=a.get("node_id"),
        amount=a.get("amount"),
        item_index=a.get("item_index"),
        metadata=a.get("metadata") or {},
        status=int(a["status"]) if a.get("status") is not None else 0,
    )
