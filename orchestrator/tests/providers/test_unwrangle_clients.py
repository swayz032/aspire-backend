"""Tests for Unwrangle provider clients -- Wave 5.1a supplier discovery."""

from __future__ import annotations
import json
from typing import Any
from unittest.mock import MagicMock
import httpx
import pytest
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.error_codes import InternalErrorCode

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-unwrangle-001"
FAKE_KEY = "fake-unwrangle-api-key-for-tests"

def _make_http_response(status_code, body, url="https://data.unwrangle.com/api/getter/"):
    req = httpx.Request("GET", url)
    return httpx.Response(status_code, json=body, request=req)

def _success_body(results=None):
    return {"success": True, "results": results or [{"title": "PVC Pipe 1/2in", "price": 2.99}], "remaining_credits": 450}

def _stores_body(stores=None):
    return {"success": True, "stores": stores or [{"store_no": 501, "name": "Lowes Forest Park"}, {"store_no": 502, "name": "Lowes Morrow"}], "remaining_credits": 449}

def _product_body():
    return {"success": True, "product": {"title": "Kohler Archer Bathtub", "sku": "K-1123-LA", "variants": []}, "remaining_credits": 448}

def _504_body():
    return {"success": False, "error": "Unable to fetch search results from upstream"}

def _401_body():
    return {"success": False, "error": "Unauthorized - invalid api_key"}

def _402_body():
    return {"success": False, "error": "Payment Required - credits exhausted"}

def _429_body():
    return {"success": False, "error": "Rate limit exceeded"}

def _500_body():
    return {"success": False, "error": "Internal server error"}

def _fake_get(status, body):
    async def _g(self, url, **kw):
        return _make_http_response(status, body)
    return _g

@pytest.fixture(autouse=True)
def reset_clients():
    import aspire_orchestrator.providers.unwrangle_homedepot_client as hd
    import aspire_orchestrator.providers.unwrangle_ferguson_client as fg
    import aspire_orchestrator.providers.unwrangle_lowes_client as lw
    hd._client = None; fg._client = None; lw._client = None
    yield
    hd._client = None; fg._client = None; lw._client = None

@pytest.fixture(autouse=True)
def mock_receipt_logger(monkeypatch):
    from aspire_orchestrator.services import provider_call_logger as pcl
    s = MagicMock(); s.log_call = MagicMock(return_value=None)
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

