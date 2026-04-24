"""Per-token rate limit middleware.

Bucket key is sha256(bearer_token) — keeps the raw token out of the
counter dict and out of any error message we might log later. The
window is a simple fixed 60-second window per key; a token bucket
would be more accurate but the workload (single shared token, ~60
req/min) doesn't justify the extra moving parts.

The middleware ignores requests without a Bearer header and lets
``/health`` through unconditionally so uptime probes never count
against the limit. Auth still happens downstream via the route
dependency, so an unauth'd flood gets cheap 401s without burning a
bucket entry.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

WINDOW_SECONDS = 60


def _bucket_key(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-token rate limit."""

    def __init__(
        self,
        app,
        *,
        limit_per_min: int,
        clock: Callable[[], float] = time.monotonic,
        skip_paths: frozenset[str] = frozenset({"/health"}),
    ) -> None:
        super().__init__(app)
        self.limit = max(1, int(limit_per_min))
        self._clock = clock
        self._skip = skip_paths
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[float, int]] = {}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._skip:
            return await call_next(request)
        key = _bucket_key(request.headers.get("Authorization"))
        if key is None:
            return await call_next(request)

        now = self._clock()
        with self._lock:
            window_start, count = self._counts.get(key, (now, 0))
            if now - window_start >= WINDOW_SECONDS:
                window_start, count = now, 0
            count += 1
            self._counts[key] = (window_start, count)

        if count > self.limit:
            retry_after = max(1, int(WINDOW_SECONDS - (now - window_start)))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "detail": f"limit {self.limit}/min exceeded",
                },
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
