"""Tests for Global Exception Handler Middleware (Wave 1B — F4 fix).

Verifies that unhandled exceptions:
1. Create incident records
2. Store failure receipts (Law #2)
3. Return sanitized 500 with correlation_id (Law #9)
4. Never expose secrets/PII in error responses
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.middleware.exception_handler import (
    GlobalExceptionMiddleware,
    _sanitize_error_message,
)


def _create_test_app() -> FastAPI:
    """Create a minimal FastAPI app with exception middleware for testing."""
    test_app = FastAPI()
    test_app.add_middleware(GlobalExceptionMiddleware)

    @test_app.get("/healthy")
    async def healthy():
        return {"status": "ok"}

    @test_app.get("/failing")
    async def failing():
        raise ValueError("Something went wrong in the handler")

    @test_app.get("/failing-with-secrets")
    async def failing_with_secrets():
        raise RuntimeError(
            "Connection failed: postgresql://admin:s3cretP4ss@db.host.com:5432/aspire "
            "with key sk-test-abc123def456ghi789jkl012mno345pqr678stu901vwx234"
        )

    return test_app


class TestGlobalExceptionMiddleware:
    """Core exception handling behavior."""

    def setup_method(self):
        self.app = _create_test_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_healthy_endpoint_passes_through(self):
        """Non-failing endpoints should work normally."""
        resp = self.client.get("/healthy")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_unhandled_exception_returns_500(self):
        """Unhandled exception should return 500, not crash."""
        resp = self.client.get("/failing")
        assert resp.status_code == 500

    def test_500_response_has_correlation_id(self):
        """500 response must include correlation_id for tracing."""
        resp = self.client.get("/failing")
        body = resp.json()
        assert "correlation_id" in body
        assert body["correlation_id"]  # Not empty

    def test_500_response_has_incident_id(self):
        """500 response must include incident_id for admin lookup."""
        resp = self.client.get("/failing")
        body = resp.json()
        assert "incident_id" in body
        assert body["incident_id"]

    def test_500_response_has_standard_error_code(self):
        """500 response must use standard error code."""
        resp = self.client.get("/failing")
        body = resp.json()
        assert body["error"] == "INTERNAL_SERVER_ERROR"

    def test_500_response_does_not_expose_exception_details(self):
        """Law #9: 500 response must NOT contain the raw exception message."""
        resp = self.client.get("/failing")
        body = resp.json()
        assert "Something went wrong in the handler" not in body["message"]
        assert "ValueError" not in body["message"]

    def test_correlation_id_from_header_propagated(self):
        """If X-Correlation-Id is provided, use it in the response."""
        resp = self.client.get(
            "/failing",
            headers={"X-Correlation-Id": "test-corr-id-123"},
        )
        body = resp.json()
        assert body["correlation_id"] == "test-corr-id-123"
        assert resp.headers.get("X-Correlation-Id") == "test-corr-id-123"

    def test_correlation_id_generated_if_missing(self):
        """If no X-Correlation-Id header, generate one."""
        resp = self.client.get("/failing")
        body = resp.json()
        assert body["correlation_id"]
        assert len(body["correlation_id"]) == 36  # UUID format

    def test_incident_registered(self):
        """Exception should create an incident record."""
        incidents = []

        original_register = None
        try:
            from aspire_orchestrator.routes import admin
            original_register = admin.register_incident
            admin.register_incident = lambda inc: incidents.append(inc)
            resp = self.client.get("/failing")
            assert resp.status_code == 500
            assert len(incidents) == 1
            inc = incidents[0]
            assert inc["state"] == "open"
            assert inc["severity"] == "high"
            assert "correlation_id" in inc
            assert len(inc["timeline"]) >= 1
        finally:
            if original_register:
                admin.register_incident = original_register

    def test_receipt_stored(self):
        """Exception should store a failure receipt (Law #2)."""
        stored = []

        original_store = None
        try:
            from aspire_orchestrator.services import receipt_store
            original_store = receipt_store.store_receipts
            receipt_store.store_receipts = lambda receipts: stored.extend(receipts)
            resp = self.client.get("/failing")
        finally:
            if original_store:
                receipt_store.store_receipts = original_store

        assert resp.status_code == 500
        assert len(stored) == 1
        receipt = stored[0]
        assert receipt["outcome"] == "FAILED"
        assert receipt["reason_code"] == "INTERNAL_SERVER_ERROR"
        assert receipt["receipt_type"] == "exception"
        assert receipt["correlation_id"]
        assert receipt["suite_id"]

    def test_secrets_not_exposed_in_error(self):
        """Law #9: Connection strings and API keys must be redacted."""
        resp = self.client.get("/failing-with-secrets")
        body = resp.json()
        # The 500 response itself should not contain the secret
        assert "s3cretP4ss" not in json.dumps(body)
        assert "sk-test-abc123" not in json.dumps(body)


class TestSanitizeErrorMessage:
    """Test PII/secret redaction in error messages."""

    def test_api_key_redacted(self):
        """sk-test-... keys should be redacted."""
        msg = "Auth failed with key sk-test-abc123def456ghi789jkl012mno345"
        result = _sanitize_error_message(msg)
        assert "abc123def456" not in result
        assert "REDACTED" in result

    def test_connection_string_password_redacted(self):
        """postgresql://user:pass@host should redact password."""
        msg = "Connection to postgresql://admin:SuperSecret123@db.host:5432 failed"
        result = _sanitize_error_message(msg)
        assert "SuperSecret123" not in result
        assert "***" in result

    def test_jwt_token_redacted(self):
        """JWT tokens (eyJ...) should be redacted."""
        msg = "Token validation failed: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
        result = _sanitize_error_message(msg)
        assert "eyJ" not in result
        assert "JWT_REDACTED" in result

    def test_long_message_truncated(self):
        """Messages over 500 chars should be truncated."""
        msg = "x" * 1000
        result = _sanitize_error_message(msg)
        assert len(result) < 600
        assert "truncated" in result

    def test_normal_message_unchanged(self):
        """Normal error messages without secrets should pass through."""
        msg = "Division by zero in calculate_total"
        result = _sanitize_error_message(msg)
        assert result == msg

    def test_empty_message(self):
        """Empty message should not crash."""
        result = _sanitize_error_message("")
        assert result == ""
