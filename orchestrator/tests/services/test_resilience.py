"""Tests for services/resilience.py — Pass 18+ Lane 2.

Covers:
  - Circuit breaker state transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.
  - Retry behavior on RetryableError (idempotent=True).
  - No retry on non-RetryableError (idempotent=False).
  - Network errors retry regardless of idempotency.
  - Total budget cap halts retries early.
  - Breaker opens after threshold; subsequent calls fail fast with
    CircuitOpenError.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from aspire_orchestrator.services.resilience import (
    AsyncCircuitBreaker,
    BreakerConfig,
    CircuitOpenError,
    CircuitState,
    RetryPolicy,
    RetryableError,
    resilient_call,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CallCounter:
    """Builds an async callable that fails a configurable number of times."""

    def __init__(self, failures_before_success: int, exc_factory) -> None:
        self.calls = 0
        self.fail_n = failures_before_success
        self.exc_factory = exc_factory

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.fail_n:
            raise self.exc_factory()
        return "ok"


def _retryable_503() -> RetryableError:
    return RetryableError("HTTP_503", "service unavailable", retryable=True)


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("conn refused")


def _read_timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("timeout")


# ---------------------------------------------------------------------------
# Circuit breaker — state transitions
# ---------------------------------------------------------------------------


def test_breaker_starts_closed() -> None:
    b = AsyncCircuitBreaker("test", BreakerConfig(threshold=3))
    assert b.state == CircuitState.CLOSED


def test_breaker_opens_after_threshold() -> None:
    b = AsyncCircuitBreaker("test", BreakerConfig(threshold=3))
    for _ in range(3):
        b.record_failure()
    assert b.state == CircuitState.OPEN


def test_breaker_before_call_raises_when_open() -> None:
    b = AsyncCircuitBreaker("test", BreakerConfig(threshold=2))
    b.record_failure()
    b.record_failure()
    assert b.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        b.before_call()


def test_breaker_open_to_half_open_after_recovery() -> None:
    # Use a small but non-zero recovery_timeout so we can observe the OPEN
    # state before auto-transitioning. With recovery_timeout=0 the state
    # property flips on every read, masking the transition.
    b = AsyncCircuitBreaker(
        "test", BreakerConfig(threshold=1, recovery_timeout=0.05)
    )
    b.record_failure()
    assert b.state == CircuitState.OPEN  # within window, stays OPEN
    import time as _time
    _time.sleep(0.06)
    assert b.state == CircuitState.HALF_OPEN  # window elapsed → HALF_OPEN


def test_breaker_half_open_success_closes() -> None:
    b = AsyncCircuitBreaker(
        "test", BreakerConfig(threshold=1, recovery_timeout=0.0)
    )
    b.record_failure()
    _ = b.state  # forces transition to HALF_OPEN
    b.before_call()
    b.record_success()
    assert b.state == CircuitState.CLOSED


def test_breaker_half_open_failure_reopens() -> None:
    # Use a long recovery_timeout so the property doesn't immediately re-flip
    # back to HALF_OPEN after we record a failure in HALF_OPEN.
    b = AsyncCircuitBreaker(
        "test", BreakerConfig(threshold=1, recovery_timeout=60.0)
    )
    b.record_failure()
    # Manually force transition to HALF_OPEN by mutating internal state
    # (production code never does this — this is a testing seam).
    b._state = CircuitState.HALF_OPEN  # type: ignore[attr-defined]
    b.before_call()
    b.record_failure()
    # Now _state is OPEN again with a fresh _opened_at; with recovery_timeout=60
    # the property won't auto-flip to HALF_OPEN.
    assert b.state == CircuitState.OPEN


def test_breaker_success_resets_consecutive_failures() -> None:
    b = AsyncCircuitBreaker("test", BreakerConfig(threshold=3))
    b.record_failure()
    b.record_failure()
    b.record_success()
    # Two more failures should NOT open (counter reset)
    b.record_failure()
    b.record_failure()
    assert b.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# resilient_call — retry semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resilient_call_returns_first_success() -> None:
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=3))
    counter = CallCounter(0, _retryable_503)
    policy = RetryPolicy(attempts=3, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    result = await resilient_call(counter, breaker=breaker, policy=policy, idempotent=True)
    assert result == "ok"
    assert counter.calls == 1


@pytest.mark.asyncio
async def test_resilient_call_retries_idempotent_503() -> None:
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=10))
    counter = CallCounter(2, _retryable_503)
    policy = RetryPolicy(attempts=3, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    result = await resilient_call(counter, breaker=breaker, policy=policy, idempotent=True)
    assert result == "ok"
    assert counter.calls == 3  # 2 fail + 1 success


@pytest.mark.asyncio
async def test_resilient_call_does_not_retry_non_idempotent_retryable() -> None:
    """idempotent=False + non-network failure => no retry, even if RetryableError."""
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=10))
    counter = CallCounter(2, _retryable_503)
    policy = RetryPolicy(attempts=3, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    with pytest.raises(RetryableError):
        await resilient_call(counter, breaker=breaker, policy=policy, idempotent=False)
    assert counter.calls == 1  # no retry


@pytest.mark.asyncio
async def test_resilient_call_retries_network_errors_even_when_non_idempotent() -> None:
    """Network errors retry regardless of idempotent flag (remote never saw the call)."""
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=10))
    counter = CallCounter(2, _connect_error)
    policy = RetryPolicy(attempts=3, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    result = await resilient_call(counter, breaker=breaker, policy=policy, idempotent=False)
    assert result == "ok"
    assert counter.calls == 3


@pytest.mark.asyncio
async def test_resilient_call_timeout_treated_as_network_error() -> None:
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=10))
    counter = CallCounter(1, _read_timeout)
    policy = RetryPolicy(attempts=3, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    result = await resilient_call(counter, breaker=breaker, policy=policy, idempotent=False)
    assert result == "ok"
    assert counter.calls == 2


@pytest.mark.asyncio
async def test_resilient_call_breaker_opens_after_threshold() -> None:
    """Repeated retryable failures eventually open the breaker."""
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=3))
    counter = CallCounter(99, _retryable_503)  # always fails
    policy = RetryPolicy(attempts=4, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    with pytest.raises(RetryableError):
        await resilient_call(counter, breaker=breaker, policy=policy, idempotent=True)
    # 4 retry attempts = 4 failures, threshold=3 => OPEN
    assert breaker.state == CircuitState.OPEN
    # Next call fails fast — without invoking the wrapped function
    counter.calls = 0
    with pytest.raises(CircuitOpenError):
        await resilient_call(counter, breaker=breaker, policy=policy, idempotent=True)
    assert counter.calls == 0


@pytest.mark.asyncio
async def test_resilient_call_total_budget_caps_retries() -> None:
    """A short total budget aborts even if attempts remain."""
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=99))
    counter = CallCounter(99, _retryable_503)
    policy = RetryPolicy(
        attempts=10, base_seconds=0.05, max_seconds=0.1, total_budget_seconds=0.05
    )
    with pytest.raises(RetryableError):
        await resilient_call(counter, breaker=breaker, policy=policy, idempotent=True)
    # Should have attempted fewer than 10
    assert counter.calls < 10


@pytest.mark.asyncio
async def test_resilient_call_passes_args_kwargs() -> None:
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=3))
    policy = RetryPolicy(attempts=1, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)

    seen: dict = {}

    async def fn(x: int, *, y: str) -> str:
        seen["x"] = x
        seen["y"] = y
        return f"{x}-{y}"

    result = await resilient_call(fn, 7, breaker=breaker, policy=policy, y="foo")
    assert result == "7-foo"
    assert seen == {"x": 7, "y": "foo"}


# ---------------------------------------------------------------------------
# Edge: classifier override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resilient_call_custom_classifier() -> None:
    breaker = AsyncCircuitBreaker("test", BreakerConfig(threshold=10))

    class CustomError(Exception):
        pass

    def classify(exc: BaseException) -> bool:
        return isinstance(exc, CustomError)

    state = {"calls": 0}

    async def fn() -> str:
        state["calls"] += 1
        if state["calls"] < 2:
            raise CustomError("transient")
        return "ok"

    policy = RetryPolicy(attempts=3, base_seconds=0.001, max_seconds=0.01, total_budget_seconds=0.5)
    result = await resilient_call(
        fn,
        breaker=breaker,
        policy=policy,
        idempotent=True,
        classify_failure=classify,
    )
    assert result == "ok"
    assert state["calls"] == 2


# ---------------------------------------------------------------------------
# Phase B-1 — attom_breaker() + apify_breaker() factories
# ---------------------------------------------------------------------------
#
# These cover the contract the Phase B-2 playbook refactor relies on:
#   threshold=3 (consecutive failures), recovery_timeout=30s.
# We avoid sleeping 30s in tests by verifying the CONFIG independently of
# verifying recovery BEHAVIOR (recovery is already covered by the existing
# test_breaker_open_to_half_open_after_recovery test). This split keeps the
# suite fast while still asserting both halves of the contract.

from aspire_orchestrator.services.resilience import (  # noqa: E402
    APIFY_RETRY,
    ATTOM_RETRY,
    apify_breaker,
    attom_breaker,
    reset_all_breakers,
)


@pytest.fixture(autouse=True)
def _reset_breakers_around_factory_tests():
    """Adam breakers are module-level singletons; reset before AND after each
    test in this file's factory section so leftover failure counts from one
    test never contaminate the next. The autouse fixture applies to every
    test in this module but is idempotent for the earlier tests which use
    locally-constructed AsyncCircuitBreaker instances.
    """
    reset_all_breakers()
    yield
    reset_all_breakers()


def test_attom_breaker_is_singleton() -> None:
    """Factory returns the same instance every call (module-level singleton)."""
    a = attom_breaker()
    b = attom_breaker()
    assert a is b
    assert a.name == "attom"


def test_apify_breaker_is_singleton() -> None:
    a = apify_breaker()
    b = apify_breaker()
    assert a is b
    assert a.name == "apify_zillow"


def test_attom_breaker_config_matches_phase_b1_spec() -> None:
    """Phase B-1 spec: threshold=3, recovery_timeout=30.0, half_open=1."""
    b = attom_breaker()
    assert b.config.threshold == 3
    assert b.config.recovery_timeout == 30.0
    assert b.config.half_open_max_calls == 1


def test_apify_breaker_config_matches_phase_b1_spec() -> None:
    b = apify_breaker()
    assert b.config.threshold == 3
    assert b.config.recovery_timeout == 30.0
    assert b.config.half_open_max_calls == 1


def test_attom_breaker_opens_after_3_consecutive_failures() -> None:
    b = attom_breaker()
    assert b.state == CircuitState.CLOSED
    b.record_failure()
    b.record_failure()
    assert b.state == CircuitState.CLOSED  # not yet at threshold
    b.record_failure()
    assert b.state == CircuitState.OPEN


def test_apify_breaker_opens_after_3_consecutive_failures() -> None:
    b = apify_breaker()
    assert b.state == CircuitState.CLOSED
    for _ in range(3):
        b.record_failure()
    assert b.state == CircuitState.OPEN


def test_attom_breaker_success_resets_failure_counter() -> None:
    """One success between failures must reset the consecutive-failure count
    so isolated blips don't open the breaker."""
    b = attom_breaker()
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state == CircuitState.CLOSED  # counter reset by the success