class TestUnwrangleHomeDepotSearchProducts:
    @pytest.mark.asyncio
    async def test_success_constructs_api_key_query_param(self, monkeypatch, with_api_key):
        captured = []
        async def fg(self, url, **kw):
            captured.append(url)
            return _make_http_response(200, _success_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("PVC pipe", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip="30297", correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert f"api_key={FAKE_KEY}" in captured[0]
        assert "platform=homedepot_search" in captured[0]

    @pytest.mark.asyncio
    async def test_success_returns_results_and_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _success_body([{"title": "A", "price": 1}, {"title": "B", "price": 2}])))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("PVC pipe", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.receipt_data is not None
        assert r.receipt_data.get("reason_code") == "EXECUTED"
        assert r.data["result_count"] == 2
        assert r.data["source"] == "unwrangle_homedepot"

    @pytest.mark.asyncio
    async def test_success_tracks_credits_remaining(self, monkeypatch, with_api_key):
        b = _success_body(); b["remaining_credits"] = 123
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, b))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("drywall", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip="30297", correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.data["credits_remaining"] == 123
        assert r.receipt_data.get("provider_metadata", {}).get("credits_remaining") == 123

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls = []
        async def fg(self, url, **kw):
            calls.append(url)
            return _make_http_response(200, _success_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("lumber", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert len(calls) == 0
        assert r.receipt_data.get("reason_code") == InternalErrorCode.AUTH_INVALID_KEY.value
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_missing_line_item_fails_with_receipt(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_MISSING_REQUIRED.value

    @pytest.mark.asyncio
    async def test_line_item_too_long_fails(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("x"*501, suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_INVALID_FORMAT.value

    @pytest.mark.asyncio
    async def test_504_soft_fail_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("drain snake", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"
        assert "504" in (r.error or "")
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_401_maps_auth_invalid_key(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(401, _401_body()))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("lumber", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data is not None
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_402_credit_exhausted(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(402, _402_body()))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("drywall", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        pm = r.receipt_data.get("provider_metadata", {})
        assert pm.get("error_code") == InternalErrorCode.RATE_QUOTA_EXCEEDED.value

    @pytest.mark.asyncio
    async def test_429_rate_limited(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(429, _429_body()))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("paint", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data is not None

    @pytest.mark.asyncio
    async def test_receipt_on_every_path(self, monkeypatch, with_api_key):
        for sc, body in [(200, _success_body()), (504, _504_body()), (401, _401_body()), (500, _500_body())]:
            def mk(s, b):
                async def g(self, url, **kw): return _make_http_response(s, b)
                return g
            monkeypatch.setattr(httpx.AsyncClient, "get", mk(sc, body))
            import aspire_orchestrator.providers.unwrangle_homedepot_client as hd
            hd._client = None
            from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
            r = await search_products("item", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
            assert r.receipt_data is not None, f"No receipt for HTTP {sc}"
            assert "id" in r.receipt_data

    @pytest.mark.asyncio
    async def test_pii_safe_key_not_in_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _success_body()))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("copper wire", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip="30297", correlation_id=CORR_ID)
        assert FAKE_KEY not in json.dumps(r.receipt_data)


class TestUnwrangleFergusonSearchProducts:
    @pytest.mark.asyncio
    async def test_builds_correct_build_search_url(self, monkeypatch, with_api_key):
        captured = []
        async def fg(self, url, **kw):
            captured.append(url)
            return _make_http_response(200, _success_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("Kohler bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=build_search" in captured[0]
        assert f"api_key={FAKE_KEY}" in captured[0]

    @pytest.mark.asyncio
    async def test_success_returns_results_and_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _success_body([{"title": "Kohler", "price": 649}])))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("Kohler bathtub", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.receipt_data is not None
        assert r.data["source"] == "unwrangle_ferguson"

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("copper pipe", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls = []
        async def fg(self, url, **kw):
            calls.append(url)
            return _make_http_response(200, _success_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("copper", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED and len(calls) == 0
        assert r.receipt_data.get("reason_code") == InternalErrorCode.AUTH_INVALID_KEY.value

    @pytest.mark.asyncio
    async def test_401_auth_invalid_key(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(401, _401_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("copper", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert FAKE_KEY not in (r.error or "")
        assert r.receipt_data is not None

    @pytest.mark.asyncio
    async def test_402_credit_exhausted(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(402, _402_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("faucet", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        pm = r.receipt_data.get("provider_metadata", {})
        assert pm.get("error_code") == InternalErrorCode.RATE_QUOTA_EXCEEDED.value


class TestUnwrangleFergusonGetProductDetail:
    @pytest.mark.asyncio
    async def test_builds_correct_build_detail_url(self, monkeypatch, with_api_key):
        captured = []
        async def fg(self, url, **kw):
            captured.append(url)
            return _make_http_response(200, _product_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(product_url="https://www.build.com/product/detail/kohler-k-1123", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=build_detail" in captured[0]
        assert f"api_key={FAKE_KEY}" in captured[0]
        assert "url=" in captured[0]

    @pytest.mark.asyncio
    async def test_returns_product_data_and_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _product_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(product_url="https://www.build.com/product/detail/kohler-k-1123", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.data["product"] is not None
        assert r.data["source"] == "unwrangle_ferguson"

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(product_url="https://www.build.com/product/detail/kohler-k-1123", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"

    @pytest.mark.asyncio
    async def test_missing_product_url_fails(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(product_url="", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_MISSING_REQUIRED.value

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls = []
        async def fg(self, url, **kw):
            calls.append(url)
            return _make_http_response(200, _product_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_ferguson_client import get_product_detail
        r = await get_product_detail(product_url="https://www.build.com/product/detail/kohler-k-1123", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED and len(calls) == 0
        assert r.receipt_data.get("reason_code") == InternalErrorCode.AUTH_INVALID_KEY.value


class TestUnwrangleLowFindNearestStore:
    @pytest.mark.asyncio
    async def test_builds_correct_lowes_store_url(self, monkeypatch, with_api_key):
        captured = []
        async def fg(self, url, **kw):
            captured.append(url)
            return _make_http_response(200, _stores_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=lowes_store" in captured[0]
        assert "zipcode=30297" in captured[0]
        assert f"api_key={FAKE_KEY}" in captured[0]

    @pytest.mark.asyncio
    async def test_returns_stores_with_store_no(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _stores_body([{"store_no": 501, "name": "Lowes FP"}, {"store_no": 502, "name": "Lowes M"}])))
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.data["store_count"] == 2
        assert r.data["stores"][0]["store_no"] == 501
        assert r.receipt_data is not None

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"
        assert FAKE_KEY not in (r.error or "")

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls = []
        async def fg(self, url, **kw):
            calls.append(url)
            return _make_http_response(200, _stores_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED and len(calls) == 0
        assert r.receipt_data.get("reason_code") == InternalErrorCode.AUTH_INVALID_KEY.value

    @pytest.mark.asyncio
    async def test_missing_zipcode_fails(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_MISSING_REQUIRED.value

    @pytest.mark.asyncio
    async def test_pii_safe_no_store_addresses_in_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _stores_body([{"store_no": 501, "address": "4800 Jonesboro Rd, Forest Park, GA 30297"}])))
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        receipt_str = json.dumps(r.receipt_data)
        assert "4800 Jonesboro Rd" not in receipt_str
        assert FAKE_KEY not in receipt_str

    @pytest.mark.asyncio
    async def test_401_auth_invalid_key(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(401, _401_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        pm = r.receipt_data.get("provider_metadata", {})
        assert pm.get("error_code") == InternalErrorCode.AUTH_INVALID_KEY.value

    @pytest.mark.asyncio
    async def test_402_credit_exhausted(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(402, _402_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import find_nearest_store
        r = await find_nearest_store(zipcode="30297", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        pm = r.receipt_data.get("provider_metadata", {})
        assert pm.get("error_code") == InternalErrorCode.RATE_QUOTA_EXCEEDED.value


class TestUnwrangleLoweBrowseCategory:
    @pytest.mark.asyncio
    async def test_builds_correct_lowes_category_url(self, monkeypatch, with_api_key):
        captured = []
        async def fg(self, url, **kw):
            captured.append(url)
            return _make_http_response(200, _success_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_lowes_client import browse_category
        r = await browse_category(category_url="https://www.lowes.com/pl/Dishwashers", store_no=501, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=lowes_category" in captured[0]
        assert "store_no=501" in captured[0]
        assert f"api_key={FAKE_KEY}" in captured[0]

    @pytest.mark.asyncio
    async def test_returns_results_and_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _success_body([{"title": "Bosch", "price": 799}])))
        from aspire_orchestrator.providers.unwrangle_lowes_client import browse_category
        r = await browse_category(category_url="https://www.lowes.com/pl/Dishwashers", store_no=501, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.data["result_count"] == 1
        assert r.data["store_no"] == 501
        assert r.data["source"] == "unwrangle_lowes"

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import browse_category
        r = await browse_category(category_url="https://www.lowes.com/pl/Dishwashers", store_no=501, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls = []
        async def fg(self, url, **kw):
            calls.append(url)
            return _make_http_response(200, _success_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_lowes_client import browse_category
        r = await browse_category(category_url="https://www.lowes.com/pl/Dishwashers", store_no=501, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED and len(calls) == 0

    @pytest.mark.asyncio
    async def test_missing_category_url_fails(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_lowes_client import browse_category
        r = await browse_category(category_url="", store_no=501, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_MISSING_REQUIRED.value


class TestUnwrangleLowesGetProductDetail:
    @pytest.mark.asyncio
    async def test_builds_correct_lowes_detail_url(self, monkeypatch, with_api_key):
        captured = []
        async def fg(self, url, **kw):
            captured.append(url)
            return _make_http_response(200, _product_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="https://www.lowes.com/pd/Husqvarna-450X/5013596041", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS, r.error
        assert "platform=lowes_detail" in captured[0]
        assert f"api_key={FAKE_KEY}" in captured[0]
        assert "url=" in captured[0]

    @pytest.mark.asyncio
    async def test_returns_product_data_and_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _product_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="https://www.lowes.com/pd/Husqvarna-450X/5013596041", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.SUCCESS
        assert r.data["product"] is not None
        assert r.data["source"] == "unwrangle_lowes"

    @pytest.mark.asyncio
    async def test_504_upstream_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(504, _504_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="https://www.lowes.com/pd/Husqvarna-450X/5013596041", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"

    @pytest.mark.asyncio
    async def test_fail_closed_missing_api_key(self, monkeypatch, without_api_key):
        calls = []
        async def fg(self, url, **kw):
            calls.append(url)
            return _make_http_response(200, _product_body())
        monkeypatch.setattr(httpx.AsyncClient, "get", fg)
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="https://www.lowes.com/pd/Husqvarna-450X/5013596041", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED and len(calls) == 0

    @pytest.mark.asyncio
    async def test_missing_product_url_fails(self, monkeypatch, with_api_key):
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        assert r.receipt_data.get("reason_code") == InternalErrorCode.INPUT_MISSING_REQUIRED.value

    @pytest.mark.asyncio
    async def test_402_credit_exhausted(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(402, _402_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="https://www.lowes.com/pd/Husqvarna-450X/5013596041", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.outcome == Outcome.FAILED
        pm = r.receipt_data.get("provider_metadata", {})
        assert pm.get("error_code") == InternalErrorCode.RATE_QUOTA_EXCEEDED.value

    @pytest.mark.asyncio
    async def test_pii_safe_key_not_in_receipt(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, _product_body()))
        from aspire_orchestrator.providers.unwrangle_lowes_client import get_product_detail
        r = await get_product_detail(product_url="https://www.lowes.com/pd/Husqvarna-450X/5013596041", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert FAKE_KEY not in json.dumps(r.receipt_data)


class TestUnwrangleUpstreamDegradationBodyDetection:
    @pytest.mark.asyncio
    async def test_homedepot_200_success_false_is_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, {"success": False, "error": "Unable to fetch search results from the provider"}))
        from aspire_orchestrator.providers.unwrangle_homedepot_client import search_products
        r = await search_products("test", suite_id=SUITE_ID, office_id=OFFICE_ID, office_zip=None, correlation_id=CORR_ID)
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"

    @pytest.mark.asyncio
    async def test_ferguson_200_success_false_is_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, {"success": False, "error": "Unable to fetch search results from upstream scraper"}))
        from aspire_orchestrator.providers.unwrangle_ferguson_client import search_products
        r = await search_products("test", suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"

    @pytest.mark.asyncio
    async def test_lowes_category_200_success_false_is_degraded(self, monkeypatch, with_api_key):
        monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get(200, {"success": False, "error": "Unable to fetch search results from Lowes"}))
        from aspire_orchestrator.providers.unwrangle_lowes_client import browse_category
        r = await browse_category(category_url="https://www.lowes.com/pl/Dishwashers", store_no=501, suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)
        assert r.receipt_data.get("reason_code") == "UPSTREAM_DEGRADED"


class TestUnwrangleSettingsField:
    def test_unwrangle_api_key_field_exists(self, monkeypatch):
        import aspire_orchestrator.config.settings as sm
        monkeypatch.setattr(sm.settings, "unwrangle_api_key", "my-test-key")
        assert sm.settings.unwrangle_api_key == "my-test-key"

    def test_unwrangle_api_key_defaults_to_string(self):
        import aspire_orchestrator.config.settings as sm
        assert hasattr(sm.settings, "unwrangle_api_key")
        assert isinstance(sm.settings.unwrangle_api_key, str)
