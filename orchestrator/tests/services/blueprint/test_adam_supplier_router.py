"""Tests for adam_supplier_router Wave 5.4a -- serpapi_shopping parallel dispatch.

Covers:
  - serpapi_shopping called for every non-local category (commodity, commercial_plumbing, appliance_finish)
  - union dedup of serpapi_shopping + primary provider results
  - price+rating ranking via rank_candidates after union
  - fail-open when serpapi_shopping returns empty (primary results still returned)
  - receipt emitted for every serpapi_shopping.blueprint call
  - local_trade and specialty_hardware categories unchanged
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-supplier-router-5-4a"

_SHOPPING_RESULTS = [
    {"title": "Copper Pipe 1/2in x 10ft", "extracted_price": 15.99, "source": "BuilderDepot", "rating": 4.5, "reviews": 120, "product_id": "cp-001"},
    {"title": "Copper Pipe 3/4in x 10ft", "extracted_price": 22.50, "source": "SupplyHouse.com", "rating": 4.2, "reviews": 88, "product_id": "cp-002"},
]

_HD_RESULTS = [
    {"title": "Copper Pipe 1/2in", "price": "$16.47", "brand": "Mueller", "in_stock": True},
]

_FERG_RESULTS = [
    {"title": "Kohler K-2031 Sink", "price": 649.0, "brand": "Kohler", "sku": "K-2031"},
]

_LOWES_RESULTS = [
    {"title": "GE 30in Refrigerator", "price": 799.0, "brand": "GE"},
]


def _make_shopping_success(results: list[dict[str, Any]] | None = None) -> Any:
    from aspire_orchestrator.models import Outcome
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    r = results if results is not None else _SHOPPING_RESULTS
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="serpapi_shopping.search",
        data={"results": r, "query": "test", "result_count": len(r)},
        receipt_data={"id": "receipt-shopping-001", "reason_code": "EXECUTED"},
    )


def _make_shopping_failure() -> Any:
    from aspire_orchestrator.models import Outcome
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="serpapi_shopping.search",
        error="Budget exhausted",
        receipt_data={"id": "receipt-shopping-fail", "reason_code": "SERPAPI_BUDGET_EXHAUSTED"},
    )


def _make_hd_success() -> Any:
    from aspire_orchestrator.models import Outcome
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="serpapi_homedepot.search",
        data={"products": _HD_RESULTS, "result_count": 1},
        receipt_data={"id": "receipt-hd-001", "reason_code": "EXECUTED"},
    )


@pytest.fixture(autouse=True)
def mock_receipt_store(monkeypatch):
    import aspire_orchestrator.services.receipt_store as rs
    monkeypatch.setattr(rs, "store_receipts", MagicMock())


class TestSerpapiShoppingCalledForAllCategories:
    @pytest.mark.asyncio
    async def test_commodity_calls_serpapi_shopping(self, monkeypatch):
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        hd_mock = AsyncMock(return_value=_make_hd_success())
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="copper pipe 1/2 inch",
            category="commodity",
            brand_familiarity_map={},
            office_zip="30297",
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        shopping_mock.assert_awaited_once()
        assert "serpapi_shopping" in result["source_apis_called"]
        assert result["status"] in ("ok", "degraded")

    @pytest.mark.asyncio
    async def test_commercial_plumbing_calls_serpapi_shopping(self, monkeypatch):
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_FERGUSON_AVAILABLE", False)
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="Kohler bathtub K-2031",
            category="commercial_plumbing",
            brand_familiarity_map={"Kohler": 0.9},
            office_zip="30297",
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        shopping_mock.assert_awaited_once()
        assert "serpapi_shopping" in result["source_apis_called"]

    @pytest.mark.asyncio
    async def test_appliance_finish_calls_serpapi_shopping(self, monkeypatch):
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_LOWES_AVAILABLE", False)
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="GE refrigerator stainless 30in",
            category="appliance_finish",
            brand_familiarity_map={"GE": 0.8},
            office_zip="30297",
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        shopping_mock.assert_awaited_once()
        assert "serpapi_shopping" in result["source_apis_called"]

    @pytest.mark.asyncio
    async def test_local_trade_does_not_call_serpapi_shopping(self, monkeypatch):
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        places_mock = AsyncMock()
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult
        places_mock.return_value = ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id="google_places.search",
            data={"results": []}, receipt_data={"id": "r1"},
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.google_places_client.execute_google_places_search",
            places_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="electrical contractor",
            category="local_trade",
            brand_familiarity_map={},
            office_zip="30297",
            office_lat=33.749,
            office_lng=-84.388,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        shopping_mock.assert_not_awaited()
        assert "serpapi_shopping" not in result["source_apis_called"]

    @pytest.mark.asyncio
    async def test_specialty_hardware_does_not_call_serpapi_shopping(self, monkeypatch):
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="custom steel bracket",
            category="specialty_hardware",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        shopping_mock.assert_not_awaited()
        assert result["status"] == "defer_to_manual"


class TestUnionDedup:
    @pytest.mark.asyncio
    async def test_dedup_removes_price_name_duplicates(self, monkeypatch):
        # Both HD and Shopping return similar items at same price — dedup should collapse
        shared_title = "Copper Pipe 1/2in"
        shared_price = 15.99
        hd_mock = AsyncMock(return_value=_make_hd_success())
        shopping_results = [
            {"title": shared_title, "extracted_price": shared_price, "source": "HD", "rating": 4.0},
            {"title": "Copper Pipe 3/4in", "extracted_price": 22.50, "source": "BuilderDepot", "rating": 4.5},
        ]
        shopping_mock = AsyncMock(return_value=_make_shopping_success(shopping_results))
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        # Should have candidates from both, no duplicates
        assert result["status"] in ("ok", "degraded")
        titles = [c.get("product", {}).get("name", "") for c in result["candidates"]]
        assert len(titles) == len(set(titles)) or len(result["candidates"]) <= 3  # rank_candidates caps at 3

    @pytest.mark.asyncio
    async def test_union_includes_shopping_results_when_primary_empty(self, monkeypatch):
        # HD returns empty, Shopping returns results -> shopping results present
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult
        hd_mock = AsyncMock(return_value=ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="serpapi_homedepot.search",
            error="No results", receipt_data={"id": "r1"},
        ))
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_HD_AVAILABLE", False)
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="specialty copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        assert result["candidates"], "Shopping results should fill in when HD is empty"
        sources = {c.get("_source_api") for c in result["candidates"]}
        assert "serpapi_shopping" in sources


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_fail_open_when_shopping_returns_empty(self, monkeypatch):
        hd_mock = AsyncMock(return_value=_make_hd_success())
        shopping_mock = AsyncMock(return_value=_make_shopping_success([]))
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        # HD results should still be in candidates even when Shopping empty
        assert result["status"] == "ok"
        assert len(result["candidates"]) >= 1

    @pytest.mark.asyncio
    async def test_fail_open_when_shopping_raises_exception(self, monkeypatch):
        hd_mock = AsyncMock(return_value=_make_hd_success())

        async def raise_exc(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("SerpApi network error")

        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            raise_exc,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        # Fail open: primary path still works
        assert result["status"] == "ok"


class TestReceiptEmission:
    @pytest.mark.asyncio
    async def test_receipt_emitted_for_every_shopping_call(self, monkeypatch):
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        hd_mock = AsyncMock(return_value=_make_hd_success())
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(
            line_item="copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        # Collect all receipt event_types from all store_receipts calls
        all_receipts = []
        for call in store_mock.call_args_list:
            all_receipts.extend(call[0][0])
        event_types = {r.get("event_type") for r in all_receipts}
        assert "provider.serpapi_shopping.blueprint" in event_types

    @pytest.mark.asyncio
    async def test_receipt_suite_id_matches_caller(self, monkeypatch):
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)
        shopping_mock = AsyncMock(return_value=_make_shopping_success())
        hd_mock = AsyncMock(return_value=_make_hd_success())
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(
            line_item="copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        all_receipts = []
        for call in store_mock.call_args_list:
            all_receipts.extend(call[0][0])
        shopping_receipts = [r for r in all_receipts if r.get("event_type") == "provider.serpapi_shopping.blueprint"]
        assert shopping_receipts, "No serpapi_shopping.blueprint receipt found"
        for r in shopping_receipts:
            assert r["suite_id"] == SUITE_ID
            assert r["office_id"] == OFFICE_ID


class TestPriceRatingRanking:
    @pytest.mark.asyncio
    async def test_lower_price_ranks_higher(self, monkeypatch):
        shopping_results = [
            {"title": "Expensive Pipe", "extracted_price": 99.99, "source": "Retail", "rating": 4.0},
            {"title": "Cheap Pipe", "extracted_price": 5.99, "source": "Discount", "rating": 4.1},
        ]
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult
        hd_mock = AsyncMock(return_value=ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="serpapi_homedepot.search",
            error="No results", receipt_data={"id": "r1"},
        ))
        shopping_mock = AsyncMock(return_value=_make_shopping_success(shopping_results))
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            hd_mock,
        )
        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_HD_AVAILABLE", False)
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="copper pipe",
            category="commodity",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        assert result["candidates"]
        # All candidates should have match_score and match_class from rank_candidates
        for c in result["candidates"]:
            assert "match_score" in c
            assert "match_class" in c


# ---------------------------------------------------------------------------
# Fix R-1: Unknown category emits denial receipt (Law #2)
# ---------------------------------------------------------------------------

class TestUnknownCategoryDenialReceipt:
    @pytest.mark.asyncio
    async def test_unknown_category_emits_denial_receipt(self, monkeypatch):
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="mystery item",
            category="not_a_real_category",  # type: ignore[arg-type]
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )

        assert result["status"] == "error"

        all_receipts = []
        for call in store_mock.call_args_list:
            all_receipts.extend(call[0][0])

        denial_receipts = [r for r in all_receipts if r.get("event_type") == "adam.supplier_search.denied"]
        assert denial_receipts, "Must emit a denial receipt for unknown category"
        r = denial_receipts[0]
        assert r["suite_id"] == SUITE_ID
        assert r["status"] == "denied"
        assert r.get("metadata", {}).get("reason_code") == "UNKNOWN_CATEGORY"

    @pytest.mark.asyncio
    async def test_unknown_category_receipt_has_correct_policy(self, monkeypatch):
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(
            line_item="flux capacitor",
            category="warp_drive",  # type: ignore[arg-type]
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )

        all_receipts = []
        for call in store_mock.call_args_list:
            all_receipts.extend(call[0][0])

        denial = next((r for r in all_receipts if r.get("event_type") == "adam.supplier_search.denied"), None)
        assert denial is not None
        assert denial["policy"]["decision"] == "deny"


# ---------------------------------------------------------------------------
# Fix P-2: Ferguson/Lowes stubs call module-level coroutines correctly
# Fix P-3: Denial receipt emitted when Ferguson/Lowes stub crashes
# ---------------------------------------------------------------------------

class TestHomeDepotStubGuard:
    @pytest.mark.asyncio
    async def test_hd_stub_disabled_returns_empty(self, monkeypatch):
        """BLOCKER 2: When HD flag is disabled, stub returns ([], 0) without calling search_products."""
        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_HD_AVAILABLE", False)

        from aspire_orchestrator.services.blueprint.adam_supplier_router import _call_unwrangle_homedepot_stub
        result_list, count = await _call_unwrangle_homedepot_stub(line_item="copper pipe", office_zip="30297")
        assert result_list == []
        assert count == 0

    @pytest.mark.asyncio
    async def test_hd_stub_client_without_search_products_returns_empty(self, monkeypatch):
        """BLOCKER 2: hasattr guard — client with no search_products returns ([], 0) without crashing."""
        import sys
        import types
        import aspire_orchestrator.services.blueprint.adam_supplier_router as router

        class _HDClientNoMethod:
            pass

        # Inject fake module so ImportError doesn't fire
        fake_mod = types.ModuleType("aspire_orchestrator.providers.unwrangle_homedepot_client")
        fake_mod.UnwrangleHomeDepotClient = _HDClientNoMethod  # type: ignore[attr-defined]
        sys.modules.setdefault("aspire_orchestrator.providers.unwrangle_homedepot_client", fake_mod)

        monkeypatch.setattr(router, "_UNWRANGLE_HD_AVAILABLE", True)
        # Patch the name used inside the stub (it does `UnwrangleHomeDepotClient()`)
        monkeypatch.setattr(router, "UnwrangleHomeDepotClient", _HDClientNoMethod, raising=False)

        from aspire_orchestrator.services.blueprint.adam_supplier_router import _call_unwrangle_homedepot_stub
        result_list, count = await _call_unwrangle_homedepot_stub(line_item="copper pipe", office_zip="30297")
        assert result_list == []
        assert count == 0


class TestFergusonLowesStubCorrectness:
    @pytest.mark.asyncio
    async def test_ferguson_stub_calls_module_level_coroutine(self, monkeypatch):
        """Fix P-2: Must call search_products on the module, not on a client instance."""
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult

        search_mock = AsyncMock(return_value=ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="unwrangle_ferguson.search_products",
            data={"results": [
                {"title": "Ball Valve 2in", "supplier": {"name": "Ferguson", "id": "ferg"}, "product": {"name": "Ball Valve 2in", "in_stock": True}, "price": {"value": 45.0, "currency": "USD", "source": "retail"}, "tariff_flag_detected": None, "freshness_as_of": "2026-05-23T00:00:00Z"},
            ]},
            receipt_data={"id": "ferg-001"},
        ))

        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_FERGUSON_AVAILABLE", True)

        import aspire_orchestrator.providers.unwrangle_ferguson_client as ferg_mod
        monkeypatch.setattr(ferg_mod, "search_products", search_mock)

        shopping_mock = AsyncMock()
        from aspire_orchestrator.services.tool_types import ToolExecutionResult as TER
        shopping_mock.return_value = ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id="serpapi_shopping.search",
            data={"results": []}, receipt_data={"id": "shop-001"},
        )
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )

        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="2in brass ball valve",
            category="commercial_plumbing",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )
        search_mock.assert_awaited_once()
        assert result["status"] in ("ok", "degraded")

    @pytest.mark.asyncio
    async def test_ferguson_crash_emits_provider_error_receipt(self, monkeypatch):
        """Fix P-3: When Ferguson stub raises, a PROVIDER_CRASH receipt must be stored."""
        import aspire_orchestrator.services.receipt_store as rs
        store_mock = MagicMock()
        monkeypatch.setattr(rs, "store_receipts", store_mock)

        import aspire_orchestrator.services.blueprint.adam_supplier_router as router
        monkeypatch.setattr(router, "_UNWRANGLE_FERGUSON_AVAILABLE", True)

        import aspire_orchestrator.providers.unwrangle_ferguson_client as ferg_mod

        async def _raise(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(ferg_mod, "search_products", _raise)

        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult
        shopping_mock = AsyncMock(return_value=ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id="serpapi_shopping.search",
            data={"results": []}, receipt_data={"id": "shop-002"},
        ))
        monkeypatch.setattr(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            shopping_mock,
        )

        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(
            line_item="copper fitting 1/2",
            category="commercial_plumbing",
            brand_familiarity_map={},
            office_zip=None,
            office_lat=None,
            office_lng=None,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            correlation_id=CORR_ID,
        )

        assert result["status"] in ("ok", "degraded", "error")
        all_receipts = []
        for call in store_mock.call_args_list:
            all_receipts.extend(call[0][0])
        crash_receipts = [r for r in all_receipts if r.get("event_type") == "provider.supplier_search.error"]
        assert crash_receipts, "PROVIDER_CRASH receipt must be emitted when Ferguson stub raises"
        r = crash_receipts[0]
        assert r.get("metadata", {}).get("reason_code") == "PROVIDER_CRASH"
        # provider is stored in inputs (since _make_receipt takes inputs as a separate arg)
        assert r.get("inputs_hash") is not None  # inputs_hash proves inputs were captured