def test_apify_breaker_success_resets_failure_counter() -> None:
    b = apify_breaker()
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state == CircuitState.CLOSED


def test_attom_and_apify_breakers_are_independent() -> None:
    """Failures on ATTOM must not open the Apify breaker (and vice versa)."""
    a = attom_breaker()
    p = apify_breaker()
    for _ in range(3):
        a.record_failure()
    assert a.state == CircuitState.OPEN
    assert p.state == CircuitState.CLOSED


def test_reset_all_breakers_resets_new_adam_breakers() -> None:
    """Verify reset_all_breakers() was wired up for the new factories so
    test suites that rely on it don't leak state across tests."""
    a = attom_breaker()
    p = apify_breaker()
    for _ in range(3):
        a.record_failure()
        p.record_failure()
    assert a.state == CircuitState.OPEN
    assert p.state == CircuitState.OPEN
    reset_all_breakers()
    assert a.state == CircuitState.CLOSED
    assert p.state == CircuitState.CLOSED


def test_attom_retry_policy_bounded_for_28s_wrapper() -> None:
    """ATTOM_RETRY total_budget_seconds must fit comfortably under the
    Phase B-1 outer wrapper (28s) so a single bad ATTOM call cannot
    exhaust the playbook budget."""
    assert ATTOM_RETRY.total_budget_seconds <= 12.0
    assert ATTOM_RETRY.attempts >= 2


def test_apify_retry_policy_bounded_for_28s_wrapper() -> None:
    """APIFY_RETRY must be tighter than ATTOM (photos are nice-to-have,
    facts are not). Total budget must leave room for ATTOM in parallel."""
    assert APIFY_RETRY.total_budget_seconds <= 10.0
    assert APIFY_RETRY.attempts >= 2
