"""Wave 9h: OpenAI cache observability tests.

Tests for:
1. UsageDict returned with correct cache_hit_ratio
2. Structured log line contains all required fields
3. Backward compat: missing cached_tokens defaults to 0 with no crash
4. prompt_prefix_hash computation
5. LLMTextResult and LLMJsonResult shapes
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_usage(
    prompt_tokens: int = 1200,
    cached_tokens: int | None = 500,
    completion_tokens: int = 200,
    total_tokens: int = 1400,
) -> MagicMock:
    """Build a mock OpenAI usage object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens

    if cached_tokens is not None:
        details = MagicMock()
        details.cached_tokens = cached_tokens
        usage.prompt_tokens_details = details
    else:
        # Simulate older SDK: no prompt_tokens_details attribute
        del usage.prompt_tokens_details

    return usage


def _make_response(text: str = "Hello", *, usage: MagicMock | None = None) -> MagicMock:
    """Build a mock OpenAI response object."""
    response = MagicMock()
    response.output_text = text
    response.usage = usage or _make_usage()
    return response


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestExtractCachedTokens:
    """_extract_cached_tokens graceful degradation tests."""

    def test_returns_value_when_present(self) -> None:
        from aspire_orchestrator.services.openai_client import _extract_cached_tokens

        usage = _make_usage(cached_tokens=512)
        assert _extract_cached_tokens(usage) == 512

    def test_returns_zero_when_details_missing(self) -> None:
        from aspire_orchestrator.services import openai_client

        # Reset the one-time warning flag so this test can verify the warning path
        openai_client._warned_missing_cached_tokens = False

        usage = _make_usage(cached_tokens=None)
        result = openai_client._extract_cached_tokens(usage)
        assert result == 0

    def test_one_time_warning_emitted(self, caplog: pytest.LogCaptureFixture) -> None:
        from aspire_orchestrator.services import openai_client

        openai_client._warned_missing_cached_tokens = False
        usage = _make_usage(cached_tokens=None)

        with caplog.at_level(logging.WARNING, logger="aspire_orchestrator.services.openai_client"):
            openai_client._extract_cached_tokens(usage)

        assert any("cached_tokens absent" in r.message or "prompt_tokens_details" in r.message
                   for r in caplog.records)
        # Second call should NOT emit another warning
        initial_count = len(caplog.records)
        openai_client._extract_cached_tokens(usage)
        assert len(caplog.records) == initial_count

    def test_returns_zero_when_cached_tokens_is_none_on_details(self) -> None:
        from aspire_orchestrator.services.openai_client import _extract_cached_tokens

        usage = MagicMock()
        usage.prompt_tokens_details = MagicMock()
        usage.prompt_tokens_details.cached_tokens = None
        assert _extract_cached_tokens(usage) == 0


class TestBuildUsageDict:
    """_build_usage_dict constructs UsageDict correctly."""

    def test_correct_cache_hit_ratio(self) -> None:
        from aspire_orchestrator.services.openai_client import _build_usage_dict

        usage = _make_usage(prompt_tokens=1000, cached_tokens=400)
        result = _build_usage_dict(
            usage=usage, model="gpt-5.4-mini", latency_ms=150.0, prompt_prefix_hash="abc12345"
        )
        assert result["prompt_tokens"] == 1000
        assert result["cached_tokens"] == 400
        assert abs(result["cache_hit_ratio"] - 0.4) < 1e-4
        assert result["model"] == "gpt-5.4-mini"
        assert result["latency_ms"] == 150.0
        assert result["prompt_prefix_hash"] == "abc12345"

    def test_zero_ratio_when_no_cached_tokens(self) -> None:
        from aspire_orchestrator.services.openai_client import _build_usage_dict

        usage = _make_usage(prompt_tokens=800, cached_tokens=0)
        result = _build_usage_dict(
            usage=usage, model="gpt-5", latency_ms=200.0, prompt_prefix_hash="xyz"
        )
        assert result["cache_hit_ratio"] == 0.0

    def test_full_cache_hit_ratio(self) -> None:
        from aspire_orchestrator.services.openai_client import _build_usage_dict

        usage = _make_usage(prompt_tokens=1024, cached_tokens=1024)
        result = _build_usage_dict(
            usage=usage, model="gpt-5", latency_ms=50.0, prompt_prefix_hash="aaa"
        )
        assert result["cache_hit_ratio"] == 1.0

    def test_no_divide_by_zero_when_prompt_tokens_zero(self) -> None:
        from aspire_orchestrator.services.openai_client import _build_usage_dict

        usage = _make_usage(prompt_tokens=0, cached_tokens=0, total_tokens=0)
        result = _build_usage_dict(
            usage=usage, model="gpt-5", latency_ms=100.0, prompt_prefix_hash="bbb"
        )
        assert result["cache_hit_ratio"] == 0.0


