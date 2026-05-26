"""M-2 tests — DisciplineCompatibilityConstraint + validator integration.

Coverage:
- HARD violations REJECT via validate_edge (Wave 20.13.3 prevention)
- SOFT violations route VERIFIED-confidence edges to UNKNOWN_REVIEW
- Missing context returns SOFT skip (fail open)
- OCR-text inference catches discipline mismatch when one side known
- Compatible pairs pass through
- L-6 RuleBasedVerifier imports same matrix (DRY check)
"""
from __future__ import annotations

import pytest

from ..audit import RuleBasedVerifier
from ..taxonomy import (
    EdgeProposal,
    EdgeType,
    EntityType,
    LocalityScope,
    RejectionReason,
    ResolutionState,
    validate_edge,
)
from .discipline import (
    DISCIPLINE_TOKENS,
    HARD_INCOMPATIBLE_DISCIPLINES,
    DisciplineCompatibilityConstraint,
    detect_discipline_from_text,
    disciplines_hard_incompatible,
)
from .base import ConstraintSeverity


# ============================================================
# Fixtures
# ============================================================


def _proposal(
    *,
    edge_type: EdgeType = EdgeType.ANNOTATED_BY,
    src_disc: str | None = None,
    dst_disc: str | None = None,
    ocr_text: str = "A-101",
    confidence: float = 0.85,
) -> EdgeProposal:
    provenance: dict[str, object] = {
        "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
        "sheet_id": "sheet-1", "generator_window": "test",
        "bbox": [0, 0, 10, 10],
    }
    if src_disc:
        provenance["source_discipline"] = src_disc
    if dst_disc:
        provenance["destination_discipline"] = dst_disc
    return EdgeProposal(
        edge_type=edge_type,
        suite_id="suite-1", project_id="proj-1",
        src_entity_id="p-1", src_entity_type=EntityType.PRIMITIVE,
        dst_entity_id="o-1",
        dst_entity_type=(
            EntityType.OCR_SPAN if edge_type == EdgeType.ANNOTATED_BY
            else EntityType.LEGEND
        ),
        locality_scope=LocalityScope.SAME_SHEET,
        spatial_distance=10.0, confidence=confidence,
        evidence={"ocr_text": ocr_text, "distance_px": 10,
                  "alignment": "horizontal", "zone": "title_block"},
        provenance=provenance,
        extraction_method="test_v1",
    )


# ============================================================
# Static matrix invariants
# ============================================================


def test_hard_incompatible_set_is_symmetric():
    """Every (a, b) has a matching (b, a) — both directions REJECT."""
    for a, b in HARD_INCOMPATIBLE_DISCIPLINES:
        assert (b, a) in HARD_INCOMPATIBLE_DISCIPLINES, (
            f"asymmetric HARD pair: ({a}, {b}) without ({b}, {a})"
        )


def test_hard_incompatible_covers_critical_trades():
    """The Wave 20.13.3 bid-killing combinations must be present."""
    assert ("plumbing", "electrical") in HARD_INCOMPATIBLE_DISCIPLINES
    assert ("plumbing", "fire") in HARD_INCOMPATIBLE_DISCIPLINES
    assert ("electrical", "fire") in HARD_INCOMPATIBLE_DISCIPLINES


def test_compatible_pairs_intentionally_absent():
    """Mechanical-electrical (HVAC controls) is NOT HARD — confidence decides."""
    assert ("mechanical", "electrical") not in HARD_INCOMPATIBLE_DISCIPLINES
    assert ("structural", "architectural") not in HARD_INCOMPATIBLE_DISCIPLINES


def test_disciplines_hard_incompatible_function():
    assert disciplines_hard_incompatible("plumbing", "electrical") is True
    assert disciplines_hard_incompatible("electrical", "plumbing") is True
    assert disciplines_hard_incompatible("plumbing", "plumbing") is False
    assert disciplines_hard_incompatible("mechanical", "electrical") is False
    assert disciplines_hard_incompatible(None, "electrical") is False
    assert disciplines_hard_incompatible("", "electrical") is False


# ============================================================
# OCR-text inference
# ============================================================


def test_detect_discipline_from_text_plumbing():
    assert detect_discipline_from_text("3/4 WASTE PIPE") == "plumbing"


def test_detect_discipline_from_text_electrical():
    assert detect_discipline_from_text("ELECTRICAL PANEL P-1") == "electrical"


def test_detect_discipline_from_text_fire():
    assert detect_discipline_from_text("SPRINKLER HEAD QR-2") == "fire"


def test_detect_discipline_from_text_unknown_returns_none():
    assert detect_discipline_from_text("A-101") is None
    assert detect_discipline_from_text("") is None


# ============================================================
# Constraint direct check
# ============================================================


def test_constraint_passes_compatible_disciplines():
    c = DisciplineCompatibilityConstraint()
    result = c.check(_proposal(src_disc="plumbing", dst_disc="plumbing"))
    assert result.passed is True


def test_constraint_hard_violates_plumbing_electrical():
    c = DisciplineCompatibilityConstraint()
    result = c.check(_proposal(src_disc="plumbing", dst_disc="electrical"))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.HARD
    assert "hard_incompatible" in result.reason


def test_constraint_ocr_inference_catches_mismatch():
    """One-sided discipline + OCR text from incompatible discipline → HARD."""
    c = DisciplineCompatibilityConstraint()
    result = c.check(_proposal(
        src_disc="plumbing",
        dst_disc=None,
        ocr_text="ELECTRICAL PANEL P-1",
    ))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.HARD
    assert "via_ocr" in result.reason


