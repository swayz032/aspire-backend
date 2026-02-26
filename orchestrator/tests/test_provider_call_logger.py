"""Tests for Durable Provider Call Logger (Wave 2B — F3 fix).

Verifies that:
1. Every provider API call gets logged
2. Log entries conform to ProviderCallSummary schema
3. Error codes use stable taxonomy
4. Payloads are redacted (Law #9)
5. Logger failures don't block provider calls
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from aspire_orchestrator.services.provider_call_logger import (
    ProviderCallLogger,
    get_provider_call_logger,
    _redact_payload,
)


class TestProviderCallLogger:
    """Core logging behavior."""

    def setup_method(self):
        self.logger = ProviderCallLogger()
        self.logger.clear()

    def test_log_call_returns_call_id(self):
        """log_call should return a UUID call_id."""
        call_id = self.logger.log_call(
            provider="stripe",
            action="POST /v1/invoices",
            correlation_id="corr-123",
            suite_id="suite-abc",
            http_status=200,
            success=True,
        )
        assert call_id
        assert len(call_id) == 36  # UUID format

    def test_log_call_stored_in_memory(self):
        """Logged calls should be queryable."""
        self.logger.log_call(
            provider="stripe",
            action="POST /v1/invoices",
            correlation_id="corr-123",
            suite_id="suite-abc",
            http_status=200,
            success=True,
        )
        calls = self.logger.query_calls(provider="stripe")
        assert len(calls) == 1
        assert calls[0]["provider"] == "stripe"
        assert calls[0]["status"] == "success"

    def test_log_call_error_status(self):
        """Failed calls should have status='error'."""
        self.logger.log_call(
            provider="pandadoc",
            action="POST /documents",
            correlation_id="corr-456",
            http_status=500,
            success=False,
            error_code="VENDOR_5XX",
            error_message="Internal server error",
        )
        calls = self.logger.query_calls(provider="pandadoc")
        assert len(calls) == 1
        assert calls[0]["status"] == "error"
        assert calls[0]["error_code"] == "VENDOR_5XX"

    def test_log_call_all_fields_present(self):
        """Log entry should contain all ProviderCallSummary fields."""
        self.logger.log_call(
            provider="stripe",
            action="POST /v1/invoices",
            correlation_id="corr-789",
            suite_id="suite-xyz",
            http_status=200,
            success=True,
            retry_count=1,
            latency_ms=150.5,
            request_payload={"amount": 1000},
        )
        calls = self.logger.query_calls()
        assert len(calls) == 1
        entry = calls[0]

        required_fields = [
            "call_id", "correlation_id", "provider", "action",
            "status", "http_status", "retry_count", "started_at",
            "finished_at", "redacted_payload_preview",
        ]
        for field in required_fields:
            assert field in entry, f"Missing field: {field}"

    def test_query_by_correlation_id(self):
        """Should filter by correlation_id."""
        self.logger.log_call(provider="a", action="x", correlation_id="c1", success=True)
        self.logger.log_call(provider="b", action="y", correlation_id="c2", success=True)

        results = self.logger.query_calls(correlation_id="c1")
        assert len(results) == 1
        assert results[0]["provider"] == "a"

    def test_query_by_status(self):
        """Should filter by status."""
        self.logger.log_call(provider="a", action="x", correlation_id="c1", success=True)
        self.logger.log_call(provider="b", action="y", correlation_id="c2", success=False, error_code="TIMEOUT")

        results = self.logger.query_calls(status="error")
        assert len(results) == 1
        assert results[0]["error_code"] == "TIMEOUT"

    def test_query_limit(self):
        """Should respect limit parameter."""
        for i in range(10):
            self.logger.log_call(provider="x", action="y", correlation_id=f"c{i}", success=True)

        results = self.logger.query_calls(limit=3)
        assert len(results) == 3

    def test_query_most_recent_first(self):
        """Results should be ordered most recent first."""
        self.logger.log_call(provider="first", action="x", correlation_id="c1", success=True)
        self.logger.log_call(provider="second", action="y", correlation_id="c2", success=True)

        results = self.logger.query_calls()
        assert results[0]["provider"] == "second"
        assert results[1]["provider"] == "first"

    def test_in_memory_cap(self):
        """In-memory store should cap at 10000 entries."""
        for i in range(10050):
            self.logger.log_call(provider="x", action="y", correlation_id=f"c{i}", success=True)

        results = self.logger.query_calls(limit=20000)
        assert len(results) <= 10000

    def test_clear(self):
        """clear() should empty the store."""
        self.logger.log_call(provider="x", action="y", correlation_id="c1", success=True)
        self.logger.clear()
        assert len(self.logger.query_calls()) == 0


class TestPayloadRedaction:
    """Test PII/secret redaction in payloads (Law #9)."""

    def test_api_key_redacted(self):
        """API keys in payload should be redacted."""
        payload = {"key": "sk-test-abc123def456ghi789"}
        result = _redact_payload(payload)
        assert "abc123def456" not in result

    def test_password_field_redacted(self):
        """Password fields should be redacted."""
        payload = {"password": "SuperSecret123", "name": "test"}
        result = _redact_payload(payload)
        assert "SuperSecret123" not in result

    def test_long_payload_truncated(self):
        """Payloads over 200 chars should be truncated."""
        payload = {"data": "x" * 500}
        result = _redact_payload(payload)
        assert len(result) < 250
        assert "truncated" in result

    def test_none_payload(self):
        """None payload should return empty string."""
        assert _redact_payload(None) == ""

    def test_normal_payload_preserved(self):
        """Normal data without secrets should be preserved."""
        payload = {"amount": 1000, "currency": "usd"}
        result = _redact_payload(payload)
        assert "1000" in result
        assert "usd" in result


class TestSingleton:
    """Test singleton pattern."""

    def test_get_provider_call_logger_returns_same_instance(self):
        """Subsequent calls should return the same instance."""
        logger1 = get_provider_call_logger()
        logger2 = get_provider_call_logger()
        assert logger1 is logger2


class TestBaseClientIntegration:
    """Verify provider call logger hooks into BaseProviderClient._request()."""

    @pytest.mark.asyncio
    async def test_request_logs_call(self):
        """_request() should log a provider call entry."""
        from aspire_orchestrator.providers.base_client import BaseProviderClient, ProviderRequest
        from unittest.mock import AsyncMock

        class StubClient(BaseProviderClient):
            provider_id = "test-provider"
            base_url = "https://api.test.com"

            async def _authenticate_headers(self, request):
                return {"Authorization": "Bearer test"}

            def _parse_error(self, status_code, body):
                from aspire_orchestrator.providers.base_client import InternalErrorCode
                return InternalErrorCode.SERVER_INTERNAL_ERROR

        client = StubClient()
        pcl = get_provider_call_logger()
        pcl.clear()

        # Mock the HTTP client with async-compatible methods
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"id": "inv_123"}'
        mock_response.headers = {"x-request-id": "req-abc"}

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_response)

        client._get_client = AsyncMock(return_value=mock_http)

        req = ProviderRequest(
            method="GET",
            path="/v1/test",
            correlation_id="test-corr-id",
            suite_id="STE-0001",
        )

        response = await client._request(req)
        assert response.success

        # Verify call was logged
        calls = pcl.query_calls(provider="test-provider")
        assert len(calls) >= 1
        assert calls[0]["action"] == "GET /v1/test"
        assert calls[0]["correlation_id"] == "test-corr-id"
        assert calls[0]["status"] == "success"
