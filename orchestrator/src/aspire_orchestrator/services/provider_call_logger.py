"""Durable provider call logger (Wave 2B — F3 fix).

Logs every external provider API call to Supabase `provider_call_log` table.
Conforms to ProviderCallSummary schema from ops_telemetry_facade.openapi.yaml.

Design:
- log_call() is called AFTER each _request() completes (success or failure)
- Uses stable error codes from provider_error_taxonomy.md
- Logger failures NEVER block the actual provider call (best-effort persistence)
- Singleton via get_provider_call_logger()
- Falls back to in-memory if Supabase unavailable

Fields per OpenAPI contract:
  call_id, correlation_id, provider, action, status, http_status,
  retry_count, started_at, finished_at, error_code, redacted_payload_preview
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# In-memory fallback store (always active)
_call_log_lock = threading.Lock()
_call_log: list[dict[str, Any]] = []

# Supabase client (lazy init)
_supabase_client: Any = None
_supabase_init_done = False
_supabase_init_lock = threading.Lock()


def _init_supabase() -> Any | None:
    """Lazy-init Supabase client for provider call logging."""
    global _supabase_client, _supabase_init_done

    if _supabase_init_done:
        return _supabase_client

    with _supabase_init_lock:
        if _supabase_init_done:
            return _supabase_client

        url = os.environ.get("ASPIRE_SUPABASE_URL", "")
        key = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY", "")

        if url and key:
            try:
                from supabase import create_client
                _supabase_client = create_client(url, key)
                logger.info("ProviderCallLogger: Supabase client initialized")
            except Exception as e:
                logger.warning("ProviderCallLogger: Supabase init failed: %s", e)
                _supabase_client = None
        else:
            logger.info("ProviderCallLogger: No Supabase config — in-memory only")
            _supabase_client = None

        _supabase_init_done = True
        return _supabase_client


def _redact_payload(payload: Any, max_length: int = 200) -> str:
    """Redact and truncate payload for safe logging (Law #9)."""
    if payload is None:
        return ""

    import json
    try:
        text = json.dumps(payload) if not isinstance(payload, str) else payload
    except (TypeError, ValueError):
        text = str(payload)

    # Redact obvious secrets and PII (Law #9)
    import re
    text = re.sub(r'(sk[-_](?:test|live|prod)[-_])\w+', r'\1***', text)
    text = re.sub(r'"(?:password|secret|token|key|auth|api_key|apikey|access_token|refresh_token)":\s*"[^"]*"', '"***":"***REDACTED***"', text, flags=re.IGNORECASE)
    # PII: email addresses
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '***EMAIL***', text)
    # PII: phone numbers (US/international)
    text = re.sub(r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '***PHONE***', text)
    # PII: SSN
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '***SSN***', text)
    # PII: credit card numbers (13-19 digits, optionally grouped)
    text = re.sub(r'\b(?:\d[-\s]?){13,19}\b', '***CC***', text)

    if len(text) > max_length:
        text = text[:max_length] + "...(truncated)"

    return text


class ProviderCallLogger:
    """Logs provider API calls to Supabase + in-memory.

    Thread-safe. Failures never block the calling code path.
    """

    def log_call(
        self,
        *,
        provider: str,
        action: str,
        correlation_id: str,
        suite_id: str = "",
        http_status: int = 0,
        success: bool = False,
        error_code: str = "",
        error_message: str = "",
        retry_count: int = 0,
        latency_ms: float = 0.0,
        request_payload: Any = None,
        response_summary: str = "",
    ) -> str:
        """Log a provider call. Returns call_id."""
        call_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "call_id": call_id,
            "correlation_id": correlation_id,
            "provider": provider,
            "action": action,
            "suite_id": suite_id,
            "status": "success" if success else "error",
            "http_status": http_status,
            "error_code": error_code,
            "error_message": _redact_payload((error_message or "")[:500], max_length=500),
            "retry_count": retry_count,
            "latency_ms": round(latency_ms, 1),
            "redacted_payload_preview": _redact_payload(request_payload),
            "started_at": now,
            "finished_at": now,
        }

        # Always store in-memory (fast, guaranteed)
        with _call_log_lock:
            _call_log.append(record)
            # Cap in-memory store at 10000 entries
            if len(_call_log) > 10000:
                _call_log.pop(0)

        # Best-effort Supabase persistence (never blocks)
        try:
            client = _init_supabase()
            if client:
                client.table("provider_call_log").insert(record).execute()
        except Exception as e:
            logger.warning("ProviderCallLogger: Supabase write failed: %s", e)

        return call_id

    def query_calls(
        self,
        *,
        provider: str | None = None,
        correlation_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query provider calls from in-memory store."""
        with _call_log_lock:
            results = list(_call_log)

        if provider:
            results = [r for r in results if r["provider"] == provider]
        if correlation_id:
            results = [r for r in results if r["correlation_id"] == correlation_id]
        if status:
            results = [r for r in results if r["status"] == status]

        # Most recent first
        results.reverse()
        return results[:limit]

    def clear(self) -> None:
        """Clear in-memory log. Testing only."""
        with _call_log_lock:
            _call_log.clear()


# Module singleton
_logger_instance: ProviderCallLogger | None = None


def get_provider_call_logger() -> ProviderCallLogger:
    """Get the singleton ProviderCallLogger instance."""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = ProviderCallLogger()
    return _logger_instance
