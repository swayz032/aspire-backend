"""Tests for Adam MATERIAL_SUPPLIER_SEARCH -- Wave 5.1a-1."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_B = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CORR_ID = "corr-adam-sup-0000-0000-000000000001"


def _hd_product(**kw: Any) -> dict[str, Any]:
    return {"title": kw.get("title", "Generic Product"), "price": kw.get("price", "$12.99"), "in_stock": kw.get("in_stock", True), "link": "https://www.homedepot.com/p/foo/12345", "brand": kw.get("brand")}


def _places_result(**kw: Any) -> dict[str, Any]:
    return {"name": kw.get("name", "Miami Supply"), "place_id": "ChIJ0000test", "formatted_address": "123 Trade St", "formatted_phone_number": kw.get("phone", "305-555-0100"), "location": {"lat": kw.get("lat", 25.761), "lng": kw.get("lng", -80.190)}, "opening_hours": {"open_now": kw.get("open_now", True)}}


@pytest.fixture(autouse=True)
def patch_store_receipts():
    with patch("aspire_orchestrator.services.receipt_store.store_receipts", new_callable=MagicMock) as m:
        yield m


@pytest.fixture()
def receipts_store_spy(patch_store_receipts: Any) -> Any:
    return patch_store_receipts


@pytest.fixture()
def mock_serpapi_success() -> Any:
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    from aspire_orchestrator.models import Outcome
    result = ToolExecutionResult(outcome=Outcome.SUCCESS, data={"products": [_hd_product(title=f"HD Product {i}") for i in range(3)]}, tool_id="serpapi_home_depot", error=None)
    with patch("aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search", new=AsyncMock(return_value=result)) as m:
        yield m


@pytest.fixture()
def mock_serpapi_empty() -> Any:
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    from aspire_orchestrator.models import Outcome
    result = ToolExecutionResult(outcome=Outcome.SUCCESS, data={"products": []}, tool_id="serpapi_home_depot", error=None)
    with patch("aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search", new=AsyncMock(return_value=result)) as m:
        yield m


@pytest.fixture()
def mock_places_success() -> Any:
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    from aspire_orchestrator.models import Outcome
    result = ToolExecutionResult(outcome=Outcome.SUCCESS, data={"results": [_places_result(name=f"Trade Supplier {i}") for i in range(3)]}, tool_id="google_places", error=None)
    with patch("aspire_orchestrator.providers.google_places_client.execute_google_places_search", new=AsyncMock(return_value=result)) as m:
        yield m


@pytest.fixture()
def mock_places_empty() -> Any:
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    from aspire_orchestrator.models import Outcome
    result = ToolExecutionResult(outcome=Outcome.SUCCESS, data={"results": []}, tool_id="google_places", error=None)
    with patch("aspire_orchestrator.providers.google_places_client.execute_google_places_search", new=AsyncMock(return_value=result)) as m:
        yield m


# ---------------------------------------------------------------------------
# Candidate ranker
# ---------------------------------------------------------------------------

class TestCandidateRanker:

    def test_in_stock_scores_higher(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import score_candidate
        base = {"product": {"in_stock": True, "brand": None}, "supplier": {"distance_mi": 5.0}, "price": {"value": 10.0}, "tariff_flag_detected": None}
        oos = dict(base)
        oos["product"] = {"in_stock": False, "brand": None}
        assert score_candidate(base, brand_familiarity_map={}) > score_candidate(oos, brand_familiarity_map={})

    def test_closer_supplier_ranks_higher(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import score_candidate
        near = {"product": {"in_stock": True, "brand": None}, "supplier": {"distance_mi": 2.0}, "price": {"value": 10.0}, "tariff_flag_detected": None}
        far = dict(near)
        far["supplier"] = {"distance_mi": 80.0}
        assert score_candidate(near, brand_familiarity_map={}) > score_candidate(far, brand_familiarity_map={})

    def test_brand_familiarity_boost_applied(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import score_candidate
        base = {"product": {"in_stock": True, "brand": "Kohler"}, "supplier": {"distance_mi": 10.0}, "price": {"value": 50.0}, "tariff_flag_detected": None}
        assert score_candidate(base, brand_familiarity_map={"Kohler": 1.0}) > score_candidate(base, brand_familiarity_map={})

    def test_tariff_flag_reduces_score(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import score_candidate
        clean = {"product": {"in_stock": True, "brand": None}, "supplier": {"distance_mi": 5.0}, "price": {"value": 10.0}, "tariff_flag_detected": None}
        tariffed = dict(clean)
        tariffed["tariff_flag_detected"] = "steel_section_232"
        assert score_candidate(clean, brand_familiarity_map={}) > score_candidate(tariffed, brand_familiarity_map={})

    def test_match_class_exact(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import assign_match_class
        assert assign_match_class(0.90, in_stock=True, brand="Moen", brand_familiarity_map={"Moen": 1.0}) == "exact"

    def test_match_class_functional(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import assign_match_class
        assert assign_match_class(0.75, in_stock=True, brand=None, brand_familiarity_map={}) == "functional"

    def test_match_class_substitute_oos(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import assign_match_class
        assert assign_match_class(0.60, in_stock=False, brand=None, brand_familiarity_map={}) == "substitute"

    def test_match_class_substitute_low_score(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import assign_match_class
        assert assign_match_class(0.30, in_stock=True, brand="Moen", brand_familiarity_map={"Moen": 1.0}) == "substitute"

    def test_rank_returns_top_3(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import rank_candidates
        candidates = [{"product": {"in_stock": i % 2 == 0, "brand": None}, "supplier": {"distance_mi": float(i * 5)}, "price": {"value": 10.0 + i}, "tariff_flag_detected": None} for i in range(8)]
        results = rank_candidates(candidates, brand_familiarity_map={})
        assert len(results) <= 3
        scores = [r["match_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_rank_annotates_match_class(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import rank_candidates
        c = [{"product": {"in_stock": True, "brand": None}, "supplier": {"distance_mi": 3.0}, "price": {"value": 10.0}, "tariff_flag_detected": None}]
        results = rank_candidates(c, brand_familiarity_map={})
        assert len(results) == 1
        assert results[0]["match_class"] in ("exact", "functional", "substitute")
        assert 0.0 <= results[0]["match_score"] <= 1.0

    def test_rank_empty_input(self) -> None:
        from aspire_orchestrator.services.blueprint.candidate_ranker import rank_candidates
        assert rank_candidates([], brand_familiarity_map={}) == []


# ---------------------------------------------------------------------------
# Category routing
# ---------------------------------------------------------------------------

class TestCategoryRouting:

    @pytest.mark.asyncio
    async def test_commodity_calls_serpapi(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="1/2 inch copper pipe", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_serpapi_success.assert_called_once()
        assert result["status"] in ("ok", "degraded")
        assert "serpapi_homedepot" in result["source_apis_called"]

    @pytest.mark.asyncio
    async def test_commodity_fallback_when_serpapi_empty(self, mock_serpapi_empty: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="obscure product", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_serpapi_empty.assert_called_once()
        assert result["status"] in ("ok", "degraded")

    @pytest.mark.asyncio
    async def test_commercial_plumbing_skips_serpapi(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="2 inch ball valve brass", category="commercial_plumbing", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_serpapi_success.assert_not_called()
        assert result["status"] in ("ok", "degraded")

    @pytest.mark.asyncio
    async def test_appliance_finish_skips_serpapi(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="built-in dishwasher stainless", category="appliance_finish", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_serpapi_success.assert_not_called()
        assert result["status"] in ("ok", "degraded")

    @pytest.mark.asyncio
    async def test_local_trade_calls_google_places(self, mock_places_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="welding rod E7018", category="local_trade", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=25.762, office_lng=-80.191, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_places_success.assert_called_once()
        assert "google_places" in result["source_apis_called"]

    @pytest.mark.asyncio
    async def test_local_trade_returns_call_for_quote(self, mock_places_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="crane rental 50-ton", category="local_trade", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=25.762, office_lng=-80.191, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        for c in result.get("candidates", []):
            assert c["price"]["source"] == "call_for_quote"
            assert c["price"]["value"] is None

    @pytest.mark.asyncio
    async def test_specialty_hardware_defers(self) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="titanium socket hex M8", category="specialty_hardware", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert result["status"] == "defer_to_manual"
        assert result["candidates"] == []
        assert result["credits_used"] == 0

    @pytest.mark.asyncio
    async def test_specialty_hardware_calls_no_providers(self, mock_serpapi_success: Any, mock_places_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(line_item="custom alloy flange", category="specialty_hardware", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_serpapi_success.assert_not_called()
        mock_places_success.assert_not_called()


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------

class TestFailClosedValidation:

    @pytest.mark.asyncio
    async def test_missing_line_item_skill_pack(self) -> None:
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchContext, AdamResearchSkillPack
        ctx = AdamResearchContext(suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        pack = AdamResearchSkillPack()
        result = await pack.material_supplier_search(payload={"line_item": "", "category": "commodity"}, context=ctx)
        assert result.success is False
        assert "line_item" in (result.error or "").lower()
        assert result.receipt.get("event_type") == "adam.material_supplier_search"
        assert result.receipt.get("policy", {}).get("decision") == "deny"

    @pytest.mark.asyncio
    async def test_invalid_category_skill_pack(self) -> None:
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchContext, AdamResearchSkillPack
        ctx = AdamResearchContext(suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        pack = AdamResearchSkillPack()
        result = await pack.material_supplier_search(payload={"line_item": "PVC pipe", "category": "not_a_category"}, context=ctx)
        assert result.success is False
        assert result.receipt.get("policy", {}).get("decision") == "deny"

    @pytest.mark.asyncio
    async def test_router_missing_line_item(self) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert result["status"] == "error"
        assert result["candidates"] == []


# ---------------------------------------------------------------------------
# Receipt emission (Law #2)
# ---------------------------------------------------------------------------

class TestReceiptEmission:

    @pytest.mark.asyncio
    async def test_commodity_emits_serpapi_receipt(self, mock_serpapi_success: Any, receipts_store_spy: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(line_item="1/2 inch copper pipe", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert receipts_store_spy.called
        all_receipts: list[dict] = []
        for call in receipts_store_spy.call_args_list:
            all_receipts.extend(call.args[0] if call.args else [])
        event_types = {r.get("event_type") for r in all_receipts}
        assert "provider.serpapi_homedepot" in event_types

    @pytest.mark.asyncio
    async def test_local_trade_emits_google_places_receipt(self, mock_places_success: Any, receipts_store_spy: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(line_item="concrete pump rental", category="local_trade", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=25.762, office_lng=-80.191, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        all_receipts: list[dict] = []
        for call in receipts_store_spy.call_args_list:
            all_receipts.extend(call.args[0] if call.args else [])
        assert "provider.google_places" in {r.get("event_type") for r in all_receipts}

    @pytest.mark.asyncio
    async def test_specialty_hardware_emits_receipt(self, receipts_store_spy: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(line_item="custom alloy bolt", category="specialty_hardware", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert receipts_store_spy.called

    @pytest.mark.asyncio
    async def test_skillpack_emits_parent_receipt(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchContext, AdamResearchSkillPack
        ctx = AdamResearchContext(suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        pack = AdamResearchSkillPack()
        result = await pack.material_supplier_search(payload={"line_item": "PVC conduit 1/2", "category": "commodity", "office_zip": "33101"}, context=ctx)
        assert result.receipt.get("event_type") == "adam.material_supplier_search"
        assert result.receipt.get("suite_id") == SUITE_A

    @pytest.mark.asyncio
    async def test_line_item_truncated_in_receipts(self, receipts_store_spy: Any, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        long_item = "A" * 300
        await route_supplier_search(line_item=long_item, category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        for call in receipts_store_spy.call_args_list:
            for receipt in call.args[0] if call.args else []:
                meta = receipt.get("metadata") or {}
                for v in meta.values():
                    if isinstance(v, str) and "A" * 50 in v:
                        assert len(v) <= 100


# ---------------------------------------------------------------------------
# Tenant isolation (Law #6)
# ---------------------------------------------------------------------------

class TestTenantIsolation:

    @pytest.mark.asyncio
    async def test_suite_b_independent_search(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result_a = await route_supplier_search(line_item="copper pipe 3/4", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=str(uuid.uuid4()))
        result_b = await route_supplier_search(line_item="copper pipe 3/4", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="10001", office_lat=None, office_lng=None, suite_id=SUITE_B, office_id=OFFICE_B, correlation_id=str(uuid.uuid4()))
        assert result_a["status"] in ("ok", "degraded")
        assert result_b["status"] in ("ok", "degraded")

    @pytest.mark.asyncio
    async def test_receipts_carry_correct_suite_id(self, receipts_store_spy: Any, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        await route_supplier_search(line_item="drywall 5/8 type X", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_B, office_id=OFFICE_B, correlation_id=CORR_ID)
        for call in receipts_store_spy.call_args_list:
            for receipt in call.args[0] if call.args else []:
                assert receipt.get("suite_id") == SUITE_B


# ---------------------------------------------------------------------------
# Credit accounting
# ---------------------------------------------------------------------------

class TestCreditAccounting:

    @pytest.mark.asyncio
    async def test_commodity_credits_counted(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="OSB sheathing 7/16", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert result["credits_used"] >= 1

    @pytest.mark.asyncio
    async def test_specialty_hardware_zero_credits(self) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="custom alloy bolt M10", category="specialty_hardware", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert result["credits_used"] == 0

    @pytest.mark.asyncio
    async def test_local_trade_no_lat_lng_zero_credits(self, mock_places_success: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="concrete pump 30m boom", category="local_trade", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        mock_places_success.assert_not_called()
        assert result["credits_used"] == 0


# ---------------------------------------------------------------------------
# Empty result handling
# ---------------------------------------------------------------------------

class TestEmptyResults:

    @pytest.mark.asyncio
    async def test_commodity_empty_returns_degraded(self, mock_serpapi_empty: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="obscure product not in HD", category="commodity", brand_familiarity_map={}, geofence_miles=25.0, office_zip="33101", office_lat=None, office_lng=None, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert result["status"] in ("ok", "degraded")
        assert isinstance(result["candidates"], list)

    @pytest.mark.asyncio
    async def test_local_trade_empty_returns_degraded(self, mock_places_empty: Any) -> None:
        from aspire_orchestrator.services.blueprint.adam_supplier_router import route_supplier_search
        result = await route_supplier_search(line_item="highly specialized welding", category="local_trade", brand_familiarity_map={}, geofence_miles=25.0, office_zip=None, office_lat=25.762, office_lng=-80.191, suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        assert result["status"] in ("ok", "degraded")
        assert result["candidates"] == []


# ---------------------------------------------------------------------------
# Lowe's category map
# ---------------------------------------------------------------------------

class TestLowesCategoryMap:

    def test_known_slug(self) -> None:
        from aspire_orchestrator.services.blueprint.lowes_category_map import resolve_lowes_url
        assert "lowes.com" in (resolve_lowes_url("appliance_dishwasher_builtin") or "")

    def test_alias(self) -> None:
        from aspire_orchestrator.services.blueprint.lowes_category_map import resolve_lowes_url
        assert resolve_lowes_url("dishwasher") is not None

    def test_substring_alias(self) -> None:
        from aspire_orchestrator.services.blueprint.lowes_category_map import resolve_lowes_url
        assert resolve_lowes_url("built-in dishwasher stainless 24 inch") is not None

    def test_unknown_returns_none(self) -> None:
        from aspire_orchestrator.services.blueprint.lowes_category_map import resolve_lowes_url
        assert resolve_lowes_url("quantum flux capacitor gasket") is None

    def test_empty_returns_none(self) -> None:
        from aspire_orchestrator.services.blueprint.lowes_category_map import resolve_lowes_url
        assert resolve_lowes_url("") is None


# ---------------------------------------------------------------------------
# Skill pack integration
# ---------------------------------------------------------------------------

class TestSkillPackIntegration:

    @pytest.mark.asyncio
    async def test_success_returns_candidate_list(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchContext, AdamResearchSkillPack
        ctx = AdamResearchContext(suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        pack = AdamResearchSkillPack()
        result = await pack.material_supplier_search(payload={"line_item": "2x4 SPF stud 8 foot", "category": "commodity", "office_zip": "33101"}, context=ctx)
        assert result.success is True
        assert "candidates" in result.data
        assert "source_apis_called" in result.data
        assert "credits_used" in result.data

    @pytest.mark.asyncio
    async def test_specialty_hardware_is_success(self) -> None:
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchContext, AdamResearchSkillPack
        ctx = AdamResearchContext(suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        pack = AdamResearchSkillPack()
        result = await pack.material_supplier_search(payload={"line_item": "custom chromoly fastener", "category": "specialty_hardware"}, context=ctx)
        assert result.success is True
        assert result.data["status"] == "defer_to_manual"

    @pytest.mark.asyncio
    async def test_candidate_shape(self, mock_serpapi_success: Any) -> None:
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchContext, AdamResearchSkillPack
        ctx = AdamResearchContext(suite_id=SUITE_A, office_id=OFFICE_A, correlation_id=CORR_ID)
        pack = AdamResearchSkillPack()
        result = await pack.material_supplier_search(payload={"line_item": "roofing shingles architectural", "category": "commodity"}, context=ctx)
        assert result.success is True
        for c in result.data.get("candidates", []):
            assert c["match_class"] in ("exact", "functional", "substitute")
            assert 0.0 <= c["match_score"] <= 1.0
            assert c["price"]["currency"] == "USD"
            assert "freshness_as_of" in c