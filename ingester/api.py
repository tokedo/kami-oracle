"""Co-hosted read-only HTTP query layer.

Runs in the same process as the ingest poller (see ``ingester.serve``)
so the FastAPI handlers and the poller share one DuckDB connection.
DuckDB holds a per-process exclusive file lock, which rules out the
naive "just open another read-only connection from a separate process"
approach — hence the co-hosted design.

Auth: every route except ``/health`` is gated by a bearer token. When
``api_token`` is None, ``build_app`` refuses to start if the caller
requested a non-loopback bind; on a loopback bind with no token the
auth dependency is a no-op (dev-only convenience).

Endpoints return JSON. ``uint256`` values (kami_id, amount) are
serialized as decimal strings to stay safe for JS consumers.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .sql import (
    SqlExecutionError,
    SqlTimeoutError,
    SqlValidationError,
    run_readonly,
    validate_readonly_sql,
)
from .storage import Storage
from .system_registry import SystemRegistry

log = logging.getLogger(__name__)


# Clamp ``since_days`` to prevent accidental full-history scans; Stage 1
# retention is 7 days anyway but a 28-day cap leaves room for the planned
# window extension.
MAX_SINCE_DAYS = 28
# Hard row cap across every endpoint that returns action rows.
MAX_LIMIT = 2000

# /sql bounds. The row cap is a separate, higher ceiling than MAX_LIMIT
# — ad-hoc queries need more headroom than the fixed-shape endpoints.
SQL_DEFAULT_LIMIT = 1000
SQL_MAX_LIMIT = 10_000
SQL_TIMEOUT_S = 10.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _action_row_to_dict(row: tuple) -> dict[str, Any]:
    (
        id_, tx_hash, sub_index, block_number, block_timestamp,
        action_type, system_id, from_addr, kami_id, target_kami_id,
        node_id, amount, item_index, metadata_json, status,
    ) = row
    meta: Any = None
    if metadata_json:
        try:
            meta = json.loads(metadata_json)
        except json.JSONDecodeError:
            meta = metadata_json
    return {
        "id": id_,
        "tx_hash": tx_hash,
        "sub_index": sub_index,
        "block_number": block_number,
        "block_timestamp": block_timestamp.isoformat() if block_timestamp else None,
        "action_type": action_type,
        "system_id": system_id,
        "from_addr": from_addr,
        "kami_id": kami_id,
        "target_kami_id": target_kami_id,
        "node_id": node_id,
        "amount": amount,
        "item_index": item_index,
        "metadata": meta,
        "status": status,
    }


ACTION_COLS = (
    "id, tx_hash, sub_index, block_number, block_timestamp, "
    "action_type, system_id, from_addr, kami_id, target_kami_id, "
    "node_id, amount, item_index, metadata_json, status"
)


def _clamp_since_days(v: int) -> int:
    if v < 1:
        return 1
    if v > MAX_SINCE_DAYS:
        return MAX_SINCE_DAYS
    return v


def _clamp_limit(v: int, default: int) -> int:
    if v < 1:
        return 1
    if v > MAX_LIMIT:
        return MAX_LIMIT
    return v


class SqlRequest(BaseModel):
    q: str = Field(..., min_length=1)
    limit: int = Field(default=SQL_DEFAULT_LIMIT, ge=1, le=SQL_MAX_LIMIT)


def build_app(
    storage: Storage,
    registry: SystemRegistry | None = None,
    *,
    api_token: str | None = None,
    bind_host: str = "127.0.0.1",
) -> FastAPI:
    """Build the FastAPI app bound to a specific ``Storage`` instance.

    The storage reference is captured in closures rather than pulled from
    a module global so tests can build an app against a scratch DuckDB
    without touching the real one.

    If ``api_token`` is None and ``bind_host`` is not a loopback host,
    this raises at startup — refusing to silently serve unauth'd over
    the network.
    """
    if api_token is None and not _is_loopback(bind_host):
        raise RuntimeError(
            f"refusing to build API without a token while bound to "
            f"non-loopback host {bind_host!r}. Set KAMI_ORACLE_API_TOKEN."
        )

    app = FastAPI(
        title="kami-oracle",
        description="Read-only query layer over the Kamigotchi action DB (Stage 1).",
        version="0.1.0",
    )

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        supplied = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(supplied.encode("utf-8"), api_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    # /health is the only unauth'd route — kept open so uptime probes and
    # the systemd healthcheck don't need a token.
    @app.get("/health")
    def health() -> dict[str, Any]:
        cursor = storage.get_cursor_state()
        n_raw = storage.fetchone("SELECT COUNT(*) FROM raw_tx")
        n_actions = storage.fetchone("SELECT COUNT(*) FROM kami_action")
        snapshot_rows = storage.fetchone("SELECT COUNT(*) FROM system_address_snapshot")
        payload: dict[str, Any] = {
            "status": "ok",
            "cursor": cursor,
            "row_counts": {
                "raw_tx": int(n_raw[0]) if n_raw else 0,
                "kami_action": int(n_actions[0]) if n_actions else 0,
                "system_address_snapshot": int(snapshot_rows[0]) if snapshot_rows else 0,
            },
        }
        if registry is not None:
            payload["registry"] = {
                "n_systems": len(registry.system_ids()),
                "n_addresses": len(registry),
            }
        return payload

    protected = APIRouter(dependencies=[Depends(require_token)])

    @protected.get("/kami/{kami_id}/actions")
    def kami_actions(
        kami_id: str,
        since_days: int = Query(7, ge=1, le=MAX_SINCE_DAYS),
        limit: int = Query(500, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        since_days = _clamp_since_days(since_days)
        limit = _clamp_limit(limit, 500)
        rows = storage.fetchall(
            f"SELECT {ACTION_COLS} FROM kami_action "
            "WHERE kami_id = ? "
            "AND block_timestamp >= now() - INTERVAL (?) DAY "
            "ORDER BY block_timestamp DESC LIMIT ?",
            [kami_id, since_days, limit],
        )
        return {
            "kami_id": kami_id,
            "since_days": since_days,
            "count": len(rows),
            "actions": [_action_row_to_dict(r) for r in rows],
        }

    @protected.get("/kami/{kami_id}/summary")
    def kami_summary(
        kami_id: str,
        since_days: int = Query(28, ge=1, le=MAX_SINCE_DAYS),
    ) -> dict[str, Any]:
        since_days = _clamp_since_days(since_days)
        by_type = storage.fetchall(
            "SELECT action_type, COUNT(*) "
            "FROM kami_action "
            "WHERE kami_id = ? "
            "AND block_timestamp >= now() - INTERVAL (?) DAY "
            "GROUP BY action_type ORDER BY 2 DESC",
            [kami_id, since_days],
        )
        totals_row = storage.fetchone(
            "SELECT COUNT(*), MIN(block_timestamp), MAX(block_timestamp) "
            "FROM kami_action WHERE kami_id = ? "
            "AND block_timestamp >= now() - INTERVAL (?) DAY",
            [kami_id, since_days],
        )
        total = int(totals_row[0]) if totals_row else 0
        return {
            "kami_id": kami_id,
            "since_days": since_days,
            "total_actions": total,
            "first_seen": totals_row[1].isoformat() if totals_row and totals_row[1] else None,
            "last_seen": totals_row[2].isoformat() if totals_row and totals_row[2] else None,
            "by_type": [{"action_type": r[0], "count": int(r[1])} for r in by_type],
        }

    @protected.get("/operator/{addr}/summary")
    def operator_summary(
        addr: str,
        since_days: int = Query(28, ge=1, le=MAX_SINCE_DAYS),
    ) -> dict[str, Any]:
        since_days = _clamp_since_days(since_days)
        by_type = storage.fetchall(
            "SELECT action_type, COUNT(*) "
            "FROM kami_action "
            "WHERE from_addr = ? "
            "AND block_timestamp >= now() - INTERVAL (?) DAY "
            "GROUP BY action_type ORDER BY 2 DESC",
            [addr, since_days],
        )
        totals_row = storage.fetchone(
            "SELECT COUNT(*), COUNT(DISTINCT kami_id), MIN(block_timestamp), MAX(block_timestamp) "
            "FROM kami_action WHERE from_addr = ? "
            "AND block_timestamp >= now() - INTERVAL (?) DAY",
            [addr, since_days],
        )
        total = int(totals_row[0]) if totals_row else 0
        distinct_kami = int(totals_row[1]) if totals_row else 0
        return {
            "operator": addr,
            "since_days": since_days,
            "total_actions": total,
            "distinct_kami": distinct_kami,
            "first_seen": totals_row[2].isoformat() if totals_row and totals_row[2] else None,
            "last_seen": totals_row[3].isoformat() if totals_row and totals_row[3] else None,
            "by_type": [{"action_type": r[0], "count": int(r[1])} for r in by_type],
        }

    @protected.get("/actions/types")
    def action_types(
        since_days: int = Query(28, ge=1, le=MAX_SINCE_DAYS),
    ) -> dict[str, Any]:
        since_days = _clamp_since_days(since_days)
        rows = storage.fetchall(
            "SELECT action_type, COUNT(*) "
            "FROM kami_action "
            "WHERE block_timestamp >= now() - INTERVAL (?) DAY "
            "GROUP BY action_type ORDER BY 2 DESC",
            [since_days],
        )
        total = sum(int(r[1]) for r in rows)
        return {
            "since_days": since_days,
            "total": total,
            "by_type": [
                {
                    "action_type": r[0],
                    "count": int(r[1]),
                    "pct": round(int(r[1]) / total * 100, 2) if total else 0.0,
                }
                for r in rows
            ],
        }

    @protected.get("/nodes/top")
    def top_nodes(
        since_days: int = Query(28, ge=1, le=MAX_SINCE_DAYS),
        limit: int = Query(20, ge=1, le=200),
    ) -> dict[str, Any]:
        since_days = _clamp_since_days(since_days)
        rows = storage.fetchall(
            "SELECT node_id, COUNT(*) "
            "FROM kami_action "
            "WHERE action_type = 'harvest_start' "
            "AND node_id IS NOT NULL "
            "AND block_timestamp >= now() - INTERVAL (?) DAY "
            "GROUP BY node_id ORDER BY 2 DESC LIMIT ?",
            [since_days, limit],
        )
        return {
            "since_days": since_days,
            "metric": "harvest_start",
            "nodes": [{"node_id": r[0], "harvest_starts": int(r[1])} for r in rows],
        }

    @protected.get("/actions/recent")
    def recent_actions(
        limit: int = Query(100, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        rows = storage.fetchall(
            f"SELECT {ACTION_COLS} FROM kami_action "
            "ORDER BY block_timestamp DESC LIMIT ?",
            [limit],
        )
        return {
            "count": len(rows),
            "actions": [_action_row_to_dict(r) for r in rows],
        }

    @protected.get("/registry/snapshot")
    def registry_snapshot() -> dict[str, Any]:
        rows = storage.fetchall(
            "SELECT system_id, address, abi_name, first_seen_block, last_seen_block "
            "FROM system_address_snapshot ORDER BY system_id, first_seen_block"
        )
        by_system: dict[str, list[dict[str, Any]]] = {}
        for sid, addr, abi_name, fsb, lsb in rows:
            by_system.setdefault(sid, []).append({
                "address": addr,
                "abi_name": abi_name,
                "first_seen_block": int(fsb) if fsb is not None else None,
                "last_seen_block": int(lsb) if lsb is not None else None,
            })
        return {
            "n_systems": len(by_system),
            "n_addresses": len(rows),
            "by_system": by_system,
        }

    @protected.post("/sql")
    def sql_query(req: SqlRequest, request: Request) -> dict[str, Any]:
        client_ip = request.client.host if request.client else "?"
        preview = req.q[:500].replace("\n", " ")
        t0 = time.monotonic()
        try:
            cleaned = validate_readonly_sql(req.q)
        except SqlValidationError as e:
            log.info(
                "sql: status=validation client=%s latency_ms=%d q=%r detail=%s",
                client_ip, int((time.monotonic() - t0) * 1000), preview, str(e),
            )
            raise HTTPException(
                status_code=400,
                detail={"error": "validation", "detail": str(e)},
            )
        try:
            result = run_readonly(
                storage, cleaned, row_cap=req.limit, timeout_s=SQL_TIMEOUT_S,
            )
        except SqlTimeoutError as e:
            log.info(
                "sql: status=timeout client=%s latency_ms=%d q=%r",
                client_ip, int((time.monotonic() - t0) * 1000), preview,
            )
            raise HTTPException(
                status_code=504,
                detail={"error": "timeout", "detail": str(e)},
            )
        except SqlExecutionError as e:
            log.info(
                "sql: status=execution client=%s latency_ms=%d q=%r detail=%s",
                client_ip, int((time.monotonic() - t0) * 1000), preview, str(e),
            )
            raise HTTPException(
                status_code=400,
                detail={"error": "execution", "detail": str(e)},
            )
        log.info(
            "sql: status=ok client=%s rows=%d truncated=%s latency_ms=%d q=%r",
            client_ip, result.row_count, result.truncated, result.latency_ms, preview,
        )
        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "latency_ms": result.latency_ms,
        }

    app.include_router(protected)

    @app.exception_handler(Exception)
    async def _generic_error(_request, exc):
        # HTTPExceptions already have a proper status; let them through.
        if isinstance(exc, HTTPException):
            raise exc
        log.exception("api: unhandled error: %s", exc)
        raise HTTPException(status_code=500, detail="internal server error")

    return app
