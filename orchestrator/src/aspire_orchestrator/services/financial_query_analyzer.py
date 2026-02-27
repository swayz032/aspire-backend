"""Financial Query Analyzer — Deterministic filter extraction for Finn RAG.

Analyzes natural language queries to extract structured filters for
hybrid search. Pure regex/keyword matching — NO LLM calls.

Used by FinancialRetrievalService to scope search before embedding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# US State mapping (shared with legal_query_analyzer)
# ---------------------------------------------------------------------------

_STATE_MAP: dict[str, str] = {
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

_AMBIGUOUS_ABBREVS = {"IN", "OR", "ME", "OK", "HI", "ID"}

# ---------------------------------------------------------------------------
# Domain keyword patterns
# ---------------------------------------------------------------------------

_TAX_KEYWORDS = {
    "tax", "taxes", "taxation", "deduction", "deductions", "write-off",
    "write-offs", "writeoff", "1099", "w-2", "w2", "w-4", "w4",
    "estimated payment", "quarterly tax", "irs", "fica", "futa", "suta",
    "section 179", "bonus depreciation", "qbi", "qualified business income",
    "s-corp", "s corp", "schedule c", "salt", "salt cap",
    "self-employment tax", "se tax", "home office deduction",
    "mileage deduction", "vehicle deduction", "depreciation",
    "capital gains", "capital loss", "tax bracket", "marginal rate",
    "effective rate", "amt", "alternative minimum tax",
    "tax credit", "earned income credit", "child tax credit",
    "estimated quarterly", "tax planning", "tax strategy",
    "entity election", "llc taxation", "pass-through",
}

_ACCOUNTING_KEYWORDS = {
    "gaap", "accrual", "accrual basis", "cash basis", "cash method",
    "chart of accounts", "coa", "journal entry", "journal entries",
    "reconciliation", "bank reconciliation", "depreciation schedule",
    "p&l", "profit and loss", "income statement", "balance sheet",
    "statement of cash flows", "cash flow statement",
    "accounts receivable", "accounts payable", "ar", "ap",
    "revenue recognition", "matching principle", "double-entry",
    "general ledger", "trial balance", "closing entries",
    "fiscal year", "financial statements", "audit",
}

_BOOKKEEPING_KEYWORDS = {
    "categorize", "categorization", "receipt", "receipts",
    "bank reconciliation", "monthly close", "month-end close",
    "year-end close", "transaction", "transactions",
    "expense tracking", "expense report", "petty cash",
    "bank feed", "bank feeds", "unreconciled",
    "duplicate transaction", "split transaction",
    "vendor payment", "bill pay", "accounts payable",
}

_PAYROLL_KEYWORDS = {
    "payroll", "paycheck", "direct deposit", "overtime",
    "benefits", "workers comp", "workers compensation",
    "employee classification", "contractor vs employee",
    "pay period", "pay frequency", "withholding",
    "federal withholding", "state withholding",
    "fica tax", "medicare", "social security",
    "unemployment insurance", "paid time off", "pto",
    "sick leave", "vacation", "bonus", "commission",
    "garnishment", "wage garnishment", "payroll tax",
    "form 941", "form 940", "new hire reporting",
}

_PAYMENT_KEYWORDS = {
    "payment", "payments", "invoice payment", "refund",
    "ach", "ach transfer", "wire transfer", "credit card",
    "credit card fee", "processing fee", "merchant fee",
    "chargeback", "dispute", "pci", "pci compliance",
    "payment processing", "payment gateway", "checkout",
    "subscription billing", "recurring payment",
    "late payment", "net-30", "net-60", "net-90",
}

_PROVIDER_KEYWORDS = {
    "plaid", "plaid link", "stripe", "stripe connect",
    "quickbooks", "qbo", "quickbooks online", "intuit",
    "gusto", "adp", "adp run", "square", "paypal",
    "bank connection", "bank linking", "financial aggregation",
    "api integration", "oauth", "webhook",
}

_PLANNING_KEYWORDS = {
    "budget", "budgeting", "forecast", "forecasting",
    "cash flow", "cash flow forecast", "runway",
    "burn rate", "break-even", "breakeven",
    "financial projection", "projection", "pro forma",
    "scenario analysis", "sensitivity analysis",
    "financial model", "financial modeling",
    "working capital", "debt service", "debt ratio",
}

_COMPLIANCE_KEYWORDS = {
    "compliance", "record retention", "1099 deadline",
    "filing deadline", "sales tax", "sales tax nexus",
    "payroll tax deposit", "quarterly filing",
    "annual filing", "business license", "tax id",
    "ein", "employer identification number",
    "state registration", "regulatory", "audit preparation",
}

# Chunk type keyword patterns
_CHUNK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "rule": ["rule", "regulation", "requirement", "must", "shall"],
    "definition": ["definition", "defined as", "means", "refers to"],
    "example": ["example", "for instance", "scenario", "case study"],
    "strategy": ["strategy", "approach", "optimize", "minimize", "maximize"],
    "checklist": ["checklist", "steps", "procedure", "how to"],
    "faq": ["faq", "frequently asked", "common question"],
    "provider_spec": ["api", "endpoint", "integration", "setup", "configure"],
    "tax_form": ["form", "schedule", "filing", "return"],
    "calculation": ["calculate", "formula", "compute", "rate"],
    "threshold": ["threshold", "limit", "cap", "maximum", "minimum"],
    "deadline": ["deadline", "due date", "filing date", "by when"],
}

# Methods that benefit from reranking (quality-critical)
_RERANK_METHODS = {"search_financial_knowledge", "analyze_financial_health"}


@dataclass
class FinancialQueryFilters:
    """Extracted filters from a natural language financial query."""

    domain: str | None = None
    provider_name: str | None = None
    tax_year: int | None = None
    jurisdiction: str | None = None
    chunk_types: list[str] | None = None
    rerank_enabled: bool = False


def analyze_financial_query(
    query: str,
    method_context: str | None = None,
) -> FinancialQueryFilters:
    """Extract structured filters from a natural language financial query.

    Pure deterministic analysis — no LLM calls. Uses keyword matching
    and regex patterns to identify domain, provider, tax year, jurisdiction,
    and chunk type filters.

    Args:
        query: Natural language search query
        method_context: Finn method name for rerank decisions

    Returns:
        FinancialQueryFilters with extracted filter values
    """
    if not query or not query.strip():
        return FinancialQueryFilters()

    _MAX_QUERY_LEN = 10_000
    if len(query) > _MAX_QUERY_LEN:
        return FinancialQueryFilters()

    q_lower = query.lower().strip()
    filters = FinancialQueryFilters()

    # 1. Jurisdiction detection (state names and abbreviations)
    filters.jurisdiction = _extract_jurisdiction(q_lower, query)

    # 2. Provider name detection
    filters.provider_name = _extract_provider(q_lower)

    # 3. Tax year detection
    filters.tax_year = _extract_tax_year(query)

    # 4. Domain detection (most specific wins)
    filters.domain = _extract_domain(q_lower)

    # 5. Chunk type detection
    filters.chunk_types = _extract_chunk_types(q_lower)

    # 6. Reranking decision
    if method_context and method_context in _RERANK_METHODS:
        filters.rerank_enabled = True

    return filters


def _extract_jurisdiction(q_lower: str, q_original: str) -> str | None:
    """Extract US state or 'federal' from query."""
    if "federal" in q_lower:
        return "federal"

    # Check full state names first (more reliable)
    for name, code in _STATE_MAP.items():
        if len(name) > 2 and name in q_lower:
            return code

    # Check 2-letter abbreviations (word boundary matching)
    words = q_original.split()
    for word in words:
        clean = word.strip(".,;:!?()\"'")
        if len(clean) == 2 and clean.isupper():
            if clean in _AMBIGUOUS_ABBREVS:
                idx = words.index(word)
                if idx > 0 and words[idx - 1].lower() in ("in", "from", "for", "state"):
                    return _STATE_MAP.get(clean)
            elif clean in _STATE_MAP:
                return _STATE_MAP[clean]

    return None


def _extract_provider(q_lower: str) -> str | None:
    """Extract financial provider name from query."""
    provider_patterns = [
        (r"\bplaid\b", "plaid"),
        (r"\bstripe\b", "stripe"),
        (r"\bquickbooks\b|\bqbo\b|\bintuit\b", "quickbooks"),
        (r"\bgusto\b", "gusto"),
        (r"\badp\b", "adp"),
    ]
    for pattern, provider in provider_patterns:
        if re.search(pattern, q_lower):
            return provider
    return None


def _extract_tax_year(q_original: str) -> int | None:
    """Extract tax year from query (4-digit year between 2020-2030)."""
    match = re.search(r"\b(20[2-3]\d)\b", q_original)
    if match:
        return int(match.group(1))
    return None


def _extract_domain(q_lower: str) -> str | None:
    """Extract the most relevant finance knowledge domain from query."""
    scores: dict[str, int] = {
        "tax_strategy": 0,
        "accounting_standards": 0,
        "bookkeeping": 0,
        "payroll_rules": 0,
        "payment_processing": 0,
        "provider_integration": 0,
        "financial_planning": 0,
        "regulatory_compliance": 0,
    }

    for kw in _TAX_KEYWORDS:
        if kw in q_lower:
            scores["tax_strategy"] += 2

    for kw in _ACCOUNTING_KEYWORDS:
        if kw in q_lower:
            scores["accounting_standards"] += 2

    for kw in _BOOKKEEPING_KEYWORDS:
        if kw in q_lower:
            scores["bookkeeping"] += 2

    for kw in _PAYROLL_KEYWORDS:
        if kw in q_lower:
            scores["payroll_rules"] += 2

    for kw in _PAYMENT_KEYWORDS:
        if kw in q_lower:
            scores["payment_processing"] += 2

    for kw in _PROVIDER_KEYWORDS:
        if kw in q_lower:
            scores["provider_integration"] += 2

    for kw in _PLANNING_KEYWORDS:
        if kw in q_lower:
            scores["financial_planning"] += 2

    for kw in _COMPLIANCE_KEYWORDS:
        if kw in q_lower:
            scores["regulatory_compliance"] += 2

    max_score = max(scores.values())
    if max_score == 0:
        return None

    # Priority order for tie-breaking
    priority = [
        "tax_strategy", "payroll_rules", "accounting_standards",
        "payment_processing", "bookkeeping", "provider_integration",
        "financial_planning", "regulatory_compliance",
    ]
    for domain in priority:
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
