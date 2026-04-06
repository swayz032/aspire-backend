"""Adam Research Playbooks — Accounting & Bookkeeping Segment.

Six playbook execute functions for the accounting segment:
  1. prospect_research     — find prospective clients by niche + geography
  2. client_verification   — verify an existing/prospective client's identity
  3. tax_and_compliance    — IRS / compliance source research
  4. local_niche_scan      — business density map for a niche + geography
  5. industry_benchmark    — financial benchmarks for an industry
  6. ar_collections_intel  — AR / collections intelligence for a debtor business

Provider routing (per ecosystem providers.yaml):
  Places:  google_places (primary) → foursquare (fallback)
  Web:     exa (primary) → brave (fallback)

Confidence guardrails:
  - Compliance data without an official source (irs.gov, state.gov, etc.)
    is capped at "partially_verified" regardless of source count.
  - Adapters never retry. Orchestrator retries on PROVIDER_TIMEOUT / RATE_LIMITED.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.brave_client import execute_brave_search
from aspire_orchestrator.providers.exa_client import execute_exa_search
from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
from aspire_orchestrator.providers.google_places_client import execute_google_places_search
from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
    normalize_from_foursquare,
    normalize_from_google_places,
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


# ---------------------------------------------------------------------------
# 1. Prospect Research
# ---------------------------------------------------------------------------

async def execute_prospect_research(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Find prospective accounting clients by niche + geography.

    Provider plan:
      google_places: business text search → primary business identity
      exa: category=company + outputSchema → enriched structured prospect data

    Returns: ProspectList artifact.
    """
    logger.info(
        "accounting.prospect_research start",
        extra={"correlation_id": context.correlation_id, "query": query},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # --- Google Places: business identity ---
    gp_result = await execute_google_places_search(
        payload={"query": query},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        for raw in (gp_result.data or {}).get("results", []):
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))
    else:
        logger.warning(
            "accounting.prospect_research: google_places failed",
            extra={"error": gp_result.error, "correlation_id": context.correlation_id},
        )

    # --- Exa: enriched company data with outputSchema ---
    prospect_schema = {
        "type": "object",
        "properties": {
            "company_name": {"type": "string"},
            "industry_niche": {"type": "string"},
            "estimated_revenue": {"type": "string"},
            "employee_count": {"type": "string"},
            "website": {"type": "string"},
            "key_contact": {"type": "string"},
        },
    }
    exa_result = await execute_exa_search(
        payload={
            "query": query,
            "category": "company",
            "num_results": 10,
            "output_schema": prospect_schema,
            "moderation": True,
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        exa_data = exa_result.data or {}
        for item in exa_data.get("results", []):
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))
    else:
        logger.warning(
            "accounting.prospect_research: exa failed",
            extra={"error": exa_result.error, "correlation_id": context.correlation_id},
        )

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name", "normalized_address"],
        exa_grounding=exa_grounding or None,
    )

    missing = [f for f in report.missing_fields]
    next_queries = []
    if not records:
        next_queries.append(f"Alternative niche search: {query} small business")

    return ResearchResponse(
        artifact_type="ProspectList",
        summary=(
            f"Found {len(records)} prospects for query '{query}'. "
            f"Verification: {report.status} (score={report.confidence_score})."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "google_places+exa"},
        confidence=_confidence_dict(report),
        missing_fields=missing,
        next_queries=next_queries,
        verification_report=report,
        segment="accounting_bookkeeping",
        intent="prospect_research",
        playbook="PROSPECT_RESEARCH",
        providers_called=providers_called,
    )


# ---------------------------------------------------------------------------
# 2. Client Verification
# ---------------------------------------------------------------------------

