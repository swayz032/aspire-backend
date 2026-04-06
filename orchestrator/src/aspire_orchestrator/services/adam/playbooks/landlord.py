"""Adam Research Playbooks — Landlord Segment.

Six playbook execute functions for the landlord segment:
  1. property_facts          — full property fact pack from ATTOM
  2. rent_comp_context       — rental AVM + sales comparables context
  3. permit_and_renovation   — permit signals + web evidence
  4. neighborhood_demand     — sales trends + competitor density
  5. screening_compliance    — tenant screening law compliance research
  6. turnover_vendor_scout   — make-ready vendor discovery

Provider routing (per ecosystem providers.yaml + ATTOM ADR-002):
  Geocode: here (primary address normalization before ATTOM)
  Property: attom (authoritative — trust class A)
  Places:   google_places → foursquare → here
  Web:      exa (primary) → brave (fallback)

Guardrails:
  - execute_rent_comp_context: never manufacture a price target if rental AVM
    returns no data. Return missing_fields=["estimated_rent"] + next_queries.
  - execute_screening_compliance: never produce tenant scoring, never emit
    accept/reject recommendations. Compliance-information only.
  - ATTOM requires address normalization via HERE geocoding first (ADR-002).
    If HERE geocoding fails, proceed with raw address string (best-effort).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.attom_client import (
    execute_attom_property_detail,
    execute_attom_property_detail_with_schools,
    execute_attom_rental_avm,
    execute_attom_sales_comparables,
    execute_attom_sales_history,
    execute_attom_sales_trends,
    execute_attom_valuation_avm,
)
from aspire_orchestrator.providers.brave_client import execute_brave_search
from aspire_orchestrator.providers.exa_client import execute_exa_search
from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
from aspire_orchestrator.providers.google_places_client import execute_google_places_search
from aspire_orchestrator.providers.here_client import execute_here_search
from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
    normalize_from_foursquare,
    normalize_from_google_places,
    normalize_from_here,
)
from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
    normalize_from_attom_detail,
    normalize_from_attom_rental,
    normalize_from_attom_sales_history,
    normalize_from_attom_valuation,
)
from aspire_orchestrator.services.adam.normalizers.web_normalizer import (
    normalize_from_brave,
    normalize_from_exa,
)
from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.schemas.verification_report import VerificationReport
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)

_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _provider_args(ctx: PlaybookContext) -> dict[str, Any]:
    """Shared keyword args for every provider execute call."""
    return {
        "correlation_id": ctx.correlation_id,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "capability_token_id": ctx.capability_token_id,
        "capability_token_hash": ctx.capability_token_hash,
    }


def _source(provider: str) -> SourceAttribution:
    return SourceAttribution(provider=provider, retrieved_at=_NOW())


def _confidence_dict(report: VerificationReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "score": report.confidence_score,
        "source_count": report.source_count,
        "conflict_count": report.conflict_count,
    }


async def _geocode_address(address: str, context: PlaybookContext) -> str:
    """Normalize address via HERE geocoding before ATTOM queries (ADR-002).

    Returns the normalized label from HERE, or the raw address string on any
    failure (best-effort — ATTOM will attempt its own address parsing).
    """
    args = _provider_args(context)
    try:
        result = await execute_here_search(
            payload={"query": address, "limit": 1},
            **args,
        )
        if result.outcome == Outcome.SUCCESS:
            items = (result.data or {}).get("items", [])
            if items:
                label = items[0].get("address", {}).get("label", "")
                if label:
                    logger.debug(
                        "landlord: HERE geocoded '%s' -> '%s'",
                        address,
                        label,
                        extra={"correlation_id": context.correlation_id},
                    )
                    return label
    except Exception as exc:
        logger.warning(
            "landlord: HERE geocoding failed, using raw address: %s",
            exc,
            extra={"correlation_id": context.correlation_id},
        )
    return address


# ---------------------------------------------------------------------------
# 1. Property Facts
# ---------------------------------------------------------------------------

async def execute_property_facts(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Build a full property fact pack from ATTOM authoritative data.

    Provider plan:
      here:   geocode address -> normalized address string
      attom:  property_detail_with_schools + sales_history + valuation_avm + rental_avm
              (parallel calls after geocoding)

    required_fields: normalized_address, living_sqft, year_built, owner_name
    Returns: LandlordPropertyPack artifact.
    """
    logger.info(
        "landlord.property_facts start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Normalize address via HERE (ADR-002)
    normalized_address = await _geocode_address(query, context)
    providers_called.append("here")

    attom_payload = {"address": normalized_address}

    # Step 2: Parallel ATTOM calls — all read-only and independent
    detail_result, sales_result, avm_result, rental_result = await asyncio.gather(
        execute_attom_property_detail_with_schools(payload=attom_payload, **args),
        execute_attom_sales_history(payload=attom_payload, **args),
        execute_attom_valuation_avm(payload=attom_payload, **args),
        execute_attom_rental_avm(payload=attom_payload, **args),
        return_exceptions=False,
    )
    providers_called.extend(["attom", "attom", "attom", "attom"])

    # Normalize base property record
    prop_dict: dict[str, Any] = {}
    if detail_result.outcome == Outcome.SUCCESS and detail_result.data:
        prop = normalize_from_attom_detail(detail_result.data)
        prop_dict = prop.to_dict()
        sources.append(_source("attom"))
    else:
        logger.warning(
            "landlord.property_facts: attom detail failed: %s",
            detail_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Merge sales history
    if sales_result.outcome == Outcome.SUCCESS and sales_result.data:
        sale_records = normalize_from_attom_sales_history(sales_result.data)
        if prop_dict:
            prop_dict["sale_history"] = [
                {
                    "date": s.date,
                    "amount": s.amount,
                    "trans_type": s.trans_type,
                    "buyer": s.buyer,
                    "seller": s.seller,
                }
                for s in sale_records
            ]
    else:
        logger.warning(
            "landlord.property_facts: attom sales_history failed: %s",
            sales_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Merge AVM valuation
    if avm_result.outcome == Outcome.SUCCESS and avm_result.data:
        avm = normalize_from_attom_valuation(avm_result.data)
        if avm and prop_dict:
            prop_dict.update(avm)
    else:
        logger.warning(
            "landlord.property_facts: attom valuation_avm failed: %s",
            avm_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Merge rental AVM
    if rental_result.outcome == Outcome.SUCCESS and rental_result.data:
        rental = normalize_from_attom_rental(rental_result.data)
        if rental and prop_dict:
            prop_dict.update(rental)
    else:
        logger.warning(
            "landlord.property_facts: attom rental_avm failed: %s",
            rental_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    if prop_dict:
        records.append(prop_dict)

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "living_sqft", "year_built", "owner_name"],
    )

    next_queries: list[str] = []
    if "normalized_address" in report.missing_fields:
        next_queries.append(f"Verify address with county assessor: {query}")
    if not records:
        next_queries.append(f"Manual ATTOM lookup: {query}")
    else:
        next_queries.extend([
            "Pull nearby sales comparables",
            "Check rental demand in this ZIP",
        ])

    return ResearchResponse(
        artifact_type="LandlordPropertyPack",
        summary=(
            f"Property facts for '{normalized_address}': "
            f"Verification: {report.status} (score={report.confidence_score}). "
            f"Missing: {report.missing_fields or 'none'}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="property_facts",
        playbook="landlord.property_facts",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 2. Rent Comp Context
# ---------------------------------------------------------------------------

async def execute_rent_comp_context(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Rental AVM + sales comparables for rent pricing context.

    Provider plan:
      here:   geocode address
      attom:  rental_avm + sales_comparables (parallel)
      google_places: neighborhood context

    GUARDRAIL: If rental_avm returns no data, do NOT manufacture a price target.
    Return missing_fields=["estimated_rent"] with corrective next_queries.

    required_fields: normalized_address, estimated_rent
    Returns: RentCompPack artifact.
    """
    logger.info(
        "landlord.rent_comp_context start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Normalize address
    normalized_address = await _geocode_address(query, context)
    providers_called.append("here")

    attom_payload = {"address": normalized_address}

    # Step 2: Parallel ATTOM calls
    rental_result, comps_result = await asyncio.gather(
        execute_attom_rental_avm(payload=attom_payload, **args),
        execute_attom_sales_comparables(
            payload={**attom_payload, "searchtype": "rental", "miles": "1"},
            **args,
        ),
        return_exceptions=False,
    )
    providers_called.extend(["attom", "attom"])

    rental_record: dict[str, Any] = {"normalized_address": normalized_address}
    has_rental_data = False

    if rental_result.outcome == Outcome.SUCCESS and rental_result.data:
        rental = normalize_from_attom_rental(rental_result.data)
        if rental.get("estimated_rent") is not None:
            has_rental_data = True
        rental_record.update(rental)
        sources.append(_source("attom"))
    else:
        logger.warning(
            "landlord.rent_comp_context: attom rental_avm failed: %s",
            rental_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    if comps_result.outcome == Outcome.SUCCESS and comps_result.data:
        comps_props = comps_result.data.get("property", [])
        rental_record["sales_comparables"] = comps_props[:10]
    else:
        logger.warning(
            "landlord.rent_comp_context: attom sales_comparables failed: %s",
            comps_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    records.append(rental_record)

    # Step 3: Google Places — neighborhood context
    gp_result = await execute_google_places_search(
        payload={"query": f"neighborhood rental market {normalized_address}"},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        for raw in (gp_result.data or {}).get("results", [])[:3]:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address", "estimated_rent"],
    )

    # Guardrail: surface missing rental data clearly
    guardrail_note = ""
    if not has_rental_data:
        guardrail_note = (
            " GUARDRAIL: No rental valuation available — do not use as rent target."
        )

    next_queries: list[str] = []
    if not has_rental_data:
        next_queries.extend([
            f"Zillow rental estimate: {normalized_address}",
            f"Rentometer comparable rents: {normalized_address}",
        ])
    else:
        next_queries.extend([
            "Get full property detail",
            "Check neighborhood demand trends",
        ])

    return ResearchResponse(
        artifact_type="RentCompPack",
        summary=(
            f"Rent comp context for '{normalized_address}': "
            f"Rental data available: {has_rental_data}. "
            f"Verification: {report.status}.{guardrail_note}"
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom+google_places"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="rent_comp_context",
        playbook="landlord.rent_comp_context",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 3. Permit and Renovation
# ---------------------------------------------------------------------------

async def execute_permit_and_renovation(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research permit history and renovation signals for a property.

    Provider plan:
      here:   geocode address
      attom:  property_detail (for permit signals in building.construction fields)
      exa:    web permit evidence (county portals, news)
      brave:  fallback web evidence

    Returns: PermitContextPack artifact.
    """
    logger.info(
        "landlord.permit_and_renovation start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # Step 1: Normalize address
    normalized_address = await _geocode_address(query, context)
    providers_called.append("here")

    # Step 2: ATTOM property detail for permit/construction signals
    detail_result = await execute_attom_property_detail(
        payload={"address": normalized_address},
        **args,
    )
    providers_called.append("attom")
    if detail_result.outcome == Outcome.SUCCESS and detail_result.data:
        prop = normalize_from_attom_detail(detail_result.data)
        prop_dict = prop.to_dict()
        # Surface permit-relevant construction fields prominently
        prop_dict["permit_context"] = {
            "year_built": prop_dict.get("year_built"),
            "construction_frame": prop_dict.get("construction_frame"),
            "roof_cover": prop_dict.get("roof_cover"),
            "quality": prop_dict.get("quality"),
            "permit_signals": prop_dict.get("permit_signals", []),
        }
        records.append(prop_dict)
        sources.append(_source("attom"))
    else:
        logger.warning(
            "landlord.permit_and_renovation: attom detail failed: %s",
            detail_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Step 3: Exa for web permit evidence
    exa_result = await execute_exa_search(
        payload={
            "query": f"{normalized_address} building permit renovation construction",
            "num_results": 8,
            "moderation": True,
            "contents": {"text": True, "highlights": True},
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        for item in (exa_result.data or {}).get("results", []):
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))
    else:
        # Brave fallback
        brave_result = await execute_brave_search(
            payload={
                "query": f"building permits renovation {normalized_address}",
                "count": 5,
            },
            **args,
        )
        providers_called.append("brave")
        if brave_result.outcome == Outcome.SUCCESS:
            for item in (brave_result.data or {}).get("results", [])[:5]:
                ev = normalize_from_brave(item)
                records.append(ev.to_dict())
            sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["normalized_address"],
        exa_grounding=exa_grounding or None,
    )

    return ResearchResponse(
        artifact_type="PermitContextPack",
        summary=(
            f"Permit & renovation context for '{normalized_address}': "
            f"{len(records)} signals. Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "attom+exa"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=[
            f"{normalized_address} county building department permit search",
            "Find renovation contractors",
        ],
        verification_report=report,
        segment="landlord",
        intent="permit_and_renovation",
        playbook="landlord.permit_and_renovation",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 4. Neighborhood Demand
# ---------------------------------------------------------------------------

async def execute_neighborhood_demand(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Multi-factor neighborhood demand analysis.

    Provider plan:
      attom:    sales_trends for geographic trends (geoid from provider_plan or ZIP regex)
      google_places: rental/housing competitor density
      exa:      rental demand intelligence (news category)
      parallel: multi-factor demand analysis if ADAM_PROVIDER_PARALLEL_V1 flag enabled

    Returns: NeighborhoodDemandBrief artifact.
    """
    logger.info(
        "landlord.neighborhood_demand start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    from aspire_orchestrator.services.adam.feature_flags import (
        ADAM_PROVIDER_PARALLEL_V1,
        is_adam_flag_enabled,
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # Extract ZIP from provider_plan (preferred) or query (fallback heuristic)
    zip_match = re.search(r"\b(\d{5})\b", query)
    geoid: str = (provider_plan or {}).get("geoid", zip_match.group(1) if zip_match else "")

    # Step 1: ATTOM sales trends (requires valid geoid)
    if geoid:
        trends_result = await execute_attom_sales_trends(
            payload={"geoid": geoid, "geo_type": "ZI", "interval": "monthly"},
            **args,
        )
        providers_called.append("attom")
        if trends_result.outcome == Outcome.SUCCESS and trends_result.data:
            records.append({
                "source_type": "sales_trends",
                "geoid": geoid,
                "data": trends_result.data.get("trendStat", trends_result.data),
                "provider": "attom",
            })
            sources.append(_source("attom"))
        else:
            logger.warning(
                "landlord.neighborhood_demand: attom sales_trends failed: %s",
                trends_result.error,
                extra={"correlation_id": context.correlation_id},
            )
    else:
        logger.info(
            "landlord.neighborhood_demand: no ZIP in query — skipping attom sales_trends",
            extra={"correlation_id": context.correlation_id},
        )

    # Step 2: Google Places — rental/housing competitor density
    gp_result = await execute_google_places_search(
        payload={"query": f"rental properties property management {query}"},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        raw_results = (gp_result.data or {}).get("results", [])
        for raw in raw_results[:8]:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))
        records.append({
            "source_type": "competitor_density",
            "rental_listings_nearby": len(raw_results),
            "provider": "google_places",
        })

    # Step 3: Exa — rental demand intelligence
    exa_result = await execute_exa_search(
        payload={
            "query": f"rental demand market analysis {query}",
            "type": "deep-lite",
            "category": "news",
            "num_results": 5,
            "moderation": True,
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        for item in (exa_result.data or {}).get("results", [])[:5]:
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))

    # Step 4: Parallel multi-factor demand analysis (feature-flagged)
    parallel_enabled = await is_adam_flag_enabled(
        ADAM_PROVIDER_PARALLEL_V1, context.tenant_id
    )
    if parallel_enabled:
        try:
            from aspire_orchestrator.providers.parallel_client import execute_parallel_task

            parallel_result = await execute_parallel_task(
                payload={
                    "task": (
                        f"Analyze neighborhood rental demand for: {query}. "
                        "Provide: demand score (1-10), key demand drivers, "
                        "vacancy trend, renter demographic summary."
                    ),
                    "context": {
                        "zip": geoid,
                        "query": query,
                        "records_collected": len(records),
                    },
                },
                **args,
            )
            providers_called.append("parallel")
            if parallel_result.outcome == Outcome.SUCCESS:
                records.append({
                    "source_type": "parallel_demand_analysis",
                    "analysis": parallel_result.data,
                    "provider": "parallel",
                })
                sources.append(_source("parallel"))
        except Exception as exc:
            logger.warning(
                "landlord.neighborhood_demand: parallel task failed: %s",
                exc,
                extra={"correlation_id": context.correlation_id},
            )

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["data"] if geoid else [],
        exa_grounding=exa_grounding or None,
    )

    next_queries: list[str] = []
    if not geoid:
        next_queries.append(f"Provide ZIP code for precise sales trend data: {query}")
    next_queries.extend([
        "Drill into specific ZIP",
        "Compare adjacent neighborhoods",
    ])

    return ResearchResponse(
        artifact_type="NeighborhoodDemandBrief",
        summary=(
            f"Neighborhood demand for '{query}' (ZIP={geoid or 'unknown'}): "
            f"{len(records)} data points. Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "+".join(dict.fromkeys(providers_called))},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="neighborhood_demand",
        playbook="landlord.neighborhood_demand",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 5. Screening Compliance
# ---------------------------------------------------------------------------

async def execute_screening_compliance(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research tenant screening laws and compliance requirements.

    Provider plan:
      exa: official jurisdiction sources (hud.gov, state AG sites, fair housing orgs)
      brave: fallback for broader compliance coverage

    GUARDRAIL: This playbook NEVER produces tenant scoring, accept/reject
    recommendations, or any judgment about an individual applicant.
    It returns compliance-information only — landlord education.

    Returns: ScreeningComplianceBrief artifact.
    """
    logger.info(
        "landlord.screening_compliance start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    COMPLIANCE_DOMAINS = [
        "hud.gov",
        "justice.gov",
        "ftc.gov",
        "consumerfinance.gov",
        "eeoc.gov",
        "ncsl.org",
        "nolo.com",
        "avail.co",
        "apartments.com",
        "landlordology.com",
    ]

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # --- Exa: official compliance sources with domain filtering ---
    exa_result = await execute_exa_search(
        payload={
            "query": f"tenant screening fair housing compliance {query}",
            "type": "auto",
            "category": "news",
            "num_results": 8,
            "include_domains": COMPLIANCE_DOMAINS,
            "moderation": True,
            "contents": {
                "text": {"maxCharacters": 2000},
                "highlights": {"maxCharacters": 1000},
            },
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        for item in (exa_result.data or {}).get("results", [])[:8]:
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))
    else:
        logger.warning(
            "landlord.screening_compliance: exa failed, falling back to brave: %s",
            exa_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # --- Brave fallback — always run if Exa returned fewer than 3 results ---
    if len(records) < 3:
        brave_result = await execute_brave_search(
            payload={
                "query": f"tenant screening rules fair housing {query}",
                "count": 5,
            },
            **args,
        )
        providers_called.append("brave")
        if brave_result.outcome == Outcome.SUCCESS:
            for item in (brave_result.data or {}).get("results", [])[:5]:
                ev = normalize_from_brave(item)
                records.append(ev.to_dict())
            sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["url", "title"],
        exa_grounding=exa_grounding or None,
    )

    # Guardrail note is explicit and always present
    guardrail = (
        "GUARDRAIL: No tenant scoring or accept/reject recommendations produced. "
        "Consult licensed legal counsel for jurisdiction-specific advice."
    )

    return ResearchResponse(
        artifact_type="ScreeningComplianceBrief",
        summary=(
            f"Tenant screening compliance for '{query}': {len(records)} compliance "
            f"sources. Verification: {report.status}. {guardrail}"
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "exa+brave"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=[
            guardrail,
            "Check state-specific screening rules",
            f"Review fair housing requirements: {query}",
        ],
        verification_report=report,
        segment="landlord",
        intent="screening_compliance",
        playbook="landlord.screening_compliance",
        providers_called=list(dict.fromkeys(providers_called)),
    )


# ---------------------------------------------------------------------------
# 6. Turnover Vendor Scout
# ---------------------------------------------------------------------------

async def execute_turnover_vendor_scout(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Discover make-ready vendors for property turnover.

    Provider plan:
      google_places: primary — cleaners, painters, flooring, handyman near property
      foursquare:    corroboration + contact enrichment
      here:          fallback when vendor list is sparse (<5 records)

    Returns: VendorShortlist artifact.
    """
    logger.info(
        "landlord.turnover_vendor_scout start",
        extra={"correlation_id": context.correlation_id, "query": query[:80]},
    )

    VENDOR_CATEGORIES = [
        "cleaning service",
        "house painter",
        "flooring contractor",
        "handyman",
        "property maintenance",
    ]

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []

    # Step 1: Google Places — vendor search per category (parallel)
    async def _search_gp_category(category: str) -> list[dict[str, Any]]:
        result = await execute_google_places_search(
            payload={"query": f"{category} near {query}"},
            **args,
        )
        if result.outcome == Outcome.SUCCESS:
            return (result.data or {}).get("results", [])
        return []

    gp_results_per_cat = await asyncio.gather(
        *[_search_gp_category(cat) for cat in VENDOR_CATEGORIES],
        return_exceptions=False,
    )
    providers_called.append("google_places")

    seen_names: set[str] = set()
    for cat_results in gp_results_per_cat:
        for raw in cat_results[:3]:
            biz = normalize_from_google_places(raw)
            if biz.name and biz.name not in seen_names:
                seen_names.add(biz.name)
                records.append(biz.to_dict())

    if records:
        sources.append(_source("google_places"))

    # Step 2: Foursquare — corroboration + contact enrichment
    fsq_result = await execute_foursquare_search(
        payload={"query": f"home services contractors {query}"},
        **args,
    )
    providers_called.append("foursquare")
    if fsq_result.outcome == Outcome.SUCCESS:
        for raw in (fsq_result.data or {}).get("results", [])[:10]:
            biz = normalize_from_foursquare(raw)
            if biz.name and biz.name not in seen_names:
                seen_names.add(biz.name)
                records.append(biz.to_dict())
        sources.append(_source("foursquare"))
    else:
        logger.warning(
            "landlord.turnover_vendor_scout: foursquare failed: %s",
            fsq_result.error,
            extra={"correlation_id": context.correlation_id},
        )

    # Step 3: HERE fallback — only if vendor list is thin
    if len(records) < 5:
        here_result = await execute_here_search(
            payload={"query": f"home improvement contractors {query}", "limit": 10},
            **args,
        )
        providers_called.append("here")
        if here_result.outcome == Outcome.SUCCESS:
            for raw in (here_result.data or {}).get("items", []):
                biz = normalize_from_here(raw)
                if biz.name and biz.name not in seen_names:
                    seen_names.add(biz.name)
                    records.append(biz.to_dict())
            sources.append(_source("here"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name", "phone"],
    )

    next_queries: list[str] = []
    if len(records) < 3:
        next_queries.append(f"Angi / HomeAdvisor vendors near {query}")
    next_queries.extend([
        "Verify licensing and insurance",
        "Get quotes from top vendors",
    ])

    return ResearchResponse(
        artifact_type="VendorShortlist",
        summary=(
            f"Turnover vendors near '{query}': {len(records)} vendors found. "
            f"Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "+".join(dict.fromkeys(providers_called))},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="landlord",
        intent="turnover_vendor_scout",
        playbook="landlord.turnover_vendor_scout",
        providers_called=list(dict.fromkeys(providers_called)),
    )
