"""Tests for the ad-hoc read-only ``/sql`` plane.

Covers both the static validator (``validate_readonly_sql``) and the
end-to-end FastAPI route, including timeout behavior. Uses a scratch
DuckDB so the live service isn't touched.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingester.api import build_app
from ingester.decoder import DecodedAction
from ingester.sql import (
    SqlValidationError,
    run_readonly,
    validate_readonly_sql,
)
from ingester.storage import RawTx, Storage, read_schema_sql
from ingester.system_registry import SystemRegistry

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "sql-test-token"


def _now_unix() -> int:
    return int(dt.datetime.now(tz=dt.timezone.utc).timestamp())


def _action(tx_hash: str, sub_index: int, *, kami_id: str, block_ts: int) -> DecodedAction:
    return DecodedAction(
        tx_hash=tx_hash,
        sub_index=sub_index,
        block_number=27_800_000,
        block_timestamp=block_ts,
        action_type="harvest_start",
        system_id="system.harvest.start",
        from_addr="0x0000000000000000000000000000000000000001",
        status=1,
        kami_id=kami_id,
        node_id="47",
        amount=None,
        metadata={"fn": "executeTyped"},
    )


def _raw_tx(tx_hash: str, block_ts: int) -> RawTx:
    return RawTx(
        tx_hash=tx_hash,
        block_number=27_800_000,
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
def storage(tmp_path):
    s = Storage(tmp_path / "scratch.duckdb")
    s.bootstrap(read_schema_sql(ROOT))
    s.set_cursor(block_number=27_800_000, block_timestamp=_now_unix(), vendor_sha="testsha")
    now = _now_unix()
    s.upsert_raw_txs([_raw_tx(f"0x{i:064x}", now - 60 * i) for i in range(1, 21)])
    s.upsert_actions([
        _action(f"0x{i:064x}", 0, kami_id=str(100 + (i % 3)), block_ts=now - 60 * i)
        for i in range(1, 21)
    ])
    yield s
    s.close()


@pytest.fixture()
def client(storage):
    app = build_app(storage, None, api_token=TOKEN, bind_host="127.0.0.1")
    with TestClient(app) as tc:
        yield tc


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Static validator.
# ---------------------------------------------------------------------------


def test_validator_accepts_plain_select():
    assert validate_readonly_sql("SELECT 1").strip().lower().startswith("select")


def test_validator_accepts_with_cte():
    validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x")


def test_validator_accepts_pragma():
    validate_readonly_sql("PRAGMA database_size")


def test_validator_strips_trailing_semicolon():
    cleaned = validate_readonly_sql("SELECT 1;")
    assert ";" not in cleaned


@pytest.mark.parametrize("bad", [
    "INSERT INTO kami_action VALUES (1)",
    "UPDATE kami_action SET kami_id = '1'",
    "DELETE FROM kami_action",
    "DROP TABLE kami_action",
    "CREATE TABLE t (x INT)",
    "ALTER TABLE kami_action ADD COLUMN x INT",
    "ATTACH 'foo.db' AS foo",
    "LOAD 'httpfs'",
    "COPY kami_action FROM '/etc/passwd'",
])
def test_validator_rejects_writes(bad):
    with pytest.raises(SqlValidationError):
        validate_readonly_sql(bad)


def test_validator_rejects_stacked_queries():
    with pytest.raises(SqlValidationError):
        validate_readonly_sql("SELECT 1; DROP TABLE kami_action")


def test_validator_strips_comments_before_keyword_check():
    # Leading comment shouldn't confuse the leading-keyword check.
    validate_readonly_sql("-- pick one\nSELECT 1")
    validate_readonly_sql("/* explain */ SELECT 1")


def test_validator_rejects_trailing_stacked_after_comment():
    with pytest.raises(SqlValidationError):
        validate_readonly_sql("SELECT 1; -- trailing\nDROP TABLE kami_action")


def test_validator_rejects_empty():
    with pytest.raises(SqlValidationError):
        validate_readonly_sql("")
    with pytest.raises(SqlValidationError):
        validate_readonly_sql("   \n  ")


def test_validator_rejects_oversize():
    with pytest.raises(SqlValidationError):
        validate_readonly_sql("SELECT 1 /* " + "x" * 20_000 + " */")


# ---------------------------------------------------------------------------
# run_readonly — row cap + truncation.
# ---------------------------------------------------------------------------


def test_run_readonly_returns_rows(storage):
    result = run_readonly(storage, "SELECT 1 AS x", row_cap=10, timeout_s=5.0)
    assert result.columns == ["x"]
    assert result.rows == [[1]]
    assert result.truncated is False


def test_run_readonly_enforces_row_cap(storage):
    # 20 actions seeded; ask for 5.
    result = run_readonly(
        storage,
        "SELECT kami_id FROM kami_action ORDER BY block_timestamp DESC",
        row_cap=5,
        timeout_s=5.0,
    )
    assert len(result.rows) == 5
    assert result.row_count == 5
    assert result.truncated is True


def test_run_readonly_respects_timeout(storage):
    # generate_series(100M) is enough to blow past a 1s budget on the
    # test VM; the watcher thread should interrupt.
    from ingester.sql import SqlTimeoutError
    with pytest.raises(SqlTimeoutError):
        run_readonly(
            storage,
            "SELECT count(*) FROM range(10000000000)",
            row_cap=10,
            timeout_s=1.0,
        )


# ---------------------------------------------------------------------------
# /sql HTTP route.
# ---------------------------------------------------------------------------


def test_sql_route_requires_auth(client):
    r = client.post("/sql", json={"q": "SELECT 1"})
    assert r.status_code == 401


def test_sql_happy_path(client):
    r = client.post(
        "/sql",
        json={"q": "SELECT kami_id, COUNT(*) AS n FROM kami_action GROUP BY 1 ORDER BY 1 LIMIT 5"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["kami_id", "n"]
    assert body["row_count"] >= 1
    assert body["truncated"] is False
    assert "latency_ms" in body


def test_sql_rejects_writes(client):
    r = client.post(
        "/sql",
        json={"q": "DROP TABLE kami_action"},
        headers=_auth(),
    )
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"] == "validation"


def test_sql_rejects_stacked(client):
    r = client.post(
        "/sql",
        json={"q": "SELECT 1; DROP TABLE kami_action"},
        headers=_auth(),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "validation"


def test_sql_rejects_attach(client):
    r = client.post(
        "/sql",
        json={"q": "ATTACH 'foo.db' AS foo"},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_sql_rejects_oversize_limit(client):
    r = client.post(
        "/sql",
        json={"q": "SELECT 1", "limit": 100_000},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_sql_truncated_flag(client):
    r = client.post(
        "/sql",
        json={
            "q": "SELECT kami_id FROM kami_action",
            "limit": 3,
        },
        headers=_auth(),
    )
    body = r.json()
    assert body["row_count"] == 3
    assert body["truncated"] is True


def test_sql_execution_error(client):
    r = client.post(
        "/sql",
        json={"q": "SELECT * FROM no_such_table"},
        headers=_auth(),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "execution"