async def execute_client_verification(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Verify an existing or prospective client's business identity.

    Provider plan:
      google_places: primary business identity
      foursquare: corroboration of identity (phone, website cross-check)
      exa/brave: web evidence enrichment
      attom (optional): if property context detected in query

    required_fields: name, normalized_address
    Returns: ClientVerificationPack artifact.
    """
    logger.info(
        "accounting.client_verification start",
        extra={"correlation_id": context.correlation_id, "query": query},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # --- Google Places: primary identity ---
    gp_result = await execute_google_places_search(
        payload={"query": query},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        for raw in (gp_result.data or {}).get("results", [])[:5]:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))
    else:
        logger.warning(
            "accounting.client_verification: google_places failed",
            extra={"error": gp_result.error, "correlation_id": context.correlation_id},
        )

    # --- Foursquare: corroboration ---
    fsq_result = await execute_foursquare_search(
        payload={"query": query},
        **args,
    )
    providers_called.append("foursquare")
    if fsq_result.outcome == Outcome.SUCCESS:
        for raw in (fsq_result.data or {}).get("results", [])[:5]:
            biz = normalize_from_foursquare(raw)
            records.append(biz.to_dict())
        sources.append(_source("foursquare"))
    else:
        logger.warning(
            "accounting.client_verification: foursquare failed",
            extra={"error": fsq_result.error, "correlation_id": context.correlation_id},
        )

    # --- Exa: web evidence enrichment ---
    exa_result = await execute_exa_search(
        payload={
            "query": f"{query} business profile",
            "category": "company",
            "num_results": 5,
            "moderation": True,
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
        # Brave fallback for web evidence
        brave_result = await execute_brave_search(
            payload={"query": f"{query} business verification"},
            **args,
        )
        providers_called.append("brave")
        if brave_result.outcome == Outcome.SUCCESS:
            for item in (brave_result.data or {}).get("results", []):
                ev = normalize_from_brave(item)
                records.append(ev.to_dict())
            sources.append(_source("brave"))

    # --- ATTOM: add property data if property context detected ---
    property_keywords = ("property", "address", "building", "office", "suite", "real estate")
    if any(kw in query.lower() for kw in property_keywords):
        try:
            from aspire_orchestrator.providers.attom_client import (
                execute_attom_property_detail,
            )
            from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
                normalize_from_attom_detail,
            )

            attom_result = await execute_attom_property_detail(
                payload={"address": query},
                **args,
            )
            providers_called.append("attom")
            if attom_result.outcome == Outcome.SUCCESS:
                prop = normalize_from_attom_detail(attom_result.data or {})
                records.append(prop.to_dict())
                sources.append(_source("attom"))
        except Exception as exc:
            logger.warning(
                "accounting.client_verification: attom optional call failed",
                extra={"error": str(exc), "correlation_id": context.correlation_id},
            )

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name", "normalized_address"],
        exa_grounding=exa_grounding or None,
    )

    return ResearchResponse(
        artifact_type="ClientVerificationPack",
        summary=(
            f"Client verification for '{query}': {len(records)} records from "
            f"{len(sources)} sources. Status: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "+".join(providers_called)},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=[f"Secretary of state business search: {query}"] if report.status != "verified" else [],
        verification_report=report,
        segment="accounting_bookkeeping",
        intent="client_verification",
        playbook="CLIENT_VERIFICATION",
        providers_called=providers_called,
    )


# ---------------------------------------------------------------------------
# 3. Tax and Compliance
# ---------------------------------------------------------------------------

async def execute_tax_and_compliance(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research tax regulations and compliance requirements.

    Provider plan:
      exa: includeDomains=[irs.gov, ...] + category=news + moderation=true (primary)
      brave: broader compliance source fallback

    GUARDRAIL: Compliance data without an official source domain is capped at
    "partially_verified" regardless of multi-source agreement. The verifier
    enforces this — never manufacture official citations.

    Returns: ComplianceBrief artifact.
    """
    logger.info(
        "accounting.tax_and_compliance start",
        extra={"correlation_id": context.correlation_id, "query": query},
    )

    OFFICIAL_DOMAINS = [
        "irs.gov",
        "treasury.gov",
        "dol.gov",
        "sba.gov",
        "ftc.gov",
        "sec.gov",
        "fasb.org",
        "aicpa.org",
        "gaap.fasb.org",
    ]

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []
    has_official_source = False

    # --- Exa: official domain search ---
    exa_result = await execute_exa_search(
        payload={
            "query": query,
            "category": "news",
            "include_domains": OFFICIAL_DOMAINS,
            "num_results": 10,
            "moderation": True,
            "contents": {"text": True, "highlights": True},
        },
        **args,
    )
    providers_called.append("exa")
    if exa_result.outcome == Outcome.SUCCESS:
        exa_results = (exa_result.data or {}).get("results", [])
        if exa_results:
            has_official_source = True
        for item in exa_results:
            ev = normalize_from_exa(item, grounding=item.get("grounding"))
            records.append(ev.to_dict())
            if item.get("grounding"):
                exa_grounding.append(item["grounding"])
        sources.append(_source("exa"))
    else:
        logger.warning(
            "accounting.tax_and_compliance: exa failed, falling back to brave",
            extra={"error": exa_result.error, "correlation_id": context.correlation_id},
        )

    # --- Brave: broader compliance fallback ---
    brave_result = await execute_brave_search(
        payload={"query": f"{query} tax compliance regulation"},
        **args,
    )
    providers_called.append("brave")
    if brave_result.outcome == Outcome.SUCCESS:
        for item in (brave_result.data or {}).get("results", []):
            ev = normalize_from_brave(item)
            # Check if any Brave result is from an official domain
            if any(d in ev.domain for d in OFFICIAL_DOMAINS):
                has_official_source = True
            records.append(ev.to_dict())
        sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["url", "title"],
        exa_grounding=exa_grounding or None,
    )

    # Guardrail: cap at partially_verified if no official source
    if not has_official_source and report.status == "verified":
        report.status = "partially_verified"
        report.recommendations.append(
            "No official government/regulatory source found — confidence capped at partially_verified"
        )

    next_queries: list[str] = []
    if not has_official_source:
        next_queries.append(f"site:irs.gov {query}")
        next_queries.append(f"site:treasury.gov {query}")

    return ResearchResponse(
        artifact_type="ComplianceBrief",
        summary=(
            f"Tax/compliance research for '{query}': {len(records)} sources found. "
            f"Official source present: {has_official_source}. Status: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "exa+brave"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="accounting_bookkeeping",
        intent="tax_and_compliance",
        playbook="TAX_AND_COMPLIANCE_LOOKUP",
        providers_called=providers_called,
    )


# ---------------------------------------------------------------------------
# 4. Local Niche Scan
# ---------------------------------------------------------------------------

async def execute_local_niche_scan(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Map business density for a niche + geography.

    Provider plan:
      google_places: category + geography search → business density
      exa: market context enrichment for the niche

    Returns: NicheScanReport artifact.
    """
    logger.info(
        "accounting.local_niche_scan start",
        extra={"correlation_id": context.correlation_id, "query": query},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # --- Google Places: business density by category + geography ---
    gp_result = await execute_google_places_search(
        payload={"query": query},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        raw_results = (gp_result.data or {}).get("results", [])
        for raw in raw_results:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))
    else:
        logger.warning(
            "accounting.local_niche_scan: google_places failed",
            extra={"error": gp_result.error, "correlation_id": context.correlation_id},
        )

    # --- Exa: market context for the niche ---
    exa_result = await execute_exa_search(
        payload={
            "query": f"{query} market size trends industry",
            "category": "company",
            "num_results": 5,
            "moderation": True,
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

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name"],
        exa_grounding=exa_grounding or None,
    )

    business_count = sum(1 for r in records if r.get("name"))
    next_queries = []
    if business_count < 3:
        next_queries.append(f"Broader niche search: {query} nearby businesses")

    return ResearchResponse(
        artifact_type="NicheScanReport",
        summary=(
            f"Niche scan for '{query}': {business_count} businesses found. "
            f"Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "google_places+exa"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="accounting_bookkeeping",
        intent="local_niche_scan",
        playbook="LOCAL_NICHE_SCAN",
        providers_called=providers_called,
    )


# ---------------------------------------------------------------------------
# 5. Industry Benchmark
# ---------------------------------------------------------------------------

async def execute_industry_benchmark(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research financial benchmarks for an industry.

    Provider plan:
      exa: category=financial_report + outputSchema → structured benchmark data
      brave: broader benchmark fallback

    Returns: BenchmarkPack artifact.
    """
    logger.info(
        "accounting.industry_benchmark start",
        extra={"correlation_id": context.correlation_id, "query": query},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    benchmark_schema = {
        "type": "object",
        "properties": {
            "industry": {"type": "string"},
            "metric_name": {"type": "string"},
            "metric_value": {"type": "string"},
            "year": {"type": "string"},
            "source_name": {"type": "string"},
            "percentile_context": {"type": "string"},
        },
    }

    # --- Exa: deep financial report search with structured output ---
    exa_result = await execute_exa_search(
        payload={
            "query": f"{query} industry benchmark financial metrics",
            "type": "deep",
            "category": "financial_report",
            "num_results": 10,
            "output_schema": benchmark_schema,
            "moderation": True,
            "contents": {"text": True, "summary": {"query": f"{query} financial benchmarks"}},
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
        logger.warning(
            "accounting.industry_benchmark: exa failed, falling back to brave",
            extra={"error": exa_result.error, "correlation_id": context.correlation_id},
        )

    # --- Brave: broader benchmark fallback ---
    brave_result = await execute_brave_search(
        payload={"query": f"{query} industry financial benchmarks profit margins"},
        **args,
    )
    providers_called.append("brave")
    if brave_result.outcome == Outcome.SUCCESS:
        for item in (brave_result.data or {}).get("results", []):
            ev = normalize_from_brave(item)
            records.append(ev.to_dict())
        sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["title", "url"],
        exa_grounding=exa_grounding or None,
    )

    next_queries = []
    if not records:
        next_queries.append(f"{query} SIC code financial ratios")
        next_queries.append(f"{query} industry profit margin survey")

    return ResearchResponse(
        artifact_type="BenchmarkPack",
        summary=(
            f"Industry benchmarks for '{query}': {len(records)} sources. "
            f"Verification: {report.status} (score={report.confidence_score})."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "exa+brave"},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="accounting_bookkeeping",
        intent="industry_benchmark",
        playbook="INDUSTRY_BENCHMARK_PACK",
        providers_called=providers_called,
    )


# ---------------------------------------------------------------------------
# 6. AR Collections Intel
# ---------------------------------------------------------------------------

async def execute_ar_collections_intel(
    query: str,
    context: PlaybookContext,
    provider_plan: dict[str, Any] | None = None,
) -> ResearchResponse:
    """Research AR / collections intelligence for a debtor business.

    Provider plan:
      google_places: business identity + operating status
      exa/brave: collection-relevant signals (closures, liens, disputes)

    Returns: CollectionsIntelPack artifact.
    """
    logger.info(
        "accounting.ar_collections_intel start",
        extra={"correlation_id": context.correlation_id, "query": query},
    )

    args = _provider_args(context)
    records: list[dict[str, Any]] = []
    sources: list[SourceAttribution] = []
    providers_called: list[str] = []
    exa_grounding: list[dict[str, Any]] = []

    # --- Google Places: business identity + operating status ---
    gp_result = await execute_google_places_search(
        payload={"query": query},
        **args,
    )
    providers_called.append("google_places")
    if gp_result.outcome == Outcome.SUCCESS:
        for raw in (gp_result.data or {}).get("results", [])[:3]:
            biz = normalize_from_google_places(raw)
            records.append(biz.to_dict())
        sources.append(_source("google_places"))
    else:
        logger.warning(
            "accounting.ar_collections_intel: google_places failed",
            extra={"error": gp_result.error, "correlation_id": context.correlation_id},
        )

    # --- Exa: collection-relevant signals ---
    exa_result = await execute_exa_search(
        payload={
            "query": f"{query} business closure lien lawsuit dispute financial trouble",
            "category": "news",
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
            payload={"query": f"{query} business financial distress collections"},
            **args,
        )
        providers_called.append("brave")
        if brave_result.outcome == Outcome.SUCCESS:
            for item in (brave_result.data or {}).get("results", []):
                ev = normalize_from_brave(item)
                records.append(ev.to_dict())
            sources.append(_source("brave"))

    report = verify_records(
        records=records,
        sources=sources,
        required_fields=["name"],
        exa_grounding=exa_grounding or None,
    )

    next_queries = []
    if report.status != "verified":
        next_queries.append(f"{query} secretary of state status")
        next_queries.append(f"{query} court records judgment")

    return ResearchResponse(
        artifact_type="CollectionsIntelPack",
        summary=(
            f"Collections intel for '{query}': {len(records)} signals from "
            f"{len(sources)} sources. Verification: {report.status}."
        ),
        records=records,
        sources=sources,
        freshness={"mode": "live", "provider": "+".join(providers_called)},
        confidence=_confidence_dict(report),
        missing_fields=list(report.missing_fields),
        next_queries=next_queries,
        verification_report=report,
        segment="accounting_bookkeeping",
        intent="ar_collections_intel",
        playbook="AR_COLLECTIONS_INTEL",
        providers_called=providers_called,
    )
