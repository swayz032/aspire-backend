"""Tests for per-tenant rate limiting middleware (B-H7).

Tests verify:
- Per-tenant rate limiting via x-suite-id header
- IP-based fallback when no suite_id
- Health/metrics endpoints exempt from rate limiting
- 429 response with correct headers when limit exceeded
- Rate limit headers on all responses
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from aspire_orchestrator.middleware.rate_limiter import (
    RateLimitMiddleware,
    _SlidingWindow,
)


# --- Unit tests for _SlidingWindow ---


class TestSlidingWindow:
    def test_allows_within_limit(self) -> None:
        w = _SlidingWindow()
        for i in range(5):
            allowed, remaining = w.check_and_record("key1", 5, 60)
            if i < 5:
                assert allowed

    def test_denies_over_limit(self) -> None:
        w = _SlidingWindow()
        for _ in range(10):
            w.check_and_record("key1", 10, 60)
        allowed, remaining = w.check_and_record("key1", 10, 60)
        assert not allowed
        assert remaining == 0

    def test_different_keys_independent(self) -> None:
        w = _SlidingWindow()
        for _ in range(5):
            w.check_and_record("a", 5, 60)
        # Key "a" is full
        allowed_a, _ = w.check_and_record("a", 5, 60)
        assert not allowed_a
        # Key "b" should still be open
        allowed_b, remaining_b = w.check_and_record("b", 5, 60)
        assert allowed_b
        assert remaining_b == 4

    def test_remaining_count_accurate(self) -> None:
        w = _SlidingWindow()
        _, r1 = w.check_and_record("k", 3, 60)
        assert r1 == 2
        _, r2 = w.check_and_record("k", 3, 60)
        assert r2 == 1
        _, r3 = w.check_and_record("k", 3, 60)
        assert r3 == 0

    def test_cleanup_removes_stale_keys(self) -> None:
        w = _SlidingWindow()
        w.check_and_record("live", 10, 60)
        # Manually make a stale entry (empty list = no timestamps)
        w._windows["stale"] = []
        # Use a large max_age so "live" (just recorded) survives
        w.cleanup(max_age_s=300)
        assert "stale" not in w._windows
        assert "live" in w._windows


# --- Integration tests for RateLimitMiddleware ---


def _make_test_app(limit: int = 3, window_seconds: int = 60) -> FastAPI:
    """Create a minimal FastAPI app with rate limiter for testing."""
    test_app = FastAPI()
    test_app.add_middleware(
        RateLimitMiddleware, limit=limit, window_seconds=window_seconds
    )

    @test_app.get("/v1/test")
    async def test_endpoint():
        return JSONResponse({"ok": True})

    @test_app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok"})

    @test_app.get("/readyz")
    async def readyz():
        return JSONResponse({"status": "ready"})

    @test_app.get("/metrics")
    async def metrics():
        return JSONResponse({"metrics": []})

    return test_app


class TestRateLimitMiddleware:
    @pytest.fixture(autouse=True)
    def _reset_window(self) -> None:
        """Reset the module-level sliding window between tests."""
        import aspire_orchestrator.middleware.rate_limiter as rl
        rl._window = _SlidingWindow()
        rl._last_cleanup = 0.0

    def test_allows_requests_within_limit(self) -> None:
        client = TestClient(_make_test_app(limit=3))
        for _ in range(3):
            resp = client.get("/v1/test", headers={"x-suite-id": "STE-test"})
            assert resp.status_code == 200

    def test_returns_429_when_limit_exceeded(self) -> None:
        client = TestClient(_make_test_app(limit=2))
        # Use up the limit
        for _ in range(2):
            client.get("/v1/test", headers={"x-suite-id": "STE-test"})
        # Third request should be denied
        resp = client.get("/v1/test", headers={"x-suite-id": "STE-test"})
        assert resp.status_code == 429
        data = resp.json()
        assert data["error"] == "RATE_LIMIT_EXCEEDED"
        assert "Retry-After" in resp.headers
        assert resp.headers["X-RateLimit-Remaining"] == "0"

    def test_rate_limit_headers_on_success(self) -> None:
        client = TestClient(_make_test_app(limit=5))
        resp = client.get("/v1/test", headers={"x-suite-id": "STE-test"})
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "5"
        assert resp.headers["X-RateLimit-Remaining"] == "4"

    def test_tenant_isolation(self) -> None:
        """Different tenants have independent rate limits (Law #6)."""
        client = TestClient(_make_test_app(limit=2))
        # Tenant A uses limit
        for _ in range(2):
            client.get("/v1/test", headers={"x-suite-id": "STE-A"})
        # Tenant A is blocked
        resp_a = client.get("/v1/test", headers={"x-suite-id": "STE-A"})
        assert resp_a.status_code == 429
        # Tenant B is NOT blocked
        resp_b = client.get("/v1/test", headers={"x-suite-id": "STE-B"})
        assert resp_b.status_code == 200

    def test_ip_fallback_when_no_suite_id(self) -> None:
        """Fallback to IP-based limiting when no x-suite-id header."""
        client = TestClient(_make_test_app(limit=2))
        for _ in range(2):
            client.get("/v1/test")
        resp = client.get("/v1/test")
        assert resp.status_code == 429

    def test_health_endpoints_exempt(self) -> None:
        """Health/metrics endpoints are never rate limited."""
        client = TestClient(_make_test_app(limit=1))
        # Use up the limit
        client.get("/v1/test", headers={"x-suite-id": "STE-X"})
        # Regular endpoint blocked
        resp = client.get("/v1/test", headers={"x-suite-id": "STE-X"})
        assert resp.status_code == 429
        # Health endpoints still work
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/metrics").status_code == 200
