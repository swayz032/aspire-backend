"""Correlation ID + Trace Context middleware (Wave 2A + Phase 2 OTel).

Extracts or generates a correlation ID for every request and makes it available
via contextvars so ALL code paths (provider calls, receipts, incidents, logs)
can access it without explicit parameter threading.

Also generates lightweight W3C-compatible trace context (trace_id, span_id,
parent_span_id) for receipt tracing without requiring the full OpenTelemetry SDK.

Flow:
1. Extract X-Correlation-Id from request header (per OpenAPI CorrelationIdHeader)
2. If missing, generate a new UUID
3. Extract or generate trace_id + span_id (W3C traceparent format)
4. Store in ContextVars (thread/async-safe)
5. Set headers on response
6. All downstream code calls get_correlation_id() / get_trace_id() / get_span_id()

Mount AFTER GlobalExceptionMiddleware (so exceptions also have correlation IDs).
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Paths to skip access logging (noisy health checks)
_SKIP_ACCESS_LOG = {"/healthz", "/livez", "/readyz", "/metrics"}

# ContextVar for correlation ID — accessible from any code path in the same request
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

# Trace context vars (W3C traceparent compatible)
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)
_span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "span_id", default=""
)
_parent_span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "parent_span_id", default=""
)


def get_correlation_id() -> str:
    """Get the current request's correlation ID.

    Returns empty string if called outside a request context.
    All services (receipt_store, provider_call_logger, exception_handler)
    should use this instead of threading correlation_id through params.
    """
    return _correlation_id_var.get()


def set_correlation_id(correlation_id: str) -> None:
    """Set the correlation ID for the current context.

    Primarily used by the middleware, but also useful for background tasks
    that need to establish their own correlation context.
    """
    _correlation_id_var.set(correlation_id)


def get_trace_id() -> str:
    """Get the current request's trace ID (32-hex-char W3C format)."""
    return _trace_id_var.get()


def get_span_id() -> str:
    """Get the current request's span ID (16-hex-char W3C format)."""
    return _span_id_var.get()


def get_parent_span_id() -> str:
    """Get the parent span ID if this request was part of a trace chain."""
    return _parent_span_id_var.get()


def _generate_trace_id() -> str:
    """Generate a 32-character hex trace ID (W3C compatible)."""
    return uuid.uuid4().hex


def _generate_span_id() -> str:
    """Generate a 16-character hex span ID (W3C compatible)."""
    return os.urandom(8).hex()


def _parse_traceparent(header: str) -> tuple[str, str] | None:
    """Parse W3C traceparent header: 00-{trace_id}-{parent_span_id}-{flags}.

    Returns (trace_id, parent_span_id) or None if invalid.
    """
    parts = header.strip().split("-")
    if len(parts) != 4:
        return None
    _, trace_id, parent_span_id, _ = parts
    if len(trace_id) != 32 or len(parent_span_id) != 16:
        return None
    return trace_id, parent_span_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Extract/generate X-Correlation-Id and propagate via contextvars."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract from header or generate new
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

        # Sanitize: strip CRLF to prevent HTTP response splitting (THREAT-001)
        correlation_id = correlation_id.replace("\r", "").replace("\n", "")

        # Trace context: parse W3C traceparent or generate new
        parent_span_id = ""
        traceparent = request.headers.get("traceparent", "")
        parsed = _parse_traceparent(traceparent) if traceparent else None
        if parsed:
            trace_id, parent_span_id = parsed
        else:
            trace_id = _generate_trace_id()
        span_id = _generate_span_id()

        # Store in contextvars for all downstream code
        corr_token = _correlation_id_var.set(correlation_id)
        trace_token = _trace_id_var.set(trace_id)
        span_token = _span_id_var.set(span_id)
        parent_token = _parent_span_id_var.set(parent_span_id)
        start = time.monotonic()

        try:
            response = await call_next(request)
            # Always set correlation ID + trace context on response
            response.headers["X-Correlation-Id"] = correlation_id
            response.headers["X-Trace-Id"] = trace_id
            response.headers["X-Span-Id"] = span_id
            # W3C traceparent for downstream propagation
            response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"

            # Access log (skip noisy health endpoints)
            if request.url.path not in _SKIP_ACCESS_LOG:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                logger.info(
                    "request completed",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "duration_ms": duration_ms,
                        "trace_id": trace_id,
                        "span_id": span_id,
                    },
                )

            return response
        finally:
            # Reset contextvars to prevent leakage between requests
            _correlation_id_var.reset(corr_token)
            _trace_id_var.reset(trace_token)
            _span_id_var.reset(span_token)
            _parent_span_id_var.reset(parent_token)
