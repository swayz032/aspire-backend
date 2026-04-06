"""Adam Verification Engine — Confidence scoring + conflict detection (ADR-003).

Trust tiers drive confidence:
  A (ATTOM, govt): highest weight
  B (Google Places, SerpApi, Tripadvisor): strong weight
  C (Brave, Exa, Tavily, Parallel): supporting weight

Exa's native grounding.confidence (low/medium/high) is used directly
when available, providing field-level confidence from the provider itself.

Rules:
  - Conflicting values are SURFACED, never silently merged
  - Missing fields are always reported
  - Unknown is preferable to invented
  - verified: trusted source(s) with no material conflict
  - partially_verified: useful but incomplete or conflicted
  - unverified: insufficient evidence
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.verification_report import (
    FieldConflict,
    VerificationReport,
)

logger = logging.getLogger(__name__)

# Trust class weights for confidence calculation
_TRUST_WEIGHTS: dict[str, float] = {
    "A": 0.95,  # ATTOM, official govt
    "B": 0.80,  # Google Places, SerpApi, Tripadvisor, HERE, Foursquare
    "C": 0.60,  # Brave, Exa, Tavily, Parallel
}

# Provider → trust class mapping
_PROVIDER_TRUST: dict[str, str] = {
    "attom": "A",
    "google_places": "B",
    "here": "B",
    "foursquare": "B",
    "tomtom": "B",
    "serpapi_shopping": "B",
    "serpapi_home_depot": "B",
    "tripadvisor": "B",
    "brave": "C",
    "exa": "C",
    "tavily": "C",
    "parallel": "C",
    "mapbox": "B",
}

# Exa grounding confidence → numeric score
_EXA_GROUNDING_MAP: dict[str, float] = {
    "high": 0.90,
    "medium": 0.70,
    "low": 0.40,
}


def get_trust_weight(provider: str) -> float:
    """Get the trust weight for a provider."""
    trust_class = _PROVIDER_TRUST.get(provider, "C")
    return _TRUST_WEIGHTS.get(trust_class, 0.50)


def verify_records(
    *,
    records: list[dict[str, Any]],
    sources: list[SourceAttribution],
    required_fields: list[str] | None = None,
    exa_grounding: list[dict[str, Any]] | None = None,
) -> VerificationReport:
    """Verify a set of records and produce a confidence report.

    Args:
        records: List of canonical record dicts to verify
        sources: All source attributions used
        required_fields: Fields that MUST be present for "verified" status
        exa_grounding: Exa's native grounding data (field-level citations + confidence)

    Returns:
        VerificationReport with status, score, conflicts, missing fields
    """
    required = required_fields or []

    # Count unique providers
    provider_set = {s.provider for s in sources}
    source_count = len(provider_set)

    # Detect missing fields across all records
    missing: list[str] = []
    for field_name in required:
        found = False
        for record in records:
            if record.get(field_name) is not None and record.get(field_name) != "":
                found = True
                break
        if not found:
            missing.append(field_name)

    # Detect conflicts (same field, different values from different providers)
    conflicts = _detect_conflicts(records)

    # Calculate confidence score
    score = _calculate_confidence(
        source_count=source_count,
        sources=sources,
        missing_count=len(missing),
        conflict_count=len(conflicts),
        total_required=len(required),
        exa_grounding=exa_grounding,
    )

    # Determine status
    if score >= 0.80 and len(conflicts) == 0 and len(missing) == 0:
        status = "verified"
    elif score >= 0.40 and source_count >= 1:
        status = "partially_verified"
    else:
        status = "unverified"

    # Build recommendations
    recommendations: list[str] = []
    if missing:
        recommendations.append(f"Missing fields: {', '.join(missing)}")
    if conflicts:
        recommendations.append(f"{len(conflicts)} field conflict(s) detected — review before trusting")
    if source_count == 1:
        recommendations.append("Single source only — consider additional verification")

    return VerificationReport(
        status=status,
        confidence_score=round(score, 3),
        source_count=source_count,
        conflict_count=len(conflicts),
        conflicts=conflicts,
        missing_fields=missing,
        recommendations=recommendations,
    )


def _calculate_confidence(
    *,
    source_count: int,
    sources: list[SourceAttribution],
    missing_count: int,
    conflict_count: int,
    total_required: int,
    exa_grounding: list[dict[str, Any]] | None = None,
) -> float:
    """Calculate overall confidence score (0.0 - 1.0).

    Inputs:
      - Provider trust tier weights
      - Agreement across sources (more sources = higher base)
      - Exa grounding confidence (if available)
      - Completeness (missing fields penalize)
      - Conflicts penalize
    """
    if source_count == 0:
        return 0.0

    # Base: weighted average of provider trust
    provider_weights = [get_trust_weight(s.provider) for s in sources]
    base_score = max(provider_weights) if provider_weights else 0.50

    # Multi-source bonus (agreement)
    if source_count >= 3:
        base_score = min(base_score + 0.10, 1.0)
    elif source_count >= 2:
        base_score = min(base_score + 0.05, 1.0)

    # Exa grounding integration (use native confidence when available)
    if exa_grounding:
        grounding_scores = []
        for g in exa_grounding:
            conf = g.get("confidence", "")
            if conf in _EXA_GROUNDING_MAP:
                grounding_scores.append(_EXA_GROUNDING_MAP[conf])
        if grounding_scores:
            avg_grounding = sum(grounding_scores) / len(grounding_scores)
            # Blend Exa grounding with provider trust (Exa grounding is field-level)
            base_score = (base_score * 0.6) + (avg_grounding * 0.4)

    # Missing field penalty
    if total_required > 0 and missing_count > 0:
        completeness = 1.0 - (missing_count / total_required)
        base_score *= completeness

    # Conflict penalty
    if conflict_count > 0:
        base_score *= max(0.5, 1.0 - (conflict_count * 0.15))

    return max(0.0, min(1.0, base_score))


def _detect_conflicts(records: list[dict[str, Any]]) -> list[FieldConflict]:
    """Detect fields where records from different providers disagree.

    Only checks fields that appear in multiple records with different values.
    Numeric fields use a tolerance threshold; strings use exact match.
    """
    if len(records) <= 1:
        return []

    # Collect all field values with their provider
    field_values: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        provider = ""
        for src in record.get("sources", []):
            if isinstance(src, dict):
                provider = src.get("provider", "")
                break

        for key, value in record.items():
            # Skip metadata fields and web-evidence fields where uniqueness is expected
            if key in (
                "sources", "extra", "verification_status", "confidence",
                "url", "title", "content", "snippet", "domain",
                "published_date", "relevance_score", "exa_grounding_confidence",
                "retrieved_at", "provider",
            ):
                continue
            if value is None or value == "" or value == []:
                continue

            if key not in field_values:
                field_values[key] = []
            field_values[key].append({"provider": provider, "value": value})

    # Find conflicts
    conflicts: list[FieldConflict] = []
    for field_name, entries in field_values.items():
        if len(entries) < 2:
            continue

        # Check if values disagree
        values = [e["value"] for e in entries]
        if _values_conflict(values):
            # Resolve by trust: highest-trust provider wins
            best_provider = max(entries, key=lambda e: get_trust_weight(e["provider"]))
            conflicts.append(FieldConflict(
                field_name=field_name,
                values=entries,
                resolution=f"highest_trust_wins:{best_provider['provider']}",
            ))

    return conflicts


def _values_conflict(values: list[Any]) -> bool:
    """Check if a list of values represents a conflict."""
    if not values:
        return False

    first = values[0]

    for v in values[1:]:
        if isinstance(first, (int, float)) and isinstance(v, (int, float)):
            # Numeric: 10% tolerance
            if first == 0 and v == 0:
                continue
            denom = max(abs(first), abs(v), 1)
            if abs(first - v) / denom > 0.10:
                return True
        elif isinstance(first, str) and isinstance(v, str):
            if first.lower().strip() != v.lower().strip():
                return True
        elif first != v:
            return True

    return False
