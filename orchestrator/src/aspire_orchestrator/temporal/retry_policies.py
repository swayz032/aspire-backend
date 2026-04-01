"""Provider-Specific Retry Policies — Enhancement #10.

Maps provider names to Temporal RetryPolicy configurations. Uses the existing
ProviderErrorCategory taxonomy from providers/error_codes.py to classify
non-retryable error types.

One-size-fits-all retries are insufficient:
  - Stripe: generous retries (5 attempts, 2s→30s) — idempotency keys protect
  - PandaDoc: conservative (3 attempts, 5s→120s) — slow API, no idempotency
  - Twilio: moderate (4 attempts, 1s→15s) — fast failures, SMS is time-sensitive
  - QuickBooks: conservative (3 attempts, 3s→60s) — flaky API, long timeouts
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from temporalio.common import RetryPolicy

# Non-retryable error types (AUTH, INPUT, DOMAIN never retry at Temporal level)
NON_RETRYABLE_ERROR_TYPES: Final[list[str]] = [
    "AuthError",
    "ValidationError",
    "DomainError",
    "PolicyDeniedError",
    "SafetyBlockedError",
    "TenantMismatchError",
]


@dataclass(frozen=True)
class ProviderRetryConfig:
    """Retry configuration for a specific provider."""

    max_attempts: int
    initial_interval: timedelta
    max_interval: timedelta
    backoff_coefficient: float = 2.0


# Provider-specific configurations
_PROVIDER_CONFIGS: Final[dict[str, ProviderRetryConfig]] = {
    "stripe": ProviderRetryConfig(
        max_attempts=5,
        initial_interval=timedelta(seconds=2),
        max_interval=timedelta(seconds=30),
    ),
    "pandadoc": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=5),
        max_interval=timedelta(seconds=120),
    ),
    "twilio": ProviderRetryConfig(
        max_attempts=4,
        initial_interval=timedelta(seconds=1),
        max_interval=timedelta(seconds=15),
    ),
    "quickbooks": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=3),
        max_interval=timedelta(seconds=60),
    ),
    "gusto": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=5),
        max_interval=timedelta(seconds=60),
    ),
    "plaid": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=2),
        max_interval=timedelta(seconds=30),
    ),
    "elevenlabs": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=1),
        max_interval=timedelta(seconds=10),
    ),
    "deepgram": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=1),
        max_interval=timedelta(seconds=10),
    ),
    "zoom": ProviderRetryConfig(
        max_attempts=3,
        initial_interval=timedelta(seconds=1),
        max_interval=timedelta(seconds=15),
    ),
}

# Default for unknown providers
_DEFAULT_CONFIG: Final[ProviderRetryConfig] = ProviderRetryConfig(
    max_attempts=3,
    initial_interval=timedelta(seconds=2),
    max_interval=timedelta(seconds=30),
)


def get_retry_policy(provider: str) -> RetryPolicy:
    """Get a Temporal RetryPolicy for a specific provider.

    Uses provider-specific retry config if available, else default.
    AUTH/INPUT/DOMAIN errors are always non-retryable.
    """
    config = _PROVIDER_CONFIGS.get(provider.lower(), _DEFAULT_CONFIG)
    return RetryPolicy(
        initial_interval=config.initial_interval,
        maximum_interval=config.max_interval,
        backoff_coefficient=config.backoff_coefficient,
        maximum_attempts=config.max_attempts,
        non_retryable_error_types=NON_RETRYABLE_ERROR_TYPES,
    )


def get_provider_names() -> list[str]:
    """Return all registered provider names."""
    return list(_PROVIDER_CONFIGS.keys())
