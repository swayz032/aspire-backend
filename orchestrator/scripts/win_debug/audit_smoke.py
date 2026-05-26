"""Lightweight smoke test for L-6 audit module. No scipy/numpy needed.
Bypasses 20-min pytest discovery on Windows by importing only the scipy-free modules.
Run: `python scripts/win_debug/audit_smoke.py`"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    from aspire_orchestrator.services.blueprint.semantic_edges.audit import (
        AuditVerdict, OracleVerifier, RuleBasedVerifier, audit_edges,
        serialize_report, _stable_sample,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.taxonomy import (
        EdgeProposal, EdgeType, EntityType, LocalityScope,
    )

    def make_edge(*, src="p-1", dst="o-1", confidence=0.85,
                  ocr_text="A-101", alignment="horizontal",
                  source_discipline=None):
        provenance = {
            "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
            "sheet_id": "sheet-1", "generator_window": "test", "bbox": [0, 0, 10, 10],
        }
        if source_discipline:
            provenance["source_discipline"] = source_discipline
        return EdgeProposal(
            edge_type=EdgeType.ANNOTATED_BY,
            suite_id="suite-1", project_id="proj-1",
            src_entity_id=src, src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id=dst, dst_entity_type=EntityType.OCR_SPAN,
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=10.0, confidence=confidence,
            evidence={"ocr_text": ocr_text, "distance_px": 10,
                      "alignment": alignment, "zone": "title_block"},
            provenance=provenance,
            extraction_method="test_v1",
        )

    results: dict[str, str] = {}

    # --- Determinism ---
    try:
        edges = [make_edge(src=f"p-{i:04d}") for i in range(50)]
        a = _stable_sample(edges, sample_size=10, seed=42)
        b = _stable_sample(list(reversed(edges)), sample_size=10, seed=42)
        assert {e.src_entity_id for e in a} == {e.src_entity_id for e in b}
        results["stable_sample_deterministic"] = "PASS"
    except Exception as e:
        results["stable_sample_deterministic"] = f"FAIL: {e}"
        traceback.print_exc()

    # --- RuleBasedVerifier catches N.I.C. ---
    try:
        edge = make_edge(confidence=0.85, ocr_text="NOT IN CONTRACT")
        verdict, rationale = RuleBasedVerifier().verify(edge)
        assert verdict == AuditVerdict.FALSE_POSITIVE, f"got {verdict}"
        results["rule_based_catches_nic"] = "PASS"
    except Exception as e:
        results["rule_based_catches_nic"] = f"FAIL: {e}"

    # --- RuleBasedVerifier credits UR-routed N.I.C. ---
    try:
        edge = make_edge(confidence=0.4, ocr_text="NOT IN CONTRACT")
        verdict, _ = RuleBasedVerifier().verify(edge)
        assert verdict == AuditVerdict.UR_CORRECT
        results["rule_based_ur_correct"] = "PASS"
    except Exception as e:
        results["rule_based_ur_correct"] = f"FAIL: {e}"

    # --- RuleBasedVerifier catches discipline mismatch ---
    try:
        edge = make_edge(
            confidence=0.85,
            ocr_text="ELECTRICAL PANEL P-1",
            source_discipline="plumbing",
        )
        verdict, rationale = RuleBasedVerifier().verify(edge)
        assert verdict == AuditVerdict.FALSE_POSITIVE, f"got {verdict} ({rationale})"
        assert "discipline" in rationale
        results["rule_based_discipline_mismatch"] = "PASS"
    except Exception as e:
        results["rule_based_discipline_mismatch"] = f"FAIL: {e}"

    # --- RuleBasedVerifier passes aligned discipline ---
    try:
        edge = make_edge(
            confidence=0.85,
            ocr_text="3/4 WASTE PIPE",
            source_discipline="plumbing",
        )
        verdict, _ = RuleBasedVerifier().verify(edge)
        assert verdict == AuditVerdict.TRUE_POSITIVE
        results["rule_based_passes_aligned"] = "PASS"
    except Exception as e:
        results["rule_based_passes_aligned"] = f"FAIL: {e}"

    # --- RuleBasedVerifier flags diagonal+high-conf ---
    try:
        edge = make_edge(confidence=0.85, alignment="diagonal")
        verdict, _ = RuleBasedVerifier().verify(edge)
        assert verdict == AuditVerdict.UR_MISSED
        results["rule_based_diagonal_ur_missed"] = "PASS"
    except Exception as e:
        results["rule_based_diagonal_ur_missed"] = f"FAIL: {e}"

    # --- audit_edges report aggregation ---
    try:
        edges = [
            make_edge(src="p-1", dst="o-1"),
            make_edge(src="p-2", dst="o-2"),
            make_edge(src="p-3", dst="o-3"),
        ]
        truth = {
            ("p-1", "o-1", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE,
            ("p-2", "o-2", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE,
            ("p-3", "o-3", EdgeType.ANNOTATED_BY): AuditVerdict.FALSE_POSITIVE,
        }
        report = audit_edges(
            edges,
            verifier=OracleVerifier(truth),
            generator_name="annotated_by", generator_version="1.0",
            project_id="proj-1", sample_size=10, threshold=0.90,
        )
        bucket = report.per_type[EdgeType.ANNOTATED_BY]
        assert bucket.n_true_positive == 2 and bucket.n_false_positive == 1
        assert abs(bucket.precision - 2/3) < 0.001
        assert report.passes_threshold() is False
        assert "annotated_by" in report.failing_types()
        results["audit_report_aggregation"] = "PASS"
    except Exception as e:
        results["audit_report_aggregation"] = f"FAIL: {e}"
        traceback.print_exc()

    # --- Empty audit vacuously passes ---
    try:
        report = audit_edges(
            [], verifier=OracleVerifier({}),
            generator_name="annotated_by", generator_version="1.0",
            project_id="proj-1",
        )
        assert report.passes_threshold() is True
        assert report.sample_size == 0
        results["audit_empty_vacuous_pass"] = "PASS"
    except Exception as e:
        results["audit_empty_vacuous_pass"] = f"FAIL: {e}"

    # --- Serialization ---
    try:
        report = audit_edges(
            [make_edge()], verifier=OracleVerifier({}),
            generator_name="annotated_by", generator_version="1.0",
            project_id="proj-1",
        )
        import json
        parsed = json.loads(serialize_report(report))
        assert "audit_id" in parsed and "passes_threshold" in parsed
        results["audit_serialization"] = "PASS"
    except Exception as e:
        results["audit_serialization"] = f"FAIL: {e}"

    # --- FP_FIXTURES sanity ---
    try:
        from aspire_orchestrator.services.blueprint.semantic_edges.fp_regression import (
            FP_FIXTURES, fixtures_by_target,
        )
        assert len(FP_FIXTURES) >= 7
        assert len(fixtures_by_target("annotated_by")) >= 3
        # Every fixture has at least one expectation
        for fx in FP_FIXTURES:
            has_expectation = (
                fx.expected_no_edges
                or fx.expected_ur_count is not None
                or fx.expected_verified_count is not None
                or fx.expected_verdict_for_first_edge is not None
            )
            assert has_expectation, f"{fx.name}: no expectation set"
        results["fp_fixtures_well_formed"] = "PASS"
    except Exception as e:
        results["fp_fixtures_well_formed"] = f"FAIL: {e}"

    # --- Report ---
    print("=== L-6 audit smoke test ===")
    n_pass = sum(1 for v in results.values() if v == "PASS")
    for name, v in results.items():
        print(f"  [{v[:4]}] {name}")
    print(f"\n{n_pass}/{len(results)} pass")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
