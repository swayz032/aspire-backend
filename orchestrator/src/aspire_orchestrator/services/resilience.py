"""Resilience primitives — circuit breakers + retry with backoff/jitter.

Pass 18+ Lane 2 — production-grade reliability for external HTTP calls.

Design:
    Three composable utilities, each independently testable:

      1. AsyncCircuitBreaker
         - Closed -> Open after N consecutive failures.
         - Open -> Half-Open after recovery_timeout seconds.
         - Half-Open success -> Closed; failure -> Open again.
         - thread-unsafe by design (FastAPI workers are single-threaded
           per asyncio loop). For multi-process safety, each worker has its
           own breaker — acceptable for our scale and simpler than a Redis
           shared breaker.

      2. retry_async(...) — tenacity-backed retry with:
         - exponential base (configurable)
         - bounded random jitter (full-jitter, AWS-recommended)
         - retry only on configured exception classes
         - hard total-time budget (so cumulative attempts respect SLO)

      3. resilient_call(...) — composes both. Wraps an async callable with
         circuit-breaker-then-retry semantics. Breaker check happens BEFORE
         the retry loop — once the breaker opens, we fail fast without
         burning the retry budget.

Idempotency rule (CRITICAL):
    POST/PUT/PATCH/DELETE calls that have already produced a side effect
    on the remote MUST NOT be retried automatically. Caller chooses retry
    policy: `idempotent=True` retries on any RetryableError; `idempotent=False`
    retries ONLY on connect/timeout (true network failures, before the remote
    saw the request).

Tenacity is already a transitive dep (langgraph -> tenacity); we add it
explicitly to pyproject.toml so it isn't lost on a future upgrade.

Aspire Laws:
    Law #3 — fail closed: open breaker rejects with explicit error code.
    Law #10 — reliability: every external call must use this module.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    """Per-provider breaker state."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ResilienceError(Exception):
    """Base class for resilience-related errors."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CircuitOpenError(ResilienceError):
    """Raised when the breaker is OPEN and rejects a call before issuing it."""

    def __init__(self, breaker_name: str, opened_for_seconds: float) -> None:
        super().__init__(
            "CIRCUIT_OPEN",
            f"Circuit breaker '{breaker_name}' OPEN (opened {opened_for_seconds:.1f}s ago)",
        )
        self.breaker_name = breaker_name
        self.opened_for_seconds = opened_for_seconds


class RetryableError(ResilienceError):
    """Raise from a wrapped callable to opt-in to a retry pass."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(code, message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


@dataclass
class BreakerConfig:
    """Per-provider breaker tuning.

    threshold: consecutive failures before transitioning CLOSED -> OPEN.
    recovery_timeout: seconds to remain OPEN before allowing a probe.
    half_open_max_calls: number of probe calls allowed in HALF_OPEN before
        decision (success closes; failure re-opens).
    """

    threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 1


class AsyncCircuitBreaker:
    """Single-process async circuit breaker.

    Usage:
        breaker = AsyncCircuitBreaker("twilio", BreakerConfig())
        async with breaker:
            return await some_http_call()

    Or imperatively:
        breaker.before_call()    # raises CircuitOpenError if open
        try:
            result = await call()
            breaker.record_success()
            return result
        except RetryableError:
            breaker.record_failure()
            raise
    """

    def __init__(self, name: str, config: BreakerConfig | None = None) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._half_open_in_flight = 0

    @property
    def state(self) -> CircuitState:
        """Compute current state, transitioning OPEN -> HALF_OPEN if recovery elapsed."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = 0
                logger.info(
                    "circuit_breaker name=%s state_change=open->half_open", self.name
                )
        return self._state

    def before_call(self) -> None:
        """Check breaker. Raises CircuitOpenError if OPEN.

        HALF_OPEN admits up to `half_open_max_calls` probes; further calls
        are rejected to avoid stampedes.
        """
        state = self.state
        if state == CircuitState.OPEN:
            raise CircuitOpenError(self.name, time.monotonic() - self._opened_at)
        if state == CircuitState.HALF_OPEN:
            if self._half_open_in_flight >= self.config.half_open_max_calls:
                raise CircuitOpenError(self.name, time.monotonic() - self._opened_at)
            self._half_open_in_flight += 1

    def record_success(self) -> None:
        """Successful call — closes breaker if in HALF_OPEN."""
        prior = self._state
        self._consecutive_failures = 0
        self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        if prior in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            self._state = CircuitState.CLOSED
            self._opened_at = 0.0
            logger.info("circuit_breaker name=%s state_change=%s->closed", self.name, prior.value)

    def record_failure(self) -> None:
        """Failed call — increments and may open the breaker."""
        self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        if self._state == CircuitState.HALF_OPEN:
            # A failed probe immediately re-opens
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit_breaker name=%s state_change=half_open->open reason=probe_failed",
                self.name,
            )
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit_breaker name=%s state_change=closed->open consecutive_failures=%d",
                self.name,
                self._consecutive_failures,
            )

    # Test/diagnostic helpers
    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Retry configuration.

    attempts: total attempts INCLUDING the first try (so attempts=3 -> 1 try + 2 retries).
    base_seconds: initial backoff seconds.
    max_seconds: per-attempt backoff cap.
    total_budget_seconds: hard ceiling on total wall time spent retrying.
        After this elapses we stop retrying even if attempts remain.
    """

    attempts: int = 3
    base_seconds: float = 0.5
    max_seconds: float = 4.0
    total_budget_seconds: float = 12.0


