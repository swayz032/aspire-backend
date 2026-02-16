"""Provider Error Codes — Canonical error taxonomy for all provider integrations.

Every provider failure maps to an InternalErrorCode, which is:
  1. Stored in receipts as `reason_code` (Law #2)
  2. Used by circuit breaker to decide open/close
  3. Mapped to HTTP status codes in Gateway responses
  4. Never exposed raw to users — user-facing errors are translated

Categories:
  AUTH    — Authentication/credential failures (auto-retry via OAuth2 refresh)
  NETWORK — Connectivity/timeout (circuit breaker eligible)
  RATE    — Rate limiting (backoff + retry)
  INPUT   — Client-side validation errors (never retry)
  SERVER  — Provider server errors (retry with backoff)
  DOMAIN  — Business logic rejection from provider (never retry)
"""

from __future__ import annotations

from enum import Enum


class ProviderErrorCategory(str, Enum):
    """High-level error category for circuit breaker decisions."""

    AUTH = "auth"
    NETWORK = "network"
    RATE = "rate"
    INPUT = "input"
    SERVER = "server"
    DOMAIN = "domain"


class InternalErrorCode(str, Enum):
    """Canonical error codes for all provider integrations.

    Format: {CATEGORY}_{SPECIFIC_ERROR}
    These appear in receipt `reason_code` fields.
    """

    # --- AUTH errors (credential/token issues) ---
    AUTH_INVALID_KEY = "AUTH_INVALID_KEY"
    AUTH_EXPIRED_TOKEN = "AUTH_EXPIRED_TOKEN"
    AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"
    AUTH_SCOPE_INSUFFICIENT = "AUTH_SCOPE_INSUFFICIENT"
    AUTH_REVOKED = "AUTH_REVOKED"

    # --- NETWORK errors (connectivity) ---
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_CONNECTION_REFUSED = "NETWORK_CONNECTION_REFUSED"
    NETWORK_DNS_FAILURE = "NETWORK_DNS_FAILURE"
    NETWORK_TLS_ERROR = "NETWORK_TLS_ERROR"
    NETWORK_CIRCUIT_OPEN = "NETWORK_CIRCUIT_OPEN"

    # --- RATE errors (throttling) ---
    RATE_LIMITED = "RATE_LIMITED"
    RATE_QUOTA_EXCEEDED = "RATE_QUOTA_EXCEEDED"

    # --- INPUT errors (client-side validation) ---
    INPUT_MISSING_REQUIRED = "INPUT_MISSING_REQUIRED"
    INPUT_INVALID_FORMAT = "INPUT_INVALID_FORMAT"
    INPUT_CONSTRAINT_VIOLATED = "INPUT_CONSTRAINT_VIOLATED"

    # --- SERVER errors (provider-side) ---
    SERVER_INTERNAL_ERROR = "SERVER_INTERNAL_ERROR"
    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
    SERVER_BAD_GATEWAY = "SERVER_BAD_GATEWAY"
    SERVER_RESPONSE_INVALID = "SERVER_RESPONSE_INVALID"

    # --- DOMAIN errors (business logic rejections) ---
    DOMAIN_NOT_FOUND = "DOMAIN_NOT_FOUND"
    DOMAIN_CONFLICT = "DOMAIN_CONFLICT"
    DOMAIN_FORBIDDEN = "DOMAIN_FORBIDDEN"
    DOMAIN_INSUFFICIENT_FUNDS = "DOMAIN_INSUFFICIENT_FUNDS"
    DOMAIN_IDEMPOTENCY_CONFLICT = "DOMAIN_IDEMPOTENCY_CONFLICT"

    @property
    def category(self) -> ProviderErrorCategory:
        """Derive error category from code prefix."""
        prefix = self.value.split("_")[0]
        return ProviderErrorCategory(prefix.lower())

    @property
    def retryable(self) -> bool:
        """Whether this error type is safe to retry."""
        return self.category in (
            ProviderErrorCategory.NETWORK,
            ProviderErrorCategory.RATE,
            ProviderErrorCategory.SERVER,
        )

    @property
    def circuit_breaker_relevant(self) -> bool:
        """Whether this error should count toward circuit breaker threshold."""
        return self.category in (
            ProviderErrorCategory.NETWORK,
            ProviderErrorCategory.SERVER,
        )


# HTTP status code → InternalErrorCode mapping (for provider responses)
HTTP_STATUS_TO_ERROR: dict[int, InternalErrorCode] = {
    400: InternalErrorCode.INPUT_INVALID_FORMAT,
    401: InternalErrorCode.AUTH_INVALID_KEY,
    403: InternalErrorCode.DOMAIN_FORBIDDEN,
    404: InternalErrorCode.DOMAIN_NOT_FOUND,
    409: InternalErrorCode.DOMAIN_CONFLICT,
    422: InternalErrorCode.INPUT_CONSTRAINT_VIOLATED,
    429: InternalErrorCode.RATE_LIMITED,
    500: InternalErrorCode.SERVER_INTERNAL_ERROR,
    502: InternalErrorCode.SERVER_BAD_GATEWAY,
    503: InternalErrorCode.SERVER_UNAVAILABLE,
    504: InternalErrorCode.NETWORK_TIMEOUT,
}


def error_from_http_status(status_code: int) -> InternalErrorCode:
    """Map HTTP status code to the closest InternalErrorCode."""
    if status_code in HTTP_STATUS_TO_ERROR:
        return HTTP_STATUS_TO_ERROR[status_code]
    if 400 <= status_code < 500:
        return InternalErrorCode.INPUT_INVALID_FORMAT
    if 500 <= status_code < 600:
        return InternalErrorCode.SERVER_INTERNAL_ERROR
    return InternalErrorCode.SERVER_RESPONSE_INVALID
