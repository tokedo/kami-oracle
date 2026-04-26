"""Integration tests for the ``client/`` package.

Hits the live loopback endpoint (the same shape Colab + kami-zero will
use). Skips cleanly when ``KAMI_ORACLE_API_TOKEN`` is unset so the
test suite still passes for outside contributors who don't have a
running service.

These are intentionally not unit tests — round-tripping against the
real API is the point. Unit tests for the HTTP layer would just
re-test ``requests``.
"""

from __future__ import annotations

import os

import pytest

from client import (
    HarvestLeaderRow,
    HealthStatus,
    KamiAction,
    KamiSummary,
    NodeStat,
    OperatorSummary,
    OracleAuthError,
    OracleClient,
    RegistrySnapshot,
    SqlResult,
)

TOKEN = os.environ.get("KAMI_ORACLE_API_TOKEN", "").strip() or None
BASE_URL = os.environ.get("KAMI_ORACLE_TEST_URL", "http://127.0.0.1:8787")

pytestmark = pytest.mark.skipif(
    TOKEN is None,
    reason="KAMI_ORACLE_API_TOKEN not set — skipping client integration tests",
)


@pytest.fixture(scope="module")
def oc() -> OracleClient:
    return OracleClient(base_url=BASE_URL, token=TOKEN)


# ---- /health (no auth) ----------------------------------------------------


def test_health_returns_typed_status(oc: OracleClient) -> None:
    h = oc.health()
    assert isinstance(h, HealthStatus)
    assert h.status == "ok"
    assert h.last_block_scanned > 0
    assert h.schema_version >= 2  # session 6 bumped to v2
    assert h.total_actions > 0
    # n_systems should reflect the registry snapshot (~30+).
    assert h.n_systems > 0


# ---- typed wrappers -------------------------------------------------------


def test_action_types_returns_buckets(oc: OracleClient) -> None:
    rows = oc.action_types(since_days=7)
    assert isinstance(rows, list)
    assert all(r.action_type for r in rows)
    assert all(r.count > 0 for r in rows)
    # Stage-1 dominant types should be present.
    types = {r.action_type for r in rows}
    assert "harvest_start" in types or "harvest_stop" in types


def test_nodes_top_returns_node_stats(oc: OracleClient) -> None:
    nodes = oc.nodes_top(since_days=7, limit=5)
    assert isinstance(nodes, list)
    assert len(nodes) <= 5
    for n in nodes:
        assert isinstance(n, NodeStat)
        assert n.node_id
        assert n.harvest_starts > 0


def test_actions_recent_returns_typed_actions(oc: OracleClient) -> None:
    actions = oc.actions_recent(limit=5)
    assert isinstance(actions, list)
    assert len(actions) <= 5
    for a in actions:
        assert isinstance(a, KamiAction)
        assert a.tx_hash and a.tx_hash.startswith("0x")
        assert a.block_number > 0


def test_registry_snapshot_returns_typed(oc: OracleClient) -> None:
    snap = oc.registry_snapshot()
    assert isinstance(snap, RegistrySnapshot)
    assert snap.n_systems > 0
    assert snap.n_addresses >= snap.n_systems
    assert snap.by_system  # non-empty dict


def test_kami_summary_round_trips(oc: OracleClient) -> None:
    # Fish a real kami_id off /actions/recent so the test isn't
    # hard-coded against a particular kami.
    actions = oc.actions_recent(limit=200)
    kami_id = next(
        (a.kami_id for a in actions if a.kami_id is not None),
        None,
    )
    if kami_id is None:
        pytest.skip("no recent actions had a non-null kami_id")
    summary = oc.kami_summary(kami_id, since_days=7)
    assert isinstance(summary, KamiSummary)
    assert summary.kami_id == kami_id
    assert summary.since_days == 7


def test_kami_actions_round_trips(oc: OracleClient) -> None:
    actions = oc.actions_recent(limit=200)
    kami_id = next(
        (a.kami_id for a in actions if a.kami_id is not None),
        None,
    )
    if kami_id is None:
        pytest.skip("no recent actions had a non-null kami_id")
    rows = oc.kami_actions(kami_id, since_days=7, limit=10)
    assert isinstance(rows, list)
    assert all(isinstance(r, KamiAction) for r in rows)
    assert all(r.kami_id == kami_id for r in rows)


def test_operator_summary_round_trips(oc: OracleClient) -> None:
    # bpeon is the canonical reference operator from Stage 1 validation.
    bpeon = "0x86aDb8f741E945486Ce2B0560D9f643838FAcEC2"
    summary = oc.operator_summary(bpeon, since_days=7)
    assert isinstance(summary, OperatorSummary)
    assert summary.operator == bpeon
    assert summary.since_days == 7


# ---- /sql escape hatch ----------------------------------------------------


def test_sql_count_returns_typed_result(oc: OracleClient) -> None:
    r = oc.sql("SELECT COUNT(*) AS n FROM kami_action")
    assert isinstance(r, SqlResult)
    assert r.columns == ["n"]
    assert r.row_count == 1
    n = int(r.rows[0][0])
    assert n > 0


def test_sql_truncation_flag(oc: OracleClient) -> None:
    # Force more rows than the cap so truncated=True.
    r = oc.sql("SELECT id FROM kami_action ORDER BY block_number DESC", limit=5)
    assert r.row_count <= 5
    assert r.truncated is True


# ---- harvest_leaderboard convenience method -------------------------------


def test_harvest_leaderboard_runs(oc: OracleClient) -> None:
    rows = oc.harvest_leaderboard(since_days=7, limit=5)
    assert isinstance(rows, list)
    assert len(rows) <= 5
    if not rows:
        pytest.skip("no harvest activity in the last 7 days")
    for row in rows:
        assert isinstance(row, HarvestLeaderRow)
        assert row.kami_id is not None
        assert row.musu_gross > 0
        assert row.collects + row.stops > 0
    # Leaderboard ranked by musu_gross desc.
    musu = [r.musu_gross for r in rows]
    assert musu == sorted(musu, reverse=True)


# ---- auth ----------------------------------------------------------------


def test_wrong_token_raises_auth_error() -> None:
    bad = OracleClient(base_url=BASE_URL, token="not-a-real-token")
    with pytest.raises(OracleAuthError):
        bad.action_types(since_days=1)
