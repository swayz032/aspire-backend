"""TRADES Playbooks — 6 research playbooks for trades ICP.

Segments: plumbers, HVAC, electricians, roofers, painters, GCs, landscapers
Playbooks: Property Facts & Permits, Estimate Research, Tool/Material Price Check,
           Competitor Pricing Scan, Subcontractor Scout, Territory Opportunity Scan
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)


async def execute_property_facts_and_permits(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """PROPERTY_FACTS_AND_PERMITS — Resolve property context for quoting."""
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_property_detail,
        execute_attom_sales_history,
    )
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
        normalize_from_attom_detail,
        normalize_from_attom_sales_history,
    )

    logger.info("Executing PROPERTY_FACTS_AND_PERMITS for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM property detail
    detail_result = await execute_attom_property_detail(
        payload={"address": address or query},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        capability_token_id=ctx.capability_token_id,
        capability_token_hash=ctx.capability_token_hash,
    )
    providers_called.append("attom")

    if detail_result.outcome.value == "success" and detail_result.data:
        prop = normalize_from_attom_detail(detail_result.data)
        records.append(prop.to_dict())
        sources.extend(prop.sources)

    # 2. ATTOM sales history
    history_result = await execute_attom_sales_history(
        payload={"address": address or query},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        capability_token_id=ctx.capability_token_id,
        capability_token_hash=ctx.capability_token_hash,
    )

    if history_result.outcome.value == "success" and history_result.data:
        sales = normalize_from_attom_sales_history(history_result.data)
        if sales and records:
            records[0]["sale_history"] = [
                {"date": s.date, "amount": s.amount, "trans_type": s.trans_type,
                 "buyer": s.buyer, "seller": s.seller}
                for s in sales
            ]

    # Verify
    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "living_sqft", "year_built"],
    )

    return ResearchResponse(
        artifact_type="PropertyFactPack",
        summary=f"Property facts for {address or query}",
        records=records,
        sources=sources,
        freshness={"provider": "attom"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Add rental valuation", "Pull nearby sales comparables"],
        verification_report=report,
        segment="trades",
        intent="property_fact",
        playbook="PROPERTY_FACTS_AND_PERMITS",
        providers_called=providers_called,
    )


async def execute_estimate_research(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """ESTIMATE_RESEARCH — Support quoting with property facts + material pricing."""
    from aspire_orchestrator.providers.attom_client import execute_attom_property_detail
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.property_normalizer import normalize_from_attom_detail
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import normalize_from_serpapi_homedepot

    logger.info("Executing ESTIMATE_RESEARCH for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM for property context
    if address:
        detail_result = await execute_attom_property_detail(
            payload={"address": address},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom")
        if detail_result.outcome.value == "success" and detail_result.data:
            prop = normalize_from_attom_detail(detail_result.data)
            records.append(prop.to_dict())
            sources.extend(prop.sources)

    # 2. SerpApi Home Depot for material pricing
    hd_result = await execute_serpapi_homedepot_search(
        payload={"query": query, "hd_sort": "price_low_to_high"},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("serpapi_home_depot")

    if hd_result.outcome.value == "success" and hd_result.data:
        for item in hd_result.data.get("results", [])[:8]:
            product = normalize_from_serpapi_homedepot(item)
            records.append(product.to_dict())
            sources.extend(product.sources)

    report = verify_records(records=records, sources=sources, required_fields=["normalized_address", "living_sqft"])

    return ResearchResponse(
        artifact_type="EstimateResearchPack",
        summary=f"Estimate research for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Compare with Google Shopping prices", "Find subcontractors for this job"],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="ESTIMATE_RESEARCH",
        providers_called=providers_called,
    )


async def execute_tool_material_price_check(
    query: str, ctx: PlaybookContext, zip_code: str = "", store_id: str = "",
    on_sale: bool = False,
) -> ResearchResponse:
    """TOOL_MATERIAL_PRICE_CHECK — Find current pricing for tools/materials."""
    from aspire_orchestrator.providers.serpapi_shopping_client import execute_serpapi_shopping_search
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import (
        normalize_from_serpapi_shopping,
        normalize_from_serpapi_homedepot,
    )

    logger.info("Executing TOOL_MATERIAL_PRICE_CHECK for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. SerpApi Google Shopping (cross-retailer)
    shopping_payload: dict[str, Any] = {"query": query, "sort_by": 1}  # price low-high
    if zip_code:
        shopping_payload["location"] = zip_code
    if on_sale:
        shopping_payload["on_sale"] = True

    shopping_result = await execute_serpapi_shopping_search(
        payload=shopping_payload,
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("serpapi_shopping")

    if shopping_result.outcome.value == "success" and shopping_result.data:
        for item in shopping_result.data.get("results", [])[:6]:
            product = normalize_from_serpapi_shopping(item)
            records.append(product.to_dict())
            sources.extend(product.sources)

    # 2. SerpApi Home Depot (store-specific with stock)
    hd_payload: dict[str, Any] = {"query": query, "hd_sort": "price_low_to_high"}
    if store_id:
        hd_payload["store_id"] = store_id
    if zip_code:
        hd_payload["delivery_zip"] = zip_code

    hd_result = await execute_serpapi_homedepot_search(
        payload=hd_payload,
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("serpapi_home_depot")

    if hd_result.outcome.value == "success" and hd_result.data:
        for item in hd_result.data.get("results", [])[:6]:
            product = normalize_from_serpapi_homedepot(item)
            records.append(product.to_dict())
            sources.extend(product.sources)

    report = verify_records(records=records, sources=sources, required_fields=["product_name", "price", "retailer"])

    return ResearchResponse(
        artifact_type="PriceComparison",
        summary=f"Price check for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Check availability at specific store", "Compare alternative products"],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="TOOL_MATERIAL_PRICE_CHECK",
        providers_called=providers_called,
    )


async def execute_competitor_pricing_scan(
    query: str, ctx: PlaybookContext, location: str = "",
) -> ResearchResponse:
    """COMPETITOR_PRICING_SCAN — Map local competitors and pricing signals."""
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import normalize_from_google_places
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing COMPETITOR_PRICING_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. Google Places for local competitors
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 2. Exa deep-lite for competitor intelligence with structured output
    exa_result = await execute_exa_search(
        payload={
            "query": f"competitor pricing analysis {query}",
            "type": "deep-lite",
            "category": "company",
            "num_results": 5,
            "moderation": True,
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("exa")

    exa_grounding: list[dict[str, Any]] = []
    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:5]:
            we = normalize_from_exa(r)
            records.append(we.to_dict())
            sources.append(SourceAttribution(provider="exa"))
        exa_grounding = exa_result.data.get("grounding", [])

    report = verify_records(
        records=records, sources=sources,
        required_fields=["name", "normalized_address"],
        exa_grounding=exa_grounding,
    )

    return ResearchResponse(
        artifact_type="CompetitorBrief",
        summary=f"Competitor scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Deep dive on top competitor", "Compare pricing models"],
        verification_report=report,
        segment="trades",
        intent="compare",
        playbook="COMPETITOR_PRICING_SCAN",
        providers_called=providers_called,
    )


async def execute_subcontractor_scout(
    query: str, ctx: PlaybookContext, location: str = "",
) -> ResearchResponse:
    """SUBCONTRACTOR_SCOUT — Find nearby subcontractors by trade."""
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
        normalize_from_google_places,
        normalize_from_foursquare,
    )

    logger.info("Executing SUBCONTRACTOR_SCOUT for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. Google Places
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 2. Foursquare for additional coverage
    fs_result = await execute_foursquare_search(
        payload={"query": query, "near": location},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("foursquare")

    if fs_result.outcome.value == "success" and fs_result.data:
        for place in fs_result.data.get("results", [])[:5]:
            biz = normalize_from_foursquare(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    report = verify_records(
        records=records, sources=sources,
        required_fields=["name", "normalized_address", "phone"],
    )

    return ResearchResponse(
        artifact_type="VendorShortlist",
        summary=f"Subcontractor search for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Verify licensing", "Check reviews in detail"],
        verification_report=report,
        segment="trades",
        intent="lookup",
        playbook="SUBCONTRACTOR_SCOUT",
        providers_called=providers_called,
    )


async def execute_territory_opportunity_scan(
    query: str, ctx: PlaybookContext, geo_scope: str = "",
) -> ResearchResponse:
    """TERRITORY_OPPORTUNITY_SCAN — Identify promising ZIPs by density + activity."""
    from aspire_orchestrator.providers.attom_client import execute_attom_sales_trends
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.business_normalizer import normalize_from_google_places
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing TERRITORY_OPPORTUNITY_SCAN for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # 1. ATTOM sales trends for market activity
    if geo_scope:
        trends_result = await execute_attom_sales_trends(
            payload={"geoid": geo_scope, "geo_type": "ZI"},
            correlation_id=ctx.correlation_id,
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
        )
        providers_called.append("attom")
        if trends_result.outcome.value == "success" and trends_result.data:
            records.append({"type": "market_trends", "data": trends_result.data})
            sources.append(SourceAttribution(provider="attom"))

    # 2. Google Places for competitor density
    gp_result = await execute_google_places_search(
        payload={"query": query, "location": geo_scope},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:10]:
            biz = normalize_from_google_places(place)
            records.append(biz.to_dict())
            sources.extend(biz.sources)

    # 3. Exa for market intelligence
    exa_result = await execute_exa_search(
        payload={
            "query": f"market opportunity {query} {geo_scope}",
            "type": "deep-lite",
            "num_results": 5,
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("exa")

    if exa_result.outcome.value == "success" and exa_result.data:
        for r in exa_result.data.get("results", [])[:5]:
            we = normalize_from_exa(r)
            records.append(we.to_dict())
            sources.append(SourceAttribution(provider="exa"))

    report = verify_records(records=records, sources=sources)

    return ResearchResponse(
        artifact_type="TerritoryAnalysis",
        summary=f"Territory scan for {query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=["Drill into top ZIP code", "Compare adjacent territories"],
        verification_report=report,
        segment="trades",
        intent="territory_scan",
        playbook="TERRITORY_OPPORTUNITY_SCAN",
        providers_called=providers_called,
    )
