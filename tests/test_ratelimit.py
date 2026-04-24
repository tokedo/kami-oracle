"""Tests for the per-token rate-limit middleware.

Builds a minimal FastAPI app wired with ``RateLimitMiddleware`` and a
single auth-protected route, then drives requests with an injected
clock so the 60-second window logic is deterministic.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ingester.ratelimit import RateLimitMiddleware


TOKEN = "rl-token-xyz"
LIMIT = 60


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _build(clock: FakeClock, limit: int = LIMIT) -> TestClient:
    app = FastAPI()

    @app.get("/health")
    def _health():
        return {"ok": True}

    @app.get("/echo")
    def _echo():
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        limit_per_min=limit,
        clock=clock,
    )
    return TestClient(app)


def _hit(client: TestClient, path: str = "/echo") -> int:
    r = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    return r.status_code


def test_burst_at_limit_all_succeed():
    clock = FakeClock()
    client = _build(clock)
    statuses = [_hit(client) for _ in range(LIMIT)]
    assert statuses == [200] * LIMIT


def test_one_over_limit_returns_429():
    clock = FakeClock()
    client = _build(clock)
    for _ in range(LIMIT):
        assert _hit(client) == 200
    r = client.get("/echo", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    body = r.json()
    assert body["error"] == "rate_limited"


def test_window_resets_after_60s():
    clock = FakeClock()
    client = _build(clock)
    for _ in range(LIMIT):
        assert _hit(client) == 200
    assert _hit(client) == 429
    clock.advance(61)
    assert _hit(client) == 200


def test_health_is_skipped():
    clock = FakeClock()
    client = _build(clock)
    # Hammer /health far past the limit; should never 429.
    for _ in range(LIMIT * 3):
        r = client.get("/health")
        assert r.status_code == 200


def test_unauth_request_passes_through_rate_limit():
    # Without a Bearer header the middleware shouldn't bucket the
    # request — auth (downstream) is responsible for rejecting it.
    clock = FakeClock()
    client = _build(clock)
    for _ in range(LIMIT * 2):
        r = client.get("/echo")
        assert r.status_code == 200


def test_distinct_tokens_have_independent_buckets():
    clock = FakeClock()
    client = _build(clock)
    for _ in range(LIMIT):
        r = client.get("/echo", headers={"Authorization": "Bearer token-A"})
        assert r.status_code == 200
    # Second token starts fresh.
    r = client.get("/echo", headers={"Authorization": "Bearer token-B"})
    assert r.status_code == 200
