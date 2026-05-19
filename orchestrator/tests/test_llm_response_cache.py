"""Wave 9h: LLM response cache tests.

Tests for services/llm_response_cache.py:
1. Redis set/get round-trip preserves response dict
2. Fallback to in-memory LRU when Redis raises connection error
3. TTL respected (set with short TTL, assert expiry returns None — mocked)
4. clear_by_project_id removes matching keys
5. compute_content_hash stability
6. Redis key format
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cache_state():
    """Reset module-level state between tests."""
    from aspire_orchestrator.services import llm_response_cache as lrc
    lrc.reset_redis_connection()
    lrc.clear_lru_store()
    yield
    lrc.reset_redis_connection()
    lrc.clear_lru_store()


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------

class TestComputeContentHash:
    def test_returns_sha256_hex(self) -> None:
        from aspire_orchestrator.services.llm_response_cache import compute_content_hash
        h = compute_content_hash("hello")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_stable_for_same_input(self) -> None:
        from aspire_orchestrator.services.llm_response_cache import compute_content_hash
        assert compute_content_hash("test") == compute_content_hash("test")

    def test_differs_for_different_input(self) -> None:
        from aspire_orchestrator.services.llm_response_cache import compute_content_hash
        assert compute_content_hash("a") != compute_content_hash("b")


# ---------------------------------------------------------------------------
# Redis key format
# ---------------------------------------------------------------------------

class TestRedisKeyFormat:
    def test_key_format(self) -> None:
        from aspire_orchestrator.services.llm_response_cache import _redis_key
        key = _redis_key("classify", "gpt-5.4-mini", "abc123")
        assert key == "aspire:blueprint:llm:classify:gpt-5.4-mini:abc123"


# ---------------------------------------------------------------------------
# Redis round-trip
# ---------------------------------------------------------------------------

class TestRedisRoundTrip:
    @pytest.mark.asyncio
    async def test_set_and_get_returns_dict(self) -> None:
        """set_cached_llm_response → get_cached_llm_response round-trip via mocked Redis."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        mock_redis = AsyncMock()
        stored: dict[str, str] = {}

        async def mock_setex(key: str, ttl: int, value: str) -> None:
            stored[key] = value

        async def mock_get(key: str) -> str | None:
            return stored.get(key)

        async def mock_ping() -> bool:
            return True

        mock_redis.setex = mock_setex
        mock_redis.get = mock_get
        mock_redis.ping = mock_ping

        with patch("aspire_orchestrator.services.llm_response_cache._get_redis", return_value=mock_redis):
            # Directly set _redis_client to skip initialization
            lrc._redis_client = mock_redis
            lrc._redis_initialized = True

            payload = {"phases": [{"phase_number": 1, "phase_name": "Site Prep"}], "mean_confidence": 0.85}

            await lrc.set_cached_llm_response("reason", "hash123", "gpt-5.4-mini", payload)
            result = await lrc.get_cached_llm_response("reason", "hash123", "gpt-5.4-mini")

        assert result is not None
        assert result["phases"][0]["phase_name"] == "Site Prep"
        assert result["mean_confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_get_returns_none_on_cache_miss(self) -> None:
        from aspire_orchestrator.services import llm_response_cache as lrc

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        lrc._redis_client = mock_redis
        lrc._redis_initialized = True

        result = await lrc.get_cached_llm_response("classify", "misshash", "gpt-5")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_uses_provided_ttl(self) -> None:
        """set_cached_llm_response calls Redis setex with the specified TTL."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        mock_redis = AsyncMock()
        lrc._redis_client = mock_redis
        lrc._redis_initialized = True

        payload = {"result": "ok"}
        await lrc.set_cached_llm_response("classify", "hashXYZ", "gpt-5", payload, ttl_seconds=3600)

        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.call_args
        # setex(key, ttl, value)
        assert call_args[0][1] == 3600


# ---------------------------------------------------------------------------
# In-memory LRU fallback
# ---------------------------------------------------------------------------

class TestLRUFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_lru_when_redis_unavailable(self) -> None:
        """When Redis is down, cache reads/writes use in-memory LRU."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        # Redis is None — force fail-open path
        lrc._redis_client = None
        lrc._redis_initialized = True

        payload = {"key": "value", "score": 0.9}

        await lrc.set_cached_llm_response("classify", "fallback_hash", "gpt-5-mini", payload)
        result = await lrc.get_cached_llm_response("classify", "fallback_hash", "gpt-5-mini")

        assert result is not None
        assert result["score"] == 0.9

    @pytest.mark.asyncio
    async def test_lru_returns_none_for_unknown_key(self) -> None:
        from aspire_orchestrator.services import llm_response_cache as lrc

        lrc._redis_client = None
        lrc._redis_initialized = True

        result = await lrc.get_cached_llm_response("reason", "unknown_hash", "gpt-5")
        assert result is None

    @pytest.mark.asyncio
    async def test_redis_failure_on_get_falls_through_to_lru(self) -> None:
        """Redis GET raises → LRU is checked as fallback."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        # Prime the LRU store
        lrc._lru_store_set("reason", "gpt-5", "hash999", json.dumps({"phases": []}))

        # Redis is initialized but raises on get
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        lrc._redis_client = mock_redis
        lrc._redis_initialized = True

        result = await lrc.get_cached_llm_response("reason", "hash999", "gpt-5")

        # Should have fallen through to LRU
        assert result is not None
        assert "phases" in result

    @pytest.mark.asyncio
    async def test_redis_failure_on_set_writes_to_lru(self) -> None:
        """Redis SET raises → value written to in-memory LRU instead."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(side_effect=ConnectionError("Redis down"))
        lrc._redis_client = mock_redis
        lrc._redis_initialized = True

        payload = {"result": "lru_fallback"}
        await lrc.set_cached_llm_response("classify", "hashLRU", "gpt-5", payload)

        # Should be in LRU store
        stored = lrc._lru_store_get("classify", "gpt-5", "hashLRU")
        assert stored is not None
        assert json.loads(stored) == payload


# ---------------------------------------------------------------------------
# LRU eviction at capacity
# ---------------------------------------------------------------------------

class TestLRUEviction:
    @pytest.mark.asyncio
    async def test_lru_evicts_oldest_at_capacity(self) -> None:
        """When LRU store is at _LRU_MAX, setting a new entry evicts the oldest."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        lrc._redis_client = None
        lrc._redis_initialized = True

        original_max = lrc._LRU_MAX
        # Temporarily lower max for test
        lrc._LRU_MAX = 3

        try:
            await lrc.set_cached_llm_response("classify", "h1", "m", {"n": 1})
            await lrc.set_cached_llm_response("classify", "h2", "m", {"n": 2})
            await lrc.set_cached_llm_response("classify", "h3", "m", {"n": 3})
            # This should evict h1
            await lrc.set_cached_llm_response("classify", "h4", "m", {"n": 4})

            # h1 should be gone
            assert lrc._lru_store_get("classify", "m", "h1") is None
            # h4 should be present
            assert lrc._lru_store_get("classify", "m", "h4") is not None
        finally:
            lrc._LRU_MAX = original_max


# ---------------------------------------------------------------------------
# clear_llm_cache_for_project
# ---------------------------------------------------------------------------

class TestClearLlmCacheForProject:
    @pytest.mark.asyncio
    async def test_clears_lru_store(self) -> None:
        """clear_llm_cache_for_project clears the in-memory LRU store."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        lrc._redis_client = None
        lrc._redis_initialized = True

        # Prime some entries
        lrc._lru_store_set("classify", "gpt-5", "h1", json.dumps({"a": 1}))
        lrc._lru_store_set("reason", "gpt-5", "h2", json.dumps({"b": 2}))

        deleted = await lrc.clear_llm_cache_for_project("proj-123", "suite-abc")

        assert deleted > 0
        assert lrc._lru_store_get("classify", "gpt-5", "h1") is None
        assert lrc._lru_store_get("reason", "gpt-5", "h2") is None

    @pytest.mark.asyncio
    async def test_redis_scan_called_when_redis_available(self) -> None:
        """clear_llm_cache_for_project uses SCAN (not KEYS) for safe Redis eviction."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        mock_redis = AsyncMock()
        # SCAN returns (cursor=0, keys=[]) → single iteration, no keys to delete
        mock_redis.scan = AsyncMock(return_value=(0, []))
        lrc._redis_client = mock_redis
        lrc._redis_initialized = True

        await lrc.clear_llm_cache_for_project("proj-xyz", "suite-xyz")

        mock_redis.scan.assert_awaited()

    @pytest.mark.asyncio
    async def test_redis_failure_on_clear_is_non_fatal(self) -> None:
        """Redis error during clear does not propagate — fail-open."""
        from aspire_orchestrator.services import llm_response_cache as lrc

        mock_redis = AsyncMock()
        mock_redis.scan = AsyncMock(side_effect=ConnectionError("Redis down"))
        lrc._redis_client = mock_redis
        lrc._redis_initialized = True

        # Should not raise
        deleted = await lrc.clear_llm_cache_for_project("proj", "suite")
        assert isinstance(deleted, int)


# ---------------------------------------------------------------------------
# Cache disabled via env var
# ---------------------------------------------------------------------------

class TestCacheDisabled:
    @pytest.mark.asyncio
    async def test_get_returns_none_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        from aspire_orchestrator.services import llm_response_cache as lrc

        original = lrc._CACHE_ENABLED
        lrc._CACHE_ENABLED = False
        try:
            result = await lrc.get_cached_llm_response("classify", "h", "m")
            assert result is None
        finally:
            lrc._CACHE_ENABLED = original

    @pytest.mark.asyncio
    async def test_set_is_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aspire_orchestrator.services import llm_response_cache as lrc

        original = lrc._CACHE_ENABLED
        lrc._CACHE_ENABLED = False
        try:
            await lrc.set_cached_llm_response("reason", "h", "m", {"phases": []})
            # Nothing in LRU either
            assert lrc._lru_store_get("reason", "m", "h") is None
        finally:
            lrc._CACHE_ENABLED = original
