"""Sentry error tracking integration for Aspire Orchestrator.

Optional: if SENTRY_DSN is not set, all functions are no-ops.
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
_initialized = False

# ---------------------------------------------------------------------------
# PII scrubbing (Law #9)
# ---------------------------------------------------------------------------

# Field names that always contain PII; value replaced with "[Filtered]"
_PII_FIELD_PATTERNS: set[str] = {
    "email",
    "phone",
    "ssn",
    "password",
    "passwd",
    "secret",
    "token",
    "key",
    "authorization",
    "credit_card",
    "card_number",
    "cvv",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "session_id",
    "social_security",
}

# Regex patterns for PII values embedded in arbitrary strings
_PII_VALUE_REGEXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(sk[-_](?:test|live|prod)[-_])\w+"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "***JWT***"),
    (re.compile(r"://\w+:[^@]+@"), "://***:***@"),
    (re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "***@***.***"),
    (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "***-***-****"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***-**-****"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer ***"),
]

# Paths excluded from performance tracing (noisy, zero diagnostic value)
_HEALTH_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/livez",
        "/readyz",
        "/metrics",
    }
)


def _is_pii_field(field_name: str) -> bool:
    lower = field_name.lower()
    return any(pattern in lower for pattern in _PII_FIELD_PATTERNS)


def _scrub_value(value: str) -> str:
    for pattern, replacement in _PII_VALUE_REGEXES:
        value = pattern.sub(replacement, value)
    return value


def _scrub_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub PII from a dictionary."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _is_pii_field(key):
            result[key] = "[Filtered]"
        elif isinstance(value, dict):
            result[key] = _scrub_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _scrub_dict(item)
                if isinstance(item, dict)
                else _scrub_value(item)
                if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = _scrub_value(value)
        else:
            result[key] = value
    return result


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Strip PII from Sentry events before sending (Law #9)."""
    del hint

    request = event.get("request")
    if isinstance(request, dict):
        for section in ("headers", "data", "env"):
            if isinstance(request.get(section), dict):
                request[section] = _scrub_dict(request[section])
        if isinstance(request.get("query_string"), str):
            request["query_string"] = _scrub_value(request["query_string"])
        if "cookies" in request:
            request["cookies"] = "[Filtered]"

    exc_info = event.get("exception")
    if isinstance(exc_info, dict):
        for exc_val in exc_info.get("values", []):
            if isinstance(exc_val, dict) and isinstance(exc_val.get("value"), str):
                exc_val["value"] = _scrub_value(exc_val["value"])

    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        for breadcrumb in breadcrumbs.get("values", []):
            if isinstance(breadcrumb, dict):
                if isinstance(breadcrumb.get("message"), str):
                    breadcrumb["message"] = _scrub_value(breadcrumb["message"])
                if isinstance(breadcrumb.get("data"), dict):
                    breadcrumb["data"] = _scrub_dict(breadcrumb["data"])

    for section in ("extra", "contexts", "tags"):
        if isinstance(event.get(section), dict):
            event[section] = _scrub_dict(event[section])

    if isinstance(event.get("user"), dict):
        event["user"] = _scrub_dict(event["user"])

    return event


def _traces_sampler(sampling_context: dict[str, Any]) -> float:
    """Filter health-check transactions; sample everything else at configured rate."""
    tx_context = sampling_context.get("transaction_context", {})
    name = tx_context.get("name", "")

    if any(name == path or name.startswith(path) for path in _HEALTH_PATHS):
        return 0.0

    return float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))


def _resolve_release() -> str:
    candidates = [
        os.getenv("ASPIRE_RELEASE"),
        os.getenv("RAILWAY_GIT_COMMIT_SHA"),
        os.getenv("GITHUB_SHA"),
        os.getenv("SOURCE_VERSION"),
        os.getenv("APP_VERSION"),
    ]
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return "aspire-orchestrator@0.1.0"


def _resolve_dsn() -> str:
    candidates = [
        os.getenv("SENTRY_DSN"),
        os.getenv("SENTRY_BACKEND_DSN"),
    ]
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def init_sentry() -> None:
    """Initialize Sentry SDK if SENTRY_DSN is set. No-op otherwise."""
    global _initialized

    if _initialized:
        return

    dsn = _resolve_dsn()
    if not dsn:
        logger.info(
            "SENTRY_DSN/SENTRY_BACKEND_DSN not set - Sentry error tracking disabled (no-op)"
        )
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        environment = os.getenv("ASPIRE_ENV", "development").strip().lower()

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=_resolve_release(),
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
        _initialized = True
        logger.info("Sentry initialized: environment=%s", environment)
    except ImportError:
        logger.warning("sentry-sdk not installed - Sentry error tracking disabled")
    except Exception as exc:
        logger.error("Sentry initialization failed: %s", exc)
