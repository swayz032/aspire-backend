"""M-3 tests — LayerSeparationConstraint + validate_edge integration.

Coverage:
- CAD layer name parsing (DEMO / EXISTING / NEW / RELOCATE variants)
- OCR text shorthand parsing ((E), (D), (N), DEMO, EXISTING, T.B.D., etc.)
- DEMO↔NEW + EXISTING↔NEW SOFT violations symmetric
- Same-state pairs PASS
- DEMO↔EXISTING (both pre-bid) PASS — no bid impact
- RELOCATE↔NEW/DEMO PASS — transitional items
- Missing context → SOFT_SKIP (fail open)
- Partial context (one side known) → SOFT_SKIP
- validate_edge integration: SOFT violation forces UR even at high conf
- Spatial + ANNOTATED_BY edges SKIP layer check
"""
from __future__ import annotations

import pytest

from ..taxonomy import (
    EdgeProposal,
    EdgeType,
    EntityType,
    LocalityScope,
    RejectionReason,
    ResolutionState,
    validate_edge,
)
from .base import ConstraintSeverity
from .layer import (
    LayerSeparationConstraint,
    LayerState,
    classify_layer_name,
    classify_layer_text,
    layer_states_incompatible,
)


# ============================================================
# Fixtures
# ============================================================


def _proposal(
    *,
    edge_type: EdgeType = EdgeType.FEEDS,
    src_layer: str | None = None,
    dst_layer: str | None = None,
    src_state: str | None = None,  # explicit LayerState string in provenance
    dst_state: str | None = None,
    ocr_text: str | None = None,
    confidence: float = 0.85,
) -> EdgeProposal:
    provenance: dict[str, object] = {
        "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
        "sheet_id": "sheet-1", "generator_window": "test",
        "bbox": [0, 0, 10, 10],
        # FEEDS / RETURNS / etc. need this to pass discipline constraint
        "source_discipline": "plumbing", "destination_discipline": "plumbing",
    }
    if src_state:
        provenance["source_layer_state"] = src_state
    if dst_state:
        provenance["destination_layer_state"] = dst_state

    evidence: dict[str, object] = {
        "flow_direction": "downstream",
        "discipline": "plumbing",
    }
    if src_layer:
        evidence["src_layer"] = src_layer
    if dst_layer:
        evidence["dst_layer"] = dst_layer
    if ocr_text:
        evidence["ocr_text"] = ocr_text

    return EdgeProposal(
        edge_type=edge_type,
        suite_id="suite-1", project_id="proj-1",
        src_entity_id="p-1", src_entity_type=EntityType.PRIMITIVE,
        dst_entity_id="p-2", dst_entity_type=EntityType.PRIMITIVE,
        locality_scope=LocalityScope.SAME_SHEET,
        spatial_distance=10.0, confidence=confidence,
        evidence=evidence,
        provenance=provenance,
        extraction_method="test_v1",
    )


# ============================================================
# CAD layer name parser
# ============================================================


@pytest.mark.parametrize("name,expected", [
    ("P-PIPE-DEMO", LayerState.DEMO),
    ("A-WALL-DEMO", LayerState.DEMO),
    ("DEMO-PIPE", LayerState.DEMO),
    ("M-DUCT-DEM", LayerState.DEMO),
    ("DEMOLITION", LayerState.DEMO),
    ("DEMOLISHED", LayerState.DEMO),
    ("E-LITE-EXST", LayerState.EXISTING),
    ("EXST-WALL", LayerState.EXISTING),
    ("M-DUCT-EXIST", LayerState.EXISTING),
    ("M-DUCT-EXISTING", LayerState.EXISTING),
    ("P-PIPE-NEW", LayerState.NEW),
    ("NEW-WORK", LayerState.NEW),
    ("A-WALL-N", LayerState.NEW),
    ("P-PIPE-RELO", LayerState.RELOCATE),
    ("RELOCATE-FIXTURE", LayerState.RELOCATE),
    ("RELOCATED", LayerState.RELOCATE),
    ("RANDOM-LAYER-NAME", LayerState.UNKNOWN),
    ("", LayerState.UNKNOWN),
    (None, LayerState.UNKNOWN),
])
def test_classify_layer_name(name, expected):
    assert classify_layer_name(name) == expected


