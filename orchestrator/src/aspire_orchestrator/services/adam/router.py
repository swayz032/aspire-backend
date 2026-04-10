"""Adam Master Router — Segment + intent classification → playbook selection.

Algorithm:
  1. Classify segment + intent via fast keyword classifier
  2. Select playbook from segment×intent matrix
  3. Generate provider plan using registry metadata
  4. Execute playbook (providers → normalize → verify → respond)

Fallback: If no playbook matches or classification confidence is low,
fall back to existing Adam 4-mode behavior (search_web, search_places, etc.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.classifiers import (
    ClassificationResult,
    classify_fast,
)
from aspire_orchestrator.services.adam.schemas.provider_metadata import ProviderMetadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Playbook definitions
# ---------------------------------------------------------------------------

@dataclass
class PlaybookSpec:
    """Specification for a single playbook."""

    name: str
    segment: str
    intents: tuple[str, ...]
    entity_types: tuple[str, ...]
    artifact_type: str
    description: str
    provider_order: tuple[str, ...] = ()  # Preferred provider IDs in order
    required_fields: tuple[str, ...] = ()  # Fields needed for "verified" status
    max_provider_calls: int = 4
    confidence_threshold: float = 0.70  # Stop early when reached


# TRADES playbooks (6)
TRADES_PROPERTY_FACTS_AND_PERMITS = PlaybookSpec(
    name="PROPERTY_FACTS_AND_PERMITS",
    segment="trades",
    intents=("property_fact", "verify"),
    entity_types=("property",),
    artifact_type="PropertyFactPack",
    description="Resolve property context for quoting/outreach",
    provider_order=("attom",),
    required_fields=("normalized_address", "living_sqft", "year_built"),
)

TRADES_ESTIMATE_RESEARCH = PlaybookSpec(
    name="ESTIMATE_RESEARCH",
    segment="trades",
    intents=("price_check", "property_fact"),
    entity_types=("property", "product"),
    artifact_type="EstimateResearchPack",
    description="Support quoting with property facts + material pricing",
    provider_order=("attom", "serpapi_home_depot"),
    required_fields=("normalized_address", "living_sqft"),
    max_provider_calls=5,
)

TRADES_TOOL_MATERIAL_PRICE_CHECK = PlaybookSpec(
    name="TOOL_MATERIAL_PRICE_CHECK",
    segment="trades",
    intents=("price_check",),
    entity_types=("product",),
    artifact_type="PriceComparison",
    description="Find current pricing for tools, materials, fixtures",
    provider_order=("serpapi_shopping", "serpapi_home_depot", "brave"),
    required_fields=("product_name", "price", "retailer"),
)

TRADES_COMPETITOR_PRICING_SCAN = PlaybookSpec(
    name="COMPETITOR_PRICING_SCAN",
    segment="trades",
    intents=("compare",),
    entity_types=("business",),
    artifact_type="CompetitorBrief",
    description="Map local competitors and pricing signals",
    provider_order=("google_places", "foursquare", "exa", "brave"),
    required_fields=("name", "normalized_address"),
)

TRADES_SUBCONTRACTOR_SCOUT = PlaybookSpec(
    name="SUBCONTRACTOR_SCOUT",
    segment="trades",
    intents=("lookup", "verify"),
    entity_types=("business",),
    artifact_type="VendorShortlist",
    description="Find nearby subcontractors by trade and geography",
    provider_order=("google_places", "foursquare", "here", "brave"),
    required_fields=("name", "normalized_address", "phone"),
)

TRADES_TERRITORY_OPPORTUNITY_SCAN = PlaybookSpec(
    name="TERRITORY_OPPORTUNITY_SCAN",
    segment="trades",
    intents=("territory_scan", "compare"),
    entity_types=("property", "business"),
    artifact_type="TerritoryAnalysis",
    description="Identify promising ZIPs/neighborhoods by density + activity",
    provider_order=("attom", "google_places", "parallel", "exa"),
    required_fields=(),
    max_provider_calls=4,
)

# ACCOUNTING/BOOKKEEPING playbooks (6)
ACCT_PROSPECT_RESEARCH = PlaybookSpec(
    name="PROSPECT_RESEARCH",
    segment="accounting_bookkeeping",
    intents=("prospect_research", "lookup"),
    entity_types=("business",),
    artifact_type="ProspectList",
    description="Find target clients by niche, size, pain indicators",
    provider_order=("google_places", "foursquare", "exa", "brave"),
    required_fields=("name", "normalized_address", "category"),
)

ACCT_CLIENT_VERIFICATION = PlaybookSpec(
    name="CLIENT_VERIFICATION",
    segment="accounting_bookkeeping",
    intents=("verify",),
    entity_types=("business",),
    artifact_type="ClientVerificationPack",
    description="Verify client business basics and operating context",
    provider_order=("google_places", "foursquare", "exa", "attom"),
    required_fields=("name", "normalized_address"),
)

ACCT_TAX_AND_COMPLIANCE = PlaybookSpec(
    name="TAX_AND_COMPLIANCE_LOOKUP",
    segment="accounting_bookkeeping",
    intents=("compliance_lookup",),
    entity_types=("web",),
    artifact_type="ComplianceBrief",
    description="Return cited research on deadlines, recordkeeping, compliance",
    provider_order=("exa", "brave", "tavily"),
    required_fields=(),
)

ACCT_LOCAL_NICHE_SCAN = PlaybookSpec(
    name="LOCAL_NICHE_SCAN",
    segment="accounting_bookkeeping",
    intents=("lookup", "compare"),
    entity_types=("business",),
    artifact_type="NicheScanReport",
    description="Find vertical niches in geography worth targeting",
    provider_order=("google_places", "foursquare", "exa"),
    required_fields=(),
)

ACCT_INDUSTRY_BENCHMARK = PlaybookSpec(
    name="INDUSTRY_BENCHMARK_PACK",
    segment="accounting_bookkeeping",
    intents=("compare", "lookup"),
    entity_types=("web",),
    artifact_type="BenchmarkPack",
    description="Provide market/industry context for advisory conversations",
    provider_order=("exa", "brave", "attom"),
    required_fields=(),
)

ACCT_AR_COLLECTIONS_INTEL = PlaybookSpec(
    name="AR_COLLECTIONS_INTEL",
    segment="accounting_bookkeeping",
    intents=("verify", "lookup"),
    entity_types=("business", "web"),
    artifact_type="CollectionsIntelPack",
    description="Research debtor context and collection-relevant signals",
    provider_order=("google_places", "exa", "brave"),
    required_fields=("name",),
)

# LANDLORD playbooks (6)
LANDLORD_PROPERTY_FACTS = PlaybookSpec(
    name="PROPERTY_FACTS",
    segment="landlord",
    intents=("property_fact",),
    entity_types=("property",),
    artifact_type="LandlordPropertyPack",
    description="Verified property profile for investment/operations/due diligence",
    provider_order=("here", "attom"),
    required_fields=("normalized_address", "living_sqft", "year_built", "owner_name"),
)

LANDLORD_RENT_COMP_CONTEXT = PlaybookSpec(
    name="RENT_COMP_CONTEXT",
    segment="landlord",
    intents=("property_fact", "compare"),
    entity_types=("property",),
    artifact_type="RentCompPack",
    description="Property and market context for evaluating achievable rent",
    provider_order=("here", "attom", "google_places", "exa"),
    required_fields=("normalized_address", "estimated_rent"),
)

LANDLORD_PERMIT_AND_RENOVATION = PlaybookSpec(
    name="PERMIT_AND_RENOVATION_CONTEXT",
    segment="landlord",
    intents=("property_fact", "lookup"),
    entity_types=("property",),
    artifact_type="PermitContextPack",
    description="Research renovation/permit context around property or area",
    provider_order=("here", "attom", "brave"),
    required_fields=("normalized_address",),
)

LANDLORD_NEIGHBORHOOD_DEMAND = PlaybookSpec(
    name="NEIGHBORHOOD_DEMAND_SCAN",
    segment="landlord",
    intents=("territory_scan", "compare"),
    entity_types=("property",),
    artifact_type="NeighborhoodDemandBrief",
    description="Summarize area context relevant to demand and property viability",
    provider_order=("attom", "google_places", "parallel", "exa"),
    required_fields=(),
)

LANDLORD_SCREENING_COMPLIANCE = PlaybookSpec(
    name="SCREENING_COMPLIANCE_LOOKUP",
    segment="landlord",
    intents=("compliance_lookup",),
    entity_types=("web",),
    artifact_type="ScreeningComplianceBrief",
    description="Cited research on tenant-screening and fair-housing rules",
    provider_order=("exa", "brave", "tavily"),
    required_fields=(),
)

LANDLORD_TURNOVER_VENDOR_SCOUT = PlaybookSpec(
    name="TURNOVER_VENDOR_SCOUT",
    segment="landlord",
    intents=("lookup",),
    entity_types=("business",),
    artifact_type="VendorShortlist",
    description="Find make-ready and maintenance vendors fast",
    provider_order=("google_places", "foursquare", "here", "brave"),
    required_fields=("name", "normalized_address", "phone"),
)

LANDLORD_INVESTMENT_OPPORTUNITY_SCAN = PlaybookSpec(
    name="INVESTMENT_OPPORTUNITY_SCAN",
    segment="landlord",
    intents=("investment_scan", "territory_scan"),
    entity_types=("property",),
    artifact_type="InvestmentOpportunityPack",
    description="Scan ZIP for investment: foreclosures, absentee owners, equity spreads",
    provider_order=("attom",),
    required_fields=("zip_code",),
)

# TRAVEL playbook (1)
TRAVEL_HOTEL_RESEARCH = PlaybookSpec(
    name="BUSINESS_TRIP_HOTEL_RESEARCH",
    segment="travel",
    intents=("hotel_research",),
    entity_types=("hotel",),
    artifact_type="HotelShortlist",
    description="Find strong hotel options for business trips",
    provider_order=("tripadvisor", "here", "google_places", "exa"),
    required_fields=("name", "normalized_address", "traveler_rating"),
)

# ---------------------------------------------------------------------------
# All playbooks registry
# ---------------------------------------------------------------------------

ALL_PLAYBOOKS: list[PlaybookSpec] = [
    # Trades
    TRADES_PROPERTY_FACTS_AND_PERMITS,
    TRADES_ESTIMATE_RESEARCH,
    TRADES_TOOL_MATERIAL_PRICE_CHECK,
    TRADES_COMPETITOR_PRICING_SCAN,
    TRADES_SUBCONTRACTOR_SCOUT,
    TRADES_TERRITORY_OPPORTUNITY_SCAN,
    # Accounting
    ACCT_PROSPECT_RESEARCH,
    ACCT_CLIENT_VERIFICATION,
    ACCT_TAX_AND_COMPLIANCE,
    ACCT_LOCAL_NICHE_SCAN,
    ACCT_INDUSTRY_BENCHMARK,
    ACCT_AR_COLLECTIONS_INTEL,
    # Landlord
    LANDLORD_PROPERTY_FACTS,
    LANDLORD_RENT_COMP_CONTEXT,
    LANDLORD_PERMIT_AND_RENOVATION,
    LANDLORD_NEIGHBORHOOD_DEMAND,
    LANDLORD_SCREENING_COMPLIANCE,
    LANDLORD_TURNOVER_VENDOR_SCOUT,
    LANDLORD_INVESTMENT_OPPORTUNITY_SCAN,
    # Travel
    TRAVEL_HOTEL_RESEARCH,
]

_PLAYBOOK_INDEX: dict[str, PlaybookSpec] = {p.name: p for p in ALL_PLAYBOOKS}


# ---------------------------------------------------------------------------
# Router API
# ---------------------------------------------------------------------------

def get_playbook(name: str) -> PlaybookSpec | None:
    """Get a playbook by name."""
    return _PLAYBOOK_INDEX.get(name)


def route_to_playbook(
    query: str,
    tenant_segment: str | None = None,
) -> tuple[ClassificationResult, PlaybookSpec | None]:
    """Route a research query to the best matching playbook.

    Returns:
        (classification, playbook) — playbook is None if no match (use fallback).
    """
    classification = classify_fast(query, tenant_segment)

    # Detect hybrid queries: address + pricing co-occur → ESTIMATE_RESEARCH
    # BUT: store-specific queries ("home depot", "lowes") are product lookups, not estimates
    import re as _re
    q_lower = query.lower()
    has_address_signal = bool(_re.search(
        r"\b\d+\s+\w+\s+(st|ave|rd|blvd|dr|ln|ct|way|pl)\b"  # "123 Main St" pattern
        r"|\b\d{5}\b"                                          # ZIP code
        r"|address\b",
        q_lower,
    ))
    has_price_signal = classification.intent == "price_check" or any(
        token in q_lower for token in ("price", "cost", "estimate", "quote", "how much", "pricing")
    )
    has_store_signal = any(s in q_lower for s in [
        "home depot", "lowes", "lowe's", "menards", "ace hardware",
        "in stock", "available at", "pickup",
    ])
    # Estimate hybrid = address + price but NOT a store product lookup
    is_estimate_hybrid = has_address_signal and has_price_signal and not has_store_signal

    # Find best matching playbook for this classification
    best: PlaybookSpec | None = None
    best_score = 0

    for playbook in ALL_PLAYBOOKS:
        score = 0

        # Segment must match
        if playbook.segment != classification.segment:
            continue

        # Intent match (primary signal)
        if classification.intent in playbook.intents:
            score += 10

        # Entity type match
        if classification.entity_type in playbook.entity_types:
            score += 5

        # Prefer more specific playbooks (fewer intents = more specific)
        score += max(0, 5 - len(playbook.intents))

        # Hybrid boost: address + pricing → favor multi-entity playbooks like ESTIMATE_RESEARCH
        if is_estimate_hybrid and len(playbook.entity_types) > 1:
            score += 8

        if score > best_score:
            best_score = score
            best = playbook

    # Guardrail: addressed property-fact queries must never fall through to legacy.
    # If segment matching produced no candidate, force landlord PROPERTY_FACTS.
    if best is None and has_address_signal and classification.intent == "property_fact":
        best = LANDLORD_PROPERTY_FACTS
        best_score = 1
        classification.segment = LANDLORD_PROPERTY_FACTS.segment
        logger.info(
            "Routing guardrail applied: addressed property_fact forced to %s",
            best.name,
        )

    if best:
        classification.playbook = best.name
        logger.info(
            "Routed to playbook %s (score=%d) for segment=%s intent=%s",
            best.name, best_score, classification.segment, classification.intent,
        )
    else:
        logger.info(
            "No playbook match for segment=%s intent=%s — falling back to legacy Adam",
            classification.segment, classification.intent,
        )

    return classification, best
