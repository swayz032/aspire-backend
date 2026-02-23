"""Legal Query Analyzer — Deterministic filter extraction for Clara RAG.

Analyzes natural language queries to extract structured filters for
hybrid search. Pure regex/keyword matching — NO LLM calls.

Used by LegalRetrievalService to scope search before embedding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# US State mapping (name + abbreviation → 2-letter code)
# ---------------------------------------------------------------------------

_STATE_MAP: dict[str, str] = {
    # Full names (lowercase)
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
    # Abbreviations (uppercase)
    "AL": "AL", "AK": "AK", "AZ": "AZ", "AR": "AR", "CA": "CA",
    "CO": "CO", "CT": "CT", "DE": "DE", "FL": "FL", "GA": "GA",
    "HI": "HI", "ID": "ID", "IL": "IL", "IN": "IN", "IA": "IA",
    "KS": "KS", "KY": "KY", "LA": "LA", "ME": "ME", "MD": "MD",
    "MA": "MA", "MI": "MI", "MN": "MN", "MS": "MS", "MO": "MO",
    "MT": "MT", "NE": "NE", "NV": "NV", "NH": "NH", "NJ": "NJ",
    "NM": "NM", "NY": "NY", "NC": "NC", "ND": "ND", "OH": "OH",
    "OK": "OK", "OR": "OR", "PA": "PA", "RI": "RI", "SC": "SC",
    "SD": "SD", "TN": "TN", "TX": "TX", "UT": "UT", "VT": "VT",
    "VA": "VA", "WA": "WA", "WV": "WV", "WI": "WI", "WY": "WY",
    "DC": "DC",
}

# Disambiguation: 2-letter codes that collide with common words
_AMBIGUOUS_ABBREVS = {"IN", "OR", "ME", "OK", "HI", "ID"}

# ---------------------------------------------------------------------------
# Domain keyword patterns
# ---------------------------------------------------------------------------

_PANDADOC_KEYWORDS = {
    "pandadoc", "api", "endpoint", "webhook", "merge field", "merge fields",
    "token prefill", "template details", "document session", "rate limit",
    "api-key", "embedded signing",
}

_CONTRACT_LAW_KEYWORDS = {
    "clause", "indemnification", "indemnify", "liability", "force majeure",
    "termination", "confidentiality", "non-compete", "noncompete",
    "ip ownership", "intellectual property", "dispute resolution",
    "governing law", "severability", "entire agreement", "amendment",
    "assignment", "notices", "waiver", "statute of frauds",
    "consideration", "capacity", "offer and acceptance",
    "breach", "damages", "specific performance", "injunctive relief",
    "liquidated damages", "limitation of liability", "warranty",
    "representation", "covenant",
}

_COMPLIANCE_KEYWORDS = {
    "compliance", "gdpr", "ccpa", "red flag", "red flags", "risk assessment",
    "data privacy", "privacy", "regulatory", "attorney escalation",
    "unconscionable", "auto-renewal", "personal guarantee",
    "insurance requirement", "tax obligation",
}

_TEMPLATE_KEYWORDS = {
    "template", "nda", "msa", "sow", "statement of work",
    "lease", "engagement letter", "work order", "change order",
    "subcontractor", "independent contractor", "bookkeeping",
    "payroll authorization", "tax preparation", "financial advisory",
    "eviction", "property management",
}

_BUSINESS_KEYWORDS = {
    "business scenario", "payment terms", "net-30", "net-60", "net-90",
    "milestone", "retainer", "progress billing", "late payment",
    "contractor hiring", "vendor agreement", "partnership",
    "sole proprietor", "llc", "corporation", "signing authority",
}

# Chunk type keyword patterns
_CHUNK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "clause": ["clause", "provision", "covenant"],
    "definition": ["definition", "defined term", "means"],
    "checklist": ["checklist", "requirements list", "steps"],
    "jurisdiction_rule": ["jurisdiction", "state law", "state rule"],
    "api_endpoint": ["endpoint", "api call", "http method"],
    "faq": ["faq", "frequently asked", "common question"],
}

# Methods that benefit from reranking (quality-critical)
_RERANK_METHODS = {"review_contract_terms", "sign_contract", "assess_compliance_risk"}


@dataclass
class QueryFilters:
    """Extracted filters from a natural language query."""

    domain: str | None = None
    template_key: str | None = None
    template_lane: str | None = None
    jurisdiction_state: str | None = None
    chunk_types: list[str] | None = None
    rerank_enabled: bool = False


def analyze_query(
    query: str,
    method_context: str | None = None,
) -> QueryFilters:
    """Extract structured filters from a natural language query.

    Pure deterministic analysis — no LLM calls. Uses keyword matching
    and regex patterns to identify domain, template, jurisdiction, and
    chunk type filters.

    Args:
        query: Natural language search query
        method_context: Clara method name for rerank decisions

    Returns:
        QueryFilters with extracted filter values
    """
    if not query or not query.strip():
        return QueryFilters()

    # Size limit — reject oversized queries to prevent DoS via regex
    _MAX_QUERY_LEN = 10_000
    if len(query) > _MAX_QUERY_LEN:
        return QueryFilters()

    q_lower = query.lower().strip()
    filters = QueryFilters()

    # 1. Jurisdiction detection (state names and abbreviations)
    filters.jurisdiction_state = _extract_jurisdiction(q_lower, query)

    # 2. Template key detection
    filters.template_key = _extract_template_key(q_lower)

    # 3. Domain detection (most specific wins)
    filters.domain = _extract_domain(q_lower)

    # 4. Chunk type detection
    filters.chunk_types = _extract_chunk_types(q_lower)

    # 5. Reranking decision
    if method_context and method_context in _RERANK_METHODS:
        filters.rerank_enabled = True

    return filters


def _extract_jurisdiction(q_lower: str, q_original: str) -> str | None:
    """Extract US state from query. Handles full names and abbreviations."""
    # Check full state names first (more reliable)
    for name, code in _STATE_MAP.items():
        if len(name) > 2 and name in q_lower:
            return code

    # Check 2-letter abbreviations (word boundary matching)
    # Skip ambiguous abbreviations unless preceded by "in " context
    words = q_original.split()
    for word in words:
        clean = word.strip(".,;:!?()\"'")
        if len(clean) == 2 and clean.isupper():
            if clean in _AMBIGUOUS_ABBREVS:
                # Only match if preceded by location context
                idx = words.index(word)
                if idx > 0 and words[idx - 1].lower() in ("in", "from", "for", "state"):
                    return _STATE_MAP.get(clean)
            elif clean in _STATE_MAP:
                return _STATE_MAP[clean]

    return None


def _extract_template_key(q_lower: str) -> str | None:
    """Extract template key from query using Clara's template resolution."""
    try:
        from aspire_orchestrator.skillpacks.clara_legal import (
            _resolve_template_key,
            get_template_spec,
        )
    except ImportError:
        return None

    # Try common template type mentions
    template_patterns = [
        (r"\b(mutual\s+nda|general\s+mutual\s+nda)\b", "general_mutual_nda"),
        (r"\b(unilateral\s+nda)\b", "general_unilateral_nda"),
        (r"\b(msa|master\s+service\s+agreement)\b", "trades_msa_lite"),
        (r"\b(sow|statement\s+of\s+work)\b", "trades_sow"),
        (r"\b(work\s+order)\b", "trades_work_order"),
        (r"\b(change\s+order)\b", "trades_change_order"),
        (r"\b(residential\s+lease)\b", "landlord_residential_lease_base"),
        (r"\b(commercial\s+lease)\b", "landlord_commercial_lease_base"),
        (r"\b(engagement\s+letter)\b", "accounting_engagement_letter"),
        (r"\b(subcontractor\s+agreement)\b", "trades_subcontractor_agreement"),
        (r"\b(independent\s+contractor)\b", "trades_independent_contractor_agreement"),
        (r"\b(eviction\s+notice)\b", "landlord_eviction_notice"),
    ]

    for pattern, key in template_patterns:
        if re.search(pattern, q_lower):
            spec = get_template_spec(key)
            if spec:
                return key

    # Fallback: try _resolve_template_key with extracted words
    for word in q_lower.split():
        if len(word) >= 3:
            resolved = _resolve_template_key(word)
            if get_template_spec(resolved):
                return resolved

    return None


