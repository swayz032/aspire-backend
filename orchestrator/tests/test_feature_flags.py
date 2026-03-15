"""Tests for feature_flags service — cache, receipts, fail-closed behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services import feature_flags
from aspire_orchestrator.services.feature_flags import (
    _cache,
    clear_cache,
    is_feature_enabled,
)
from aspire_orchestrator.services.supabase_client import SupabaseClientError


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    """Ensure cache is empty before and after each test."""
    clear_cache()
    yield  # type: ignore[misc]
    clear_cache()


TENANT = "00000000-0000-0000-0000-000000000099"


class TestIsFeatureEnabled:
    """Core flag evaluation logic."""

    @pytest.mark.asyncio
    async def test_enabled_flag_returns_true(self) -> None:
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            new_callable=AsyncMock,
            return_value=[{"enabled": True}],
        ):
            result = await is_feature_enabled("dark_mode", TENANT)
        assert result is True

    @pytest.mark.asyncio
    async def test_disabled_flag_returns_false(self) -> None:
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            new_callable=AsyncMock,
            return_value=[{"enabled": False}],
        ):
            result = await is_feature_enabled("dark_mode", TENANT)
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_flag_returns_false(self) -> None:
        """Law #3: Missing flag = fail closed = disabled."""
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await is_feature_enabled("nonexistent", TENANT)
        assert result is False

    @pytest.mark.asyncio
    async def test_supabase_error_returns_false(self) -> None:
        """Law #3: Supabase failure = fail closed = disabled."""
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            new_callable=AsyncMock,
            side_effect=SupabaseClientError("select", 500, "Internal Server Error"),
        ):
            result = await is_feature_enabled("some_flag", TENANT)
        assert result is False

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_false(self) -> None:
        """Law #3: Any unexpected error = fail closed = disabled."""
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network gone"),
        ):
            result = await is_feature_enabled("some_flag", TENANT)
        assert result is False


class TestCache:
    """In-memory cache with 60s TTL."""

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self) -> None:
        mock_select = AsyncMock(return_value=[{"enabled": True}])
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            mock_select,
        ):
            await is_feature_enabled("cached_flag", TENANT)
            await is_feature_enabled("cached_flag", TENANT)

        # Supabase should only be called once — second call served from cache
        assert mock_select.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self) -> None:
        mock_select = AsyncMock(return_value=[{"enabled": True}])
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            mock_select,
        ):
            await is_feature_enabled("ttl_flag", TENANT)

        # Expire the cache entry manually
        key = ("ttl_flag", TENANT)
        _cache[key] = (_cache[key][0], 0.0)  # Set expiry to past

        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            mock_select,
        ):
            await is_feature_enabled("ttl_flag", TENANT)

        assert mock_select.call_count == 2

    @pytest.mark.asyncio
    async def test_different_tenants_have_separate_cache(self) -> None:
        """Law #6: Tenant isolation in cache keys."""
        mock_select = AsyncMock(return_value=[{"enabled": True}])
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            mock_select,
        ):
            await is_feature_enabled("shared_flag", "tenant-a")
            await is_feature_enabled("shared_flag", "tenant-b")

        # Each tenant should trigger a separate Supabase query
        assert mock_select.call_count == 2

    @pytest.mark.asyncio
    async def test_error_result_is_cached(self) -> None:
        """Errors are cached to prevent hammering a broken Supabase."""
        mock_select = AsyncMock(
            side_effect=SupabaseClientError("select", 500, "down"),
        )
        with patch(
            "aspire_orchestrator.services.feature_flags.supabase_select",
            mock_select,
        ):
            await is_feature_enabled("broken_flag", TENANT)
            await is_feature_enabled("broken_flag", TENANT)

        # Only one call — the error result (False) is cached
        assert mock_select.call_count == 1

    def test_clear_cache_empties_all_entries(self) -> None:
        _cache[("flag", "tenant")] = (True, 9999999999.0)
        assert len(_cache) == 1
        clear_cache()
        assert len(_cache) == 0


class TestReceipts:
    """Law #2: Every flag evaluation emits a receipt."""

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_cache_hit(self) -> None:
        mock_store = patch(
            "aspire_orchestrator.services.feature_flags.store_receipts",
        )
        mock_select = AsyncMock(return_value=[{"enabled": True}])

        with (
            patch(
                "aspire_orchestrator.services.feature_flags.supabase_select",
                mock_select,
            ),
            mock_store as store_mock,
        ):
            await is_feature_enabled("receipt_flag", TENANT)
            # First call emits receipt with source=supabase
            assert store_mock.call_count == 1
            receipt = store_mock.call_args_list[0][0][0][0]
            assert receipt["redacted_outputs"]["source"] == "supabase"

            await is_feature_enabled("receipt_flag", TENANT)
            # Second call emits receipt with source=cache
            assert store_mock.call_count == 2
            receipt = store_mock.call_args_list[1][0][0][0]
            assert receipt["redacted_outputs"]["source"] == "cache"

    @pytest.mark.asyncio
    async def test_receipt_fields_are_correct(self) -> None:
        with (
            patch(
                "aspire_orchestrator.services.feature_flags.supabase_select",
                new_callable=AsyncMock,
                return_value=[{"enabled": True}],
            ),
            patch(
                "aspire_orchestrator.services.feature_flags.store_receipts",
            ) as store_mock,
        ):
            await is_feature_enabled("test_flag", TENANT)

        receipt: dict[str, Any] = store_mock.call_args_list[0][0][0][0]
        assert receipt["receipt_type"] == "feature_flag_evaluation"
        assert receipt["action_type"] == "feature_flag.check"
        assert receipt["outcome"] == "success"
        assert receipt["risk_tier"] == "GREEN"
        assert receipt["suite_id"] == TENANT
        assert receipt["redacted_inputs"]["flag_name"] == "test_flag"
        assert receipt["redacted_outputs"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_receipt_on_error_includes_error_message(self) -> None:
        with (
            patch(
                "aspire_orchestrator.services.feature_flags.supabase_select",
                new_callable=AsyncMock,
                side_effect=SupabaseClientError("select", 500, "boom"),
            ),
            patch(
                "aspire_orchestrator.services.feature_flags.store_receipts",
            ) as store_mock,
        ):
            await is_feature_enabled("err_flag", TENANT)

        receipt: dict[str, Any] = store_mock.call_args_list[0][0][0][0]
        assert receipt["outcome"] == "failed"
        assert "boom" in receipt["error_message"]
