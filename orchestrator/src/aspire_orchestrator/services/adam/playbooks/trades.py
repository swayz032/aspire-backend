"""TRADES Playbooks — 6 research playbooks for trades ICP.

Segments: plumbers, HVAC, electricians, roofers, painters, GCs, landscapers
Playbooks: Property Facts & Permits, Estimate Research, Tool/Material Price Check,
           Competitor Pricing Scan, Subcontractor Scout, Territory Opportunity Scan
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)


def _extract_address_from_query(query: str) -> str:
    """Extract address from a natural language query for ATTOM."""
    import re
    # Strict pattern (state abbreviation or full state name)
    match = re.search(
        r'(\d+\s+[\w\s]+(?:St|Ave|Rd|Blvd|Dr|Ln|Ct|Way|Pl|Cir|Pkwy|Hwy|Ter)\.?'
        r'(?:\s*,\s*[\w\s]+,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?))',
        query, re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    prefixes = [
        "pull property facts for", "pull property details for", "pull property profile for",
        "find property facts for", "find property details for", "find property profile for",
        "property facts for", "property details for", "property profile for",
        "pull the square footage and permit context for",
        "pull", "get", "show me", "find", "look up",
    ]
    remaining = query.strip()
    while remaining:
        q_lower = remaining.lower().strip()
        consumed = False
        for prefix in sorted(prefixes, key=len, reverse=True):
            if q_lower.startswith(prefix):
                remaining = remaining[len(prefix):].strip(" .,:;-")
                consumed = True
                break
        if not consumed:
            break
    marker = "additional details:"
    rem_lower = remaining.lower()
    if marker in rem_lower:
        idx = rem_lower.rfind(marker)
        tail = remaining[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail
    if remaining and remaining != query:
        return remaining
    lower_query = query.lower()
    if marker in lower_query:
        idx = lower_query.rfind(marker)
        tail = query[idx + len(marker):].strip(" .,:;-")
        if tail:
            return tail
    # Loose fallback for wrapped inputs like:
    # "property lookup. Additional details: 4863 Price Street, Forest Park, Georgia, 30297"
    loose = re.search(
        r'(\d+\s+[\w\s]+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Way|Pl|Place|Cir|Circle|Pkwy|Parkway|Hwy|Highway|Ter|Terrace)\b[^,\n]*'
        r'(?:,\s*[\w\s]+){0,2}\s*,?\s*(?:[A-Z]{2}|[A-Za-z]{4,})\s*,?\s*\d{5}(?:-\d{4})?)',
        query,
        re.IGNORECASE,
    )
    if loose:
        return loose.group(1).strip()
    return query


async def execute_property_facts_and_permits(
    query: str, ctx: PlaybookContext, address: str = "",
) -> ResearchResponse:
    """PROPERTY_FACTS_AND_PERMITS — Resolve property context for quoting."""
    from aspire_orchestrator.providers.attom_client import (
        execute_attom_detail_mortgage_owner,
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

    # Extract address from query
    attom_address = address or _extract_address_from_query(query)

    # 1. ATTOM property detail (with mortgage+owner for full context)
    detail_result = await execute_attom_detail_mortgage_owner(
        payload={"address": attom_address},
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
        payload={"address": attom_address},
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
    """TOOL_MATERIAL_PRICE_CHECK - Find current pricing, stock, and store info for tools/materials.

    Strict policy for product cards:
      1. Run search with resolved location/store context.
      2. Retry with tightened query up to 3 attempts.
      3. Fail closed (no partial cards) if required product/store-summary fields are incomplete.
    """
    import re as _re
    from aspire_orchestrator.providers.serpapi_shopping_client import execute_serpapi_shopping_search
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    from aspire_orchestrator.services.adam.normalizers.product_normalizer import (
        normalize_from_serpapi_shopping,
        normalize_from_serpapi_homedepot,
    )
    from aspire_orchestrator.services.adam.hd_store_resolver import resolve_store_async

    logger.info("Executing TOOL_MATERIAL_PRICE_CHECK for: %s", query[:80])

    if not zip_code:
        zip_match = _re.search(r"\b(\d{5})\b", query)
        if zip_match:
            zip_code = zip_match.group(1)

    location_hint = ""
    city_match = _re.search(r"\bin\s+([A-Za-z][A-Za-z\s]+(?:,\s*[A-Za-z]{2})?)\b", query)
    if city_match:
        location_hint = city_match.group(1).strip(" .,")

    def _product_missing_fields(r: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in ("product_name", "price", "url", "image_url", "retailer"):
            v = r.get(field)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(field)
        return missing

    def _store_missing_fields(store: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for field in ("store_name", "address", "phone", "website"):
            v = store.get(field)
            if v is None or (isinstance(v, str) and not str(v).strip()):
                missing.append(field)
        return missing

    providers_called: list[str] = []
    last_missing_fields: list[str] = []
    final_records: list[dict[str, Any]] = []
    final_sources: list[SourceAttribution] = []
    final_store_summary: dict[str, Any] = {}

    query_attempts = [
        query,
        f"{query} Home Depot",
        f"{query} Home Depot {location_hint}".strip(),
    ]

    for attempt_idx, attempt_query in enumerate(query_attempts, start=1):
        records: list[dict[str, Any]] = []
        sources: list[SourceAttribution] = []
        hd_store_info: dict[str, Any] = {}
        resolved_store_id = store_id

        shopping_payload: dict[str, Any] = {"query": attempt_query, "sort_by": 1}
        if zip_code:
            shopping_payload["location"] = zip_code
        elif location_hint:
            shopping_payload["location"] = location_hint
        if on_sale:
            shopping_payload["on_sale"] = True

        async def _resolve_and_search_hd() -> Any:
            nonlocal resolved_store_id, hd_store_info
            if not resolved_store_id and (zip_code or location_hint):
                store_match = await resolve_store_async(
                    zip_code=zip_code,
                    location_hint=location_hint,
                    correlation_id=ctx.correlation_id,
                    suite_id=ctx.suite_id,
                    office_id=ctx.office_id,
                )
                if store_match:
                    resolved_store_id = str(store_match.get("store_id", "")).strip()
                    hd_store_info = dict(store_match)

            hd_payload: dict[str, Any] = {"query": attempt_query, "hd_sort": "best_match"}
            if resolved_store_id:
                hd_payload["store_id"] = resolved_store_id
            if zip_code:
                hd_payload["delivery_zip"] = zip_code
            return await execute_serpapi_homedepot_search(
                payload=hd_payload,
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
            )

        hd_result, shopping_result = await asyncio.gather(
            _resolve_and_search_hd(),
            execute_serpapi_shopping_search(
                payload=shopping_payload,
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
            ),
            return_exceptions=True,
        )

        if "serpapi_home_depot" not in providers_called:
            providers_called.append("serpapi_home_depot")
        if "serpapi_shopping" not in providers_called:
            providers_called.append("serpapi_shopping")

        if not isinstance(hd_result, Exception) and hd_result.outcome.value == "success" and hd_result.data:
            serpapi_store = hd_result.data.get("store", {})
            if serpapi_store.get("store_name"):
                hd_store_info["store_name"] = serpapi_store["store_name"]
            if not hd_store_info.get("store_id") and serpapi_store.get("store_id"):
                hd_store_info["store_id"] = serpapi_store["store_id"]
            for item in hd_result.data.get("results", [])[:8]:
                product = normalize_from_serpapi_homedepot(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        if not isinstance(shopping_result, Exception) and shopping_result.outcome.value == "success" and shopping_result.data:
            for item in shopping_result.data.get("results", [])[:6]:
                product = normalize_from_serpapi_shopping(item)
                records.append(product.to_dict())
                sources.extend(product.sources)

        hd_products = [r for r in records if r.get("retailer") == "Home Depot"]
        complete_products = [r for r in hd_products if not _product_missing_fields(r)]
        store_summary = {
            "card_kind": "store_summary",
            "store_id": hd_store_info.get("store_id", ""),
            "store_name": hd_store_info.get("store_name", ""),
            "address": hd_store_info.get("address", ""),
            "city": hd_store_info.get("city", ""),
            "state": hd_store_info.get("state", ""),
            "postal_code": hd_store_info.get("postal_code", ""),
            "phone": hd_store_info.get("phone", ""),
            "website": hd_store_info.get("website", ""),
            "open_now": hd_store_info.get("open_now"),
            "rating": hd_store_info.get("rating"),
            "retailer": "Home Depot",
        }
        store_missing = _store_missing_fields(store_summary)
        last_missing_fields = sorted({
            *[m for r in hd_products for m in _product_missing_fields(r)],
            *[f"store_summary.{f}" for f in store_missing],
        })

        logger.info(
            "TOOL_MATERIAL_PRICE_CHECK attempt=%s hd_products=%s complete_hd_products=%s store_missing=%s",
            attempt_idx, len(hd_products), len(complete_products), store_missing,
        )

        if complete_products and not store_missing:
            final_records = [store_summary, *complete_products, *[r for r in records if r.get("retailer") != "Home Depot"]]
            final_sources = sources
            final_store_summary = store_summary
            break

    if not final_records:
        return ResearchResponse(
            artifact_type="error",
            summary="I could not retrieve complete Home Depot product and store details right now. Please try again in a moment.",
            records=[],
            sources=[],
            freshness={"mode": "live"},
            confidence={"status": "unverified", "score": 0.0},
            missing_fields=last_missing_fields,
            next_queries=["Try again in a moment", "Use a different city or ZIP"],
            segment="trades",
            intent="price_check",
            playbook="TOOL_MATERIAL_PRICE_CHECK",
            providers_called=providers_called,
            extra={"hard_fail": True, "missing_fields": last_missing_fields},
        )

    report = verify_records(records=final_records, sources=final_sources, required_fields=["product_name", "price", "retailer"])

    hd_count = sum(1 for r in final_records if r.get("retailer") == "Home Depot" and r.get("card_kind") != "store_summary")
    in_stock = sum(1 for r in final_records if r.get("retailer") == "Home Depot" and r.get("in_store_stock") and r["in_store_stock"] > 0)
    summary_parts = [f"Price check for {query[:60]}"]
    if final_store_summary.get("store_name"):
        summary_parts.append(
            f"Home Depot store: {final_store_summary['store_name']} (#{final_store_summary.get('store_id', '')})"
        )
    summary_parts.append(f"{hd_count} Home Depot products, {in_stock} in stock")

    return ResearchResponse(
        artifact_type="PriceComparison",
        summary=". ".join(summary_parts) + ".",
        records=final_records,
        sources=final_sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=[
            f"Compare prices at Lowe's near {zip_code}" if zip_code else "Compare at other retailers",
            "Check for current sales and promotions",
        ],
        verification_report=report,
        segment="trades",
        intent="price_check",
        playbook="TOOL_MATERIAL_PRICE_CHECK",
        providers_called=providers_called,
        extra={"store_summary": final_store_summary, "cards_version": "v1"},
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

