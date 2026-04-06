"""Adam Segment + Intent Classifiers — Route queries to playbooks.

Classification approach: LLM-based (GPT-5-mini) with structured output.
Input: user query + conversation context + user segment (if known).
Output: {segment, intent, entity_type, geo_scope}
Fallback: general_smb + lookup if uncertain → existing Adam 4-mode behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    """Result of segment + intent classification."""

    segment: str = "general_smb"  # trades | accounting_bookkeeping | landlord | travel | general_smb
    intent: str = "lookup"        # lookup | compare | verify | price_check | property_fact | compliance_lookup | hotel_research | prospect_research | territory_scan | monitor
    entity_type: str = "web"      # business | property | product | hotel | web | geo
    geo_scope: str = ""           # address | radius | zip | city | county | state | national
    confidence: float = 0.0
    playbook: str = ""            # Resolved by router after classification


# ---------------------------------------------------------------------------
# Keyword-based fast classifier (deterministic, no LLM cost)
# ---------------------------------------------------------------------------

_SEGMENT_KEYWORDS: dict[str, list[str]] = {
    "trades": [
        "plumb", "hvac", "electric", "roof", "paint", "landscap", "contractor",
        "handyman", "flooring", "tile", "drywall", "mason", "weld", "carpent",
        "general contractor", "gc ", "subcontractor", "sub ", "insulation",
        "renovation", "remodel", "repair", "install", "estimate", "quote",
        "bid", "job site", "permit", "inspection", "material", "tool",
        "condenser", "compressor", "furnace", "water heater", "fixture",
    ],
    "landlord": [
        "landlord", "tenant", "rent", "lease", "evict", "property manage",
        "rental", "vacancy", "turnover", "screening", "fair housing",
        "security deposit", "maintenance request", "section 8", "hud",
        "occupied", "absentee", "owner", "sqft", "square foot",
        "property fact", "property detail", "apn", "parcel",
        "assessed value", "avm", "valuation", "comp", "comparable",
        "school district", "neighborhood", "zip code analysis",
    ],
    "accounting_bookkeeping": [
        "bookkeep", "accountant", "cpa", "tax", "irs", "quarterly",
        "estimated tax", "payroll tax", "1099", "w-2", "schedule c",
        "profit loss", "balance sheet", "accounts receivable", "ar ",
        "collections", "invoice aging", "client verification", "prospect",
        "niche", "benchmark", "industry average", "compliance",
        "recordkeeping", "audit", "reconcil",
    ],
    "travel": [
        "hotel", "flight", "travel", "trip", "business trip", "convention",
        "conference center", "airport", "lodging", "accommodation",
        "parking", "breakfast", "walkab",
    ],
}

_INTENT_KEYWORDS: dict[str, list[str]] = {
    "price_check": [
        "price", "cost", "how much", "pricing", "quote", "estimate cost",
        "compare price", "cheapest", "affordable", "budget", "expensive",
        "deal", "sale", "discount", "on sale",
    ],
    "property_fact": [
        "square foot", "sqft", "lot size", "year built", "beds", "baths",
        "owner", "parcel", "apn", "assessed", "avm", "valuation",
        "permit", "sold for", "last sale", "transaction", "school district",
        "property fact", "property detail", "tell me about",
    ],
    "compare": [
        "compare", "competitor", "versus", "vs", "alternative", "better",
        "which is", "ranking", "top rated", "best",
    ],
    "verify": [
        "verify", "confirm", "check", "validate", "legitimate", "real",
        "license", "insur", "bbb", "complaint",
    ],
    "compliance_lookup": [
        "compliance", "regulation", "law", "rule", "requirement", "deadline",
        "irs", "tax due", "filing", "screening rule", "fair housing",
        "eviction", "notice period",
    ],
    "hotel_research": [
        "hotel", "lodging", "accommodation", "stay", "book a room",
        "business trip hotel", "near convention",
    ],
    "prospect_research": [
        "prospect", "find client", "target", "lead", "pipeline",
        "potential client", "new business",
    ],
    "territory_scan": [
        "territory", "expand", "market", "opportunity", "zip code",
        "neighborhood", "demand", "saturation", "underserved",
    ],
    "lookup": [],  # Default fallback
}

_ENTITY_MAP: dict[str, str] = {
    "price_check": "product",
    "property_fact": "property",
    "hotel_research": "hotel",
    "prospect_research": "business",
    "territory_scan": "property",
    "compare": "business",
    "verify": "business",
    "compliance_lookup": "web",
    "lookup": "web",
}


def classify_fast(query: str, tenant_segment: str | None = None) -> ClassificationResult:
    """Fast keyword-based classification. No LLM call.

    Used as the primary classifier — fast, deterministic, zero cost.
    Falls back to general_smb + lookup when no keywords match.
    """
    q = query.lower()

    # 1. Classify segment
    segment = tenant_segment or "general_smb"
    best_score = 0
    for seg, keywords in _SEGMENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in q)
        if score > best_score:
            best_score = score
            segment = seg

    # 2. Classify intent
    intent = "lookup"
    best_intent_score = 0
    for intent_name, keywords in _INTENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in q)
        if score > best_intent_score:
            best_intent_score = score
            intent = intent_name

    # 3. Determine entity type from intent
    entity_type = _ENTITY_MAP.get(intent, "web")

    # 4. Detect geo scope
    geo_scope = _detect_geo_scope(q)

    confidence = min(1.0, (best_score + best_intent_score) * 0.15) if (best_score + best_intent_score) > 0 else 0.0

    result = ClassificationResult(
        segment=segment,
        intent=intent,
        entity_type=entity_type,
        geo_scope=geo_scope,
        confidence=confidence,
    )

    logger.info(
        "Fast classification: segment=%s intent=%s entity=%s geo=%s confidence=%.2f query='%s'",
        result.segment, result.intent, result.entity_type, result.geo_scope,
        result.confidence, query[:80],
    )

    return result


def _detect_geo_scope(q: str) -> str:
    """Detect geographic scope from query text."""
    import re
    if re.search(r"\b\d{5}\b", q):
        return "zip"
    if any(w in q for w in ["near me", "nearby", "within", "miles", "radius"]):
        return "radius"
    if any(w in q for w in ["county", "parish"]):
        return "county"
    if any(w in q for w in ["state", "statewide"]):
        return "state"
    if any(w in q for w in ["national", "nationwide", "country"]):
        return "national"
    # Check for city-like patterns (capitalized words followed by state abbreviation)
    if re.search(r"in\s+[A-Z][a-z]+", q) or re.search(r"\b[A-Z]{2}\b", q):
        return "city"
    return ""
