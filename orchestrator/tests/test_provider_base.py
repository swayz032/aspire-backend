"""Tests for provider base class, error codes, circuit breaker, and OAuth2 manager.

Wave 0 shared infrastructure validation:
  - Error code taxonomy and properties
  - Circuit breaker state machine
  - Provider base class request/response handling
  - OAuth2 token management
  - Receipt data generation
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    CircuitBreaker,
    CircuitState,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import (
    HTTP_STATUS_TO_ERROR,
    InternalErrorCode,
    ProviderErrorCategory,
    error_from_http_status,
)
from aspire_orchestrator.providers.oauth2_manager import (
    OAuth2Config,
    OAuth2Manager,
    OAuth2Token,
)


# =============================================================================
# Error Codes
# =============================================================================


class TestInternalErrorCode:
    """Test error code taxonomy."""

    def test_all_codes_have_category(self):
        """Every error code must map to a valid category."""
        for code in InternalErrorCode:
            assert isinstance(code.category, ProviderErrorCategory)

    def test_auth_codes_not_retryable(self):
        """Auth errors should not be retried."""
        auth_codes = [c for c in InternalErrorCode if c.category == ProviderErrorCategory.AUTH]
        assert len(auth_codes) >= 3
        for code in auth_codes:
            assert not code.retryable

    def test_network_codes_retryable(self):
        """Network errors should be retried."""
        network_codes = [c for c in InternalErrorCode if c.category == ProviderErrorCategory.NETWORK]
        for code in network_codes:
            if code != InternalErrorCode.NETWORK_CIRCUIT_OPEN:
                assert code.retryable

    def test_input_codes_not_retryable(self):
        """Input validation errors should not be retried."""
        input_codes = [c for c in InternalErrorCode if c.category == ProviderErrorCategory.INPUT]
        for code in input_codes:
            assert not code.retryable

    def test_domain_codes_not_retryable(self):
        """Domain/business errors should not be retried."""
        domain_codes = [c for c in InternalErrorCode if c.category == ProviderErrorCategory.DOMAIN]
        for code in domain_codes:
            assert not code.retryable

    def test_server_codes_retryable(self):
        """Server errors should be retried."""
        server_codes = [c for c in InternalErrorCode if c.category == ProviderErrorCategory.SERVER]
        for code in server_codes:
            assert code.retryable

    def test_circuit_breaker_relevance(self):
        """Only network and server errors count for circuit breaker."""
        for code in InternalErrorCode:
            if code.category in (ProviderErrorCategory.NETWORK, ProviderErrorCategory.SERVER):
                assert code.circuit_breaker_relevant
            else:
                assert not code.circuit_breaker_relevant


class TestHttpStatusMapping:
    """Test HTTP status to error code mapping."""

    def test_standard_mappings(self):
        assert error_from_http_status(400) == InternalErrorCode.INPUT_INVALID_FORMAT
        assert error_from_http_status(401) == InternalErrorCode.AUTH_INVALID_KEY
        assert error_from_http_status(403) == InternalErrorCode.DOMAIN_FORBIDDEN
        assert error_from_http_status(404) == InternalErrorCode.DOMAIN_NOT_FOUND
        assert error_from_http_status(429) == InternalErrorCode.RATE_LIMITED
        assert error_from_http_status(500) == InternalErrorCode.SERVER_INTERNAL_ERROR
        assert error_from_http_status(503) == InternalErrorCode.SERVER_UNAVAILABLE

    def test_unknown_4xx(self):
        assert error_from_http_status(418) == InternalErrorCode.INPUT_INVALID_FORMAT

    def test_unknown_5xx(self):
        assert error_from_http_status(599) == InternalErrorCode.SERVER_INTERNAL_ERROR

    def test_unknown_other(self):
        assert error_from_http_status(300) == InternalErrorCode.SERVER_RESPONSE_INVALID


# =============================================================================
# Circuit Breaker
# =============================================================================


class TestCircuitBreaker:
    """Test circuit breaker state machine."""

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_under_threshold(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_rejects_requests(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        with pytest.raises(ProviderError) as exc_info:
            cb.check()
        assert exc_info.value.code == InternalErrorCode.NETWORK_CIRCUIT_OPEN

    def test_half_open_after_recovery(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

    def test_success_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_window_clears_old_failures(self):
        cb = CircuitBreaker(failure_threshold=3, window_s=0.05)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.06)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED  # Old failures expired


# =============================================================================
# Provider Response
# =============================================================================


class TestProviderResponse:
    """Test ProviderResponse receipt data generation."""

    def test_receipt_data_success(self):
        resp = ProviderResponse(
            status_code=200,
            body={"id": "inv_123"},
            success=True,
            provider_request_id="req_abc",
            latency_ms=42.5,
        )
        data = resp.receipt_data
        assert data["provider_status_code"] == 200
        assert data["provider_request_id"] == "req_abc"
        assert data["latency_ms"] == 42.5
        assert data["error_code"] is None

    def test_receipt_data_error(self):
        resp = ProviderResponse(
            status_code=429,
            body={"error": "rate_limit"},
            success=False,
            error_code=InternalErrorCode.RATE_LIMITED,
            error_message="Too many requests",
        )
        data = resp.receipt_data
        assert data["error_code"] == "RATE_LIMITED"


# =============================================================================
# Base Provider Client (via concrete subclass)
# =============================================================================


class _TestClient(BaseProviderClient):
    """Concrete test implementation of BaseProviderClient."""

    provider_id = "test_provider"
    base_url = "https://api.test.com"
    timeout_seconds = 5.0
    max_retries = 1

    async def _authenticate_headers(self, request: ProviderRequest) -> dict[str, str]:
        return {"Authorization": "Bearer test-key"}


class TestBaseProviderClient:
    """Test base provider client functionality."""

    def test_make_receipt_data_success(self):
        client = _TestClient()
        receipt = client.make_receipt_data(
            correlation_id="corr-123",
            suite_id="suite-abc",
            office_id="office-xyz",
            tool_id="test.action",
            risk_tier="green",
            outcome=Outcome.SUCCESS,
            reason_code="EXECUTED",
        )
        assert receipt["correlation_id"] == "corr-123"
        assert receipt["suite_id"] == "suite-abc"
        assert receipt["outcome"] == "success"
        assert receipt["actor_id"] == "provider.test_provider"
        assert receipt["action_type"] == "execute.test.action"
        assert receipt["receipt_type"] == "tool_execution"

    def test_make_receipt_data_failure(self):
        client = _TestClient()
        receipt = client.make_receipt_data(
            correlation_id="corr-456",
            suite_id="suite-def",
            office_id="office-uvw",
            tool_id="test.action",
            risk_tier="yellow",
            outcome=Outcome.FAILED,
            reason_code="NETWORK_TIMEOUT",
            capability_token_id="tok-id",
            capability_token_hash="tok-hash",
        )
        assert receipt["outcome"] == "failed"
        assert receipt["capability_token_id"] == "tok-id"
        assert receipt["capability_token_hash"] == "tok-hash"

    def test_make_receipt_data_with_provider_response(self):
        client = _TestClient()
        resp = ProviderResponse(
            status_code=200, body={}, success=True,
            provider_request_id="req_x", latency_ms=55.0,
        )
        receipt = client.make_receipt_data(
            correlation_id="c", suite_id="s", office_id="o",
            tool_id="t", risk_tier="green",
            outcome=Outcome.SUCCESS, reason_code="EXECUTED",
            provider_response=resp,
        )
        assert "provider_metadata" in receipt
        assert receipt["provider_metadata"]["latency_ms"] == 55.0

    def test_idempotency_key_deterministic(self):
        client = _TestClient()
        req = ProviderRequest(
            method="POST", path="/test",
            body={"amount": 100},
            suite_id="suite-1",
            correlation_id="corr-1",
        )
        key1 = client._compute_idempotency_key(req)
        key2 = client._compute_idempotency_key(req)
        assert key1 == key2
        assert len(key1) == 32  # SHA256 truncated

    def test_idempotency_key_explicit(self):
        client = _TestClient()
        req = ProviderRequest(
            method="POST", path="/test",
            body={"amount": 100},
            idempotency_key="my-custom-key",
        )
        assert client._compute_idempotency_key(req) == "my-custom-key"

    def test_backoff_increases(self):
        client = _TestClient()
        b0 = client._backoff_seconds(0)
        b1 = client._backoff_seconds(1)
        b2 = client._backoff_seconds(2)
        # With jitter, exact values vary, but base increases
        assert b0 < 3  # 2^0 = 1 + jitter
        assert b2 > b0  # Higher attempts = longer backoff


# =============================================================================
# OAuth2 Manager
# =============================================================================


class TestOAuth2Token:
    """Test OAuth2Token properties."""

    def test_expired(self):
        token = OAuth2Token(
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() - 10,
        )
        assert token.expired
        assert token.needs_refresh

    def test_not_expired(self):
        token = OAuth2Token(
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
        )
        assert not token.expired
        assert not token.needs_refresh

    def test_needs_refresh_within_threshold(self):
        token = OAuth2Token(
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 200,  # < 300s threshold
        )
        assert not token.expired
        assert token.needs_refresh

    def test_remaining_seconds(self):
        token = OAuth2Token(
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 100,
        )
        assert 99 <= token.remaining_seconds <= 101


class TestOAuth2Manager:
    """Test OAuth2Manager token management."""

    def _make_config(self) -> OAuth2Config:
        return OAuth2Config(
            provider_id="test",
            client_id="cid",
            client_secret="csecret",
            token_url="https://auth.test.com/token",
        )

    @pytest.mark.asyncio
    async def test_get_token_no_token_raises(self):
        manager = OAuth2Manager(self._make_config())
        with pytest.raises(ProviderError) as exc_info:
            await manager.get_token("suite-missing")
        assert exc_info.value.code == InternalErrorCode.AUTH_EXPIRED_TOKEN

    @pytest.mark.asyncio
    async def test_get_token_valid(self):
        manager = OAuth2Manager(self._make_config())
        manager.set_token(
            "suite-1",
            OAuth2Token(
                access_token="valid_access",
                refresh_token="valid_refresh",
                expires_at=time.time() + 3600,
            ),
        )
        token = await manager.get_token("suite-1")
        assert token.access_token == "valid_access"

    @pytest.mark.asyncio
    async def test_set_token_from_db(self):
        manager = OAuth2Manager(self._make_config())
        manager.set_token_from_db(
            "suite-2",
            access_token="db_access",
            refresh_token="db_refresh",
            expires_at=time.time() + 3600,
        )
        token = await manager.get_token("suite-2")
        assert token.access_token == "db_access"

    def test_clear_token(self):
        manager = OAuth2Manager(self._make_config())
        manager.set_token(
            "suite-3",
            OAuth2Token(
                access_token="x", refresh_token="y",
                expires_at=time.time() + 3600,
            ),
        )
        assert "suite-3" in manager.active_suites
        manager.clear_token("suite-3")
        assert "suite-3" not in manager.active_suites

    @pytest.mark.asyncio
    async def test_refresh_token_success(self):
        manager = OAuth2Manager(self._make_config())
        manager.set_token(
            "suite-4",
            OAuth2Token(
                access_token="old_access",
                refresh_token="valid_refresh",
                expires_at=time.time() + 100,  # Needs refresh
            ),
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_response.content = b'{"access_token": "new_access"}'

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False
        manager._http_client = mock_client

        token = await manager.get_token("suite-4")
        assert token.access_token == "new_access"

    @pytest.mark.asyncio
    async def test_refresh_token_failure(self):
        manager = OAuth2Manager(self._make_config())
        manager.set_token(
            "suite-5",
            OAuth2Token(
                access_token="old_access",
                refresh_token="invalid_refresh",
                expires_at=time.time() + 100,
            ),
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"error": "invalid_grant"}
        mock_response.content = b'{"error": "invalid_grant"}'

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.is_closed = False
        manager._http_client = mock_client

        with pytest.raises(ProviderError) as exc_info:
            await manager.get_token("suite-5")
        assert exc_info.value.code == InternalErrorCode.AUTH_REFRESH_FAILED


# =============================================================================
# Provider Error
# =============================================================================


class TestProviderError:
    """Test ProviderError exception."""

    def test_error_message_format(self):
        err = ProviderError(
            code=InternalErrorCode.AUTH_INVALID_KEY,
            message="Bad key",
            provider_id="stripe",
            status_code=401,
        )
        assert "stripe" in str(err)
        assert "AUTH_INVALID_KEY" in str(err)
        assert err.status_code == 401

    def test_error_without_provider(self):
        err = ProviderError(
            code=InternalErrorCode.NETWORK_TIMEOUT,
            message="Timed out",
        )
        assert err.provider_id == ""
        assert err.status_code is None
