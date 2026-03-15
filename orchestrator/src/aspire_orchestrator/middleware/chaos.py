"""ChaosMonkey middleware — controlled failure injection for resilience testing.

Enabled ONLY when ``CHAOS_ENABLED=true`` (fail-closed by default — Law 3).
All chaos injections are logged with correlation IDs for full traceability.

Feature flags (env vars):
  CHAOS_LATENCY_MS   — Add artificial latency (milliseconds) to every eligible request.
  CHAOS_ERROR_RATE   — Inject HTTP 500 errors at X% of eligible requests (0.0–1.0 fraction).
  CHAOS_DROP_RATE    — Drop connections at X% of eligible requests (0.0–1.0 fraction).

Safety guarantees:
  - Health/readiness/metrics endpoints are NEVER affected.
  - Default is OFF for all chaos types (0ms latency, 0.0 error, 0.0 drop).
  - Each injection is logged at WARNING level with the chaos type and target path.
  - Runtime re-reads env vars on each request so chaos can be toggled without restart.

Law compliance:
  - Law #3: Disabled by default; must be explicitly enabled.
  - Law #2: Every injection is logged (receipt-level traceability via correlation ID).
  - Law #9: No secrets or PII in chaos logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger: Final = logging.getLogger(__name__)

# Paths exempt from chaos injection — infrastructure endpoints must always respond.
_EXEMPT_PREFIXES: Final[tuple[str, ...]] = (
    "/health",
    "/healthz",
    "/ready",
    "/readyz",
    "/livez",
    "/metrics",
    "/admin/ops/health",
)


def _is_chaos_enabled() -> bool:
    """Check if chaos engineering is globally enabled (fail-closed)."""
    return os.environ.get("CHAOS_ENABLED", "").strip().lower() == "true"


def _get_chaos_latency_ms() -> int:
    """Get artificial latency to inject (milliseconds). Default: 0."""
    try:
        return max(0, int(os.environ.get("CHAOS_LATENCY_MS", "0")))
    except (ValueError, TypeError):
        return 0


def _get_chaos_error_rate() -> float:
    """Get error injection rate (0.0–1.0 fraction). Default: 0.0."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("CHAOS_ERROR_RATE", "0"))))
    except (ValueError, TypeError):
        return 0.0


def _get_chaos_drop_rate() -> float:
    """Get connection drop rate (0.0–1.0 fraction). Default: 0.0."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("CHAOS_DROP_RATE", "0"))))
    except (ValueError, TypeError):
        return 0.0


def _is_exempt(path: str) -> bool:
    """Check if the request path is exempt from chaos injection."""
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _get_correlation_id(request: Request) -> str:
    """Extract correlation ID from request headers for traceability."""
    return request.headers.get("x-correlation-id", "none")


class ChaosMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for controlled failure injection.

    Mount order: should be INNERMOST middleware (added first in server.py)
    so that correlation ID and exception handler are already in scope.

    All chaos parameters are re-read from env vars on each request, allowing
    live tuning without restarting the server.

    Usage in server.py::

        if os.environ.get("CHAOS_ENABLED", "").lower() == "true":
            app.add_middleware(ChaosMiddleware)
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path: str = request.url.path
        correlation_id: str = _get_correlation_id(request)

        # Never inject chaos on infrastructure endpoints.
        if _is_exempt(path):
            return await call_next(request)

        # Safety check: if chaos was somehow disabled between middleware mount
        # and request time, pass through (defense in depth).
        if not _is_chaos_enabled():
            return await call_next(request)

        # --- Connection Drop (checked first — no point adding latency to a dropped request) ---
        drop_rate: float = _get_chaos_drop_rate()
        if drop_rate > 0 and random.random() < drop_rate:
            logger.warning(
                "CHAOS_DROP: Dropping connection | path=%s | correlation_id=%s | rate=%.2f",
                path,
                correlation_id,
                drop_rate,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "chaos_drop",
                    "detail": "Connection dropped by ChaosMonkey (resilience test)",
                    "correlation_id": correlation_id,
                },
            )

        # --- Error Injection ---
        error_rate: float = _get_chaos_error_rate()
        if error_rate > 0 and random.random() < error_rate:
            logger.warning(
                "CHAOS_ERROR: Injecting 500 | path=%s | correlation_id=%s | rate=%.2f",
                path,
                correlation_id,
                error_rate,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "chaos_error",
                    "detail": "Internal error injected by ChaosMonkey (resilience test)",
                    "correlation_id": correlation_id,
                },
            )

        # --- Latency Injection ---
        latency_ms: int = _get_chaos_latency_ms()
        if latency_ms > 0:
            logger.warning(
                "CHAOS_LATENCY: Adding %dms delay | path=%s | correlation_id=%s",
                latency_ms,
                path,
                correlation_id,
            )
            await asyncio.sleep(latency_ms / 1000.0)

        return await call_next(request)


def maybe_add_chaos(app: object) -> None:
    """Add ChaosMiddleware only if CHAOS_ENABLED=true.

    Call this from server.py after all other middleware is mounted.
    The middleware is innermost so correlation ID and exception handler
    wrap it from outside.
    """
    if _is_chaos_enabled():
        logger.warning(
            "CHAOS MODE ENABLED — latency=%dms error_rate=%.2f drop_rate=%.2f",
            _get_chaos_latency_ms(),
            _get_chaos_error_rate(),
            _get_chaos_drop_rate(),
        )
        app.add_middleware(ChaosMiddleware)  # type: ignore[attr-defined]
