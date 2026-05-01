"""Adam response flags contract — Wave D.5.

Verifies that `execute_tool_material_price_check` always emits the three
decision flags in `response.extra`:
  - nearest_store_distance_miles: float | None
  - hd_too_far: bool
  - hd_has_stock: bool

Two fixtures are exercised:

  Fixture 1 — Tallahassee, FL: user_address near a Home Depot ~3 miles away.
    -> nearest_store_distance_miles ~= 3.0 (± wide tolerance)
    -> hd_too_far: False (3mi < HD_TOO_FAR_MILES=25)
    -> hd_has_stock: True (mocked product has quantity > 0)

  Fixture 2 — Rural Wyoming: user_address with no HD within 100mi.
    -> hd_too_far: True (no HD resolved)
    -> nearest_store_distance_miles: None (no store)

Law compliance:
  Law #3 — Fail closed: flags must ALWAYS be present on every response shape
            (success, error, degraded). Missing flag = fail.
  Law #2 — Receipt: providers_called logged for both fixtures.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.places_nearest_finder import NearestStore
from aspire_orchestrator.services.adam.playbooks.trades import (
    HD_TOO_FAR_MILES,
    execute_tool_material_price_check,
)
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# ─── Module-level autouse patches ─────────────────────────────────────────────
# resolve_store_async makes a real Google Places API call when zip_code is set.
# D.5 tests use user_address (which triggers find_nearest_home_depot_by_address,
# mocked per-test) not zip_code directly. But resolve_store_async is also called
# if zip_code gets set after nearest-store resolution. Mock it globally.

@pytest.fixture(autouse=True)
def _patch_store_resolver():
    """Auto-mock resolve_store_async to avoid Google Places network calls."""
    with patch(
        "aspire_orchestrator.services.adam.hd_store_resolver.resolve_store_async",
        AsyncMock(return_value=None),
    ):
        yield


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _ctx(label: str = "flags-test") -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-4111-8111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id=label,
    )


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"id": "test", "outcome": "success"},
    )


# ─── Tallahassee: HD ~3mi away, in stock ──────────────────────────────────────

_TALLAHASSEE_NEAREST = NearestStore(
    place_id="ChIJTallahasseeHD",
    name="Home Depot Tallahassee",
    address="1925 W Tennessee St, Tallahassee, FL 32304",
    postal_code="32304",
    lat=30.4429,
    lng=-84.3018,
    distance_miles=3.1,
    photo_url="",
    user_lat=30.4615,
    user_lng=-84.2829,
)

_TALLAHASSEE_HD_RESPONSE = {
    "results": [
        {
            "title": "USG Sheetrock 4x8 1/2in",
            "brand": "USG",
            "product_id": "202011387",
            "price": 14.98,
            "rating": 4.6,
            "reviews": 3120,
            "link": "https://homedepot.com/p/usg",
            "thumbnail": "https://example.com/thumb.jpg",
            "image_url": "/v1/places/photo?ref=places/ChIJ/photos/AU",
            "pickup": {
                "store_id": "0254",
                "store_name": "West Tallahassee",
                "quantity": 48,
            },
            "delivery": {"has_delivery": True},
        }
    ],
    "query": "sheetrock",
    "result_count": 1,
    "store": {
        "store_id": "0254",
        "store_name": "West Tallahassee",
        "city": "Tallahassee",
        "state": "FL",
    },
}


class TestTallahasseeResponseFlags:
    """Fixture 1: Tallahassee user, HD ~3mi away, product in stock."""

    @pytest.mark.asyncio
    async def test_nearest_store_distance_miles_approx_3(self):
        """D.5(1): nearest_store_distance_miles reported as ~3.0 for Tallahassee HD."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=_TALLAHASSEE_NEAREST),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d5-tallahassee-distance"),
                user_address="123 Main St, Tallahassee, FL 32301",
                voice_path=True,
            )

        dist = response.extra.get("nearest_store_distance_miles")
        assert dist is not None, (
            "nearest_store_distance_miles must not be None when a nearby HD is found"
        )
        assert isinstance(dist, (int, float)), (
            f"nearest_store_distance_miles must be numeric, got {type(dist)}"
        )
        # The mock returns distance_miles=3.1 — allow generous tolerance.
        assert 0.5 <= dist <= 10.0, (
            f"nearest_store_distance_miles={dist} is outside expected range [0.5, 10.0] "
            f"for the Tallahassee HD fixture (expected ~3.1 mi)"
        )

    @pytest.mark.asyncio
    async def test_hd_too_far_false_for_nearby_tallahassee_hd(self):
        """D.5(1): hd_too_far must be False when HD is ~3mi away (< 25mi threshold)."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=_TALLAHASSEE_NEAREST),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d5-tallahassee-not-far"),
                user_address="123 Main St, Tallahassee, FL 32301",
                voice_path=True,
            )

        hd_too_far = response.extra.get("hd_too_far")
        assert hd_too_far is False, (
            f"hd_too_far must be False for a ~3mi HD (threshold={HD_TOO_FAR_MILES}mi). "
            f"Got hd_too_far={hd_too_far!r}"
        )

    @pytest.mark.asyncio
    async def test_hd_has_stock_true_for_tallahassee_product_with_quantity(self):
        """D.5(1): hd_has_stock must be True when mocked pickup.quantity > 0."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=_TALLAHASSEE_NEAREST),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d5-tallahassee-stock"),
                user_address="123 Main St, Tallahassee, FL 32301",
                voice_path=True,
            )

        hd_has_stock = response.extra.get("hd_has_stock")
        assert isinstance(hd_has_stock, bool), (
            f"hd_has_stock must be a bool. Got {type(hd_has_stock)}"
        )
        # The mocked product has pickup.quantity=48 so stock must be True.
        assert hd_has_stock is True, (
            f"hd_has_stock must be True when mocked product has quantity=48. "
            f"Got hd_has_stock={hd_has_stock!r}"
        )

    @pytest.mark.asyncio
    async def test_all_three_flags_always_present_tallahassee(self):
        """D.5(1): all three decision flags must be present on every response shape."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _TALLAHASSEE_HD_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=_TALLAHASSEE_NEAREST),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d5-tallahassee-all-flags"),
                user_address="123 Main St, Tallahassee, FL 32301",
                voice_path=True,
            )

        for flag in ("nearest_store_distance_miles", "hd_too_far", "hd_has_stock"):
            assert flag in response.extra, (
                f"Decision flag '{flag}' MISSING from response.extra. "
                f"extra keys: {list(response.extra.keys())}"
            )


# ─── Rural Wyoming: no HD within 100mi ────────────────────────────────────────

class TestRuralWyomingResponseFlags:
    """Fixture 2: rural Wyoming user_address, no HD within 50mi radius.

    find_nearest_home_depot_by_address returns None (Google Places found no
    HD within the search radius). hd_too_far must be True.
    """

    @pytest.mark.asyncio
    async def test_hd_too_far_true_when_no_hd_found_in_wyoming(self):
        """D.5(2): hd_too_far must be True when nearest HD resolver returns None."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", {"results": [], "result_count": 0, "store": {}})
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=None),  # No HD within 50mi
        ):
            response = await execute_tool_material_price_check(
                query="drywall",
                ctx=_ctx("d5-wyoming-too-far"),
                user_address="123 Nowhere Rd, Pinedale, WY 82941",
                voice_path=True,
            )

        hd_too_far = response.extra.get("hd_too_far")
        assert hd_too_far is True, (
            f"hd_too_far must be True when no HD found within radius. "
            f"Got hd_too_far={hd_too_far!r}"
        )

    @pytest.mark.asyncio
    async def test_nearest_distance_none_when_no_hd_found_in_wyoming(self):
        """D.5(2): nearest_store_distance_miles must be None when HD resolver returns None."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", {"results": [], "result_count": 0, "store": {}})
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=None),
        ):
            response = await execute_tool_material_price_check(
                query="drywall",
                ctx=_ctx("d5-wyoming-no-distance"),
                user_address="123 Nowhere Rd, Pinedale, WY 82941",
                voice_path=True,
            )

        dist = response.extra.get("nearest_store_distance_miles")
        assert dist is None, (
            f"nearest_store_distance_miles must be None when HD resolver returns None. "
            f"Got {dist!r}"
        )

    @pytest.mark.asyncio
    async def test_all_three_flags_present_even_with_no_hd(self):
        """D.5(2): all three flags present even when HD is not found (fail-closed Law #3)."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", {"results": [], "result_count": 0, "store": {}})
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ), patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            AsyncMock(return_value=None),
        ):
            response = await execute_tool_material_price_check(
                query="drywall",
                ctx=_ctx("d5-wyoming-all-flags"),
                user_address="123 Nowhere Rd, Pinedale, WY 82941",
                voice_path=True,
            )

        for flag in ("nearest_store_distance_miles", "hd_too_far", "hd_has_stock"):
            assert flag in response.extra, (
                f"Decision flag '{flag}' MISSING from response.extra on Wyoming fixture. "
                f"extra keys: {list(response.extra.keys())}"
            )

    @pytest.mark.asyncio
    async def test_hd_too_far_threshold_is_25_miles(self):
        """D.5: HD_TOO_FAR_MILES constant must be 25.0 (per plan guardrail)."""
        assert HD_TOO_FAR_MILES == 25.0, (
            f"HD_TOO_FAR_MILES must be 25.0 per plan D.5 and code constant. "
            f"Got {HD_TOO_FAR_MILES}"
        )