def _extract_domain(q_lower: str) -> str | None:
    """Extract the most relevant knowledge domain from query."""
    scores: dict[str, int] = {
        "pandadoc_api": 0,
        "contract_law": 0,
        "compliance_risk": 0,
        "template_intelligence": 0,
        "business_context": 0,
    }

    for kw in _PANDADOC_KEYWORDS:
        if kw in q_lower:
            scores["pandadoc_api"] += 2

    for kw in _CONTRACT_LAW_KEYWORDS:
        if kw in q_lower:
            scores["contract_law"] += 2

    for kw in _COMPLIANCE_KEYWORDS:
        if kw in q_lower:
            scores["compliance_risk"] += 2

    for kw in _TEMPLATE_KEYWORDS:
        if kw in q_lower:
            scores["template_intelligence"] += 1  # Lower weight — templates overlap with contract_law

    for kw in _BUSINESS_KEYWORDS:
        if kw in q_lower:
            scores["business_context"] += 2

    # Get highest scoring domain
    max_score = max(scores.values())
    if max_score == 0:
        return None

    # Return highest scorer (tie-break: contract_law wins)
    for domain in ["contract_law", "compliance_risk", "pandadoc_api", "template_intelligence", "business_context"]:
        if scores[domain] == max_score:
            return domain

    return None


def _extract_chunk_types(q_lower: str) -> list[str] | None:
    """Extract relevant chunk types from query."""
    types: list[str] = []

    for chunk_type, keywords in _CHUNK_TYPE_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            types.append(chunk_type)

    return types if types else None
