"""Global exception handler middleware (Wave 1B — F4 fix).

Catches any unhandled exception in the request pipeline and:
1. Creates an incident record (per incident_packet.schema.json)
2. Stores a receipt for the failure (Law #2)
3. Returns a sanitized 500 response with correlation_id (Law #9 — no PII)
4. Logs the exception for observability (Gate 2)

Mount BEFORE CORS middleware in server.py so it wraps all routes.
"""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Stable error codes from provider_error_taxonomy.md
_INTERNAL_ERROR_CODE = "INTERNAL_SERVER_ERROR"


class GlobalExceptionMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions, create incident + receipt, return sanitized 500."""

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            return await self._handle_exception(request, exc)

    async def _handle_exception(self, request: Request, exc: Exception) -> JSONResponse:
        """Create incident, store receipt, return safe error response."""
        # Use correlation ID from contextvar (set by CorrelationIdMiddleware)
        # Fall back to header/uuid if middleware hasn't run yet
        from aspire_orchestrator.middleware.correlation import get_correlation_id
        correlation_id = get_correlation_id() or request.headers.get("x-correlation-id") or str(uuid.uuid4())
        # ALWAYS use "system" for exception receipts — never trust x-suite-id
        # in the exception path (THREAT-002: prevents cross-tenant incident poisoning)
        suite_id = "system"
        # Exception handler is system-level — validate actor_id format (R-003 fix)
        # Only accept UUID-like patterns or known system actor prefixes
        import re
        raw_actor = request.headers.get("x-actor-id", "")
        if raw_actor and re.match(r'^[a-zA-Z0-9_-]{1,128}$', raw_actor):
            actor_id = raw_actor
        else:
            actor_id = "system_error_handler"
        now = datetime.now(timezone.utc).isoformat()
        incident_id = str(uuid.uuid4())

        # Build sanitized error message (Law #9: no PII, no secrets, no stack traces)
        safe_message = _sanitize_error_message(str(exc))

        # Log full exception for debugging (internal logs only)
        logger.exception(
            "Unhandled exception [correlation_id=%s, path=%s, method=%s]: %s",
            correlation_id,
            request.url.path,
            request.method,
            exc,
        )

        # Build incident record (per incident_packet.schema.json)
        incident = {
            "incident_id": incident_id,
            "suite_id": suite_id,
            "state": "open",
            "severity": "high",
            "title": f"Unhandled exception on {request.method} {request.url.path}",
            "correlation_id": correlation_id,
            "first_seen": now,
            "last_seen": now,
            "timeline": [
                {
                    "timestamp": now,
                    "event": "exception.raised",
                    "detail": safe_message,
                }
            ],
            "evidence_pack": {
                "path": str(request.url.path),
                "method": request.method,
                "exception_type": type(exc).__name__,
                "error_code": _INTERNAL_ERROR_CODE,
            },
        }

        # Register incident in admin store (best-effort)
        # Imports at module load would create circular dependency (admin imports server imports admin).
        # Lazy import is intentional — but failures are logged with full stack trace.
        try:
            from aspire_orchestrator.routes.admin import register_incident
            register_incident(incident)
        except Exception as reg_err:
            logger.exception("Failed to register incident %s", incident_id)

        # Store receipt for the failure (Law #2: receipt for all)
        receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": "system",
            "actor_type": "system",
            "actor_id": actor_id,
            "action_type": f"http.{request.method.lower()}.{request.url.path}",
            "risk_tier": "green",
            "tool_used": "http_handler",
            "outcome": "FAILED",
            "reason_code": _INTERNAL_ERROR_CODE,
            "error_message": safe_message,
            "receipt_type": "exception",
            "created_at": now,
            "incident_id": incident_id,
        }

        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([receipt])
        except Exception as store_err:
            logger.exception("Failed to store exception receipt")
            # Fallback: write receipt to stderr for container log collection (Law #2 last resort)
            import sys
            import json as _json
            try:
                print(_json.dumps({"fallback_receipt": receipt}), file=sys.stderr)
            except Exception:
                pass  # Truly catastrophic — nothing more we can do

        # Return sanitized response (Law #9: never expose internals)
        return JSONResponse(
            status_code=500,
            content={
                "error": _INTERNAL_ERROR_CODE,
                "message": "An internal error occurred. The incident has been logged.",
                "correlation_id": correlation_id,
                "incident_id": incident_id,
            },
            headers={"X-Correlation-Id": correlation_id},
        )


def _sanitize_error_message(msg: str) -> str:
    """Remove PII, secrets, and sensitive details from error messages.

    Law #9: Never log secrets. Redact PII.
    """
    # Truncate to reasonable length
    if len(msg) > 500:
        msg = msg[:500] + "...(truncated)"

    # Redact common secret patterns
    import re
    # API keys (Stripe, OpenAI, etc.)
    msg = re.sub(r'(sk[-_](?:test|live|prod)[-_])\w+', r'\1***REDACTED***', msg)
    msg = re.sub(r'(key[-_=:]\s*["\']?)\w{20,}', r'\1***REDACTED***', msg, flags=re.IGNORECASE)
    # Connection strings with passwords
    msg = re.sub(r'://\w+:[^@]+@', '://***:***@', msg)
    # JWT tokens
    msg = re.sub(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '***JWT_REDACTED***', msg)
    # OAuth Bearer tokens (Google ya29.*, GitHub gho_*, etc.)
    msg = re.sub(r'Bearer\s+[A-Za-z0-9._\-]+', 'Bearer ***REDACTED***', msg, flags=re.IGNORECASE)
    msg = re.sub(r'access_token=[A-Za-z0-9._\-]+', 'access_token=***REDACTED***', msg, flags=re.IGNORECASE)
    msg = re.sub(r'(ya29\.|gho_|ghp_|ghs_)\S+', r'***OAUTH_REDACTED***', msg)

    return msg
