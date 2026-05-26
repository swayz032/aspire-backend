"""Wave 20.14.5L L-6 — False-positive regression fixtures.

Locked adversarial cases that exercise the Wave 20.13.3-class failure
modes Drew must NEVER fall back into. Every fixture has:
  - a constructed input scenario (primitives + OCR spans + zones)
  - the generator(s) it targets
  - the expected outcome (REJECT, UR, or specific verdict)

The regression suite locks in these expectations. If a future generator
change accidentally permits any of them, the test fails loudly.

FP modes covered:
  FP-1  Discipline cross-contamination — plumbing primitive annotated
        by electrical-discipline OCR span
  FP-2  NOT-IN-CONTRACT span — primitive labeled by a span that
        explicitly states it's not part of the bid
  FP-3  Diagonal adjacency at suspicious-high confidence
  FP-4  Zone-boundary leak — primitive split across two zones must
        not produce inside_zone edges to BOTH
  FP-5  Symmetric edge double emission — adjacent_to must dedup A↔B
        (lex-sorted src < dst)
  FP-6  Far-distance span emitting VERIFIED when it should be UR
        (calibration regression check for L-5)
  FP-7  Empty OCR text on annotated_by — must not emit
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .audit import AuditVerdict


@dataclass(frozen=True)
class FPFixture:
    """One adversarial test case."""
    name: str
    fp_mode: str                            # 'FP-1' .. 'FP-7'
    description: str
    generator_target: str                   # 'annotated_by' | 'adjacent_to' | 'inside_zone'

    # Scenario primitives — schema-loose so we can use it with InMemoryQueryClient
    primitives: list[dict[str, Any]] = field(default_factory=list)
    ocr_spans: list[dict[str, Any]] = field(default_factory=list)
    zones: list[dict[str, Any]] = field(default_factory=list)

    # Expected outcome — at least ONE must be set
    expected_no_edges: bool = False         # zero edges should emit
    expected_ur_count: Optional[int] = None # exact count of UR edges
    expected_verified_count: Optional[int] = None
    expected_verdict_for_first_edge: Optional[AuditVerdict] = None


# ============================================================
# Helper constructors — keep fixtures readable
# ============================================================


def _prim(eid: str, bbox: list[float], *, discipline: str = "") -> dict[str, Any]:
    return {
        "id": eid,
        "entity_type": "primitive",
        "bbox": bbox,
        "discipline": discipline,
    }


def _span(eid: str, bbox: list[float], text: str) -> dict[str, Any]:
    return {
        "id": eid,
        "entity_type": "ocr_span",
        "bbox": bbox,
        "text": text,
    }


def _zone(zid: str, bbox: list[float], zone_type: str) -> dict[str, Any]:
    return {
        "id": zid,
        "entity_type": "zone",
        "bbox": bbox,
        "zone_type": zone_type,
    }


# ============================================================
# The locked fixtures
# ============================================================

FP_FIXTURES: list[FPFixture] = [

    FPFixture(
        name="fp1_plumbing_primitive_annotated_by_electrical_text",
        fp_mode="FP-1",
        description=(
            "Plumbing primitive sitting near an electrical-discipline OCR span. "
            "Without discipline-aware filtering this would yield a confident "
            "annotated_by edge → wrong material in the bid. Auditor catches as FP."
        ),
        generator_target="annotated_by",
        primitives=[_prim("plumb-1", [100, 100, 105, 105], discipline="plumbing")],
        ocr_spans=[_span("ocr-elec", [110, 99, 160, 103], "ELECTRICAL PANEL P-1")],
        # Generator may still emit a high-conf edge (it doesn't know
        # disciplines yet). The audit verifier must catch it as FP.
        expected_verdict_for_first_edge=AuditVerdict.FALSE_POSITIVE,
    ),

    FPFixture(
        name="fp2_nic_span_must_route_to_ur_or_fp",
        fp_mode="FP-2",
        description=(
            "OCR text says 'NOT IN CONTRACT'. The primitive next to it must "
            "NOT be confidently annotated by this span — it's explicitly out "
            "of bid scope. Either route to UR or audit catches as FP."
        ),
        generator_target="annotated_by",
        primitives=[_prim("p1", [100, 100, 105, 105])],
        ocr_spans=[_span("ocr-nic", [110, 99, 160, 103], "NOT IN CONTRACT")],
        # Even if the geometry alone would emit VERIFIED, the verifier
        # catches it.
        expected_verdict_for_first_edge=AuditVerdict.FALSE_POSITIVE,
    ),

    FPFixture(
        name="fp3_diagonal_adjacency_high_confidence",
        fp_mode="FP-3",
        description=(
            "Two primitives placed diagonally close (not horizontally or "
            "vertically aligned). They shouldn't infer an assembly edge at "
            "high confidence. Auditor flags as UR_missed."
        ),
        generator_target="adjacent_to",
        primitives=[
            _prim("aaa-prim", [100, 100, 102, 102]),
            _prim("bbb-prim", [115, 115, 117, 117]),
        ],
        # Either gets rejected (within radius and clean axis) or gets
        # caught by audit (high-conf diagonal).
        expected_verdict_for_first_edge=AuditVerdict.UR_MISSED,
    ),

    FPFixture(
        name="fp4_zone_boundary_split",
        fp_mode="FP-4",
        description=(
            "Primitive bbox spans two abutting zones (legend + title_block). "
            "Its center is in one; it should not be inside BOTH zones. "
            "Generator emits only one edge (to the zone containing center). "
            "The other zone should not receive an inside_zone edge."
        ),
        generator_target="inside_zone",
        primitives=[_prim("p-split", [95, 100, 115, 110])],   # center (105, 105)
        zones=[
            _zone("z-legend", [90, 90, 110, 120], "legend"),    # contains (105,105)
            _zone("z-tb", [110, 90, 200, 120], "title_block"),  # adjacent
        ],
        expected_verified_count=1,
    ),

    FPFixture(
        name="fp5_symmetric_no_double_emission",
        fp_mode="FP-5",
        description=(
            "adjacent_to is symmetric — must dedup A↔B (lex-sorted src<dst). "
            "Two primitives 10px apart → exactly 1 edge, not 2."
        ),
        generator_target="adjacent_to",
        primitives=[
            _prim("aaa", [100, 100, 105, 105]),
            _prim("bbb", [110, 100, 115, 105]),
        ],
        expected_verified_count=1,
    ),

    FPFixture(
        name="fp6_far_distance_must_be_ur_not_verified",
        fp_mode="FP-6",
        description=(
            "Calibration check — primitive + horizontal span at 78%-of-radius "
            "distance. Confidence must land in ANNOTATED_BY ambiguity band "
            "(0.3 ≤ conf < 0.5) → UR. NOT VERIFIED."
        ),
        generator_target="annotated_by",
        primitives=[_prim("p1", [100, 100, 102, 102])],
        ocr_spans=[_span("ocr-far", [158, 99, 168, 103], "A-101")],
        expected_ur_count=1,
        expected_verified_count=0,
    ),

    FPFixture(
        name="fp7_empty_ocr_text_must_not_emit",
        fp_mode="FP-7",
        description=(
            "OCR span with empty text — no semantic content. Generator must "
            "not emit annotated_by because the evidence schema requires "
            "ocr_text, but if it does, audit flags INCONCLUSIVE (can't verify "
            "empty annotations)."
        ),
        generator_target="annotated_by",
        primitives=[_prim("p1", [100, 100, 105, 105])],
        ocr_spans=[_span("ocr-empty", [110, 99, 120, 103], "")],
        # Validator requires ocr_text in evidence; framework's
        # validate_semantic should reject the edge before it's emitted.
        expected_no_edges=True,
    ),
]


def fixtures_by_target(target: str) -> list[FPFixture]:
    """Return all fixtures whose generator_target matches."""
    return [f for f in FP_FIXTURES if f.generator_target == target]
