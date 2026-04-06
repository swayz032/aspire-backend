"""TRAVEL Playbooks — 1 research playbook for travel/hotel ICP.

Playbook: Business Trip Hotel Research
Guardrail: research and recommendation only, NO booking in v1.
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


async def execute_business_trip_hotel_research(
    query: str,
    ctx: PlaybookContext,
    destination: str = "",
    meeting_address: str = "",
    budget_max: float | None = None,
    preferences: list[str] | None = None,
) -> ResearchResponse:
    """BUSINESS_TRIP_HOTEL_RESEARCH — Find strong hotel options for business trips.

    Provider order: Tripadvisor → HERE → Google Places → Exa (instant for sentiment)
    Guardrail: research and recommendation only, NO booking in v1.
    """
    from aspire_orchestrator.providers.tripadvisor_client import execute_tripadvisor_search
    from aspire_orchestrator.providers.google_places_client import execute_google_places_search
    from aspire_orchestrator.providers.exa_client import execute_exa_search
    from aspire_orchestrator.services.adam.normalizers.hotel_normalizer import (
        normalize_from_tripadvisor,
        normalize_from_google_places_hotel,
    )
    from aspire_orchestrator.services.adam.normalizers.web_normalizer import normalize_from_exa

    logger.info("Executing BUSINESS_TRIP_HOTEL_RESEARCH for: %s", query[:80])

    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    prefs = preferences or []

    search_query = destination or query
    if prefs:
        search_query = f"{search_query} {' '.join(prefs)}"

    # 1. Tripadvisor for hotel sentiment + rankings
    ta_result = await execute_tripadvisor_search(
        payload={
            "query": search_query,
            "category": "hotels",
            "language": "en",
        },
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("tripadvisor")

    if ta_result.outcome.value == "success" and ta_result.data:
        for item in ta_result.data.get("results", [])[:8]:
            hotel = normalize_from_tripadvisor(item)
            records.append(hotel.to_dict())
            sources.extend(hotel.sources)

    # 2. Google Places for additional hotel coverage
    gp_result = await execute_google_places_search(
        payload={"query": f"hotels near {destination or query}", "location": destination},
        correlation_id=ctx.correlation_id,
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
    )
    providers_called.append("google_places")

    if gp_result.outcome.value == "success" and gp_result.data:
        for place in gp_result.data.get("results", [])[:5]:
            hotel = normalize_from_google_places_hotel(place)
            records.append(hotel.to_dict())
            sources.extend(hotel.sources)

    # 3. Exa instant for quick sentiment verification on top hotels
    if records:
        top_hotel = records[0].get("name", "")
        if top_hotel:
            exa_result = await execute_exa_search(
                payload={
                    "query": f"{top_hotel} {destination} hotel reviews business travel",
                    "type": "instant",
                    "num_results": 3,
                    "moderation": True,
                },
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
            )
            providers_called.append("exa")

            if exa_result.outcome.value == "success" and exa_result.data:
                for r in exa_result.data.get("results", [])[:3]:
                    we = normalize_from_exa(r)
                    sources.append(SourceAttribution(provider="exa"))
                    # Enrich top hotel with sentiment from reviews
                    if we.summary and records:
                        records[0]["web_sentiment"] = we.summary

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name", "normalized_address", "traveler_rating"],
    )

    return ResearchResponse(
        artifact_type="HotelShortlist",
        summary=f"Hotel research for {destination or query[:60]}",
        records=records,
        sources=sources,
        freshness={"mode": "live"},
        confidence={"status": report.status, "score": report.confidence_score},
        missing_fields=report.missing_fields,
        next_queries=[
            "Compare prices on booking sites",
            "Check amenities in detail",
            "Find nearby restaurants",
        ],
        verification_report=report,
        segment="travel",
        intent="hotel_research",
        playbook="BUSINESS_TRIP_HOTEL_RESEARCH",
        providers_called=providers_called,
    )