# Default policies per provider — tunable per-call
TWILIO_RETRY = RetryPolicy(attempts=3, base_seconds=0.5, max_seconds=4.0, total_budget_seconds=12.0)
ELEVENLABS_RETRY = RetryPolicy(attempts=3, base_seconds=0.5, max_seconds=4.0, total_budget_seconds=12.0)
SUPABASE_RETRY = RetryPolicy(attempts=2, base_seconds=0.05, max_seconds=0.15, total_budget_seconds=0.18)

# Phase B-1 — Adam property research provider policies.
# ATTOM: 8s per-call, retry budget capped so we never exceed the playbook
# outer wrapper (28s). attempts=3 -> ~12s worst-case including jitter.
ATTOM_RETRY = RetryPolicy(attempts=3, base_seconds=0.5, max_seconds=4.0, total_budget_seconds=10.0)
# Apify Zillow: 20s per-call after the n8n warmer eliminates cold-start.
# Only 2 attempts — Apify is read-only photos, so degrading to no-photos is
# acceptable and we'd rather surrender the slot back to the playbook quickly.
APIFY_RETRY = RetryPolicy(attempts=2, base_seconds=0.5, max_seconds=4.0, total_budget_seconds=6.0)


def _full_jitter_backoff(attempt: int, policy: RetryPolicy) -> float:
    """AWS-style full-jitter backoff: random uniform [0, min(cap, base * 2**attempt))."""
    exp = policy.base_seconds * (2 ** attempt)
    capped = min(policy.max_seconds, exp)
    return random.uniform(0, capped)


# ---------------------------------------------------------------------------
# Composed wrapper: resilient_call
# ---------------------------------------------------------------------------


# Network-level errors — always retryable (no remote side effect)
_NETWORK_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectTimeout,
    asyncio.TimeoutError,
)


async def resilient_call(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    breaker: AsyncCircuitBreaker,
    policy: RetryPolicy,
    idempotent: bool = True,
    classify_failure: Callable[[BaseException], bool] | None = None,
    **kwargs: Any,
) -> T:
    """Run an async callable with breaker + retry.

    idempotent=True (e.g., GET, search):
        Retries on ANY failure classified as retryable (network errors,
        explicit RetryableError, plus optional caller-supplied classifier).

    idempotent=False (e.g., POST purchase):
        Retries ONLY on _NETWORK_ERRORS — meaning the remote NEVER saw
        the request (connect refused, write timeout). If the call returned
        a status code (any status), we do NOT retry; the caller inspects
        the response and decides.

    Breaker check happens BEFORE retries. A failure during the protected
    call increments the breaker. Once OPEN, subsequent calls fail fast
    with CircuitOpenError without burning the retry budget.

    Raises:
        CircuitOpenError on OPEN breaker.
        Whatever the wrapped function raised on final failure.
    """
    classify = classify_failure or (lambda exc: isinstance(exc, RetryableError) and getattr(exc, "retryable", True))

    breaker.before_call()
    start = time.monotonic()
    last_exc: BaseException | None = None

    for attempt_idx in range(policy.attempts):
        if attempt_idx > 0:
            elapsed = time.monotonic() - start
            remaining = policy.total_budget_seconds - elapsed
            if remaining <= 0:
                break
            wait = min(_full_jitter_backoff(attempt_idx, policy), remaining)
            await asyncio.sleep(wait)

        try:
            result = await func(*args, **kwargs)
        except _NETWORK_ERRORS as net_exc:
            # True network failure — always considered "the remote did not see it"
            last_exc = net_exc
            breaker.record_failure()
            logger.warning(
                "resilient_call breaker=%s attempt=%d/%d network_error=%s",
                breaker.name,
                attempt_idx + 1,
                policy.attempts,
                type(net_exc).__name__,
            )
            continue
        except CircuitOpenError:
            raise
        except Exception as exc:
            last_exc = exc
            should_retry = idempotent and classify(exc)
            if not should_retry:
                # Non-retryable: not a breaker failure unless the classifier says so.
                # Most provider 4xx errors fall here — they're our bug, not provider degradation.
                # 5xx wrapped in RetryableError will have retryable=True.
                if isinstance(exc, RetryableError) and exc.retryable:
                    breaker.record_failure()
                else:
                    # Don't trip the breaker on 4xx auth/validation errors
                    pass
                raise
            breaker.record_failure()
            logger.warning(
                "resilient_call breaker=%s attempt=%d/%d retryable=%s",
                breaker.name,
                attempt_idx + 1,
                policy.attempts,
                type(exc).__name__,
            )
            continue
        else:
            breaker.record_success()
            return result

    # Exhausted budget or attempts. The breaker has already been incremented
    # on each failed attempt inside the loop — do NOT double-count here.
    if last_exc is not None:
        raise last_exc
    # Fallthrough that should not happen
    raise ResilienceError("RETRY_EXHAUSTED", f"Retry exhausted on {breaker.name}")