class TestComputePromptPrefixHash:
    """_compute_prompt_prefix_hash stability tests."""

    def test_returns_16_hex_chars(self) -> None:
        from aspire_orchestrator.services.openai_client import _compute_prompt_prefix_hash

        messages = [
            {"role": "system", "content": "You are Drew."},
            {"role": "user", "content": "Classify these sheets."},
        ]
        h = _compute_prompt_prefix_hash(messages)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_stable_for_identical_messages(self) -> None:
        from aspire_orchestrator.services.openai_client import _compute_prompt_prefix_hash

        messages = [
            {"role": "system", "content": "Static system prompt"},
            {"role": "user", "content": "Dynamic user content for batch 1"},
        ]
        h1 = _compute_prompt_prefix_hash(messages)
        h2 = _compute_prompt_prefix_hash(messages)
        assert h1 == h2

    def test_differs_when_system_prompt_changes(self) -> None:
        from aspire_orchestrator.services.openai_client import _compute_prompt_prefix_hash

        m1 = [{"role": "system", "content": "Prompt A"}, {"role": "user", "content": "Same"}]
        m2 = [{"role": "system", "content": "Prompt B"}, {"role": "user", "content": "Same"}]
        assert _compute_prompt_prefix_hash(m1) != _compute_prompt_prefix_hash(m2)

    def test_differs_when_user_head_changes(self) -> None:
        from aspire_orchestrator.services.openai_client import _compute_prompt_prefix_hash

        m1 = [{"role": "system", "content": "Same"}, {"role": "user", "content": "User A"}]
        m2 = [{"role": "system", "content": "Same"}, {"role": "user", "content": "User B"}]
        assert _compute_prompt_prefix_hash(m1) != _compute_prompt_prefix_hash(m2)

    def test_only_first_200_chars_of_user_msg_used(self) -> None:
        """Changing characters beyond position 200 of user message should NOT change hash."""
        from aspire_orchestrator.services.openai_client import _compute_prompt_prefix_hash

        base_user = "A" * 200
        m1 = [{"role": "system", "content": "S"}, {"role": "user", "content": base_user + "X"}]
        m2 = [{"role": "system", "content": "S"}, {"role": "user", "content": base_user + "Y"}]
        assert _compute_prompt_prefix_hash(m1) == _compute_prompt_prefix_hash(m2)


# ---------------------------------------------------------------------------
# Integration tests for generate_text_async return shape
# ---------------------------------------------------------------------------

