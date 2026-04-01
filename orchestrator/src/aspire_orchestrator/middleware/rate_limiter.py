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
import os
import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Default: 500 requests per 60 seconds per tenant (configurable via env)
_DEFAULT_LIMIT = int(os.environ.get("ASPIRE_RATE_LIMIT", "500"))
_DEFAULT_WINDOW_SECONDS = int(os.environ.get("ASPIRE_RATE_WINDOW", "60"))

# Per-endpoint rate limits (override _DEFAULT_LIMIT for specific paths)
# Heavy endpoints that trigger LLM calls or complex processing
_ENDPOINT_LIMITS: dict[str, int] = {  # longest prefix first
    "/v1/intents/stream": 50,    # Heavy: SSE streaming, long-lived
    "/v1/intents": 100,          # Heavy: full orchestrator pipeline
    "/v1/voice/session": 30,     # Heavy: Zoom session creation
}

# Light endpoints use the default limit (500/min)
# Admin endpoints are exempt (handled by EXEMPT_PATHS)

# Paths exempt from rate limiting (health probes, metrics)
_EXEMPT_PATHS = frozenset({
    "/healthz",
    "/livez",
    "/readyz",
    "/metrics",
    "/admin/ops/health",
})


class _RedisWindow:
    """Redis-backed rate limit counter for multi-replica deployments."""

    def __init__(self, redis_url: str) -> None:
        try:
            import redis  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency in some envs
            raise RuntimeError("redis dependency unavailable") from exc

        self._client = redis.from_url(redis_url, decode_responses=True, socket_timeout=1.5)

    def check_and_record(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        bucket = int(time.time() // window_s)
        redis_key = f"aspire:ratelimit:{key}:{bucket}"
        try:
            count = int(self._client.incr(redis_key))
            if count == 1:
                self._client.expire(redis_key, int(window_s) + 2)
            allowed = count <= limit
            remaining = max(0, limit - count)
            return allowed, remaining
        except Exception as exc:
            raise RuntimeError(f"redis rate limiter unavailable: {exc}") from exc


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
        self.backend = "memory"
        self._redis: _RedisWindow | None = None

        redis_url = (os.environ.get("ASPIRE_REDIS_URL") or os.environ.get("REDIS_URL") or "").strip()
        if redis_url:
            try:
                self._redis = _RedisWindow(redis_url)
                self.backend = "redis"
                logger.info("RateLimitMiddleware using redis backend")
            except Exception as exc:
                logger.warning("RateLimitMiddleware redis init failed, falling back to memory: %s", exc)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        global _last_cleanup

        # Skip rate limiting for health/metrics endpoints and CORS preflights
        if request.url.path in _EXEMPT_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        # Determine limit for this path (per-endpoint override or default)
        limit = self.limit
        for path_prefix, path_limit in _ENDPOINT_LIMITS.items():
            if request.url.path.startswith(path_prefix):
                limit = path_limit
                break

        # Rate limit key: prefer suite_id (per-tenant), fallback to IP
        suite_id = request.headers.get("x-suite-id", "")
        if suite_id:
            key = f"tenant:{suite_id}"
        else:
            client_ip = request.client.host if request.client else "unknown"
            key = f"ip:{client_ip}"

        if self._redis is not None:
            try:
                allowed, remaining = self._redis.check_and_record(
                    key, limit, self.window_seconds
                )
            except Exception as exc:
                logger.warning("RateLimit redis backend failed, falling back to memory: %s", exc)
                self._redis = None
                self.backend = "memory"
                allowed, remaining = _window.check_and_record(
                    key, limit, self.window_seconds
                )
        else:
            allowed, remaining = _window.check_and_record(
                key, limit, self.window_seconds
            )

        # Periodic cleanup (every 60s)
        now = time.monotonic()
        if now - _last_cleanup > 60.0:
            _window.cleanup()
            _last_cleanup = now

        if not allowed:
            logger.warning(
                "Rate limit exceeded: key=%s, limit=%d/%ds",
                key[:32], limit, self.window_seconds,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": f"Too many requests. Limit: {limit} per {self.window_seconds}s.",
                    "retry_after": self.window_seconds,
                },
                headers={
                    "Retry-After": str(self.window_seconds),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)

        # Add rate limit headers to all responses
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