def test_classify_layer_name_demo_beats_existing():
    """A layer named DEMO-EXST is being demolished, not existing."""
    assert classify_layer_name("DEMO-EXST") == LayerState.DEMO


def test_classify_layer_name_relocate_beats_new():
    """RELO suffix wins over N suffix."""
    assert classify_layer_name("P-RELO-N") == LayerState.RELOCATE


# ============================================================
# OCR text parser
# ============================================================


@pytest.mark.parametrize("text,expected", [
    ("(E)", LayerState.EXISTING),
    ("(D)", LayerState.DEMO),
    ("(N)", LayerState.NEW),
    ("(R)", LayerState.RELOCATE),
    ("EXISTING WALL", LayerState.EXISTING),
    ("EXST. PIPE", LayerState.EXISTING),
    ("EXIST. CONDUIT", LayerState.EXISTING),
    ("DEMO", LayerState.DEMO),
    ("DEMOLISH", LayerState.DEMO),
    ("DEMOLISHED", LayerState.DEMO),
    ("DEMOLITION", LayerState.DEMO),
    ("TO BE DEMOLISHED", LayerState.DEMO),
    ("TO BE REMOVED", LayerState.DEMO),
    ("T.B.D.", LayerState.DEMO),
    ("T.B.R.", LayerState.DEMO),
    ("NEW WORK", LayerState.NEW),
    ("NEW PIPE", LayerState.NEW),
    ("RELOCATE", LayerState.RELOCATE),
    ("RELOCATED FIXTURE", LayerState.RELOCATE),
    ("A-101", LayerState.UNKNOWN),
    ("", LayerState.UNKNOWN),
    (None, LayerState.UNKNOWN),
])
def test_classify_layer_text(text, expected):
    assert classify_layer_text(text) == expected


# ============================================================
# Rules
# ============================================================


def test_incompatible_pairs_symmetric():
    """DEMO↔NEW and EXISTING↔NEW are SOFT incompatible in both directions."""
    assert layer_states_incompatible(LayerState.DEMO, LayerState.NEW)
    assert layer_states_incompatible(LayerState.NEW, LayerState.DEMO)
    assert layer_states_incompatible(LayerState.EXISTING, LayerState.NEW)
    assert layer_states_incompatible(LayerState.NEW, LayerState.EXISTING)


def test_compatible_pairs():
    """Same-class + DEMO↔EXISTING + RELOCATE↔anything are compatible."""
    assert not layer_states_incompatible(LayerState.NEW, LayerState.NEW)
    assert not layer_states_incompatible(LayerState.DEMO, LayerState.DEMO)
    assert not layer_states_incompatible(LayerState.EXISTING, LayerState.EXISTING)
    assert not layer_states_incompatible(LayerState.DEMO, LayerState.EXISTING)
    # RELOCATE is transitional — never flag
    assert not layer_states_incompatible(LayerState.RELOCATE, LayerState.NEW)
    assert not layer_states_incompatible(LayerState.RELOCATE, LayerState.DEMO)


def test_unknown_returns_not_incompatible():
    """UNKNOWN on either side → not incompatible (caller issues SOFT_SKIP)."""
    assert not layer_states_incompatible(LayerState.UNKNOWN, LayerState.NEW)
    assert not layer_states_incompatible(LayerState.NEW, LayerState.UNKNOWN)
    assert not layer_states_incompatible(None, LayerState.NEW)


# ============================================================
# Constraint direct check
# ============================================================


def test_demo_to_new_soft_violation():
    c = LayerSeparationConstraint()
    result = c.check(_proposal(
        src_state="demo", dst_state="new",
        edge_type=EdgeType.FEEDS,
    ))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.SOFT
    assert "incompatible_layers" in result.reason


def test_existing_to_new_soft_violation():
    c = LayerSeparationConstraint()
    result = c.check(_proposal(
        src_state="existing", dst_state="new",
        edge_type=EdgeType.CONNECTED_TO,
    ))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.SOFT


def test_new_to_new_passes():
    c = LayerSeparationConstraint()
    result = c.check(_proposal(src_state="new", dst_state="new"))
    assert result.passed is True


def test_demo_to_existing_passes():
    """Both pre-bid scope — no bid impact, no flag."""
    c = LayerSeparationConstraint()
    result = c.check(_proposal(src_state="demo", dst_state="existing"))
    assert result.passed is True


