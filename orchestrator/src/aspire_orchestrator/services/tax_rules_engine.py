"""Tax Strategy Rules Engine — Versioned rules registry (proposal-only).

Per Finn v2 spec: Provides deduction heatmaps, red flag radar,
and substantiation gap analysis based on jurisdiction-specific tax rules.

This engine NEVER files taxes. It provides CPA-grade process rigor:
  - Eligibility rules backed by source references
  - Substantiation requirements
  - Risk ratings per deduction category
  - Common failure modes

Fail-closed (Law #3): Missing jurisdiction/year → error, not guesswork.
No side effects (Law #7): Read-only analysis, produces proposals only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_TAX_RULES_DIR = Path(__file__).parent.parent / "config" / "tax_rules"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class TaxProfile:
    """Tenant's tax profile for analysis.

    If any required field is None/empty, the engine produces a
    Profile Completion Proposal instead of substantive advice.
    """

    jurisdiction: str  # e.g., "US"
    entity_type: str  # sole_prop, s_corp, c_corp, partnership, llc
    accounting_method: str  # cash, accrual
    tax_year: int
    payroll_posture: str | None = None  # w2_owner, contractors, both
    home_office_intent: str | None = None  # yes, no, unknown
    vehicle_method: str | None = None  # mileage, actuals, unknown

    @property
    def is_complete(self) -> bool:
        """Check if profile has minimum required fields."""
        return bool(
            self.jurisdiction
            and self.entity_type
            and self.accounting_method
            and self.tax_year
        )


@dataclass(frozen=True)
class TaxRule:
    """A single tax rule from the registry."""

    rule_id: str
    title: str
    eligibility_facts_required: list[str]
    substantiation_required: list[str]
    common_failure_modes: list[str]
    risk_default: str  # low, medium, high
    source_refs: list[str]


@dataclass(frozen=True)
class DeductionCandidate:
    """A candidate deduction opportunity from the heatmap."""

    rule_id: str
    title: str
    risk_level: str
    relevance: str  # high, medium, low
    reason: str
    substantiation_needed: list[str]


@dataclass(frozen=True)
class RedFlag:
    """An audit risk indicator from the red flag radar."""

    rule_id: str
    flag: str
    severity: str  # high, medium, low
    recommendation: str


@dataclass(frozen=True)
class SubstantiationGap:
    """A missing piece of evidence for a specific rule."""

    rule_id: str
    title: str
    missing_items: list[str]
    impact: str  # "May invalidate deduction", etc.


@dataclass
class ProfileCompletionProposal:
    """Proposal returned when TaxProfile is incomplete."""

    missing_fields: list[str]
    message: str = "Tax profile is incomplete. Please provide the missing fields before analysis."


# =============================================================================
# Rules Loading
# =============================================================================


def load_rules(jurisdiction: str, tax_year: int) -> dict[str, TaxRule]:
    """Load tax rules for a jurisdiction and year.

    Fail-closed: returns error if jurisdiction/year not found (Law #3).
    Rules are versioned by year — stale rules are never silently used.
    """
    rules_path = _TAX_RULES_DIR / jurisdiction / str(tax_year) / "rules.yaml"

    if not rules_path.exists():
        raise FileNotFoundError(
            f"Tax rules not found: {rules_path}. "
            f"No rules for {jurisdiction}/{tax_year}. "
            "Fail-closed: cannot provide tax analysis without rules (Law #3)."
        )

    with open(rules_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "rules" not in raw:
        raise ValueError(
            f"Tax rules file malformed: {rules_path}. Expected 'rules' mapping."
        )

    rules: dict[str, TaxRule] = {}
    for rule_key, rule_def in raw["rules"].items():
        if not isinstance(rule_def, dict):
            logger.warning("Skipping malformed tax rule: %s", rule_key)
            continue

        rules[rule_key] = TaxRule(
            rule_id=rule_def.get("rule_id", rule_key),
            title=rule_def.get("title", rule_key),
            eligibility_facts_required=rule_def.get("eligibility_facts_required", []),
            substantiation_required=rule_def.get("substantiation_required", []),
            common_failure_modes=rule_def.get("common_failure_modes", []),
            risk_default=rule_def.get("risk_default", "medium"),
            source_refs=rule_def.get("source_refs", []),
        )

    logger.info(
        "Tax rules loaded: jurisdiction=%s, year=%d, rules=%d",
        jurisdiction, tax_year, len(rules),
    )
    return rules


# =============================================================================
# Analysis Functions (proposal-only — never file anything)
# =============================================================================


def get_deduction_heatmap(
    profile: TaxProfile,
    rules: dict[str, TaxRule],
    spending_categories: list[str] | None = None,
) -> list[DeductionCandidate] | ProfileCompletionProposal:
    """Identify candidate deduction opportunities based on profile + rules.

    Returns ProfileCompletionProposal if profile is incomplete.
    """
    if not profile.is_complete:
        return _profile_completion(profile)

    candidates: list[DeductionCandidate] = []
    categories = set(spending_categories or [])

    for rule_key, rule in rules.items():
        relevance = _assess_relevance(rule_key, profile, categories)
        if relevance == "none":
            continue

        candidates.append(DeductionCandidate(
            rule_id=rule.rule_id,
            title=rule.title,
            risk_level=rule.risk_default,
            relevance=relevance,
            reason=_build_reason(rule_key, profile),
            substantiation_needed=rule.substantiation_required,
        ))

    # Sort by relevance (high first), then risk (low first)
    relevance_order = {"high": 0, "medium": 1, "low": 2}
    risk_order = {"low": 0, "medium": 1, "high": 2}
    candidates.sort(
        key=lambda c: (relevance_order.get(c.relevance, 2), risk_order.get(c.risk_level, 1)),
    )

    return candidates


def get_red_flag_radar(
    profile: TaxProfile,
    rules: dict[str, TaxRule],
    transactions: list[dict[str, Any]] | None = None,
) -> list[RedFlag] | ProfileCompletionProposal:
    """Identify audit risk indicators based on profile + transaction patterns.

    Returns ProfileCompletionProposal if profile is incomplete.
    """
    if not profile.is_complete:
        return _profile_completion(profile)

    flags: list[RedFlag] = []
    txns = transactions or []

    # Check for commingling indicators
    has_personal_in_business = any(
        t.get("category", "").lower() in ("personal", "personal_expense")
        for t in txns
    )
    if has_personal_in_business:
        flags.append(RedFlag(
            rule_id="RF-001",
            flag="Personal expenses in business account",
            severity="high",
            recommendation="Separate personal and business expenses. Commingling is a top audit trigger.",
        ))

    # Check for missing mileage logs (if vehicle deduction rule exists)
    if "vehicle_deduction" in rules:
        has_mileage_log = any(
            t.get("category", "").lower() in ("mileage_log", "vehicle_log")
            for t in txns
        )
        if not has_mileage_log and profile.vehicle_method in ("mileage", "actuals", "unknown"):
            flags.append(RedFlag(
                rule_id="RF-002",
                flag="No mileage log found for vehicle deduction",
                severity="high",
                recommendation="Maintain a contemporaneous mileage log with dates, destinations, and business purpose.",
            ))

    # Check for home office without documentation
    if "home_office_deduction" in rules and profile.home_office_intent == "yes":
        has_home_office_docs = any(
            t.get("category", "").lower() in ("home_office", "office_measurement")
            for t in txns
        )
        if not has_home_office_docs:
            flags.append(RedFlag(
                rule_id="RF-003",
                flag="Home office intent declared but no documentation found",
                severity="medium",
                recommendation="Document exclusive use: floor plan, measurements, photos of dedicated workspace.",
            ))

    # Check for large round-number deductions (audit trigger)
    large_round = [
        t for t in txns
        if t.get("amount", 0) >= 1000
        and t.get("amount", 0) % 100 == 0
        and t.get("is_deduction", False)
    ]
    if len(large_round) > 3:
        flags.append(RedFlag(
            rule_id="RF-004",
            flag=f"{len(large_round)} large round-number deductions detected",
            severity="medium",
            recommendation="Round-number deductions attract IRS scrutiny. Ensure exact amounts with receipts.",
        ))

    return flags


def get_substantiation_gaps(
    rule_id: str,
    rules: dict[str, TaxRule],
    tenant_evidence: list[str] | None = None,
) -> SubstantiationGap | None:
    """Identify missing substantiation for a specific rule.

    Returns None if all evidence is present or rule not found.
    """
    rule = None
    for r in rules.values():
        if r.rule_id == rule_id:
            rule = r
            break

    if rule is None:
        return None

    evidence = set(tenant_evidence or [])
    required = set(rule.substantiation_required)
    missing = sorted(required - evidence)

    if not missing:
        return None

    return SubstantiationGap(
        rule_id=rule.rule_id,
        title=rule.title,
        missing_items=missing,
        impact=f"Missing {len(missing)} of {len(required)} substantiation items. "
               f"May invalidate or reduce deduction.",
    )


# =============================================================================
# Private Helpers
# =============================================================================


def _profile_completion(profile: TaxProfile) -> ProfileCompletionProposal:
    """Build a profile completion proposal listing missing fields."""
    missing = []
    if not profile.jurisdiction:
        missing.append("jurisdiction")
    if not profile.entity_type:
        missing.append("entity_type")
    if not profile.accounting_method:
        missing.append("accounting_method")
    if not profile.tax_year:
        missing.append("tax_year")
    return ProfileCompletionProposal(missing_fields=missing)


def _assess_relevance(
    rule_key: str,
    profile: TaxProfile,
    spending_categories: set[str],
) -> str:
    """Assess how relevant a rule is to the tenant's profile.

    Returns: high, medium, low, or none.
    """
    # Home office: relevant if intent declared
    if rule_key == "home_office_deduction":
        if profile.home_office_intent == "yes":
            return "high"
        if profile.home_office_intent == "unknown":
            return "medium"
        return "none"

    # Vehicle: relevant if method declared or spending indicates it
    if rule_key == "vehicle_deduction":
        if profile.vehicle_method in ("mileage", "actuals"):
            return "high"
        if profile.vehicle_method == "unknown":
            return "medium"
        if "vehicle" in spending_categories or "mileage" in spending_categories:
            return "medium"
        return "low"

    # Retirement: relevant for all entity types
    if rule_key == "retirement_contributions":
        return "high"

    # Health insurance: relevant for sole props and S-corps
    if rule_key == "health_insurance_deduction":
        if profile.entity_type in ("sole_prop", "s_corp"):
            return "high"
        return "medium"

    # Meals: relevant if spending indicates it
    if rule_key == "meals_deduction":
        if "meals" in spending_categories or "dining" in spending_categories:
            return "high"
        return "medium"

    # Default: medium relevance for any recognized rule
    return "medium"


def _build_reason(rule_key: str, profile: TaxProfile) -> str:
    """Build a human-readable reason for why a rule is relevant."""
    reasons = {
        "home_office_deduction": f"Home office intent: {profile.home_office_intent}",
        "vehicle_deduction": f"Vehicle method: {profile.vehicle_method or 'not declared'}",
        "meals_deduction": f"Entity type {profile.entity_type} commonly has deductible meals",
        "retirement_contributions": f"Entity type {profile.entity_type} eligible for retirement deductions",
        "health_insurance_deduction": f"Entity type {profile.entity_type} may qualify for self-employed health insurance",
    }
    return reasons.get(rule_key, f"Rule applicable to {profile.entity_type} in {profile.jurisdiction}")
