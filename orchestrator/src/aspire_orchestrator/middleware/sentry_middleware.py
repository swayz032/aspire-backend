"""Sentry error tracking integration for Aspire Orchestrator.

Optional — if SENTRY_DSN is not set, all functions are no-ops.
PII is stripped from all events before sending (Law #9).

Usage in server.py:
    from aspire_orchestrator.middleware.sentry_middleware import init_sentry
    init_sentry()  # call early, before app starts
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII scrubbing (Law #9)
# ---------------------------------------------------------------------------

# Field names that always contain PII — value replaced with "[Filtered]"
_PII_FIELD_PATTERNS: set[str] = {
    "email", "phone", "ssn", "password", "passwd",
    "secret", "token", "key", "authorization",
    "credit_card", "card_number", "cvv", "api_key",
    "apikey", "access_token", "refresh_token",
    "session_id", "social_security",
}

# Regex patterns for PII values embedded in arbitrary strings
_PII_VALUE_REGEXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'(sk[-_](?:test|live|prod)[-_])\w+'), r'\1***'),
    (re.compile(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'), '***JWT***'),
    (re.compile(r'://\w+:[^@]+@'), '://***:***@'),
    (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), '***@***.***'),
    (re.compile(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'), '***-***-****'),
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '***-**-****'),
    (re.compile(r'Bearer\s+\S+', re.IGNORECASE), 'Bearer ***'),
]

# Paths excluded from performance tracing (noisy, zero diagnostic value)
_HEALTH_PATHS: frozenset[str] = frozenset({
    "/healthz", "/livez", "/readyz", "/metrics",
})


def _is_pii_field(field_name: str) -> bool:
    lower = field_name.lower()
    return any(p in lower for p in _PII_FIELD_PATTERNS)


def _scrub_value(value: str) -> str:
    for pattern, replacement in _PII_VALUE_REGEXES:
        value = pattern.sub(replacement, value)
    return value


def _scrub_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub PII from a dictionary."""
    result: dict[str, Any] = {}
    for k, v in data.items():
        if _is_pii_field(k):
            result[k] = "[Filtered]"
        elif isinstance(v, dict):
            result[k] = _scrub_dict(v)
        elif isinstance(v, list):
            result[k] = [
                _scrub_dict(item) if isinstance(item, dict)
                else _scrub_value(item) if isinstance(item, str)
                else item
                for item in v
            ]
        elif isinstance(v, str):
            result[k] = _scrub_value(v)
        else:
            result[k] = v
    return result


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Strip PII from Sentry events before sending (Law #9)."""
    # Scrub request data (headers, body, query string, cookies)
    request = event.get("request")
    if isinstance(request, dict):
        for section in ("headers", "data", "env"):
            if isinstance(request.get(section), dict):
                request[section] = _scrub_dict(request[section])
        if isinstance(request.get("query_string"), str):
            request["query_string"] = _scrub_value(request["query_string"])
        if "cookies" in request:
            request["cookies"] = "[Filtered]"

    # Scrub exception message strings
    exc_info = event.get("exception")
    if isinstance(exc_info, dict):
        for exc_val in exc_info.get("values", []):
            if isinstance(exc_val, dict) and isinstance(exc_val.get("value"), str):
                exc_val["value"] = _scrub_value(exc_val["value"])

    # Scrub breadcrumbs
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        for bc in breadcrumbs.get("values", []):
            if isinstance(bc, dict):
                if isinstance(bc.get("message"), str):
                    bc["message"] = _scrub_value(bc["message"])
                if isinstance(bc.get("data"), dict):
                    bc["data"] = _scrub_dict(bc["data"])

    # Scrub extra, contexts, tags
    for section in ("extra", "contexts", "tags"):
        if isinstance(event.get(section), dict):
            event[section] = _scrub_dict(event[section])

    # Scrub user data
    if isinstance(event.get("user"), dict):
        event["user"] = _scrub_dict(event["user"])

    return event


def _traces_sampler(sampling_context: dict[str, Any]) -> float:
    """Filter health-check transactions; sample everything else at configured rate."""
    tx_context = sampling_context.get("transaction_context", {})
    name = tx_context.get("name", "")

    if any(name == p or name.startswith(p) for p in _HEALTH_PATHS):
        return 0.0

    return float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))


def init_sentry() -> None:
    """Initialize Sentry SDK if SENTRY_DSN is set. No-op otherwise."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN not set — Sentry error tracking disabled (no-op)")
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        environment = os.getenv("ASPIRE_ENV", "development").strip().lower()

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=os.getenv("ASPIRE_RELEASE", os.getenv("APP_VERSION", "aspire-orchestrator@0.1.0")),
            before_send=_before_send,
            traces_sampler=_traces_sampler,
            send_default_pii=False,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
                LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
            ],
            max_breadcrumbs=50,
            server_name=os.getenv("HOSTNAME", "aspire-orchestrator"),
        )
        logger.info("Sentry initialized: environment=%s", environment)

    except ImportError:
        logger.warning("sentry-sdk not installed — Sentry error tracking disabled")
    except Exception as exc:
        logger.error("Sentry initialization failed: %s", exc)
