"""Correlation ID middleware (Wave 2A).

Extracts or generates a correlation ID for every request and makes it available
via contextvars so ALL code paths (provider calls, receipts, incidents, logs)
can access it without explicit parameter threading.

Flow:
1. Extract X-Correlation-Id from request header (per OpenAPI CorrelationIdHeader)
2. If missing, generate a new UUID
3. Store in ContextVar (thread/async-safe)
4. Set X-Correlation-Id on response header
5. All downstream code calls get_correlation_id() to access it

Mount AFTER GlobalExceptionMiddleware (so exceptions also have correlation IDs).
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ContextVar for correlation ID — accessible from any code path in the same request
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
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


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Extract/generate X-Correlation-Id and propagate via contextvars."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract from header or generate new
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

        # Sanitize: strip CRLF to prevent HTTP response splitting (THREAT-001)
        correlation_id = correlation_id.replace("\r", "").replace("\n", "")

        # Store in contextvar for all downstream code
        token = _correlation_id_var.set(correlation_id)

        try:
            response = await call_next(request)
            # Always set correlation ID on response (even if it was provided)
            response.headers["X-Correlation-Id"] = correlation_id
            return response
        finally:
            # Reset contextvar to prevent leakage between requests
            _correlation_id_var.reset(token)
