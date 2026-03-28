"""Shared test fixtures for the Aspire orchestrator test suite."""

import os
import uuid

# MUST be set before any app/middleware imports — rate_limiter reads at import time
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

import pytest

# Set signing key for token_mint tests — fail-closed requires this (Law #3)
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")


@pytest.fixture(autouse=True)
def _clean_approval_state():
    """Reset approval service state between tests to prevent replay detection leakage."""
    from aspire_orchestrator.services.approval_service import clear_used_request_ids
    from aspire_orchestrator.services.presence_service import clear_presence_revocations

    clear_used_request_ids()
    clear_presence_revocations()
    yield
    clear_used_request_ids()
    clear_presence_revocations()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset rate limiter state AND limits between tests.

    Without this, test modules sharing a TestClient accumulate requests against
    the sliding window, causing later tests to get 429 instead of expected responses.

    Critical: _ENDPOINT_LIMITS hardcodes /v1/intents to 100 — we override it to
    match the test rate limit so endpoint-specific caps don't mask real failures.
    """
    import aspire_orchestrator.middleware.rate_limiter as rl

    test_limit = int(os.environ.get("ASPIRE_RATE_LIMIT", "100000"))

    # Reset window state
    rl._window = rl._SlidingWindow()
    rl._last_cleanup = 0.0

    # Override per-endpoint limits to match test limit (prevents 429 masking)
    saved_endpoint_limits = dict(rl._ENDPOINT_LIMITS)
    rl._ENDPOINT_LIMITS = {k: test_limit for k in saved_endpoint_limits}

    yield

    # Restore originals
    rl._window = rl._SlidingWindow()
    rl._last_cleanup = 0.0
    rl._ENDPOINT_LIMITS = saved_endpoint_limits


@pytest.fixture
def suite_id() -> str:
    """Test suite_id (tenant A) — premium Aspire display format (migration 063)."""
    return "STE-0001"


@pytest.fixture
def suite_id_b() -> str:
    """Test suite_id (tenant B) — for cross-tenant isolation tests."""
    return "STE-0002"


@pytest.fixture
def office_id() -> str:
    """Test office_id — premium Aspire display format (migration 063)."""
    return "OFF-0001"


@pytest.fixture
def correlation_id() -> str:
    """Test correlation_id for tracing."""
    return str(uuid.uuid4())


@pytest.fixture
def request_id() -> str:
    """Test request_id for idempotency."""
    return str(uuid.uuid4())
