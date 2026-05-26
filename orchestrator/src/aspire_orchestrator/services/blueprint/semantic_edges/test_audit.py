"""L-6 unit tests — audit harness machinery.

These verify the AUDITOR ITSELF works correctly. The FP regression
suite that exercises real generators against adversarial fixtures
lives in test_fp_regression.py (it imports scipy via the generators
so it's slower; this file is scipy-free).
"""
from __future__ import annotations

import pytest

from .audit import (
    AuditReport,
    AuditVerdict,
    OracleVerifier,
    PerTypeAudit,
    RuleBasedVerifier,
    _stable_sample,
    audit_edges,
    serialize_report,
)
from .taxonomy import (
    EdgeProposal,
    EdgeType,
    EntityType,
    LocalityScope,
)


# ============================================================
# Fixtures
# ============================================================


def _proposal(
    *,
    edge_type: EdgeType = EdgeType.ANNOTATED_BY,
    src_id: str = "p-1",
    dst_id: str = "o-1",
    confidence: float = 0.85,
    evidence: dict | None = None,
    provenance: dict | None = None,
) -> EdgeProposal:
    return EdgeProposal(
        edge_type=edge_type,
        suite_id="suite-1",
        project_id="proj-1",
        src_entity_id=src_id,
        src_entity_type=EntityType.PRIMITIVE,
        dst_entity_id=dst_id,
        dst_entity_type=EntityType.OCR_SPAN,
        locality_scope=LocalityScope.SAME_SHEET,
        spatial_distance=10.0,
        confidence=confidence,
        evidence=evidence or {
            "ocr_text": "A-101", "distance_px": 10, "alignment": "horizontal",
            "zone": "title_block",
        },
        provenance=provenance or {
            "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
            "sheet_id": "sheet-1", "generator_window": "test", "bbox": [0, 0, 10, 10],
        },
        extraction_method="test_v1",
    )


# ============================================================
# Sampling determinism
# ============================================================


def test_stable_sample_is_deterministic_under_same_seed():
    edges = [_proposal(src_id=f"p-{i:04d}") for i in range(100)]
    a = _stable_sample(edges, sample_size=20, seed=42)
    b = _stable_sample(edges, sample_size=20, seed=42)
    assert [(e.src_entity_id, e.dst_entity_id) for e in a] == \
           [(e.src_entity_id, e.dst_entity_id) for e in b]


def test_stable_sample_unaffected_by_input_order():
    forward = [_proposal(src_id=f"p-{i:04d}") for i in range(50)]
    reversed_ = list(reversed(forward))
    a = _stable_sample(forward, sample_size=10, seed=7)
    b = _stable_sample(reversed_, sample_size=10, seed=7)
    assert sorted(e.src_entity_id for e in a) == sorted(e.src_entity_id for e in b)


def test_stable_sample_returns_all_when_n_less_than_size():
    edges = [_proposal(src_id=f"p-{i}") for i in range(5)]
    sampled = _stable_sample(edges, sample_size=20, seed=0)
    assert len(sampled) == 5


# ============================================================
# OracleVerifier
# ============================================================


