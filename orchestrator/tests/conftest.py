"""Shared test fixtures for the Aspire orchestrator test suite."""

import os
import uuid

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
