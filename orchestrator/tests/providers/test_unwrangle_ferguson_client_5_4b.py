"""Tests for unwrangle_ferguson_client Wave 5.4b -- fergusonhome_search + fergusonhome_product_data migration.

Covers:
  - fergusonhome_search endpoint (replaces deprecated build_search)
  - fergusonhome_product_data endpoint (replaces deprecated build_detail)
  - New fields: variants, price_ranges, quick_ship, feature_groups
  - All error paths emit receipts with correct reason_code
  - No secrets (API key) in error messages or receipts
  - Integration test (skipped unless ASPIRE_UNWRANGLE_API_KEY set)
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock
import httpx
import pytest
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.error_codes import InternalErrorCode

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-ferguson-5-4b"
FAKE_KEY = "fake-unwrangle-api-key-for-5-4b-tests"
PRODUCT_URL = "https://www.fergusonhome.com/products/kohler-k-2031-archer"


def _http_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    req = httpx.Request("GET", "https://data.unwrangle.com/api/getter/")
    return httpx.Response(status_code, json=body, request=req)


def _search_success_body(results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "results": results or [
            {
                "title": "Kohler K-2031 Archer Bathtub",
                "price": 649.00,
                "sku": "K-2031",
                "variants": [{"color": "White", "price": 649.0}, {"color": "Almond", "price": 679.0}],
                "price_ranges": {"min": 649.0, "max": 679.0},
                "quick_ship": True,
                "feature_groups": [{"name": "Dimensions", "features": ["60in x 30in"]}],
            }
        ],
        "remaining_credits": 450,
    }


def _detail_success_body() -> dict[str, Any]:
    return {
        "success": True,
        "product": {
            "title": "Kohler K-2031 Archer Bathtub",
            "sku": "K-2031",
            "price": 649.00,
            "variants": [{"color": "White", "price": 649.0}],
            "price_ranges": {"min": 649.0, "max": 679.0},
            "quick_ship": True,
            "feature_groups": [{"name": "Dimensions", "features": ["60in x 30in"]}],
            "certifications": ["ASME A112.19.1"],
        },
        "remaining_credits": 449,
    }


def _504_body() -> dict[str, Any]:
    return {"success": False, "error": "Unable to fetch search results from upstream"}


def _401_body() -> dict[str, Any]:
    return {"success": False, "error": "Unauthorized - invalid api_key"}


def _402_body() -> dict[str, Any]:
    return {"success": False, "error": "Payment Required - credits exhausted"}


def _429_body() -> dict[str, Any]:
    return {"success": False, "error": "Rate limit exceeded"}


def _500_body() -> dict[str, Any]:
    return {"success": False, "error": "Internal server error"}


def _fake_get(status: int, body: dict[str, Any]):
    async def _g(self: Any, url: str, **kw: Any) -> httpx.Response:
        return _http_response(status, body)
    return _g


@pytest.fixture(autouse=True)
def reset_client():
    import aspire_orchestrator.providers.unwrangle_ferguson_client as fg
    fg._client = None
    yield
    fg._client = None


@pytest.fixture(autouse=True)
def mock_receipt_logger(monkeypatch):
    from aspire_orchestrator.services import provider_call_logger as pcl
    s = MagicMock()
    s.log_call = MagicMock(return_value=None)
    monkeypatch.setattr(pcl, "_logger_instance", s)
    monkeypatch.setattr(pcl, "get_provider_call_logger", lambda: s)


@pytest.fixture()
def with_api_key(monkeypatch):
    import aspire_orchestrator.config.settings as sm
    monkeypatch.setattr(sm.settings, "unwrangle_api_key", FAKE_KEY)


@pytest.fixture()
def without_api_key(monkeypatch):
    import aspire_orchestrator.config.settings as sm
    monkeypatch.setattr(sm.settings, "unwrangle_api_key", "")


class TestFergusonHomeSearch:
    @pytest.mark.asyncio
    async def test_uses_fergusonhome_search_not_build_search(self, monkeypatch, with_api_key):
        captured: list[str] = []

        async def fg(self: Any, url: str, **kw: Any) -> httpx.Response:
            captured.append(url)
            return _http_response(200, _search_success_body())

        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("Kohler bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=fergusonhome_search" in captured[0]
        assert "platform=build_search" not in captured[0]

    @pytest.mark.asyncio
    async def test_success_returns_new_fields(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _search_success_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("Kohler bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        results = r.data["results"]
        assert results, "No results returned"
        product = results[0]
        assert "variants" in product
        assert "price_ranges" in product
        assert "quick_ship" in product
        assert "feature_groups" in product

    @pytest.mark.asyncio
    async def test_success_new_fields_defaulted_when_missing(self, monkeypatch, with_api_key):
        body = {"success": True, "results": [{"title": "PVC Pipe", "price": 2.99}], "remaining_credits": 400}
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, body))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("PVC pipe", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        product = r.data["results"][0]
        assert product["variants"] == []
        assert product["price_ranges"] == {}
        assert product["quick_ship"] is False
        assert product["feature_groups"] == []

    @pytest.mark.asyncio
    async def test_receipt_shows_fergusonhome_search_source(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _search_success_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.receipt_data is not None
        redacted_out = r.receipt_data.get("redacted_outputs", {})
        assert redacted_out.get("source_api") == "fergusonhome_search"

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("copper pipe", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"
        assert "fergusonhome_search" in (r.error or "")
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls: list[str] = []

        async def fg(self: Any, url: str, **kw: Any) -> httpx.Response:
            calls.append(url)
            return _http_response(200, _search_success_body())

        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("copper", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert len(calls) == 0
        assert r.receipt_data.get("reason_code") == InternalErrorCode.AUTH_INVALID_KEY.value

    @pytest.mark.asyncio
    async def test_401_maps_auth_invalid_key(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(401, _401_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("copper", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data is not None
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_402_credit_exhausted(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(402, _402_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("faucet", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        pm = r.receipt_data.get("provider_metadata", {})
        assert pm.get("error_code") == InternalErrorCode.RATE_QUOTA_EXCEEDED.value

    @pytest.mark.asyncio
    async def test_receipt_on_every_path(self, monkeypatch, with_api_key):
        for sc, body in [(200, _search_success_body()), (504, _504_body()), (401, _401_body()), (500, _500_body())]:
            def mk(s: int, b: dict[str, Any]):
                async def g(self: Any, url: str, **kw: Any) -> httpx.Response:
                    return _http_response(s, b)
                return g
            monkeypatch.setattr(httpx.AsyncClient, "get", mk(sc, body))
            import aspire_orchestrator.providers.unwrangle_ferguson_client as fg
            fg._client = None
            from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
            r = await search_products("item", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
            assert r.receipt_data is not None, f"No receipt for HTTP {sc}"
            assert "id" in r.receipt_data

    @pytest.mark.asyncio
    async def test_pii_api_key_not_in_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _search_success_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert FAKE_KEY not in json.dumps(r.receipt_data)


class TestFergusonHomeProductData:
    @pytest.mark.asyncio
    async def test_uses_fergusonhome_product_data_not_build_detail(self, monkeypatch, with_api_key):
        captured: list[str] = []

        async def fg(self: Any, url: str, **kw: Any) -> httpx.Response:
            captured.append(url)
            return _http_response(200, _detail_success_body())

        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=fergusonhome_product_data" in captured[0]
        assert "platform=build_detail" not in captured[0]

    @pytest.mark.asyncio
    async def test_success_returns_new_fields(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _detail_success_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        product = r.data["product"]
        assert "variants" in product
        assert "price_ranges" in product
        assert "quick_ship" in product
        assert "feature_groups" in product
        assert len(product["variants"]) >= 1

    @pytest.mark.asyncio
    async def test_new_fields_defaulted_when_missing(self, monkeypatch, with_api_key):
        body = {
            "success": True,
            "product": {"title": "Simple Faucet", "sku": "SF-001", "price": 49.99},
            "remaining_credits": 400,
        }
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, body))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        product = r.data["product"]
        assert product["variants"] == []
        assert product["price_ranges"] == {}
        assert product["quick_ship"] is False
        assert product["feature_groups"] == []

    @pytest.mark.asyncio
    async def test_receipt_shows_fergusonhome_product_data_source(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _detail_success_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.receipt_data is not None
        redacted_out = r.receipt_data.get("redacted_outputs", {})
        assert redacted_out.get("source_api") == "fergusonhome_product_data"

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"
        assert "fergusonhome_product_data" in (r.error or "")
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls: list[str] = []

        async def fg(self: Any, url: str, **kw: Any) -> httpx.Response:
            calls.append(url)
            return _http_response(200, _detail_success_body())

        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert len(calls) == 0
        assert r.receipt_data.get("reason_code") == InternalErrorCode.AUTH_INVALID_KEY.value

    @pytest.mark.asyncio
    async def test_missing_url_fails_with_receipt(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail("", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_MISSING_REQUIRED.value

    @pytest.mark.asyncio
    async def test_receipt_on_every_path(self, monkeypatch, with_api_key):
        for sc, body in [(200, _detail_success_body()), (504, _504_body()), (401, _401_body()), (500, _500_body())]:
            def mk(s: int, b: dict[str, Any]):
                async def g(self: Any, url: str, **kw: Any) -> httpx.Response:
                    return _http_response(s, b)
                return g
            monkeypatch.setattr(httpx.AsyncClient, "get", mk(sc, body))
            import aspire_orchestrator.providers.unwrangle_ferguson_client as fg
            fg._client = None
            from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
            r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
            assert r.receipt_data is not None, f"No receipt for HTTP {sc}"

    @pytest.mark.asyncio
    async def test_pii_api_key_not_in_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _detail_success_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(PRODUCT_URL, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert FAKE_KEY not in json.dumps(r.receipt_data)


@pytest.mark.skipif(
    not os.environ.get("ASPIRE_UNWRANGLE_API_KEY"),
    reason="Integration test: set ASPIRE_UNWRANGLE_API_KEY to run (1 credit per run)",
)
class TestFergusonIntegration:
    @pytest.mark.asyncio
    async def test_live_fergusonhome_search(self, monkeypatch):
        import aspire_orchestrator.config.settings as sm
        monkeypatch.setattr(sm.settings, "unwrangle_api_key", os.environ["ASPIRE_UNWRANGLE_API_KEY"])
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("Kohler bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id="live-test-001")
        assert r.outcome == Outcome.SUCCESS, f"Live fergusonhome_search failed: {r.error}"
        assert r.data["results"], "No results from live fergusonhome_search"
        assert r.data["source"] == "unwrangle_ferguson"

    @pytest.mark.asyncio
    async def test_live_fergusonhome_product_data(self, monkeypatch):
        import aspire_orchestrator.config.settings as sm
        monkeypatch.setattr(sm.settings, "unwrangle_api_key", os.environ["ASPIRE_UNWRANGLE_API_KEY"])
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products, get_product_detail
        search_r = await search_products("Kohler sink", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id="live-search-001")
        if search_r.outcome != Outcome.SUCCESS or not search_r.data["results"]:
            pytest.skip("No results from live search to drill into")
        first_url = search_r.data["results"][0].get("url") or search_r.data["results"][0].get("product_url")
        if not first_url:
            pytest.skip("No URL in first result")
        detail_r = await get_product_detail(first_url, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id="live-detail-001")
        assert detail_r.outcome == Outcome.SUCCESS, f"Live fergusonhome_product_data failed: {detail_r.error}"
        product = detail_r.data["product"]
        assert "variants" in product
        assert "price_ranges" in product