def test_oracle_verifier_returns_curated_verdict():
    truth = {
        ("p-1", "o-1", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE,
        ("p-2", "o-2", EdgeType.ANNOTATED_BY): AuditVerdict.FALSE_POSITIVE,
    }
    verifier = OracleVerifier(truth)
    v1, _ = verifier.verify(_proposal(src_id="p-1", dst_id="o-1"))
    v2, _ = verifier.verify(_proposal(src_id="p-2", dst_id="o-2"))
    assert v1 == AuditVerdict.TRUE_POSITIVE
    assert v2 == AuditVerdict.FALSE_POSITIVE


def test_oracle_verifier_returns_inconclusive_for_unknown_edge():
    verifier = OracleVerifier({})
    v, _ = verifier.verify(_proposal())
    assert v == AuditVerdict.INCONCLUSIVE


# ============================================================
# RuleBasedVerifier — Wave 20.13.3 hallucination catchers
# ============================================================


def test_rule_based_catches_not_in_contract_high_confidence():
    """N.I.C. text at high confidence → FALSE_POSITIVE."""
    edge = _proposal(
        confidence=0.85,
        evidence={"ocr_text": "NOT IN CONTRACT", "distance_px": 5,
                  "alignment": "horizontal", "zone": "title_block"},
    )
    verdict, rationale = RuleBasedVerifier().verify(edge)
    assert verdict == AuditVerdict.FALSE_POSITIVE
    assert "blacklisted" in rationale


def test_rule_based_credits_nic_routed_to_ur():
    """Same N.I.C. text BUT routed to UR (confidence below floor) → UR_CORRECT."""
    edge = _proposal(
        confidence=0.4,
        evidence={"ocr_text": "NOT IN CONTRACT", "distance_px": 60,
                  "alignment": "horizontal", "zone": "title_block"},
    )
    verdict, rationale = RuleBasedVerifier().verify(edge)
    assert verdict == AuditVerdict.UR_CORRECT


def test_rule_based_catches_discipline_mismatch():
    """Plumbing primitive labeled by electrical-discipline OCR → FALSE_POSITIVE."""
    edge = _proposal(
        confidence=0.85,
        evidence={"ocr_text": "ELECTRICAL PANEL P-1", "distance_px": 10,
                  "alignment": "horizontal", "zone": "title_block"},
        provenance={
            "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
            "sheet_id": "sheet-1", "generator_window": "test", "bbox": [0, 0, 10, 10],
            "source_discipline": "plumbing",
        },
    )
    verdict, rationale = RuleBasedVerifier().verify(edge)
    assert verdict == AuditVerdict.FALSE_POSITIVE
    assert "discipline_mismatch" in rationale


def test_rule_based_passes_aligned_discipline_match():
    """Plumbing primitive labeled by plumbing OCR → TRUE_POSITIVE."""
    edge = _proposal(
        confidence=0.85,
        evidence={"ocr_text": "3/4 WASTE PIPE", "distance_px": 10,
                  "alignment": "horizontal", "zone": "title_block"},
        provenance={
            "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
            "sheet_id": "sheet-1", "generator_window": "test", "bbox": [0, 0, 10, 10],
            "source_discipline": "plumbing",
        },
    )
    verdict, _ = RuleBasedVerifier().verify(edge)
    assert verdict == AuditVerdict.TRUE_POSITIVE


def test_rule_based_flags_diagonal_high_confidence_as_ur_missed():
    """Diagonal alignment + high confidence = UR_missed (should have been UR)."""
    edge = _proposal(
        confidence=0.85,
        evidence={"ocr_text": "A-101", "distance_px": 30,
                  "alignment": "diagonal", "zone": "title_block"},
    )
    verdict, rationale = RuleBasedVerifier().verify(edge)
    assert verdict == AuditVerdict.UR_MISSED
    assert "diagonal" in rationale


def test_rule_based_inconclusive_for_unsupported_edge_type():
    edge = _proposal(edge_type=EdgeType.BELONGS_TO_SHEET,
                     evidence={"page_index": 1})
    verdict, _ = RuleBasedVerifier().verify(edge)
    assert verdict == AuditVerdict.INCONCLUSIVE


# ============================================================
# audit_edges report aggregation
# ============================================================


def test_audit_report_per_type_aggregates_correctly():
    edges = [
        _proposal(src_id="p-1", dst_id="o-1"),
        _proposal(src_id="p-2", dst_id="o-2"),
        _proposal(src_id="p-3", dst_id="o-3"),
    ]
    truth = {
        ("p-1", "o-1", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE,
        ("p-2", "o-2", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE,
        ("p-3", "o-3", EdgeType.ANNOTATED_BY): AuditVerdict.FALSE_POSITIVE,
    }
    report = audit_edges(
        edges,
        verifier=OracleVerifier(truth),
        generator_name="annotated_by",
        generator_version="1.0",
        project_id="proj-1",
        sample_size=10,
        threshold=0.90,
    )
    assert report.sample_size == 3
    per_type = report.per_type[EdgeType.ANNOTATED_BY]
    assert per_type.n_true_positive == 2
    assert per_type.n_false_positive == 1
    # precision = 2 / (2 + 1) = 0.667
    assert abs(per_type.precision - 2 / 3) < 0.001
    assert report.passes_threshold() is False
    assert "annotated_by" in report.failing_types()


def test_audit_report_passes_threshold_with_perfect_precision():
    edges = [_proposal(src_id=f"p-{i}", dst_id=f"o-{i}") for i in range(10)]
    truth = {
        (f"p-{i}", f"o-{i}", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE
        for i in range(10)
    }
    report = audit_edges(
        edges,
        verifier=OracleVerifier(truth),
        generator_name="annotated_by",
        generator_version="1.0",
        project_id="proj-1",
        sample_size=10,
    )
    assert report.passes_threshold() is True
    assert report.per_type[EdgeType.ANNOTATED_BY].precision == 1.0


def test_audit_report_excludes_ur_correct_and_inconclusive_from_precision():
    """UR_correct + INCONCLUSIVE are NOT denominators in precision."""
    edges = [
        _proposal(src_id="p-1", dst_id="o-1"),
        _proposal(src_id="p-2", dst_id="o-2"),
        _proposal(src_id="p-3", dst_id="o-3"),
    ]
    truth = {
        ("p-1", "o-1", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE,
        ("p-2", "o-2", EdgeType.ANNOTATED_BY): AuditVerdict.UR_CORRECT,
        # p-3 left out → INCONCLUSIVE
    }
    report = audit_edges(
        edges,
        verifier=OracleVerifier(truth),
        generator_name="annotated_by",
        generator_version="1.0",
        project_id="proj-1",
        sample_size=10,
    )
    pt = report.per_type[EdgeType.ANNOTATED_BY]
    # precision = TP / (TP + FP + UR_missed) = 1 / (1 + 0 + 0) = 1.0
    assert pt.precision == 1.0
    assert pt.n_inconclusive == 1
    assert pt.n_ur_correct == 1


def test_audit_report_audit_id_stable_across_runs():
    edges = [_proposal(src_id=f"p-{i}") for i in range(20)]
    truth = {(f"p-{i}", "o-1", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE
             for i in range(20)}
    r1 = audit_edges(edges, verifier=OracleVerifier(truth),
                     generator_name="annotated_by", generator_version="1.0",
                     project_id="proj-1", sample_size=10, seed=42)
    r2 = audit_edges(edges, verifier=OracleVerifier(truth),
                     generator_name="annotated_by", generator_version="1.0",
                     project_id="proj-1", sample_size=10, seed=42)
    assert r1.audit_id == r2.audit_id


def test_audit_report_serializes_to_json():
    edges = [_proposal()]
    report = audit_edges(edges, verifier=OracleVerifier({}),
                        generator_name="annotated_by", generator_version="1.0",
                        project_id="proj-1")
    j = serialize_report(report)
    import json as _json
    parsed = _json.loads(j)
    assert "audit_id" in parsed
    assert parsed["generator_name"] == "annotated_by"


def test_passes_threshold_vacuously_true_when_nothing_audited():
    """No sampled edges → passes (nothing to fail)."""
    report = audit_edges(
        [],
        verifier=OracleVerifier({}),
        generator_name="annotated_by",
        generator_version="1.0",
        project_id="proj-1",
    )
    assert report.passes_threshold() is True
    assert report.sample_size == 0
