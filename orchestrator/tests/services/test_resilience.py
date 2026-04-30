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
