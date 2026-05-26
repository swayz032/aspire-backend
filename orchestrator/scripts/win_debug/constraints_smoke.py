"""Lightweight smoke test for M-1 + M-2 (Phase 4 discipline constraint). No scipy.
Bypasses 20-min pytest discovery on Windows. Run: `python scripts/win_debug/constraints_smoke.py`"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints import (
        CONSTRAINT_REGISTRY,
        ConstraintSeverity,
        DISCIPLINE_TOKENS,
        DisciplineCompatibilityConstraint,
        HARD_INCOMPATIBLE_DISCIPLINES,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints.discipline import (
        detect_discipline_from_text,
        disciplines_hard_incompatible,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.audit import (
        AuditVerdict, RuleBasedVerifier,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.taxonomy import (
        EdgeProposal, EdgeType, EntityType, LocalityScope,
        RejectionReason, ResolutionState, validate_edge,
    )

    def make_edge(*, edge_type=EdgeType.ANNOTATED_BY, src_disc=None, dst_disc=None,
                  ocr_text="A-101", confidence=0.85):
        provenance = {
            "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
            "sheet_id": "s1", "generator_window": "test", "bbox": [0, 0, 10, 10],
        }
        if src_disc:
            provenance["source_discipline"] = src_disc
        if dst_disc:
            provenance["destination_discipline"] = dst_disc
        return EdgeProposal(
            edge_type=edge_type,
            suite_id="s", project_id="p",
            src_entity_id="p-1", src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id="o-1",
            dst_entity_type=(EntityType.OCR_SPAN
                             if edge_type == EdgeType.ANNOTATED_BY
                             else EntityType.LEGEND),
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=10.0, confidence=confidence,
            evidence={"ocr_text": ocr_text, "distance_px": 10,
                      "alignment": "horizontal", "zone": "title_block"},
            provenance=provenance,
            extraction_method="test_v1",
        )

    results: dict[str, str] = {}

    # --- Static matrix ---
    try:
        for a, b in HARD_INCOMPATIBLE_DISCIPLINES:
            assert (b, a) in HARD_INCOMPATIBLE_DISCIPLINES
        assert ("plumbing", "electrical") in HARD_INCOMPATIBLE_DISCIPLINES
        assert ("plumbing", "fire") in HARD_INCOMPATIBLE_DISCIPLINES
        assert ("electrical", "fire") in HARD_INCOMPATIBLE_DISCIPLINES
        assert ("mechanical", "electrical") not in HARD_INCOMPATIBLE_DISCIPLINES
        results["matrix_symmetric_and_complete"] = "PASS"
    except Exception as e:
        results["matrix_symmetric_and_complete"] = f"FAIL: {e}"

    # --- helper functions ---
    try:
        assert disciplines_hard_incompatible("plumbing", "electrical")
        assert disciplines_hard_incompatible("electrical", "plumbing")
        assert not disciplines_hard_incompatible("mechanical", "electrical")
        assert not disciplines_hard_incompatible(None, "electrical")
        results["disciplines_hard_incompatible_fn"] = "PASS"
    except Exception as e:
        results["disciplines_hard_incompatible_fn"] = f"FAIL: {e}"

    try:
        assert detect_discipline_from_text("3/4 WASTE PIPE") == "plumbing"
        assert detect_discipline_from_text("ELECTRICAL PANEL P-1") == "electrical"
        assert detect_discipline_from_text("SPRINKLER HEAD QR-2") == "fire"
        assert detect_discipline_from_text("A-101") is None
        results["ocr_discipline_detection"] = "PASS"
    except Exception as e:
        results["ocr_discipline_detection"] = f"FAIL: {e}"

    # --- Constraint direct check ---
    c = DisciplineCompatibilityConstraint()
    try:
        result = c.check(make_edge(src_disc="plumbing", dst_disc="plumbing"))
        assert result.passed is True
        results["constraint_compatible_passes"] = "PASS"
    except Exception as e:
        results["constraint_compatible_passes"] = f"FAIL: {e}"

    try:
        result = c.check(make_edge(src_disc="plumbing", dst_disc="electrical"))
        assert result.passed is False
        assert result.severity == ConstraintSeverity.HARD
        assert "hard_incompatible" in result.reason
        results["constraint_hard_violation"] = "PASS"
    except Exception as e:
        results["constraint_hard_violation"] = f"FAIL: {e}"

    try:
        result = c.check(make_edge(
            src_disc="plumbing", dst_disc=None,
            ocr_text="ELECTRICAL PANEL P-1",
        ))
        assert result.passed is False
        assert result.severity == ConstraintSeverity.HARD
        assert "via_ocr" in result.reason
        results["constraint_ocr_inference_catches_mismatch"] = "PASS"
    except Exception as e:
        results["constraint_ocr_inference_catches_mismatch"] = f"FAIL: {e}"

    try:
        result = c.check(make_edge(src_disc=None, dst_disc=None, ocr_text="A-101"))
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        results["constraint_soft_skip_missing_context"] = "PASS"
    except Exception as e:
        results["constraint_soft_skip_missing_context"] = f"FAIL: {e}"

    # --- validate_edge integration ---
    try:
        proposal = make_edge(src_disc="plumbing", dst_disc="electrical",
                             confidence=0.95)
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.REJECTED
        assert RejectionReason.CONSTRAINT_VIOLATION in r.final_rejection_reasons
        results["validate_edge_hard_rejects"] = "PASS"
    except Exception as e:
        results["validate_edge_hard_rejects"] = f"FAIL: {e}"

    try:
        # SOFT downgrade: high confidence + missing context → UR
        proposal = make_edge(src_disc=None, dst_disc=None, confidence=0.95,
                             ocr_text="A-101")
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.UNKNOWN_REVIEW
        assert RejectionReason.CONSTRAINT_VIOLATION in r.final_rejection_reasons
        results["validate_edge_soft_downgrades_to_ur"] = "PASS"
    except Exception as e:
        results["validate_edge_soft_downgrades_to_ur"] = f"FAIL: {e}"

    try:
        proposal = make_edge(src_disc="plumbing", dst_disc="plumbing",
                             confidence=0.95)
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.VERIFIED
        results["validate_edge_compatible_verifies"] = "PASS"
    except Exception as e:
        results["validate_edge_compatible_verifies"] = f"FAIL: {e}"

    # --- Spatial edges skipped ---
    try:
        spatial = EdgeProposal(
            edge_type=EdgeType.ADJACENT_TO,
            suite_id="s", project_id="p",
            src_entity_id="aaa", src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id="bbb", dst_entity_type=EntityType.PRIMITIVE,
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=10.0, confidence=0.85,
            evidence={"distance_px": 10, "axis": "horizontal"},
            provenance={
                "extractor": "t", "wave": "20.14.5M", "pipeline_version": "1.0",
                "sheet_id": "s1", "generator_window": "t", "bbox": [0, 0, 10, 10],
            },
            extraction_method="test",
        )
        r = validate_edge(spatial)
        assert r.resolution_state == ResolutionState.VERIFIED
        assert "discipline_compatibility" not in r.constraint.details.get(
            "constraints_evaluated", []
        )
        results["spatial_edges_skip_discipline_check"] = "PASS"
    except Exception as e:
        results["spatial_edges_skip_discipline_check"] = f"FAIL: {e}"

    # --- L-6 RuleBasedVerifier still works + DRY ---
    try:
        v = RuleBasedVerifier()
        # Drift detector: same matrix instance
        assert v._discipline_tokens() is DISCIPLINE_TOKENS
        results["l6_verifier_uses_shared_matrix"] = "PASS"
    except Exception as e:
        results["l6_verifier_uses_shared_matrix"] = f"FAIL: {e}"

    try:
        v = RuleBasedVerifier()
        proposal = make_edge(src_disc="plumbing",
                             ocr_text="ELECTRICAL PANEL P-1", confidence=0.85)
        verdict, rationale = v.verify(proposal)
        assert verdict == AuditVerdict.FALSE_POSITIVE
        assert "discipline_mismatch" in rationale
        results["l6_verifier_still_catches_mismatch"] = "PASS"
    except Exception as e:
        results["l6_verifier_still_catches_mismatch"] = f"FAIL: {e}"
        traceback.print_exc()

    # --- Registry ---
    try:
        assert len(CONSTRAINT_REGISTRY) >= 2
        names = {c.name for c in CONSTRAINT_REGISTRY}
        assert "discipline_compatibility" in names
        assert "layer_separation" in names
        results["constraint_registry_loaded"] = "PASS"
    except Exception as e:
        results["constraint_registry_loaded"] = f"FAIL: {e}"

    # =====================================================
    # M-3 LayerSeparationConstraint
    # =====================================================
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints import (
        LayerSeparationConstraint, LayerState, classify_layer_name,
        classify_layer_text,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints.layer import (
        layer_states_incompatible,
    )

    def make_feeds_edge(*, src_state=None, dst_state=None, src_layer=None,
                       dst_layer=None, ocr_text=None, confidence=0.85):
        provenance = {
            "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
            "sheet_id": "s1", "generator_window": "test",
            "bbox": [0, 0, 10, 10],
            "source_discipline": "plumbing",
            "destination_discipline": "plumbing",
        }
        if src_state:
            provenance["source_layer_state"] = src_state
        if dst_state:
            provenance["destination_layer_state"] = dst_state
        evidence = {"flow_direction": "downstream", "discipline": "plumbing"}
        if src_layer:
            evidence["src_layer"] = src_layer
        if dst_layer:
            evidence["dst_layer"] = dst_layer
        if ocr_text:
            evidence["ocr_text"] = ocr_text
        return EdgeProposal(
            edge_type=EdgeType.FEEDS,
            suite_id="s", project_id="p",
            src_entity_id="p1", src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id="p2", dst_entity_type=EntityType.PRIMITIVE,
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=None, confidence=confidence,
            evidence=evidence, provenance=provenance,
            extraction_method="test_v1",
        )

    # CAD parser
    try:
        assert classify_layer_name("P-PIPE-DEMO") == LayerState.DEMO
        assert classify_layer_name("E-LITE-EXST") == LayerState.EXISTING
        assert classify_layer_name("P-PIPE-NEW") == LayerState.NEW
        assert classify_layer_name("DEMO-EXST") == LayerState.DEMO  # DEMO wins
        assert classify_layer_name("P-PIPE-RELO") == LayerState.RELOCATE
        assert classify_layer_name("RANDOM") == LayerState.UNKNOWN
        results["layer_cad_name_parser"] = "PASS"
    except Exception as e:
        results["layer_cad_name_parser"] = f"FAIL: {e}"

    # OCR shorthand
    try:
        assert classify_layer_text("(E)") == LayerState.EXISTING
        assert classify_layer_text("(D)") == LayerState.DEMO
        assert classify_layer_text("(N)") == LayerState.NEW
        assert classify_layer_text("T.B.D.") == LayerState.DEMO
        assert classify_layer_text("EXISTING WALL") == LayerState.EXISTING
        assert classify_layer_text("NEW WORK") == LayerState.NEW
        assert classify_layer_text("RELOCATE") == LayerState.RELOCATE
        results["layer_ocr_shorthand_parser"] = "PASS"
    except Exception as e:
        results["layer_ocr_shorthand_parser"] = f"FAIL: {e}"

    # Incompatibility logic
    try:
        assert layer_states_incompatible(LayerState.DEMO, LayerState.NEW)
        assert layer_states_incompatible(LayerState.NEW, LayerState.DEMO)
        assert layer_states_incompatible(LayerState.EXISTING, LayerState.NEW)
        assert not layer_states_incompatible(LayerState.NEW, LayerState.NEW)
        assert not layer_states_incompatible(LayerState.DEMO, LayerState.EXISTING)
        assert not layer_states_incompatible(LayerState.RELOCATE, LayerState.NEW)
        assert not layer_states_incompatible(LayerState.UNKNOWN, LayerState.NEW)
        results["layer_incompatibility_rules"] = "PASS"
    except Exception as e:
        results["layer_incompatibility_rules"] = f"FAIL: {e}"

    # Constraint direct check — SOFT violation
    lc = LayerSeparationConstraint()
    try:
        result = lc.check(make_feeds_edge(src_state="demo", dst_state="new"))
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert "incompatible_layers" in result.reason
        results["layer_demo_to_new_soft"] = "PASS"
    except Exception as e:
        results["layer_demo_to_new_soft"] = f"FAIL: {e}"

    # Compatible same-state
    try:
        result = lc.check(make_feeds_edge(src_state="new", dst_state="new"))
        assert result.passed is True
        results["layer_same_state_passes"] = "PASS"
    except Exception as e:
        results["layer_same_state_passes"] = f"FAIL: {e}"

    # DEMO ↔ EXISTING passes (both pre-bid scope)
    try:
        result = lc.check(make_feeds_edge(src_state="demo", dst_state="existing"))
        assert result.passed is True
        results["layer_demo_to_existing_passes"] = "PASS"
    except Exception as e:
        results["layer_demo_to_existing_passes"] = f"FAIL: {e}"

    # RELOCATE compatible with NEW
    try:
        result = lc.check(make_feeds_edge(src_state="relocate", dst_state="new"))
        assert result.passed is True
        results["layer_relocate_passes"] = "PASS"
    except Exception as e:
        results["layer_relocate_passes"] = f"FAIL: {e}"

    # CAD layer name fallback path
    try:
        result = lc.check(make_feeds_edge(
            src_layer="P-PIPE-DEMO", dst_layer="P-PIPE-NEW",
        ))
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        results["layer_cad_path_violation"] = "PASS"
    except Exception as e:
        results["layer_cad_path_violation"] = f"FAIL: {e}"

    # Missing context → SOFT_SKIP
    try:
        result = lc.check(make_feeds_edge())
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert result.reason == "missing_layer_context"
        results["layer_missing_context_soft_skip"] = "PASS"
    except Exception as e:
        results["layer_missing_context_soft_skip"] = f"FAIL: {e}"

    # validate_edge integration — DEMO→NEW at high conf → UR
    try:
        proposal = make_feeds_edge(src_state="demo", dst_state="new",
                                    confidence=0.95)
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.UNKNOWN_REVIEW
        soft_v = r.constraint.details.get("soft_violations", [])
        assert any(v["name"] == "layer_separation" for v in soft_v)
        results["layer_validate_edge_downgrades_to_ur"] = "PASS"
    except Exception as e:
        results["layer_validate_edge_downgrades_to_ur"] = f"FAIL: {e}"

    # validate_edge — compatible verifies
    try:
        proposal = make_feeds_edge(src_state="new", dst_state="new",
                                    confidence=0.95)
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.VERIFIED
        results["layer_validate_edge_compatible_verifies"] = "PASS"
    except Exception as e:
        results["layer_validate_edge_compatible_verifies"] = f"FAIL: {e}"

    # applies_to scoping — adjacent_to NOT in applies_to
    try:
        assert EdgeType.ADJACENT_TO not in lc.applies_to
        assert EdgeType.ANNOTATED_BY not in lc.applies_to
        assert EdgeType.INSIDE_ZONE not in lc.applies_to
        assert EdgeType.FEEDS in lc.applies_to
        assert EdgeType.MEMBER_OF_FAMILY in lc.applies_to
        results["layer_applies_to_correct"] = "PASS"
    except Exception as e:
        results["layer_applies_to_correct"] = f"FAIL: {e}"

    # --- Report ---
    print("=== M-1 + M-2 constraints smoke ===")
    n_pass = sum(1 for v in results.values() if v == "PASS")
    for name, v in results.items():
        print(f"  [{v[:4]}] {name}")
    print(f"\n{n_pass}/{len(results)} pass")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
