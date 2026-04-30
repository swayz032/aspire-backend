"""Combined voice-path budget test with user_address resolution.

Output finding #13: Verify that when user_address resolves to a NearestStore
(Round 4 path), the SerpApi call uses voice_path=True (single attempt × 4s
timeout), and the total wall time stays under 4.5s.

Source: output #13 — combined-budget voice test.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks.trades import execute_tool_material_price_check
from aspire_orchestrator.services.adam.places_nearest_finder import NearestStore
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"id": "test", "outcome": "success"},
    )


def _ctx() -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-4111-8111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id="voice-user-address-budget-test",
    )


_NEAREST = NearestStore(
    place_id="ChIJtest1234",
    name="Home Depot Tallahassee",
    address="3200 Capital Cir NE, Tallahassee, FL 32308, USA",
    postal_code="32308",
    lat=30.4867,
    lng=-84.2977,
    distance_miles=2.3,
    photo_url="/v1/places/photo?ref=places/ChIJtest1234/photos/AUjq9jm",
    user_lat=30.4518,
    user_lng=-84.2807,
)

_HD_OK = {
    "results": [
        {
            "title": "USG Sheetrock 4x8 1/2in Drywall",
            "brand": "USG",
            "product_id": "202011387",
            "price": 14.98,
            "rating": 4.6,
            "reviews": 3120,
            "link": "https://homedepot.com/p/sheetrock",
            "image_url": "/v1/places/photo?ref=places/X/photos/Y",
            "pickup": {"store_id": "0254", "store_name": "West Tallahassee", "quantity": 48},
            "delivery": {"has_delivery": True},
            "thumbnail": "https://example.com/thumb.jpg",
        },
    ],
    "query": "sheetrock",
    "result_count": 1,
    "store": {"store_id": "0254", "store_name": "West Tallahassee", "city": "Tallahassee", "state": "FL"},
}


@pytest.mark.asyncio
async def test_user_address_path_uses_single_hd_attempt():
    """Output #13: user_address → NearestStore pins zip; voice_path stays True → 1 HD call.

    user_address is provided (Anam dynamic variable). voice_path=None auto-detection
    must evaluate BEFORE the resolver updates zip_code. The zip from NearestStore
    must NOT flip voice_path to False (which would trigger 3 × 8s retries).
    """
    hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _HD_OK))

    async def fast_resolver(address: str, timeout: float = 3.0) -> NearestStore:
        await asyncio.sleep(1.0)
        return _NEAREST

    with patch(
        "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
        side_effect=fast_resolver,
    ), patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
    ):
        await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            user_address="1234 N Monroe St, Tallahassee, FL 32303",
            # voice_path=None → auto-detect. user_address alone must NOT flip to text path.
        )

    assert hd_mock.await_count == 1, (
        f"user_address + voice_path=None → auto-detect voice → 1 HD call; "
        f"got {hd_mock.await_count} call(s)"
    )


@pytest.mark.asyncio
async def test_user_address_path_passes_4s_timeout_to_hd():
    """Output #13: NearestStore resolver sets zip; SerpApi call still uses timeout=4.0.

    The zip_code pin from NearestStore must not change the SerpApi timeout from
    4.0 (voice) to 8.0 (text). The F-CRIT-1 fix locks voice_path before the
    resolver runs.
    """
    hd_mock = AsyncMock(return_value=_ok("serpapi_home_depot.search", _HD_OK))

    async def fast_resolver(address: str, timeout: float = 3.0) -> NearestStore:
        await asyncio.sleep(1.0)
        return _NEAREST

    with patch(
        "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
        side_effect=fast_resolver,
    ), patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
    ):
        await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            user_address="1234 N Monroe St, Tallahassee, FL 32303",
        )

    kwargs = hd_mock.await_args.kwargs
    assert kwargs.get("timeout") == 4.0, (
        f"user_address voice path must use timeout=4.0; got {kwargs.get('timeout')!r}"
    )


@pytest.mark.asyncio
async def test_user_address_end_to_end_under_4500ms():
    """Output #13: 1.0s resolver + 1.5s SerpApi → total < 4.5s.

    Simulates the real-world case from transcript 426b860b: Anam sends
    user_address; NearestStore resolver takes ~1s; SerpApi takes ~1.5s.
    Total must stay within the 5s Anam ceiling (budget: <4.5s).
    """
    async def slow_hd(*args, **kwargs):
        await asyncio.sleep(1.5)
        return _ok("serpapi_home_depot.search", _HD_OK)

    async def medium_resolver(address: str, timeout: float = 3.0) -> NearestStore:
        await asyncio.sleep(1.0)
        return _NEAREST

    with patch(
        "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
        side_effect=medium_resolver,
    ), patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        side_effect=slow_hd,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
    ):
        start = time.perf_counter()
        await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            user_address="1234 N Monroe St, Tallahassee, FL 32303",
        )
        elapsed = time.perf_counter() - start

    assert elapsed < 4.5, (
        f"user_address voice path took {elapsed:.3f}s; budget is <4.5s "
        "(1.0s resolver + 1.5s SerpApi + overhead). F-CRIT-1 fix must hold."
    )
