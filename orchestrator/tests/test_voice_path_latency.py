"""Voice path latency budget — Wave C, Task #24.

Voice budget: 5s end-to-end for a TOOL_MATERIAL_PRICE_CHECK card pack.
Single-attempt path. SerpApi call must time out at 4s (NOT 8s × 3 retries = 24s).

Tests:
  - Voice path makes exactly ONE SerpApi home_depot call (no 3-attempt loop).
  - Voice path skips the Google Shopping cross-check.
  - Voice path total wall time stays under 4s when SerpApi returns in 1.5s.
  - SerpApi timeout = 4s on voice (vs 8s default).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks.trades import (
    execute_tool_material_price_check,
)
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


_HD_OK = {
    "results": [
        {
            "title": "Milwaukee M18 FUEL Impact Driver Kit",
            "brand": "Milwaukee",
            "model_number": "2953-22",
            "product_id": "302245363",
            "price": 199.0,
            "rating": 4.8,
            "reviews": 2341,
            "link": "https://homedepot.com/p/milwaukee-impact",
            "pickup": {"store_id": "801", "store_name": "W Mobile", "quantity": 15},
            "delivery": {"has_delivery": True},
            "thumbnail": "https://example.com/thumb.jpg",
        },
    ],
    "query": "impact driver",
    "result_count": 1,
    "store": {"store_id": "801", "store_name": "W Mobile"},
}


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"id": "test", "outcome": "success"},
    )


def _ctx() -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-1111-1111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id="voice-latency-test",
    )


@pytest.mark.asyncio
async def test_voice_path_single_attempt_no_shopping():
    """Voice path: one HD call, zero Shopping calls."""
    hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _HD_OK))
    shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        shopping_mock,
    ):
        await execute_tool_material_price_check(
            query="impact driver",
            ctx=_ctx(),
            voice_path=True,
        )

    assert hd_mock.await_count == 1, "voice path must run exactly one HD attempt"
    assert shopping_mock.await_count == 0, "voice path must skip Google Shopping"


@pytest.mark.asyncio
async def test_voice_path_passes_4s_timeout():
    """Voice path must pass timeout=4.0 to the HD client."""
    hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _HD_OK))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ):
        await execute_tool_material_price_check(
            query="impact driver",
            ctx=_ctx(),
            voice_path=True,
        )

    kwargs = hd_mock.await_args.kwargs
    assert kwargs.get("timeout") == 4.0, (
        f"voice path must pass timeout=4.0, got {kwargs.get('timeout')!r}"
    )


@pytest.mark.asyncio
async def test_voice_path_under_budget_when_serpapi_returns_in_1500ms():
    """Voice path total wall time + retries < 4s when SerpApi takes 1.5s."""
    async def slow_hd(*args, **kwargs):
        await asyncio.sleep(1.5)
        return _ok("serpapi_home_depot.search", _HD_OK)

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        side_effect=slow_hd,
    ):
        start = time.perf_counter()
        await execute_tool_material_price_check(
            query="impact driver",
            ctx=_ctx(),
            voice_path=True,
        )
        elapsed = time.perf_counter() - start

    assert elapsed < 4.0, (
        f"voice path took {elapsed:.2f}s; budget is <4s "
        "(single attempt × 1.5s mock + overhead)"
    )


@pytest.mark.asyncio
async def test_text_path_runs_three_attempts_with_8s_timeout():
    """Non-voice path keeps the 3-attempt loop and 8s default timeout."""
    hd_mock = AsyncMock(return_value=_ok(
        "serpapi_home_depot.search",
        {"results": [], "query": "impact driver", "result_count": 0, "store": {}},
    ))
    shopping_mock = AsyncMock(return_value=_ok(
        "serpapi_shopping.search",
        {"results": [], "query": "impact driver", "result_count": 0},
    ))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        shopping_mock,
    ):
        await execute_tool_material_price_check(
            query="impact driver",
            ctx=_ctx(),
            zip_code="36695",
            voice_path=False,
        )

    assert hd_mock.await_count == 3, "text path must run all 3 retry attempts"
    for call in hd_mock.await_args_list:
        assert call.kwargs.get("timeout") == 8.0


@pytest.mark.asyncio
async def test_voice_path_auto_detected_when_no_zip_or_store():
    """voice_path=None + no zip + no store_id auto-detects voice mode."""
    hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _HD_OK))
    shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        shopping_mock,
    ):
        await execute_tool_material_price_check(
            query="impact driver",
            ctx=_ctx(),
        )

    assert hd_mock.await_count == 1
    assert shopping_mock.await_count == 0


@pytest.mark.asyncio
async def test_voice_path_resolver_capped_at_1500ms():
    """Voice path: resolver wrapped in asyncio.wait_for(timeout=1.5)."""
    async def slow_resolver(*args, **kwargs):
        await asyncio.sleep(3.0)
        return {
            "store_id": "0254",
            "store_name": "Home Depot - Tallahassee",
            "address": "3200 Capital Cir Ne",
            "city": "Tallahassee",
            "state": "FL",
            "postal_code": "32308",
            "phone": "",
            "website": "",
            "image_url": "",
        }

    hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _HD_OK))

    with patch(
        "aspire_orchestrator.services.adam.hd_store_resolver.resolve_store_async",
        side_effect=slow_resolver,
    ), patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ):
        start = time.perf_counter()
        await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            zip_code="32308",
            voice_path=True,
        )
        elapsed = time.perf_counter() - start

    # Resolver should have timed out at 1.5s, leaving ~3s slack for SerpApi
    # synthetic mock to return immediately. Total budget < 2.5s.
    assert elapsed < 2.5, (
        f"voice path took {elapsed:.2f}s; resolver should cap at 1.5s "
        "and SerpApi mock returns instantly"
    )


@pytest.mark.asyncio
async def test_voice_path_end_to_end_under_4500ms_with_resolver_and_serpapi():
    """End-to-end voice path budget: 1.0s resolver + 1.5s SerpApi < 4.5s."""
    async def medium_resolver(*args, **kwargs):
        await asyncio.sleep(1.0)
        return {
            "store_id": "0254",
            "store_name": "Home Depot - Tallahassee",
            "address": "3200 Capital Cir Ne",
            "city": "Tallahassee",
            "state": "FL",
            "postal_code": "32308",
            "phone": "",
            "website": "",
            "image_url": "https://places.googleapis.com/v1/places/X/photos/Y/media",
        }

    async def slow_hd(*args, **kwargs):
        await asyncio.sleep(1.5)
        return _ok("serpapi_home_depot.search", _HD_OK)

    with patch(
        "aspire_orchestrator.services.adam.hd_store_resolver.resolve_store_async",
        side_effect=medium_resolver,
    ), patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        side_effect=slow_hd,
    ):
        start = time.perf_counter()
        await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            zip_code="32308",
            voice_path=True,
        )
        elapsed = time.perf_counter() - start

    assert elapsed < 4.5, (
        f"voice path took {elapsed:.2f}s; budget is <4.5s "
        "(1.0s resolver + 1.5s SerpApi sequential + small overhead)"
    )
