"""
Sentry integration for the Aspire orchestrator (FastAPI).

Optional: no-op if SENTRY_DSN is not set. Strips PII per Law #9.
"""
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PII_FIELDS: set[str] = {
    "email", "phone", "ssn", "password", "secret", "token",
    "key", "api_key", "apikey", "authorization", "credit_card",
    "card_number", "cvv", "social_security",
}


def _strip_pii(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Remove PII fields from Sentry events before transmission."""
    if "request" in event and "data" in event["request"]:
        data = event["request"]["data"]
        if isinstance(data, dict):
            for field in list(data.keys()):
                if field.lower() in _PII_FIELDS:
                    data[field] = "[REDACTED]"
    return event


def _filter_transactions(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Filter out health check transactions to reduce noise."""
    if event.get("type") == "transaction":
        transaction = event.get("transaction", "")
        if any(p in transaction for p in ("/healthz", "/readyz", "/health")):
            return None
    return _strip_pii(event, hint)


def init_sentry() -> None:
    """Initialize Sentry if SENTRY_DSN is configured. No-op otherwise."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN not set — Sentry disabled")
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.1,
            before_send=_filter_transactions,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            environment=os.getenv("ENVIRONMENT", "development"),
            release=os.getenv("APP_VERSION", "unknown"),
        )
        logger.info("Sentry initialized (DSN set, traces_sample_rate=0.1)")
    except ImportError:
        logger.warning("sentry-sdk not installed — Sentry disabled")
    except Exception as e:
        logger.error("Failed to initialize Sentry: %s", e)