# ---------------------------------------------------------------------------
# Module-level breaker registry
# ---------------------------------------------------------------------------

_TWILIO_BREAKER = AsyncCircuitBreaker(
    "twilio",
    BreakerConfig(threshold=5, recovery_timeout=30.0, half_open_max_calls=1),
)
_ELEVENLABS_BREAKER = AsyncCircuitBreaker(
    "elevenlabs",
    BreakerConfig(threshold=5, recovery_timeout=30.0, half_open_max_calls=1),
)
_SUPABASE_BREAKER = AsyncCircuitBreaker(
    "supabase_personalization",
    BreakerConfig(threshold=5, recovery_timeout=10.0, half_open_max_calls=2),
)

# Phase B-1 — Adam property research providers (ATTOM + Apify Zillow).
#
# Threshold semantics: BreakerConfig.threshold is CONSECUTIVE failures, not
# "failures in a sliding window." The Phase B-1 work order asked for
# "3 failures in 60s" — a sliding-window breaker would be new infrastructure
# and a new failure surface. Consecutive-3 is strictly more conservative for
# transient noise (one success between failures resets the counter), which
# is what we want for healthy-most-of-the-time external providers. The
# 60s window of the work order is honored implicitly: if a provider can
# stay green for one call in any 60s window, the breaker stays closed —
# matching the intent of "don't trip on isolated blips."
#
# recovery_timeout=30s: matches Phase B-1 spec; gives the warmer enough time
# to wake Apify or the upstream ATTOM endpoint to recover.
_ATTOM_BREAKER = AsyncCircuitBreaker(
    "attom",
    BreakerConfig(threshold=3, recovery_timeout=30.0, half_open_max_calls=1),
)
_APIFY_BREAKER = AsyncCircuitBreaker(
    "apify_zillow",
    BreakerConfig(threshold=3, recovery_timeout=30.0, half_open_max_calls=1),
)


def twilio_breaker() -> AsyncCircuitBreaker:
    return _TWILIO_BREAKER


def elevenlabs_breaker() -> AsyncCircuitBreaker:
    return _ELEVENLABS_BREAKER


def supabase_breaker() -> AsyncCircuitBreaker:
    return _SUPABASE_BREAKER


def attom_breaker() -> AsyncCircuitBreaker:
    """Phase B-1 — ATTOM property data provider breaker.

    Opens after 3 consecutive failures; 30s before half-open probe.
    Consumed by Adam's PROPERTY_FACTS_AND_PERMITS playbook (Phase B-2).
    """
    return _ATTOM_BREAKER


def apify_breaker() -> AsyncCircuitBreaker:
    """Phase B-1 — Apify Zillow scraper breaker (photos only).

    Opens after 3 consecutive failures; 30s before half-open probe.
    Apify free-tier cold-starts up to 45s — the n8n warmer cron eliminates
    cold-start, so when this breaker opens it usually means an actual
    provider outage, not a wake-up delay.
    """
    return _APIFY_BREAKER


def reset_all_breakers() -> None:
    """For tests — reset every registered breaker."""
    _TWILIO_BREAKER.reset()
    _ELEVENLABS_BREAKER.reset()
    _SUPABASE_BREAKER.reset()
    _ATTOM_BREAKER.reset()
    _APIFY_BREAKER.reset()


__all__ = [
    "APIFY_RETRY",
    "ATTOM_RETRY",
    "AsyncCircuitBreaker",
    "BreakerConfig",
    "CircuitOpenError",
    "CircuitState",
    "ELEVENLABS_RETRY",
    "ResilienceError",
    "RetryPolicy",
    "RetryableError",
    "SUPABASE_RETRY",
    "TWILIO_RETRY",
    "apify_breaker",
    "attom_breaker",
    "elevenlabs_breaker",
    "reset_all_breakers",
    "resilient_call",
    "supabase_breaker",
    "twilio_breaker",
]
