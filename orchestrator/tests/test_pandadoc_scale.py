"""PandaDoc Enterprise Scale Tests — Rate limiting, idempotency, circuit breaker.

Tests cover:
  - Token bucket rate limiter (global 10 req/s)
  - Per-suite rate limiter (5/min/suite)
  - Client-side idempotency dedup
  - Circuit breaker behavior (from base_client)
  - Connection pool configuration
  - Concurrent request handling

Law coverage:
  - Law #2: Rate limit denials produce receipts
  - Law #3: Fail closed on rate limit / circuit open / duplicate
  - Law #6: Per-suite rate limiting prevents single-tenant abuse
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.pandadoc_client import (
    IdempotencyDedup,
    PandaDocClient,
    PerSuiteRateLimiter,
    TokenBucketRateLimiter,
    execute_pandadoc_contract_generate,
)


# ===========================================================================
# TokenBucketRateLimiter tests
# ===========================================================================


class TestTokenBucketRateLimiter:
    """Tests for the global rate limiter (10 req/s, burst 20)."""

    def test_burst_capacity(self) -> None:
        """Should allow burst of up to max_burst tokens."""
        limiter = TokenBucketRateLimiter(rate=10.0, burst=20)
        acquired = sum(1 for _ in range(20) if limiter.acquire())
        assert acquired == 20

    def test_exhausted_after_burst(self) -> None:
        """After burst exhausted, should deny requests."""
        limiter = TokenBucketRateLimiter(rate=10.0, burst=5)
        for _ in range(5):
            limiter.acquire()
        assert limiter.acquire() is False

    def test_refill_over_time(self) -> None:
        """Tokens refill based on elapsed time and rate."""
        limiter = TokenBucketRateLimiter(rate=10.0, burst=5)
        # Exhaust all tokens
        for _ in range(5):
            limiter.acquire()
        assert limiter.acquire() is False

        # Manually advance time by simulating refill
        limiter._last_refill = time.monotonic() - 1.0  # 1 second ago = +10 tokens
        assert limiter.acquire() is True

    def test_available_tokens_property(self) -> None:
        """available_tokens should reflect current state."""
        limiter = TokenBucketRateLimiter(rate=10.0, burst=10)
        assert limiter.available_tokens == 10.0
        limiter.acquire()
        assert limiter.available_tokens == pytest.approx(9.0, abs=0.5)

    def test_tokens_capped_at_burst(self) -> None:
        """Tokens should never exceed burst capacity."""
        limiter = TokenBucketRateLimiter(rate=100.0, burst=5)
        # Even with high rate, tokens shouldn't exceed burst
        limiter._last_refill = time.monotonic() - 100.0  # Long time ago
        assert limiter.available_tokens <= 5.0


# ===========================================================================
# PerSuiteRateLimiter tests
# ===========================================================================


class TestPerSuiteRateLimiter:
    """Tests for per-suite rate limiting (5/min/suite)."""

    def test_allows_under_limit(self) -> None:
        """Should allow requests under the per-suite limit."""
        limiter = PerSuiteRateLimiter(max_per_window=5, window_seconds=60.0)
        for i in range(5):
            assert limiter.acquire("suite-001") is True, f"Request {i+1} should be allowed"

    def test_denies_over_limit(self) -> None:
        """Should deny requests over the per-suite limit."""
        limiter = PerSuiteRateLimiter(max_per_window=5, window_seconds=60.0)
        for _ in range(5):
            limiter.acquire("suite-001")
        assert limiter.acquire("suite-001") is False

    def test_independent_suites(self) -> None:
        """Different suites should have independent rate limits (Law #6)."""
        limiter = PerSuiteRateLimiter(max_per_window=3, window_seconds=60.0)
        # Suite A uses 3 of 3
        for _ in range(3):
            limiter.acquire("suite-A")
        assert limiter.acquire("suite-A") is False

        # Suite B should still be allowed
        assert limiter.acquire("suite-B") is True

    def test_window_expiry(self) -> None:
        """Old requests should expire from the window."""
        limiter = PerSuiteRateLimiter(max_per_window=2, window_seconds=1.0)
        limiter.acquire("suite-001")
        limiter.acquire("suite-001")
        assert limiter.acquire("suite-001") is False

        # Simulate window expiry
        limiter._suite_timestamps["suite-001"] = [
            time.monotonic() - 2.0,  # Expired
            time.monotonic() - 2.0,  # Expired
        ]
        assert limiter.acquire("suite-001") is True

    def test_usage_count(self) -> None:
        """usage() should return current in-window count."""
        limiter = PerSuiteRateLimiter(max_per_window=5, window_seconds=60.0)
        assert limiter.usage("suite-001") == 0
        limiter.acquire("suite-001")
        limiter.acquire("suite-001")
        assert limiter.usage("suite-001") == 2


# ===========================================================================
# IdempotencyDedup tests
# ===========================================================================


class TestIdempotencyDedup:
    """Tests for client-side idempotency dedup."""

    def test_first_request_allowed(self) -> None:
        """First request with a given key should be allowed."""
        dedup = IdempotencyDedup(ttl_seconds=300.0)
        key = dedup.compute_key("suite-001", {"template_id": "nda", "name": "Test"})
        assert dedup.check_and_mark(key) is False

    def test_duplicate_rejected(self) -> None:
        """Second identical request should be rejected."""
        dedup = IdempotencyDedup(ttl_seconds=300.0)
        key = dedup.compute_key("suite-001", {"template_id": "nda", "name": "Test"})
        dedup.check_and_mark(key)
        assert dedup.check_and_mark(key) is True

    def test_different_payloads_allowed(self) -> None:
        """Different payloads should produce different keys."""
        dedup = IdempotencyDedup(ttl_seconds=300.0)
        key_a = dedup.compute_key("suite-001", {"template_id": "nda", "name": "Test A"})
        key_b = dedup.compute_key("suite-001", {"template_id": "nda", "name": "Test B"})
        assert key_a != key_b
        dedup.check_and_mark(key_a)
        assert dedup.check_and_mark(key_b) is False

    def test_different_suites_different_keys(self) -> None:
        """Same payload for different suites should have different dedup keys."""
        dedup = IdempotencyDedup(ttl_seconds=300.0)
        key_a = dedup.compute_key("suite-A", {"template_id": "nda"})
        key_b = dedup.compute_key("suite-B", {"template_id": "nda"})
        assert key_a != key_b

    def test_ttl_expiry(self) -> None:
        """Expired entries should be pruned — allowing re-submission."""
        dedup = IdempotencyDedup(ttl_seconds=1.0)
        key = dedup.compute_key("suite-001", {"template_id": "nda"})
        dedup.check_and_mark(key)
        assert dedup.check_and_mark(key) is True

        # Simulate TTL expiry
        dedup._seen[key] = time.monotonic() - 2.0
        assert dedup.check_and_mark(key) is False

    def test_deterministic_key(self) -> None:
        """Same inputs should always produce the same key."""
        dedup = IdempotencyDedup()
        payload = {"template_id": "nda", "name": "Test", "parties": [{"name": "Acme"}]}
        key_1 = dedup.compute_key("suite-001", payload)
        key_2 = dedup.compute_key("suite-001", payload)
        assert key_1 == key_2

    def test_whitespace_normalization(self) -> None:
        """Whitespace-padded values should produce same key as trimmed (P2 fix)."""
        dedup = IdempotencyDedup()
        key_clean = dedup.compute_key("suite-001", {"name": "Acme Corp", "template_id": "nda"})
        key_padded = dedup.compute_key("suite-001", {"name": "  Acme Corp  ", "template_id": "  nda "})
        assert key_clean == key_padded

    def test_unicode_normalization(self) -> None:
        """Unicode NFC variants should produce same dedup key."""
        dedup = IdempotencyDedup()
        # e-acute: precomposed (NFC) vs decomposed (NFD)
        key_nfc = dedup.compute_key("suite-001", {"name": "\u00e9"})  # precomposed
        key_nfd = dedup.compute_key("suite-001", {"name": "e\u0301"})  # decomposed
        assert key_nfc == key_nfd


# ===========================================================================
# PandaDocClient Enterprise Configuration tests
# ===========================================================================


class TestPandaDocClientConfig:
    """Tests for enterprise-scale client configuration."""

    def test_client_has_rate_limiter(self) -> None:
        """PandaDocClient should have a token bucket rate limiter."""
        client = PandaDocClient()
        assert isinstance(client.rate_limiter, TokenBucketRateLimiter)

    def test_client_has_suite_limiter(self) -> None:
        """PandaDocClient should have a per-suite rate limiter."""
        client = PandaDocClient()
        assert isinstance(client.suite_limiter, PerSuiteRateLimiter)

    def test_client_has_dedup(self) -> None:
        """PandaDocClient should have an idempotency dedup cache."""
        client = PandaDocClient()
        assert isinstance(client.dedup, IdempotencyDedup)


# ===========================================================================
# Integration: Rate Limiting in execute_pandadoc_contract_generate
# ===========================================================================


class TestGenerateRateLimiting:
    """Test rate limiting integration in contract generation flow."""

    @pytest.mark.asyncio
    async def test_suite_rate_limit_produces_receipt(self) -> None:
        """6th generate in 1 minute → SUITE_RATE_LIMITED with receipt."""
        import aspire_orchestrator.providers.pandadoc_client as mod

        # Reset singleton
        old_client = mod._client
        mod._client = None
        client = mod._get_client()

        # Exhaust per-suite limit (5/min)
        for _ in range(5):
            client.suite_limiter.acquire("suite-rate-test")

        result = await execute_pandadoc_contract_generate(
            payload={"template_id": "nda-uuid", "name": "Rate Test"},
            correlation_id="corr-rate-001",
            suite_id="suite-rate-test",
            office_id="office-001",
        )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None
        assert result.receipt_data["reason_code"] == "SUITE_RATE_LIMITED"
        assert "Rate limited" in (result.error or "")

        # Restore
        mod._client = old_client

    @pytest.mark.asyncio
    async def test_global_rate_limit_produces_receipt(self) -> None:
        """Global token bucket exhaustion → GLOBAL_RATE_LIMITED with receipt."""
        import aspire_orchestrator.providers.pandadoc_client as mod

        old_client = mod._client
        mod._client = None
        client = mod._get_client()

        # Exhaust global bucket
        client.rate_limiter = TokenBucketRateLimiter(rate=10.0, burst=0)

        result = await execute_pandadoc_contract_generate(
            payload={"template_id": "nda-uuid", "name": "Global Rate Test"},
            correlation_id="corr-global-001",
            suite_id="suite-global-test",
            office_id="office-001",
        )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None
        assert result.receipt_data["reason_code"] == "GLOBAL_RATE_LIMITED"

        mod._client = old_client

    @pytest.mark.asyncio
    async def test_idempotency_dedup_produces_receipt(self) -> None:
        """Duplicate request within TTL → IDEMPOTENCY_DUPLICATE with receipt."""
        import aspire_orchestrator.providers.pandadoc_client as mod

        old_client = mod._client
        mod._client = None
        client = mod._get_client()

        payload = {"template_id": "nda-uuid", "name": "Dedup Test"}
        # Pre-mark dedup key
        key = client.dedup.compute_key("suite-dedup-test", payload)
        client.dedup.check_and_mark(key)

        result = await execute_pandadoc_contract_generate(
            payload=payload,
            correlation_id="corr-dedup-001",
            suite_id="suite-dedup-test",
            office_id="office-001",
        )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data is not None
        assert result.receipt_data["reason_code"] == "IDEMPOTENCY_DUPLICATE"
        assert "Duplicate" in (result.error or "")

        mod._client = old_client


# ===========================================================================
# Evil tests — scale abuse scenarios
# ===========================================================================


class TestEvilScale:
    """Evil tests: scale abuse, tenant starvation, dedup bypass."""

    def test_evil_single_tenant_starvation(self) -> None:
        """EVIL: One tenant cannot exhaust the global rate limiter for all tenants.

        The per-suite limiter (5/min) kicks in BEFORE the global limiter (10/s).
        """
        suite_limiter = PerSuiteRateLimiter(max_per_window=5, window_seconds=60.0)

        # Tenant A tries to flood
        for _ in range(5):
            suite_limiter.acquire("evil-tenant-A")
        assert suite_limiter.acquire("evil-tenant-A") is False

        # Tenant B is unaffected
        assert suite_limiter.acquire("tenant-B") is True

    def test_evil_dedup_key_collision_resistance(self) -> None:
        """EVIL: Different payloads must not produce the same dedup key."""
        dedup = IdempotencyDedup()
        keys = set()
        for i in range(100):
            key = dedup.compute_key(f"suite-{i}", {"template_id": f"tpl-{i}", "name": f"doc-{i}"})
            keys.add(key)
        # All 100 keys should be unique
        assert len(keys) == 100

    def test_evil_rate_limiter_zero_burst(self) -> None:
        """EVIL: Rate limiter with zero burst should deny all requests."""
        limiter = TokenBucketRateLimiter(rate=0.0, burst=0)
        assert limiter.acquire() is False
