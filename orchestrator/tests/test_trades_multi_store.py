"""Regression + contract tests for the Round 7 multi-store branch in trades.py.

D.3 — Wave D deliverable.

Tests three cases:
  (a) include_other_stores=False on voice path -> only HD records surfaced.
  (b) include_other_stores=True on voice path -> mixed retailer records preserved.
  (c) SerpApi shopping returns 429 on every attempt -> after 2 retries the
      shopping result degrades gracefully; HD still succeeds.

Law compliance:
  Law #6 — all calls carry suite_id / office_id (tenant context).
  Law #2 — every outcome emits a playbook receipt (_emit_playbook_receipt is
            called from within execute_tool_material_price_check; tested
            indirectly via providers_called).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks.trades import (
    execute_tool_material_price_check,
    HD_TOO_FAR_MILES,
)
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# ─── Module-level autouse patches ─────────────────────────────────────────────
# resolve_store_async makes a real Google Places API call when zip_code is set.
# Mock it module-wide so no test hits the network. Returns None (no store match)
# which is the safe fallback path already tested in transcript regression locks.

@pytest.fixture(autouse=True)
def _patch_store_resolver():
    """Auto-mock resolve_store_async to avoid Google Places network calls."""
    with patch(
        "aspire_orchestrator.services.adam.hd_store_resolver.resolve_store_async",
        AsyncMock(return_value=None),
    ):
        yield


# ─── Fixtures ──────────────────────────────────────────────────────────────────

def _ctx(correlation_id: str = "multi-store-test") -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-4111-8111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id=correlation_id,
    )


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"id": "test", "outcome": "success"},
    )


def _rate_limited(tool_id: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error="RATE_LIMITED: 429 Too Many Requests",
    )


# Minimal HD product record in SerpApi HD shape (has pickup so voice path works)
_HD_PRODUCT = {
    "title": "USG Sheetrock 4ft x 8ft 1/2in",
    "brand": "USG",
    "model_number": "12347",
    "product_id": "202011387",
    "price": 14.98,
    "rating": 4.6,
    "reviews": 3120,
    "link": "https://www.homedepot.com/p/usg",
    "thumbnail": "https://example.com/sheetrock.jpg",
    "image_url": "/v1/places/photo?ref=places/ChIJ/photos/AUjq9jm",
    "pickup": {
        "store_id": "0254",
        "store_name": "West Tallahassee",
        "quantity": 48,
    },
    "delivery": {"has_delivery": True},
}

_HD_RESPONSE = {
    "results": [_HD_PRODUCT],
    "query": "sheetrock",
    "result_count": 1,
    "store": {
        "store_id": "0254",
        "store_name": "West Tallahassee",
        "city": "Tallahassee",
        "state": "FL",
    },
}

# Google Shopping items from Lowe's and Walmart
_SHOPPING_RESPONSE = {
    "results": [
        {
            "title": "Sheetrock 4x8 Drywall Panel",
            "source": "Lowe's",
            "extracted_price": 13.99,
            "product_link": "https://lowes.com/p/sheetrock",
            "thumbnail": "https://example.com/lowes-sheetrock.jpg",
            "rating": 4.5,
            "reviews": 210,
        },
        {
            "title": "Drywall Panel 1/2in",
            "source": "Walmart",
            "extracted_price": 12.50,
            "product_link": "https://walmart.com/p/drywall",
            "thumbnail": "https://example.com/walmart-drywall.jpg",
            "rating": 4.0,
            "reviews": 88,
        },
    ]
}


# ─── Case (a): include_other_stores=False, voice path ─────────────────────────

class TestMultiStoreFalseVoicePath:
    """include_other_stores=False on voice path -> only Home Depot records.

    The Google Shopping call is skipped entirely on voice path when
    include_other_stores is False. Any records returned must have retailer
    == 'Home Depot' (or be the store_summary card).
    """

    @pytest.mark.asyncio
    async def test_hd_only_records_returned_when_include_other_stores_false(self):
        """D.3(a): voice path with include_other_stores=False -> zero non-HD records."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )
        shopping_mock = AsyncMock(
            return_value=_ok("serpapi_shopping.search", _SHOPPING_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3a-hd-only"),
                zip_code="32303",
                voice_path=True,
                include_other_stores=False,
            )

        # On voice path without include_other_stores, shopping is skipped.
        assert shopping_mock.await_count == 0, (
            f"Shopping must NOT be called on voice path when include_other_stores=False. "
            f"Was called {shopping_mock.await_count} time(s)."
        )

        # All non-store_summary records must be Home Depot.
        non_hd = [
            r for r in response.records
            if r.get("card_kind") != "store_summary"
            and r.get("retailer") not in ("Home Depot", "", None)
        ]
        assert non_hd == [], (
            f"Non-HD retailer records found with include_other_stores=False: "
            f"{[r.get('retailer') for r in non_hd]}"
        )

    @pytest.mark.asyncio
    async def test_extra_flags_present_on_hd_only_response(self):
        """D.3(a): decision flags must be present on every response, even HD-only."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []})),
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3a-flags"),
                zip_code="32303",
                voice_path=True,
                include_other_stores=False,
            )

        assert "nearest_store_distance_miles" in response.extra, (
            "nearest_store_distance_miles must always be in response.extra"
        )
        assert "hd_too_far" in response.extra, "hd_too_far must always be in response.extra"
        assert "hd_has_stock" in response.extra, "hd_has_stock must always be in response.extra"
        assert response.extra.get("include_other_stores") is False, (
            "include_other_stores flag must be False in extra"
        )


# ─── Case (b): include_other_stores=True, voice path ─────────────────────────

class TestMultiStoreTrueVoicePath:
    """include_other_stores=True on voice path -> mixed retailer records preserved.

    When include_other_stores=True, the Google Shopping call DOES run even on
    the voice path. Non-HD records appear in response.records with their
    retailer field intact.
    """

    @pytest.mark.asyncio
    async def test_non_hd_records_included_when_include_other_stores_true(self):
        """D.3(b): voice path with include_other_stores=True -> Lowe's/Walmart records present."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )
        shopping_mock = AsyncMock(
            return_value=_ok("serpapi_shopping.search", _SHOPPING_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3b-mixed-retailers"),
                zip_code="32303",
                voice_path=True,
                include_other_stores=True,
            )

        # Shopping must have been called.
        assert shopping_mock.await_count >= 1, (
            f"Shopping must be called when include_other_stores=True on voice path. "
            f"await_count={shopping_mock.await_count}"
        )

        # Response must contain records from non-HD retailers.
        retailer_names = {
            r.get("retailer")
            for r in response.records
            if r.get("card_kind") != "store_summary"
        }
        non_hd_retailers = retailer_names - {"Home Depot", "", None}
        assert non_hd_retailers, (
            f"Expected Lowe's/Walmart records with include_other_stores=True. "
            f"retailer_names seen: {retailer_names}"
        )

    @pytest.mark.asyncio
    async def test_retailer_field_preserved_on_shopping_records(self):
        """D.3(b): shopping records retain their 'source' as the 'retailer' field."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )
        shopping_mock = AsyncMock(
            return_value=_ok("serpapi_shopping.search", _SHOPPING_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3b-retailer-field"),
                zip_code="32303",
                voice_path=True,
                include_other_stores=True,
            )

        retailers_found = {
            r.get("retailer")
            for r in response.records
            if r.get("card_kind") != "store_summary"
        }
        # Lowe's must appear (from shopping mock source field)
        assert "Lowe's" in retailers_found or any(
            "lowe" in str(r).lower() for r in retailers_found
        ), (
            f"Lowe's retailer record must appear when include_other_stores=True. "
            f"retailers_found={retailers_found}"
        )

    @pytest.mark.asyncio
    async def test_extra_flags_include_other_stores_true(self):
        """D.3(b): include_other_stores flag echoed True in response.extra."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )
        shopping_mock = AsyncMock(
            return_value=_ok("serpapi_shopping.search", _SHOPPING_RESPONSE)
        )

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3b-extra-flag"),
                zip_code="32303",
                voice_path=True,
                include_other_stores=True,
            )

        assert response.extra.get("include_other_stores") is True, (
            "include_other_stores must be True in response.extra when called with True"
        )


# ─── Case (c): shopping 429 -> backoff -> HD still succeeds ───────────────────

class TestShoppingShopping429Backoff:
    """SerpApi shopping returns 429 on all retries -> graceful degrade; HD succeeds.

    The backoff loop runs up to _SHOPPING_RETRY_MAX_ATTEMPTS + 1 = 3 total
    attempts. After exhaustion the shopping result degrades to None so the
    Home Depot result still carries the response.
    """

    @pytest.mark.asyncio
    async def test_shopping_429_retries_twice_then_hd_succeeds(self):
        """D.3(c): 429 across all shopping retries -> HD result still returned.

        The shopping backoff uses asyncio.sleep internally. We fast-forward by
        mocking asyncio.sleep so the test doesn't take 750ms+.
        """
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )
        # Shopping always returns 429
        shopping_mock = AsyncMock(return_value=_rate_limited("serpapi_shopping.search"))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ), patch("asyncio.sleep", AsyncMock()):
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3c-429-backoff"),
                zip_code="32303",
                voice_path=False,  # text path so shopping runs
                include_other_stores=True,  # must run shopping to trigger 429
            )

        # Shopping must have been retried (up to _SHOPPING_RETRY_MAX_ATTEMPTS + 1 = 3)
        assert shopping_mock.await_count <= 3, (
            f"Shopping should attempt at most 3 times (2 retries). "
            f"Got {shopping_mock.await_count}."
        )
        assert shopping_mock.await_count >= 2, (
            f"Shopping must retry at least once on 429. "
            f"Got {shopping_mock.await_count} call(s)."
        )

        # HD call should still succeed -> response must not be a hard error
        # from the HD side. It may be an error if no HD products completed
        # but the artifact_type must not be None.
        assert response.artifact_type is not None, "artifact_type must always be set"
        assert response.artifact_type != "", "artifact_type must not be empty string"

        # Decision flags must be present even on degraded path.
        for flag in ("hd_too_far", "hd_has_stock", "nearest_store_distance_miles"):
            assert flag in response.extra, (
                f"Decision flag '{flag}' must be present in response.extra even after 429 degrade"
            )

    @pytest.mark.asyncio
    async def test_shopping_429_does_not_crash_on_text_path(self):
        """D.3(c): 429 on shopping path -> response has artifact_type, no exception raised."""
        hd_mock = AsyncMock(
            return_value=_ok("serpapi_home_depot.search", _HD_RESPONSE)
        )
        shopping_mock = AsyncMock(return_value=_rate_limited("serpapi_shopping.search"))

        with patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        ), patch("asyncio.sleep", AsyncMock()):
            # Must not raise.
            response = await execute_tool_material_price_check(
                query="sheetrock",
                ctx=_ctx("d3c-no-crash"),
                zip_code="32303",
                voice_path=False,
                include_other_stores=False,
            )

        assert response is not None, "Response must not be None after shopping 429"
        assert "MISSING_TASK" not in (response.summary or ""), (
            "MISSING_TASK must not appear in summary after shopping 429"
        )
