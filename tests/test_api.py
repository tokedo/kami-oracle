"""Tests for the co-hosted read-only query layer.

Uses FastAPI's TestClient against a scratch DuckDB populated with a
handful of decoded actions. No network I/O; no RPC calls.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingester.api import build_app
from ingester.decoder import DecodedAction
from ingester.storage import RawTx, Storage, read_schema_sql
from ingester.system_registry import SystemInfo, SystemRegistry

ROOT = Path(__file__).resolve().parent.parent


def _now_unix() -> int:
    return int(dt.datetime.now(tz=dt.timezone.utc).timestamp())


def _action(
    tx_hash: str,
    sub_index: int,
    *,
    kami_id: str | None,
    action_type: str,
    block_ts: int,
    block_number: int = 27_800_000,
    from_addr: str = "0x0000000000000000000000000000000000000001",
    node_id: str | None = None,
    amount: str | None = None,
    harvest_id: str | None = None,
    system_id: str = "system.harvest.start",
) -> DecodedAction:
    meta: dict = {"fn": "executeTyped"}
    if harvest_id is not None:
        meta["harvest_id"] = harvest_id
    return DecodedAction(
        tx_hash=tx_hash,
        sub_index=sub_index,
        block_number=block_number,
        block_timestamp=block_ts,
        action_type=action_type,
        system_id=system_id,
        from_addr=from_addr,
        status=1,
        kami_id=kami_id,
        node_id=node_id,
        amount=amount,
        metadata=meta,
    )


def _raw_tx(tx_hash: str, block_ts: int, block_number: int = 27_800_000) -> RawTx:
    return RawTx(
        tx_hash=tx_hash,
        block_number=block_number,
        block_timestamp=block_ts,
        tx_index=0,
        from_addr="0x0000000000000000000000000000000000000001",
        to_addr="0x0777687Ec9FEB7349c23a19Ba7D11a1fe8cd35F1",
        method_sig="0xc8372a87",
        system_id="system.harvest.start",
        raw_calldata=b"\xc8\x37\x2a\x87",
        status=1,
        gas_used=100_000,
        gas_price_wei=1_000_000_000,
    )


@pytest.fixture()
def client(tmp_path):
    storage = Storage(tmp_path / "scratch.duckdb")
    storage.bootstrap(read_schema_sql(ROOT))
    # Seed cursor.
    storage.set_cursor(block_number=27_800_000, block_timestamp=_now_unix(), vendor_sha="testsha")

    # Seed a few actions + raw txs spanning 1h, 3d, and 10d ago.
    now = _now_unix()
    ts_recent = now - 3600          # 1h ago
    ts_midwin = now - 3 * 86400     # 3d ago
    ts_oldwin = now - 10 * 86400    # 10d ago (outside 7d window)

    storage.upsert_raw_txs([
        _raw_tx("0xaa" + "0" * 62, ts_recent),
        _raw_tx("0xbb" + "0" * 62, ts_midwin),
        _raw_tx("0xcc" + "0" * 62, ts_oldwin),
    ])

    storage.upsert_actions([
        _action("0xaa" + "0" * 62, 0, kami_id="42", action_type="harvest_start",
                block_ts=ts_recent, node_id="47", system_id="system.harvest.start"),
        _action("0xaa" + "0" * 62, 1, kami_id="43", action_type="harvest_start",
                block_ts=ts_recent, node_id="47", system_id="system.harvest.start"),
        _action("0xbb" + "0" * 62, 0, kami_id="42", action_type="feed",
                block_ts=ts_midwin, system_id="system.kami.use.item",
                from_addr="0x0000000000000000000000000000000000000002"),
        _action("0xcc" + "0" * 62, 0, kami_id="42", action_type="feed",
                block_ts=ts_oldwin, system_id="system.kami.use.item"),
    ])

    # Seed a multi-deployment snapshot row set so /registry/snapshot returns
    # the expected shape.
    storage.upsert_system_address_snapshot([
        ("system.harvest.start", "0x" + "a" * 40, "HarvestStartSystem.json", 27_700_000, 27_800_000),
        ("system.harvest.start", "0x" + "b" * 40, "HarvestStartSystem.json", 27_550_000, 27_690_000),
        ("system.kami.level",   "0x" + "1" * 40, "KamiLevelSystem.json",    27_550_000, 27_800_000),
    ])

    registry = SystemRegistry.from_snapshot_rows(storage.load_system_address_snapshot())
    app = build_app(storage, registry)
    with TestClient(app) as tc:
        yield tc
    storage.close()


def test_health_returns_cursor_and_row_counts(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["cursor"]["last_block_scanned"] == 27_800_000
    assert body["row_counts"]["raw_tx"] == 3
    assert body["row_counts"]["kami_action"] == 4
    assert body["row_counts"]["system_address_snapshot"] == 3
    assert body["registry"]["n_systems"] == 2
    assert body["registry"]["n_addresses"] == 3


def test_actions_types_respects_since_days(client):
    # Default 28d window sees all 4 actions.
    r = client.get("/actions/types", params={"since_days": 28})
    body = r.json()
    counts = {row["action_type"]: row["count"] for row in body["by_type"]}
    assert counts == {"harvest_start": 2, "feed": 2}
    assert body["total"] == 4

    # 7d window drops the 10d-old feed.
    r = client.get("/actions/types", params={"since_days": 7})
    body = r.json()
    counts = {row["action_type"]: row["count"] for row in body["by_type"]}
    assert counts == {"harvest_start": 2, "feed": 1}


def test_actions_recent_is_time_desc(client):
    r = client.get("/actions/recent", params={"limit": 10})
    body = r.json()
    # 4 actions total; order is most-recent-first.
    timestamps = [a["block_timestamp"] for a in body["actions"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert body["count"] == 4


def test_kami_actions_filters_by_kami_and_window(client):
    r = client.get("/kami/42/actions", params={"since_days": 7, "limit": 100})
    body = r.json()
    assert body["kami_id"] == "42"
    # kami 42 has 3 rows total; only 2 fall inside the 7-day window.
    assert body["count"] == 2
    types = {a["action_type"] for a in body["actions"]}
    assert types == {"harvest_start", "feed"}


def test_kami_summary_returns_counts(client):
    r = client.get("/kami/42/summary", params={"since_days": 28})
    body = r.json()
    assert body["kami_id"] == "42"
    assert body["total_actions"] == 3
    counts = {row["action_type"]: row["count"] for row in body["by_type"]}
    assert counts == {"feed": 2, "harvest_start": 1}


def test_operator_summary_filters_by_from_addr(client):
    op1 = "0x0000000000000000000000000000000000000001"
    op2 = "0x0000000000000000000000000000000000000002"

    body1 = client.get(f"/operator/{op1}/summary", params={"since_days": 28}).json()
    # op1 has 3 rows (the two harvest_starts + the oldwin feed)
    assert body1["total_actions"] == 3
    assert body1["distinct_kami"] == 2

    body2 = client.get(f"/operator/{op2}/summary", params={"since_days": 28}).json()
    # op2 has the 3d-old feed only.
    assert body2["total_actions"] == 1


def test_top_nodes_counts_harvest_starts(client):
    body = client.get("/nodes/top", params={"since_days": 28, "limit": 5}).json()
    assert body["metric"] == "harvest_start"
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["node_id"] == "47"
    assert body["nodes"][0]["harvest_starts"] == 2


def test_registry_snapshot_endpoint(client):
    body = client.get("/registry/snapshot").json()
    assert body["n_systems"] == 2
    assert body["n_addresses"] == 3
    # system.harvest.start has two deployments.
    harv = body["by_system"]["system.harvest.start"]
    assert len(harv) == 2
    addrs = {e["address"] for e in harv}
    assert addrs == {"0x" + "a" * 40, "0x" + "b" * 40}


def test_since_days_is_clamped(client):
    # since_days=365 is above MAX_SINCE_DAYS=28; FastAPI's Query validator
    # rejects before we hit SQL. Expect 422.
    r = client.get("/actions/types", params={"since_days": 365})
    assert r.status_code == 422


def test_limit_is_bounded(client):
    # limit above MAX_LIMIT is rejected by the validator.
    r = client.get("/actions/recent", params={"limit": 10_000})
    assert r.status_code == 422
