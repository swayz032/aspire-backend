"""Integration tests — Full playbook dispatch with mocked provider responses.

Tests the complete pipeline: classify -> route -> dispatch -> provider call (mocked) ->
normalize -> verify -> ResearchResponse. One test per segment minimum.

Mock strategy: patch at `aspire_orchestrator.providers.<module>.<function>` since
playbook functions import providers inside their function body.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.services.adam.playbooks import dispatch_playbook
from aspire_orchestrator.services.adam.router import route_to_playbook
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CTX = PlaybookContext(
    suite_id="test-suite-001",
    office_id="test-office-001",
    correlation_id="corr-integration-001",
    tenant_id="test-suite-001",
)


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(outcome=Outcome.SUCCESS, tool_id=tool_id, data=data)


def _fail(tool_id: str, error: str) -> ToolExecutionResult:
    return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id, error=error)


# ---------------------------------------------------------------------------
# Mock data — matches the shape returned by provider clients (post-reshape)
# ---------------------------------------------------------------------------

SHOPPING_DATA = {
    "results": [
        {
            "title": "Goodman 3 Ton 14 SEER Condenser",
            "price": "$1,849.00",
            "extracted_price": 1849.0,
            "source": "Alpine Home Air",
            "rating": 4.5,
            "reviews": 127,
            "delivery": "Free shipping",
            "thumbnail": "https://example.com/thumb.jpg",
        },
        {
            "title": "Carrier 3 Ton 16 SEER Condenser",
            "price": "$2,399.00",
            "extracted_price": 2399.0,
            "source": "?"
        },
    ],
    "query": "3 ton condenser",
    "result_count": 2,
}

HOMEDEPOT_DATA = {
    "results": [
        {
            "title": "Milwaukee M18 FUEL Impact Driver Kit",
            "brand": "Milwaukee",
            "model_number": "2953-22",
            "price": 199.0,
            "price_was": 229.0,
            "price_saving": 30.0,
            "percentage_off": 13,
            "rating": 4.8,
            "reviews": 2341,
            "link": "https://homedepot.com/p/milwaukee-impact",
            "pickup": {"quantity": 15},
            "delivery": {"has_delivery": True},
            "thumbnails": [{"link": "https://example.com/thumb.jpg"}],
        },
    ],
    "query": "impact driver",
    "result_count": 1,
}

ATTOM_DETAIL_DATA = {
    "property": [{
        "identifier": {"Id": 12345, "fips": "21067", "apn": "123-456"},
        "address": {
            "oneLine": "123 Main St, Lexington, KY 40509",
            "locality": "Lexington",
            "countrySubd": "KY",
            "postal1": "40509",
        },
        "summary": {"yearBuilt": 1995, "propType": "SFR"},
        "building": {
            "size": {"livingSize": 1800, "universalSize": 2000},
            "rooms": {"beds": 3, "bathsFull": 2, "bathsHalf": 1},
        },
        "lot": {"lotSize1": 0.25, "lotSize2": 10890},
        "assessment": {"assessed": {"assdTtlValue": 185000}},
        "vintage": {"lastModified": "2026-01-15"},
    }],
}

ATTOM_SALES_DATA = {
    "property": [{
        "saleTransactionType": "Resale",
        "amount": {"saleAmt": 275000},
        "date": {"saleTransDate": "2024-06-15"},
    }],
}

EXA_DATA = {
    "results": [
        {
            "title": "IRS Quarterly Estimated Tax Deadlines 2026",
            "url": "https://irs.gov/estimated-taxes",
            "text": "Q1: April 15, Q2: June 16, Q3: September 15, Q4: January 15 2027",
            "publishedDate": "2026-01-01",
            "score": 0.95,
        },
    ],
    "costDollars": {"total": 0.003},
}

TRIPADVISOR_DATA = {
    "data": [
        {
            "location_id": "12345",
            "name": "Hilton Downtown Convention",
            "address_obj": {
                "street1": "100 W Main St",
                "city": "Lexington",
                "state": "Kentucky",
                "postalcode": "40507",
                "address_string": "100 W Main St, Lexington, KY 40507",
            },
            "rating": "4.5",
            "num_reviews": "1523",
            "hotel_class": "4.0",
            "price_level": "$$$",
            "subcategory": [{"name": "hotel"}],
            "category": {"name": "hotel"},
        },
    ],
}

GOOGLE_PLACES_DATA = {
    "results": [
        {
            "name": "Smith Roofing Co",
            "formatted_address": "456 Oak Ave, Lexington, KY 40509",
            "formatted_phone_number": "(859) 555-1234",
            "website": "https://smithroofing.com",
            "rating": 4.7,
            "user_ratings_total": 89,
            "types": ["general_contractor", "roofing_contractor"],
        },
        {
            "name": "Blue Ridge Roofers",
            "formatted_address": "789 Elm St, Lexington, KY 40517",
            "rating": 4.3,
            "user_ratings_total": 42,
            "types": ["roofing_contractor"],
        },
    ],
}

BRAVE_WEB_DATA = {
    "results": [
        {
            "title": "Fair Housing Screening Guide",
            "url": "https://hud.gov/fair-housing",
            "description": "Guidelines for tenant screening compliance",
        },
    ],
}


# ---------------------------------------------------------------------------
# TRADES: Tool/Material Price Check
# ---------------------------------------------------------------------------

class TestTradesPriceCheck:
    """TOOL_MATERIAL_PRICE_CHECK: SerpApi Shopping + Home Depot -> PriceComparison."""

    @pytest.mark.asyncio
    async def test_full_pipeline_returns_price_comparison(self):
        query = "Price check on 3 ton HVAC condenser units"
        _, playbook = route_to_playbook(query)
        assert playbook.name == "TOOL_MATERIAL_PRICE_CHECK"

        with patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            new_callable=AsyncMock,
            return_value=_ok("serpapi_shopping.search", SHOPPING_DATA),
        ), patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            new_callable=AsyncMock,
            return_value=_ok("serpapi_homedepot.search", HOMEDEPOT_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert isinstance(response, ResearchResponse)
        assert response.artifact_type == "PriceComparison"
        assert len(response.records) >= 1
        assert "serpapi_shopping" in response.providers_called
        assert "serpapi_home_depot" in response.providers_called

    @pytest.mark.asyncio
    async def test_degraded_one_provider_fails(self):
        """When shopping fails, Home Depot results still come through."""
        query = "Price check on 3 ton HVAC condenser units"
        _, playbook = route_to_playbook(query)

        with patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            new_callable=AsyncMock,
            return_value=_fail("serpapi_shopping.search", "Rate limited"),
        ), patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            new_callable=AsyncMock,
            return_value=_ok("serpapi_homedepot.search", HOMEDEPOT_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert response.artifact_type == "PriceComparison"
        assert len(response.records) >= 1  # At least HD results


# ---------------------------------------------------------------------------
# TRADES: Property Facts & Permits
# ---------------------------------------------------------------------------

class TestTradesPropertyFacts:
    """PROPERTY_FACTS_AND_PERMITS: ATTOM detail + sales -> PropertyFactPack."""

    @pytest.mark.asyncio
    async def test_property_facts_returns_records(self):
        query = "Pull the square footage and permit context for 123 Main St"
        _, playbook = route_to_playbook(query)
        assert playbook.name == "PROPERTY_FACTS_AND_PERMITS"

        with patch(
            "aspire_orchestrator.providers.attom_client.execute_attom_property_detail",
            new_callable=AsyncMock,
            return_value=_ok("attom.property_detail", ATTOM_DETAIL_DATA),
        ), patch(
            "aspire_orchestrator.providers.attom_client.execute_attom_sales_history",
            new_callable=AsyncMock,
            return_value=_ok("attom.sales_history", ATTOM_SALES_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert response.artifact_type == "PropertyFactPack"
        assert len(response.records) >= 1
        assert "attom" in response.providers_called


# ---------------------------------------------------------------------------
# TRADES: Subcontractor Scout
# ---------------------------------------------------------------------------

class TestTradesSubcontractorScout:
    """SUBCONTRACTOR_SCOUT: Google Places -> VendorShortlist."""

    @pytest.mark.asyncio
    async def test_subcontractor_scout_returns_vendors(self):
        query = "Find 8 licensed-looking roofers within 15 miles of 40509"
        _, playbook = route_to_playbook(query)
        assert playbook.name == "SUBCONTRACTOR_SCOUT"

        with patch(
            "aspire_orchestrator.services.search_router.route_places_search",
            new_callable=AsyncMock,
            return_value=_ok("google_places.search", GOOGLE_PLACES_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert response.artifact_type == "VendorShortlist"


# ---------------------------------------------------------------------------
# ACCOUNTING: Tax & Compliance
# ---------------------------------------------------------------------------

class TestAccountingTaxCompliance:
    """TAX_AND_COMPLIANCE_LOOKUP: Exa + Brave -> ComplianceBrief."""

    @pytest.mark.asyncio
    async def test_tax_compliance_returns_compliance_brief(self):
        query = "Official quarterly estimated tax due dates for LLC"
        _, playbook = route_to_playbook(query)
        assert playbook.name == "TAX_AND_COMPLIANCE_LOOKUP"

        with patch(
            "aspire_orchestrator.providers.exa_client.execute_exa_search",
            new_callable=AsyncMock,
            return_value=_ok("exa.search", EXA_DATA),
        ), patch(
            "aspire_orchestrator.services.search_router.route_web_search",
            new_callable=AsyncMock,
            return_value=_ok("brave.search", BRAVE_WEB_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert response.artifact_type == "ComplianceBrief"


# ---------------------------------------------------------------------------
# LANDLORD: Property Facts
# ---------------------------------------------------------------------------

class TestLandlordPropertyFacts:
    """PROPERTY_FACTS: HERE geocode + ATTOM -> LandlordPropertyPack."""

    @pytest.mark.asyncio
    async def test_landlord_property_facts(self):
        query = "Give me property facts for 245 Oak St sqft owner parcel"
        _, playbook = route_to_playbook(query, tenant_segment="landlord")
        assert playbook.name == "PROPERTY_FACTS"

        with patch(
            "aspire_orchestrator.services.search_router.route_places_search",
            new_callable=AsyncMock,
            return_value=_ok("here.geocode", {"items": [{"address": {"label": "245 Oak St"}}]}),
        ), patch(
            "aspire_orchestrator.providers.attom_client.execute_attom_property_detail",
            new_callable=AsyncMock,
            return_value=_ok("attom.property_detail", ATTOM_DETAIL_DATA),
        ), patch(
            "aspire_orchestrator.providers.attom_client.execute_attom_property_detail_with_schools",
            new_callable=AsyncMock,
            return_value=_ok("attom.property_detail_with_schools", ATTOM_DETAIL_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert response.artifact_type == "LandlordPropertyPack"


# ---------------------------------------------------------------------------
# TRAVEL: Hotel Research
# ---------------------------------------------------------------------------

class TestTravelHotelResearch:
    """BUSINESS_TRIP_HOTEL_RESEARCH: Tripadvisor + GP + Exa -> HotelShortlist."""

    @pytest.mark.asyncio
    async def test_hotel_research_returns_shortlist(self):
        query = "Find 5 business-friendly hotels near convention center"
        _, playbook = route_to_playbook(query)
        assert playbook.name == "BUSINESS_TRIP_HOTEL_RESEARCH"

        with patch(
            "aspire_orchestrator.providers.tripadvisor_client.execute_tripadvisor_search",
            new_callable=AsyncMock,
            return_value=_ok("tripadvisor.search", TRIPADVISOR_DATA),
        ), patch(
            "aspire_orchestrator.services.search_router.route_places_search",
            new_callable=AsyncMock,
            return_value=_ok("google_places.search", GOOGLE_PLACES_DATA),
        ), patch(
            "aspire_orchestrator.providers.exa_client.execute_exa_search",
            new_callable=AsyncMock,
            return_value=_ok("exa.search", EXA_DATA),
        ):
            response = await dispatch_playbook(playbook.name, query, CTX)

        assert response.artifact_type == "HotelShortlist"


# ---------------------------------------------------------------------------
# DISPATCH: Fail-closed + completeness
# ---------------------------------------------------------------------------

class TestDispatch:
    """dispatch_playbook fail-closed and completeness checks."""

    @pytest.mark.asyncio
    async def test_unknown_playbook_returns_error(self):
        response = await dispatch_playbook("NONEXISTENT", "test query", CTX)
        assert response.artifact_type == "error"
        assert "Unknown playbook" in response.summary

    @pytest.mark.asyncio
    async def test_all_19_playbooks_have_dispatch_entries(self):
        from aspire_orchestrator.services.adam.playbooks import (
            _ensure_dispatch_loaded,
            _PLAYBOOK_DISPATCH,
        )
        _ensure_dispatch_loaded()
        assert len(_PLAYBOOK_DISPATCH) == 19

    @pytest.mark.asyncio
    async def test_dispatch_context_parameter_compatibility(self):
        """Verify dispatch handles both `ctx` and `context` parameter names."""
        from aspire_orchestrator.services.adam.playbooks import (
            _ensure_dispatch_loaded,
            _PLAYBOOK_DISPATCH,
        )
        import inspect

        _ensure_dispatch_loaded()
        for name, fn in _PLAYBOOK_DISPATCH.items():
            sig = inspect.signature(fn)
            has_ctx = "ctx" in sig.parameters
            has_context = "context" in sig.parameters
            assert has_ctx or has_context, (
                f"Playbook {name} has neither 'ctx' nor 'context' parameter"
            )


# ---------------------------------------------------------------------------
# WIRING: SkillPack integration
# ---------------------------------------------------------------------------

class TestSkillPackWiring:
    """Verify new methods exist and route correctly."""

    def test_research_playbook_method_exists(self):
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchSkillPack
        pack = AdamResearchSkillPack()
        assert hasattr(pack, "research_playbook")

    def test_research_search_delegates_to_playbook(self):
        from aspire_orchestrator.skillpacks.adam_research import AdamResearchSkillPack
        import inspect
        source = inspect.getsource(AdamResearchSkillPack.research_search)
        assert "research_playbook" in source

    def test_enhanced_plan_search_uses_router(self):
        from aspire_orchestrator.skillpacks.adam_research import EnhancedAdamResearch
        import inspect
        source = inspect.getsource(EnhancedAdamResearch.plan_search)
        assert "route_to_playbook" in source


# ---------------------------------------------------------------------------
# ROUTING: All 4 segments produce correct playbooks
# ---------------------------------------------------------------------------

class TestAllSegmentRouting:
    """Verify routing correctness for each segment."""

    @pytest.mark.parametrize("query,segment,expected_playbook,expected_artifact", [
        ("Price check on condenser", None, "TOOL_MATERIAL_PRICE_CHECK", "PriceComparison"),
        ("Find bookkeeping prospects: 20 companies", None, "PROSPECT_RESEARCH", "ProspectList"),
        ("Compare rental properties versus similar sqft", "landlord", "RENT_COMP_CONTEXT", "RentCompPack"),
        ("Find business-friendly hotels near convention", None, "BUSINESS_TRIP_HOTEL_RESEARCH", "HotelShortlist"),
    ])
    def test_segment_routing(self, query, segment, expected_playbook, expected_artifact):
        _, playbook = route_to_playbook(query, tenant_segment=segment)
        assert playbook is not None
        assert playbook.name == expected_playbook
        assert playbook.artifact_type == expected_artifact