def test_constraint_soft_skip_on_missing_context():
    """No disciplines anywhere → SOFT skip (fail open)."""
    c = DisciplineCompatibilityConstraint()
    result = c.check(_proposal(src_disc=None, dst_disc=None, ocr_text="A-101"))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.SOFT
    assert result.reason == "missing_discipline_context"


def test_constraint_does_not_apply_to_spatial_edges():
    """adjacent_to is geometry — discipline irrelevant."""
    c = DisciplineCompatibilityConstraint()
    assert EdgeType.ADJACENT_TO not in c.applies_to
    assert EdgeType.ALIGNED_WITH not in c.applies_to
    assert EdgeType.INSIDE_ZONE not in c.applies_to
    # But it DOES apply to material-binding edges
    assert EdgeType.ANNOTATED_BY in c.applies_to
    assert EdgeType.LEGEND_ENTRY_FOR in c.applies_to
    assert EdgeType.MEMBER_OF_FAMILY in c.applies_to


# ============================================================
# validate_edge integration — the real Phase 4 gate
# ============================================================


def test_validate_edge_rejects_hard_discipline_violation():
    """End-to-end: validate_edge rejects an annotated_by edge with
    plumbing src + electrical dst."""
    proposal = _proposal(
        src_disc="plumbing", dst_disc="electrical", confidence=0.95,
    )
    result = validate_edge(proposal)
    assert result.resolution_state == ResolutionState.REJECTED
    assert RejectionReason.CONSTRAINT_VIOLATION in result.final_rejection_reasons
    hard_v = result.constraint.details.get("hard_violations", [])
    assert any(v["name"] == "discipline_compatibility" for v in hard_v)


def test_validate_edge_soft_violation_routes_to_ur_even_at_high_confidence():
    """SOFT violation — confidence would VERIFY but constraint says UR."""
    # Missing context → SOFT skip from discipline constraint
    proposal = _proposal(
        src_disc=None, dst_disc=None, ocr_text="A-101", confidence=0.95,
    )
    result = validate_edge(proposal)
    # The SOFT violation forces UR regardless of confidence
    assert result.resolution_state == ResolutionState.UNKNOWN_REVIEW
    assert RejectionReason.CONSTRAINT_VIOLATION in result.final_rejection_reasons
    soft_v = result.constraint.details.get("soft_violations", [])
    assert any(v["name"] == "discipline_compatibility" for v in soft_v)


def test_validate_edge_compatible_pair_still_verifies():
    """Constraint approves → normal confidence routing applies."""
    proposal = _proposal(
        src_disc="plumbing", dst_disc="plumbing", confidence=0.95,
    )
    result = validate_edge(proposal)
    assert result.resolution_state == ResolutionState.VERIFIED
    assert result.constraint.passed is True
    assert result.constraint.details.get("hard_violations") == []
    assert result.constraint.details.get("soft_violations") == []


def test_validate_edge_constraint_evaluated_list_recorded():
    """details.constraints_evaluated tracks which constraints fired."""
    proposal = _proposal(src_disc="plumbing", dst_disc="plumbing")
    result = validate_edge(proposal)
    evaluated = result.constraint.details.get("constraints_evaluated", [])
    assert "discipline_compatibility" in evaluated


def test_validate_edge_constraint_skipped_for_spatial_edges():
    """adjacent_to → constraint registry skips discipline check."""
    spatial = EdgeProposal(
        edge_type=EdgeType.ADJACENT_TO,
        suite_id="s", project_id="p",
        src_entity_id="a", src_entity_type=EntityType.PRIMITIVE,
        dst_entity_id="b", dst_entity_type=EntityType.PRIMITIVE,
        locality_scope=LocalityScope.SAME_SHEET,
        spatial_distance=10.0, confidence=0.85,
        evidence={"distance_px": 10, "axis": "horizontal"},
        provenance={
            "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
            "sheet_id": "s1", "generator_window": "test", "bbox": [0, 0, 10, 10],
        },
        extraction_method="test_v1",
    )
    result = validate_edge(spatial)
    assert result.resolution_state == ResolutionState.VERIFIED
    evaluated = result.constraint.details.get("constraints_evaluated", [])
    assert "discipline_compatibility" not in evaluated


# ============================================================
# DRY guarantee — L-6 verifier imports SAME matrix
# ============================================================


def test_l6_rule_based_verifier_uses_shared_discipline_tokens():
    """Drift detector — if L-6 ever forks the matrix, this fails."""
    v = RuleBasedVerifier()
    verifier_tokens = v._discipline_tokens()
    assert verifier_tokens is DISCIPLINE_TOKENS, (
        "L-6 RuleBasedVerifier must import the canonical DISCIPLINE_TOKENS "
        "from constraints.discipline (DRY violation)."
    )


def test_l6_still_catches_discipline_mismatch():
    """Regression: the L-6 verifier behavior is unchanged post-refactor."""
    from ..audit import AuditVerdict
    proposal = _proposal(
        src_disc="plumbing", ocr_text="ELECTRICAL PANEL P-1",
        confidence=0.85,
    )
    verdict, rationale = RuleBasedVerifier().verify(proposal)
    assert verdict == AuditVerdict.FALSE_POSITIVE
    assert "discipline_mismatch" in rationale