def test_relocate_to_new_passes():
    """Relocate is transitional — never flag."""
    c = LayerSeparationConstraint()
    result = c.check(_proposal(src_state="relocate", dst_state="new"))
    assert result.passed is True


def test_cad_layer_name_path():
    """Source: evidence.src_layer = 'P-PIPE-DEMO', evidence.dst_layer = 'P-PIPE-NEW'"""
    c = LayerSeparationConstraint()
    result = c.check(_proposal(
        src_layer="P-PIPE-DEMO", dst_layer="P-PIPE-NEW",
    ))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.SOFT


def test_ocr_text_fallback():
    """If OCR text contains DEMO and no other layer info → both sides DEMO."""
    c = LayerSeparationConstraint()
    result = c.check(_proposal(ocr_text="DEMO PIPE"))
    # Both sides resolve via OCR text to DEMO → compatible
    assert result.passed is True


def test_missing_context_soft_skip():
    c = LayerSeparationConstraint()
    result = c.check(_proposal())   # nothing set
    assert result.passed is False
    assert result.severity == ConstraintSeverity.SOFT
    assert result.reason == "missing_layer_context"


def test_partial_context_soft_skip():
    """One side known, other UNKNOWN → SOFT_SKIP."""
    c = LayerSeparationConstraint()
    result = c.check(_proposal(src_state="demo"))
    assert result.passed is False
    assert result.severity == ConstraintSeverity.SOFT
    assert result.reason == "partial_layer_context"


# ============================================================
# applies_to coverage
# ============================================================


def test_applies_to_functional_and_family_edges():
    c = LayerSeparationConstraint()
    # Should apply to these
    for et in (EdgeType.FEEDS, EdgeType.CONNECTED_TO, EdgeType.MEMBER_OF_FAMILY,
               EdgeType.LEGEND_ENTRY_FOR, EdgeType.RETURNS_TO,
               EdgeType.PROTECTED_BY, EdgeType.CONTROLS, EdgeType.SERVES,
               EdgeType.TERMINATES_AT):
        assert et in c.applies_to, f"{et} should be in applies_to"


def test_skips_geometry_and_annotation_edges():
    c = LayerSeparationConstraint()
    # Should NOT apply
    for et in (EdgeType.ADJACENT_TO, EdgeType.ALIGNED_WITH,
               EdgeType.INSIDE_ZONE, EdgeType.ANNOTATED_BY,
               EdgeType.BELONGS_TO_SHEET):
        assert et not in c.applies_to, f"{et} should NOT be in applies_to"


# ============================================================
# validate_edge integration
# ============================================================


def test_validate_edge_layer_soft_downgrades_to_ur():
    """High-confidence DEMO→NEW edge gets routed to UR by SOFT violation."""
    proposal = _proposal(
        src_state="demo", dst_state="new",
        edge_type=EdgeType.FEEDS, confidence=0.95,
    )
    result = validate_edge(proposal)
    assert result.resolution_state == ResolutionState.UNKNOWN_REVIEW
    assert RejectionReason.CONSTRAINT_VIOLATION in result.final_rejection_reasons
    soft_v = result.constraint.details.get("soft_violations", [])
    assert any(v["name"] == "layer_separation" for v in soft_v)


def test_validate_edge_layer_compatible_verifies():
    proposal = _proposal(
        src_state="new", dst_state="new",
        edge_type=EdgeType.FEEDS, confidence=0.95,
    )
    result = validate_edge(proposal)
    assert result.resolution_state == ResolutionState.VERIFIED


def test_validate_edge_layer_missing_context_downgrades_to_ur():
    """No layer info anywhere → SOFT_SKIP → UR downgrade."""
    proposal = _proposal(edge_type=EdgeType.FEEDS, confidence=0.95)
    result = validate_edge(proposal)
    assert result.resolution_state == ResolutionState.UNKNOWN_REVIEW


def test_registry_includes_both_constraints():
    """The CONSTRAINT_REGISTRY now has discipline + layer."""
    from . import CONSTRAINT_REGISTRY
    names = {c.name for c in CONSTRAINT_REGISTRY}
    assert "discipline_compatibility" in names
    assert "layer_separation" in names