class TestGenerateTextAsyncReturnShape:
    """generate_text_async returns LLMTextResult with usage dict."""

    @pytest.mark.asyncio
    async def test_returns_text_and_usage(self) -> None:
        from aspire_orchestrator.services import openai_client

        mock_response = _make_response("Generated text", usage=_make_usage(
            prompt_tokens=1200, cached_tokens=500
        ))

        with (
            patch.object(openai_client, "_openai_circuit_breaker") as mock_cb,
            patch.object(openai_client, "_resolve_model_for_profile", return_value=("gpt-5.4-mini", False)),
            patch.object(openai_client, "get_llm_cache") as mock_cache_factory,
            patch.object(openai_client, "_get_or_create_async_client") as mock_client_factory,
        ):
            mock_cb.allow_request.return_value = True
            mock_cb.record_success = MagicMock()

            mock_cache = AsyncMock()
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            mock_cache_factory.return_value = mock_cache

            mock_client = MagicMock()
            mock_client.responses.create = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await openai_client.generate_text_async(
                model="gpt-5.4-mini",
                messages=[
                    {"role": "system", "content": "You are a test assistant."},
                    {"role": "user", "content": "Say hello."},
                ],
                api_key="sk-test",
            )

        assert isinstance(result, dict)
        assert "text" in result
        assert "usage" in result
        assert result["text"] == "Generated text"

        usage = result["usage"]
        assert usage["prompt_tokens"] == 1200
        assert usage["cached_tokens"] == 500
        assert abs(usage["cache_hit_ratio"] - 500 / 1200) < 1e-3
        assert usage["model"] == "gpt-5.4-mini"
        assert "latency_ms" in usage
        assert "prompt_prefix_hash" in usage
        assert len(usage["prompt_prefix_hash"]) == 16

    @pytest.mark.asyncio
    async def test_cache_hit_returns_zero_usage(self) -> None:
        """LLM cache hit returns LLMTextResult with zeroed usage."""
        from aspire_orchestrator.services import openai_client

        with (
            patch.object(openai_client, "_openai_circuit_breaker") as mock_cb,
            patch.object(openai_client, "_resolve_model_for_profile", return_value=("gpt-5.4-mini", False)),
            patch.object(openai_client, "get_llm_cache") as mock_cache_factory,
        ):
            mock_cb.allow_request.return_value = True

            mock_cache = AsyncMock()
            mock_cache.get = AsyncMock(return_value="cached text response")
            mock_cache_factory.return_value = mock_cache

            result = await openai_client.generate_text_async(
                model="gpt-5.4-mini",
                messages=[{"role": "user", "content": "Hello"}],
                api_key="sk-test",
            )

        assert result["text"] == "cached text response"
        assert result["usage"]["cached_tokens"] == 0
        assert result["usage"]["prompt_tokens"] == 0
        assert result["usage"]["cache_hit_ratio"] == 0.0

    @pytest.mark.asyncio
    async def test_missing_cached_tokens_defaults_to_zero(self) -> None:
        """When SDK omits prompt_tokens_details, cached_tokens defaults to 0, no crash."""
        from aspire_orchestrator.services import openai_client

        # Older SDK shape: no prompt_tokens_details
        usage_no_details = _make_usage(cached_tokens=None)
        mock_response = _make_response("text", usage=usage_no_details)

        openai_client._warned_missing_cached_tokens = False

        with (
            patch.object(openai_client, "_openai_circuit_breaker") as mock_cb,
            patch.object(openai_client, "_resolve_model_for_profile", return_value=("gpt-5.4-mini", False)),
            patch.object(openai_client, "get_llm_cache") as mock_cache_factory,
            patch.object(openai_client, "_get_or_create_async_client") as mock_client_factory,
        ):
            mock_cb.allow_request.return_value = True
            mock_cb.record_success = MagicMock()

            mock_cache = AsyncMock()
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            mock_cache_factory.return_value = mock_cache

            mock_client = MagicMock()
            mock_client.responses.create = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await openai_client.generate_text_async(
                model="gpt-5.4-mini",
                messages=[{"role": "user", "content": "Test"}],
                api_key="sk-test",
            )

        assert result["usage"]["cached_tokens"] == 0
        assert result["usage"]["cache_hit_ratio"] == 0.0

    @pytest.mark.asyncio
    async def test_structured_log_contains_required_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Structured INFO log emitted by generate_text_async contains all 9h fields."""
        from aspire_orchestrator.services import openai_client

        mock_response = _make_response("log test", usage=_make_usage(
            prompt_tokens=1000, cached_tokens=300
        ))

        with (
            patch.object(openai_client, "_openai_circuit_breaker") as mock_cb,
            patch.object(openai_client, "_resolve_model_for_profile", return_value=("gpt-5-mini", False)),
            patch.object(openai_client, "get_llm_cache") as mock_cache_factory,
            patch.object(openai_client, "_get_or_create_async_client") as mock_client_factory,
            caplog.at_level(logging.INFO, logger="aspire_orchestrator.services.openai_client"),
        ):
            mock_cb.allow_request.return_value = True
            mock_cb.record_success = MagicMock()

            mock_cache = AsyncMock()
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            mock_cache_factory.return_value = mock_cache

            mock_client = MagicMock()
            mock_client.responses.create = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            await openai_client.generate_text_async(
                model="gpt-5-mini",
                messages=[
                    {"role": "system", "content": "System"},
                    {"role": "user", "content": "User"},
                ],
                api_key="sk-test",
            )

        log_messages = " ".join(r.message for r in caplog.records)
        required_tokens = ["9h", "prompt_tokens", "cached_tokens", "cache_hit_ratio",
                           "completion_tokens", "latency_ms", "prompt_prefix_hash"]
        for token in required_tokens:
            assert token in log_messages, f"Missing '{token}' in log output"


class TestGenerateJsonAsyncReturnShape:
    """generate_json_async returns LLMJsonResult with json + usage."""

    @pytest.mark.asyncio
    async def test_returns_json_and_usage(self) -> None:
        from aspire_orchestrator.services import openai_client

        mock_response = _make_response('{"key": "value"}', usage=_make_usage())

        with (
            patch.object(openai_client, "_openai_circuit_breaker") as mock_cb,
            patch.object(openai_client, "_resolve_model_for_profile", return_value=("gpt-5.4-mini", False)),
            patch.object(openai_client, "get_llm_cache") as mock_cache_factory,
            patch.object(openai_client, "_get_or_create_async_client") as mock_client_factory,
        ):
            mock_cb.allow_request.return_value = True
            mock_cb.record_success = MagicMock()

            mock_cache = AsyncMock()
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            mock_cache_factory.return_value = mock_cache

            mock_client = MagicMock()
            mock_client.responses.create = AsyncMock(return_value=mock_response)
            mock_client_factory.return_value = mock_client

            result = await openai_client.generate_json_async(
                model="gpt-5.4-mini",
                messages=[{"role": "user", "content": "Return JSON"}],
                api_key="sk-test",
            )

        assert isinstance(result, dict)
        assert "json" in result
        assert "usage" in result
        assert result["json"] == {"key": "value"}
        assert "prompt_tokens" in result["usage"]
