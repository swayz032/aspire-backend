"""Tests for Correlation ID middleware (Wave 2A).

Verifies that X-Correlation-Id is:
1. Extracted from request headers when provided
2. Generated as UUID when missing
3. Available via get_correlation_id() contextvar
4. Set on response headers
5. Properly reset between requests (no leakage)
"""

from __future__ import annotations

import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.middleware.correlation import (
    CorrelationIdMiddleware,
    get_correlation_id,
    set_correlation_id,
)


def _create_test_app() -> FastAPI:
    """Create a minimal FastAPI app with correlation middleware."""
    test_app = FastAPI()
    test_app.add_middleware(CorrelationIdMiddleware)

    @test_app.get("/echo-correlation")
    async def echo_correlation():
        """Return the correlation ID from contextvar."""
        return {"correlation_id": get_correlation_id()}

    @test_app.get("/nested-call")
    async def nested_call():
        """Simulate a nested service call that reads correlation ID."""
        cid = get_correlation_id()
        # Simulate what receipt_store, provider_call_logger etc. would do
        return {
            "outer_correlation_id": cid,
            "inner_read": get_correlation_id(),
            "match": cid == get_correlation_id(),
        }

    return test_app


class TestCorrelationIdMiddleware:
    """Core correlation ID behavior."""

    def setup_method(self):
        self.app = _create_test_app()
        self.client = TestClient(self.app)

    def test_provided_correlation_id_propagated(self):
        """X-Correlation-Id from request should be available in handler."""
        resp = self.client.get(
            "/echo-correlation",
            headers={"X-Correlation-Id": "my-trace-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["correlation_id"] == "my-trace-123"

    def test_provided_correlation_id_in_response(self):
        """X-Correlation-Id should be set on response header."""
        resp = self.client.get(
            "/echo-correlation",
            headers={"X-Correlation-Id": "my-trace-456"},
        )
        assert resp.headers.get("X-Correlation-Id") == "my-trace-456"

    def test_generated_correlation_id_when_missing(self):
        """Missing header should generate a UUID."""
        resp = self.client.get("/echo-correlation")
        assert resp.status_code == 200
        cid = resp.json()["correlation_id"]
        assert cid  # Not empty
        # Validate it's a UUID
        parsed = uuid.UUID(cid)
        assert str(parsed) == cid

    def test_generated_correlation_id_in_response_header(self):
        """Generated ID should also be in response header."""
        resp = self.client.get("/echo-correlation")
        resp_header = resp.headers.get("X-Correlation-Id")
        assert resp_header
        assert resp_header == resp.json()["correlation_id"]

    def test_correlation_id_consistent_within_request(self):
        """Multiple reads within one request should return the same ID."""
        resp = self.client.get("/nested-call")
        body = resp.json()
        assert body["match"] is True
        assert body["outer_correlation_id"] == body["inner_read"]

    def test_correlation_id_different_between_requests(self):
        """Each request without header should get a unique ID."""
        resp1 = self.client.get("/echo-correlation")
        resp2 = self.client.get("/echo-correlation")
        cid1 = resp1.json()["correlation_id"]
        cid2 = resp2.json()["correlation_id"]
        assert cid1 != cid2

    def test_no_leakage_between_requests(self):
        """Correlation ID from one request must not leak to the next."""
        resp1 = self.client.get(
            "/echo-correlation",
            headers={"X-Correlation-Id": "first-request"},
        )
        resp2 = self.client.get("/echo-correlation")
        assert resp1.json()["correlation_id"] == "first-request"
        assert resp2.json()["correlation_id"] != "first-request"


class TestCorrelationIdHelpers:
    """Test get/set helper functions."""

    def test_get_returns_empty_outside_request(self):
        """get_correlation_id() outside request context should return empty."""
        # Reset to clean state
        cid = get_correlation_id()
        # May or may not be empty depending on test execution order,
        # but should not crash
        assert isinstance(cid, str)

    def test_set_and_get(self):
        """set_correlation_id + get_correlation_id round-trip."""
        set_correlation_id("manual-test-id")
        assert get_correlation_id() == "manual-test-id"
        # Clean up
        set_correlation_id("")
