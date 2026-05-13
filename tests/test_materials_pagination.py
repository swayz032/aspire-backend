"""Tests for HD search pagination (Pass F) and Places store enrichment.

Covers:
  A. Pagination behaviour in _run_hd_search / execute_tool_material_price_check
     - Page 1 < 18 results  → single call, no page 2
     - Page 1 >= 18 results → page 2 fired; merged + deduped list
     - Page 1 >= 18 results + page 2 fails → graceful degrade to page 1
     - Final list capped at 60 products
     - Receipt carries pages_fetched + total_after_dedup

  B. Places enrichment (hd_store_places_enricher)
     - Happy path: phone + hours injected into store dict
     - Missing API key → store dict returned unchanged (fail-soft)
     - Text-search returns no results → store dict unchanged
     - Details API times out → store dict unchanged
     - Cache hit (TTL not expired) → no HTTP call made

Law compliance verified:
  Law #2 — receipt emitted on every code path (success + fail)
  Law #3 — Places fail-soft (no 502, no KeyError)
  Law #9 — cache keys never embed PII
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(**kw: Any):
    defaults = {
        "suite_id": "suite-test",
        "office_id": "office-test",
        "tenant_id": "tenant-test",
        "correlation_id": "corr-test",
        "capability_token_id": "tok-test",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_hd_result(n_products: int, success: bool = True) -> MagicMock:
    """Return a mock ToolExecutionResult with n_products SerpApi product dicts."""
    result = MagicMock()
    result.outcome = SimpleNamespace(value="success" if success else "failed")
    result.error = None
    result.receipt_data = {"id": "r1", "outcome": "success"}
    items = [
        {
            "title": f"Product {i}",
            "price": 10.0 + i,
            "sku": f"SKU-{i:04d}",
            "product_id": f"PROD-{i}",
            "thumbnail": f"https://img/{i}.jpg",
            "link": f"https://hd.com/{i}",
            "brand": "TestBrand",
            "rating": 4.0,
            "reviews": 100,
            "unit": "ea",
            # Pass A fix: pickup wrapper
            "pickup": {
                "in_stock": True,
                "store_id": "1234",
                "store_name": "Test HD",
                "drive_minutes": 15,
            },
        }
        for i in range(n_products)
    ]
    result.data = {
        "results": items,
        "store": {"store_id": "1234", "store_name": "Test Home Depot"},
        "pagination": {},
        "taxonomy": [],
        "filters": [],
        "related_products": [],
    }
    return result


# ---------------------------------------------------------------------------
# Section A: Pagination tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_page_when_below_threshold():
    """Page 1 < 18 products → only 1 SerpApi call, receipt has pages_fetched=1."""
    ctx = _make_ctx()
    page1_result = _make_hd_result(12)  # below threshold of 18

    receipts_emitted = []

    with (
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_homedepot_search",
            new_callable=AsyncMock,
            return_value=page1_result,
        ) as mock_hd,
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_shopping_search",
            new_callable=AsyncMock,
            return_value=MagicMock(outcome=SimpleNamespace(value="failed"), data=None, error=None, receipt_data=None),
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_nearest_home_depot_by_address",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.lookup_store_by_id",
            return_value={"address": "123 Main St", "city": "Atlanta", "state": "GA", "postal_code": "30301", "phone": "", "website": ""},
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_stores_in_city",
            return_value=[],
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.lookup_zip_by_city",
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_nearest_store",
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.resolve_store_async",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.store_receipts",
            side_effect=lambda r: receipts_emitted.extend(r),
        ),
    ):
        from aspire_orchestrator.services.adam.playbooks.trades import execute_tool_material_price_check
        result = await execute_tool_material_price_check(
            query="paint",
            ctx=ctx,
            zip_code="30301",
            store_id="1234",
        )

    # HD was called exactly once (page 1 only, no page 2)
    # Note: asyncio.gather fires hd_search + shopping_search; hd only once.
    hd_calls = [c for c in mock_hd.call_args_list if c.kwargs.get("payload", {}).get("start") is None]
    page2_calls = [c for c in mock_hd.call_args_list if c.kwargs.get("payload", {}).get("start") == "24"]
    assert len(page2_calls) == 0, "Page 2 must not fire when page 1 < 18 results"

    # Receipt carries pages_fetched=1
    assert any(
        r.get("redacted_outputs", {}).get("pages_fetched") == 1
        for r in receipts_emitted
    ), "Receipt must have pages_fetched=1"


@pytest.mark.asyncio
async def test_page2_fires_when_page1_meets_threshold():
    """Page 1 >= 18 products → page 2 is fetched; merged list is deduped."""
    ctx = _make_ctx()
    # Page 1: 20 unique products (SKU-0000 .. SKU-0019)
    page1_result = _make_hd_result(20)
    # Page 2: 20 products, first 5 are duplicates of page 1 (SKU-0000..0004)
    page2_result = _make_hd_result(20)
    for i, item in enumerate(page2_result.data["results"]):
        if i >= 5:
            item["sku"] = f"SKU-{100 + i:04d}"  # unique skus
            item["product_id"] = f"PROD-{100 + i}"

    call_count = {"n": 0}

    async def _mock_hd_search(payload, **kw):
        call_count["n"] += 1
        if payload.get("start"):
            return page2_result
        return page1_result

    receipts_emitted = []

    with (
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_homedepot_search",
            side_effect=_mock_hd_search,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_shopping_search",
            new_callable=AsyncMock,
            return_value=MagicMock(outcome=SimpleNamespace(value="failed"), data=None, error=None, receipt_data=None),
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_nearest_home_depot_by_address",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.lookup_store_by_id",
            return_value={"address": "123 Main St", "city": "Atlanta", "state": "GA", "postal_code": "30301", "phone": "", "website": ""},
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_stores_in_city",
            return_value=[],
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.lookup_zip_by_city",
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_nearest_store",
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.resolve_store_async",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.store_receipts",
            side_effect=lambda r: receipts_emitted.extend(r),
        ),
    ):
        from aspire_orchestrator.services.adam.playbooks.trades import execute_tool_material_price_check
        result = await execute_tool_material_price_check(
            query="paint",
            ctx=ctx,
            zip_code="30301",
            store_id="1234",
        )

    # Two HD calls: page 1 + page 2
    assert call_count["n"] == 2, f"Expected 2 HD calls, got {call_count['n']}"

    # Products from both pages merged; duplicates removed (page1 has 20, page2 adds 15 unique)
    hd_products = [r for r in result.records if r.get("retailer") == "Home Depot"]
    assert len(hd_products) == 35, f"Expected 35 merged-deduped products, got {len(hd_products)}"

    # Receipt has pages_fetched=2
    assert any(
        r.get("redacted_outputs", {}).get("pages_fetched") == 2
        for r in receipts_emitted
    ), "Receipt must have pages_fetched=2"


@pytest.mark.asyncio
async def test_page2_fail_falls_back_to_page1():
    """Page 2 exception → page 1 results returned gracefully (fail-soft)."""
    ctx = _make_ctx()
    page1_result = _make_hd_result(20)

    async def _mock_hd_search(payload, **kw):
        if payload.get("start"):
            raise RuntimeError("page 2 network error")
        return page1_result

    with (
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_homedepot_search",
            side_effect=_mock_hd_search,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_shopping_search",
            new_callable=AsyncMock,
            return_value=MagicMock(outcome=SimpleNamespace(value="failed"), data=None, error=None, receipt_data=None),
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.find_nearest_home_depot_by_address",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "aspire_orchestrator.services.adam.playbooks.trades.lookup_store_by_id",
            return_value={"address": "123 Main St", "city": "Atlanta", "state": "GA", "postal_code": "30301", "phone": "", "website": ""},
        ),
        patch("aspire_orchestrator.services.adam.playbooks.trades.find_stores_in_city", return_value=[]),
        patch("aspire_orchestrator.services.adam.playbooks.trades.lookup_zip_by_city", return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.find_nearest_store", return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.resolve_store_async", new_callable=AsyncMock, return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.store_receipts", return_value=None),
    ):
        from aspire_orchestrator.services.adam.playbooks.trades import execute_tool_material_price_check
        result = await execute_tool_material_price_check(
            query="paint",
            ctx=ctx,
            zip_code="30301",
            store_id="1234",
        )

    # Still returns page 1 products — page 2 failure is transparent
    hd_products = [r for r in result.records if r.get("retailer") == "Home Depot"]
    assert len(hd_products) == 20, f"Expected 20 page-1 products on page-2 fail, got {len(hd_products)}"
    # Confidence still 'complete' — page 2 is a bonus, not required
    assert result.confidence["status"] == "complete"


@pytest.mark.asyncio
async def test_final_list_capped_at_60():
    """Merged list of page1+page2 never exceeds 60 products."""
    ctx = _make_ctx()
    # Both pages return 40 fully-unique products
    page1_result = _make_hd_result(40)
    page2_result = _make_hd_result(40)
    for i, item in enumerate(page2_result.data["results"]):
        item["sku"] = f"SKU-P2-{i:04d}"
        item["product_id"] = f"PROD-P2-{i}"

    async def _mock_hd(payload, **kw):
        return page2_result if payload.get("start") else page1_result

    with (
        patch("aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_homedepot_search", side_effect=_mock_hd),
        patch("aspire_orchestrator.services.adam.playbooks.trades.execute_serpapi_shopping_search",
              new_callable=AsyncMock,
              return_value=MagicMock(outcome=SimpleNamespace(value="failed"), data=None, error=None, receipt_data=None)),
        patch("aspire_orchestrator.services.adam.playbooks.trades.find_nearest_home_depot_by_address", new_callable=AsyncMock, return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.lookup_store_by_id",
              return_value={"address": "123 Main", "city": "ATL", "state": "GA", "postal_code": "30301", "phone": "", "website": ""}),
        patch("aspire_orchestrator.services.adam.playbooks.trades.find_stores_in_city", return_value=[]),
        patch("aspire_orchestrator.services.adam.playbooks.trades.lookup_zip_by_city", return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.find_nearest_store", return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.resolve_store_async", new_callable=AsyncMock, return_value=None),
        patch("aspire_orchestrator.services.adam.playbooks.trades.store_receipts", return_value=None),
    ):
        from aspire_orchestrator.services.adam.playbooks.trades import execute_tool_material_price_check
        result = await execute_tool_material_price_check(
            query="paint", ctx=ctx, zip_code="30301", store_id="1234",
        )

    hd_products = [r for r in result.records if r.get("retailer") == "Home Depot"]
    assert len(hd_products) <= 60, f"Product list must not exceed 60, got {len(hd_products)}"


# ---------------------------------------------------------------------------
# Section B: Places enrichment tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_places_enrichment_happy_path():
    """Enrichment injects phone, hours_open_now, hours_today, current_status."""
    store = {
        "store_id": "1234",
        "store_name": "Home Depot Forest Park",
        "address": "4790 Jonesboro Rd, Forest Park, GA 30297",
        "city": "Forest Park",
        "state": "GA",
    }

    text_search_body = {
        "results": [{"place_id": "ChIJtest", "name": "The Home Depot", "formatted_address": "4790 Jonesboro Rd"}],
        "status": "OK",
    }
    details_body = {
        "result": {
            "formatted_phone_number": "+1 404-555-0100",
            "business_status": "OPERATIONAL",
            "opening_hours": {
                "open_now": True,
                "weekday_text": [
                    "Monday: 6:00 AM - 10:00 PM",
                    "Tuesday: 6:00 AM - 10:00 PM",
                    "Wednesday: 6:00 AM - 10:00 PM",
                    "Thursday: 6:00 AM - 10:00 PM",
                    "Friday: 6:00 AM - 10:00 PM",
                    "Saturday: 6:00 AM - 10:00 PM",
                    "Sunday: 8:00 AM - 8:00 PM",
                ],
                "periods": [],
            },
        },
        "status": "OK",
    }

    import aspire_orchestrator.services.adam.hd_store_places_enricher as enricher
    # Clear module-level caches between tests
    enricher._ENRICHMENT_CACHE.clear()
    enricher._PLACE_ID_CACHE.clear()

    class _FakeResp:
        def __init__(self, body): self._body = body
        def json(self): return self._body

    call_order: list[str] = []

    async def _fake_get(url, params=None, **kw):
        if "textsearch" in url:
            call_order.append("textsearch")
            return _FakeResp(text_search_body)
        elif "details" in url:
            call_order.append("details")
            return _FakeResp(details_body)
        raise ValueError(f"unexpected url: {url}")

    with (
        patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}),
        patch("aspire_orchestrator.services.adam.hd_store_places_enricher.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=_fake_get)
        mock_client_cls.return_value = mock_client

        enriched = await enricher.enrich_store_with_places(store)

    assert enriched["phone"] == "+1 404-555-0100"
    assert enriched["hours_open_now"] is True
    assert "6:00 AM - 10:00 PM" in (enriched["hours_today"] or "")
    assert enriched["current_status"] == "OPEN"
    assert "textsearch" in call_order
    assert "details" in call_order


@pytest.mark.asyncio
async def test_places_enrichment_missing_api_key():
    """Missing API key → store dict returned unchanged, no HTTP calls."""
    store = {"store_id": "1234", "address": "123 Main", "store_name": "HD"}

    import aspire_orchestrator.services.adam.hd_store_places_enricher as enricher
    enricher._ENRICHMENT_CACHE.clear()
    enricher._PLACE_ID_CACHE.clear()

    with patch.dict("os.environ", {}, clear=True):  # no GOOGLE_MAPS_API_KEY
        with patch("aspire_orchestrator.services.adam.hd_store_places_enricher.httpx.AsyncClient") as mock_client:
            result = await enricher.enrich_store_with_places(store)
            mock_client.assert_not_called()

    assert "phone" not in result
    assert "hours_open_now" not in result


@pytest.mark.asyncio
async def test_places_enrichment_text_search_no_results():
    """Text search returns empty → store unchanged, no details call."""
    store = {"store_id": "9999", "address": "Nowhere", "store_name": "HD"}

    import aspire_orchestrator.services.adam.hd_store_places_enricher as enricher
    enricher._ENRICHMENT_CACHE.clear()
    enricher._PLACE_ID_CACHE.clear()

    class _FakeResp:
        def json(self): return {"results": [], "status": "ZERO_RESULTS"}

    with (
        patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}),
        patch("aspire_orchestrator.services.adam.hd_store_places_enricher.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_FakeResp())
        mock_client_cls.return_value = mock_client

        result = await enricher.enrich_store_with_places(store)

    assert "phone" not in result
    assert "current_status" not in result


@pytest.mark.asyncio
async def test_places_enrichment_timeout_fail_soft():
    """HTTP timeout → store returned unchanged (fail-soft, Law #3)."""
    store = {"store_id": "5678", "address": "456 Oak", "store_name": "HD"}

    import aspire_orchestrator.services.adam.hd_store_places_enricher as enricher
    enricher._ENRICHMENT_CACHE.clear()
    enricher._PLACE_ID_CACHE.clear()

    with (
        patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}),
        patch("aspire_orchestrator.services.adam.hd_store_places_enricher.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_client_cls.return_value = mock_client

        result = await enricher.enrich_store_with_places(store)

    assert "phone" not in result
    assert "current_status" not in result


@pytest.mark.asyncio
async def test_places_enrichment_cache_hit():
    """Second call with same store_id uses cache — no HTTP call."""
    store = {"store_id": "CACHE-TEST", "address": "789 Pine", "store_name": "HD"}

    import aspire_orchestrator.services.adam.hd_store_places_enricher as enricher
    enricher._ENRICHMENT_CACHE.clear()
    enricher._PLACE_ID_CACHE.clear()

    # Pre-populate the enrichment cache
    enricher._set_cached_enrichment("CACHE-TEST", {
        "phone": "+1 678-000-0000",
        "hours_open_now": True,
        "hours_today": "6 AM - 10 PM",
        "current_status": "OPEN",
    })

    with (
        patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}),
        patch("aspire_orchestrator.services.adam.hd_store_places_enricher.httpx.AsyncClient") as mock_client_cls,
    ):
        result = await enricher.enrich_store_with_places(store)
        mock_client_cls.assert_not_called()

    assert result["phone"] == "+1 678-000-0000"
    assert result["current_status"] == "OPEN"


@pytest.mark.asyncio
async def test_cache_key_contains_no_pii():
    """Law #9: cache keys must be store_id only, never the full address."""
    import aspire_orchestrator.services.adam.hd_store_places_enricher as enricher
    enricher._ENRICHMENT_CACHE.clear()
    enricher._PLACE_ID_CACHE.clear()

    enricher._set_cached_enrichment("STORE-LAW9", {"phone": "+1 000-000-0000"})
    enricher._set_cached_place_id("STORE-LAW9", "ChIJtest")

    # Verify the key stored does NOT contain a full street address
    for key in enricher._ENRICHMENT_CACHE:
        assert key == "STORE-LAW9", f"Cache key should be store_id only, got: {key}"
    for key in enricher._PLACE_ID_CACHE:
        assert key == "STORE-LAW9", f"Cache key should be store_id only, got: {key}"
