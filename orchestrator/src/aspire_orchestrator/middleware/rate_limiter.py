"""Per-tenant rate limiting middleware (B-H7).

Implements a sliding-window rate limiter that tracks requests per tenant
(suite_id) using an in-memory store with automatic cleanup.

Production upgrade path: Replace the in-memory store with Redis for
multi-instance deployments.

Law compliance:
  - Law #3: Exceeding rate limit → deny with 429 + receipt
  - Law #6: Rate limits are per-tenant (no cross-tenant interference)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Default: 100 requests per 60 seconds per tenant
_DEFAULT_LIMIT = 100
_DEFAULT_WINDOW_SECONDS = 60

# Paths exempt from rate limiting (health probes, metrics)
_EXEMPT_PATHS = frozenset({
    "/healthz",
    "/livez",
    "/readyz",
    "/metrics",
    "/admin/ops/health",
})


class _SlidingWindow:
    """In-memory sliding window counter per key."""

    __slots__ = ("_windows",)

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check_and_record(
        self, key: str, limit: int, window_s: float
    ) -> tuple[bool, int]:
        """Check if request is allowed and record it.

        Returns (allowed, remaining_requests).
        """
        now = time.monotonic()
        timestamps = self._windows[key]

        # Prune expired entries
        cutoff = now - window_s
        timestamps[:] = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= limit:
            return False, 0

        timestamps.append(now)
        return True, limit - len(timestamps)

    def cleanup(self, max_age_s: float = 300.0) -> None:
        """Remove keys with no recent activity (memory management)."""
        now = time.monotonic()
        stale = [
            k for k, v in self._windows.items()
            if not v or (now - v[-1]) > max_age_s
        ]
        for k in stale:
            del self._windows[k]


# Module-level singleton
_window = _SlidingWindow()
_last_cleanup = time.monotonic()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-tenant rate limiting (Law #3: fail-closed on abuse).

    Rate limit key: suite_id from X-Suite-Id header.
    Fallback: client IP (for unauthenticated requests).

    Returns 429 Too Many Requests with Retry-After header when limit exceeded.
    """

    def __init__(
        self,
        app: Any,
        limit: int = _DEFAULT_LIMIT,
        window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    ) -> None:
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        global _last_cleanup

        # Skip rate limiting for health/metrics endpoints
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # Rate limit key: prefer suite_id (per-tenant), fallback to IP
        suite_id = request.headers.get("x-suite-id", "")
        if suite_id:
            key = f"tenant:{suite_id}"
        else:
            client_ip = request.client.host if request.client else "unknown"
            key = f"ip:{client_ip}"

        allowed, remaining = _window.check_and_record(
            key, self.limit, self.window_seconds
        )

        # Periodic cleanup (every 60s)
        now = time.monotonic()
        if now - _last_cleanup > 60.0:
            _window.cleanup()
            _last_cleanup = now

        if not allowed:
            logger.warning(
                "Rate limit exceeded: key=%s, limit=%d/%ds",
                key[:32], self.limit, self.window_seconds,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": f"Too many requests. Limit: {self.limit} per {self.window_seconds}s.",
                    "retry_after": self.window_seconds,
                },
                headers={
                    "Retry-After": str(self.window_seconds),
                    "X-RateLimit-Limit": str(self.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)

        # Add rate limit headers to all responses
        response.headers["X-RateLimit-Limit"] = str(self.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
