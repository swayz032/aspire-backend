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

    # validate_edge — compatible verifies. NOTE: with M-4 now in the
    # registry, a FEEDS edge also needs fixture context. Provide a
    # M-4-compatible pair (Panel→Conduit; Conduit is DISTRIBUTION so
    # direction is waived per Option C).
    try:
        ev = {
            "flow_direction": "downstream",
            "discipline": "electrical",
            "src_legend_label": "PANEL",
            "dst_legend_label": "CONDUIT",
        }
        prov = {
            "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
            "sheet_id": "s1", "generator_window": "test",
            "bbox": [0, 0, 10, 10],
            "source_discipline": "electrical",
            "destination_discipline": "electrical",
            "source_layer_state": "new",
            "destination_layer_state": "new",
        }
        proposal = EdgeProposal(
            edge_type=EdgeType.FEEDS,
            suite_id="s", project_id="p",
            src_entity_id="p1", src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id="p2", dst_entity_type=EntityType.PRIMITIVE,
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=None, confidence=0.95,
            evidence=ev, provenance=prov,
            extraction_method="test_v1",
        )
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.VERIFIED, (
            f"got {r.resolution_state}; soft={r.constraint.details.get('soft_violations')}"
        )
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

    # =====================================================
    # M-4 ConnectivityConsistencyConstraint
    # =====================================================
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints import (
        ConnectivityConsistencyConstraint, FixtureRole, FlowStream,
        classify_fixture,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints.fixture_class import (
        SUPPLY_STREAMS, RETURN_STREAMS, role_compatible_for_feeds,
    )

    def make_mep_edge(*, edge_type=EdgeType.FEEDS, src_label=None,
                       dst_label=None, src_role=None, dst_role=None,
                       src_streams=None, dst_streams=None,
                       confidence=0.85,
                       src_discipline="plumbing", dst_discipline="plumbing"):
        provenance = {
            "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
            "sheet_id": "s1", "generator_window": "test",
            "bbox": [0, 0, 10, 10],
            # Bypass M-2 by setting compatible disciplines
            "source_discipline": src_discipline,
            "destination_discipline": dst_discipline,
        }
        if src_role:
            provenance["src_fixture_role"] = src_role
        if dst_role:
            provenance["dst_fixture_role"] = dst_role
        if src_streams:
            provenance["src_streams"] = src_streams
        if dst_streams:
            provenance["dst_streams"] = dst_streams
        evidence = {"flow_direction": "downstream", "discipline": "plumbing"}
        if src_label:
            evidence["src_legend_label"] = src_label
        if dst_label:
            evidence["dst_legend_label"] = dst_label
        return EdgeProposal(
            edge_type=edge_type,
            suite_id="s", project_id="p",
            src_entity_id="p1", src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id="p2", dst_entity_type=EntityType.PRIMITIVE,
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=None, confidence=confidence,
            evidence=evidence, provenance=provenance,
            extraction_method="test_v1",
        )

    # Fixture classifier — coverage check
    try:
        p = classify_fixture("QUAD RECEPTACLE")
        assert p is not None
        assert FixtureRole.SUPPLY_TERMINAL in p.roles
        assert FlowStream.POWER_HIGH_VOLT in p.streams

        p = classify_fixture("EXISTING UPRIGHT SPRINKLER")
        assert p is not None
        assert FixtureRole.SUPPLY_TERMINAL in p.roles
        assert FlowStream.SUPPLY_WATER_COLD in p.streams

        p = classify_fixture("RETURN GRILLE")
        assert p is not None
        assert FixtureRole.RETURN_TERMINAL in p.roles
        assert FlowStream.RETURN_AIR in p.streams

        p = classify_fixture("FLOOR DRAIN")
        assert p is not None
        assert FixtureRole.RETURN_TERMINAL in p.roles
        assert FlowStream.WASTE_WATER in p.streams

        p = classify_fixture("LAVATORY SINK")  # matches LAVATORY first
        assert p is not None
        assert FixtureRole.SUPPLY_TERMINAL in p.roles
        assert FixtureRole.RETURN_TERMINAL in p.roles
        assert FlowStream.SUPPLY_WATER_HOT in p.streams
        assert FlowStream.WASTE_WATER in p.streams

        assert classify_fixture("RANDOM-NON-MEP-TEXT") is None
        assert classify_fixture("") is None
        assert classify_fixture(None) is None
        results["fixture_classifier"] = "PASS"
    except Exception as e:
        results["fixture_classifier"] = f"FAIL: {e}"

    cc = ConnectivityConsistencyConstraint()

    # Compatible FEEDS — Panel feeds Receptacle
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.FEEDS,
            src_label="PANEL", dst_label="RECEPTACLE",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is True, f"got {result.reason}"
        results["connectivity_panel_feeds_receptacle_passes"] = "PASS"
    except Exception as e:
        results["connectivity_panel_feeds_receptacle_passes"] = f"FAIL: {e}"

    # Stream-disjoint FEEDS — Supply Duct FEEDS Return Grille
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.FEEDS,
            src_label="SUPPLY DUCT", dst_label="RETURN GRILLE",
            src_discipline="mechanical", dst_discipline="mechanical",
        )
        result = cc.check(proposal)
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert "feeds_streams_disjoint" in result.reason
        results["connectivity_supply_duct_to_return_grille_violation"] = "PASS"
    except Exception as e:
        results["connectivity_supply_duct_to_return_grille_violation"] = f"FAIL: {e}"

    # Stream-disjoint plumbing — Waste Pipe FEEDS Faucet (supply only)
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.FEEDS,
            src_label="WASTE PIPE", dst_label="FAUCET",
        )
        result = cc.check(proposal)
        assert result.passed is False
        assert "feeds_streams_disjoint" in result.reason
        results["connectivity_waste_to_supply_faucet_violation"] = "PASS"
    except Exception as e:
        results["connectivity_waste_to_supply_faucet_violation"] = f"FAIL: {e}"

    # Direction enforcement (Option C) — Receptacle FEEDS Panel (wrong direction)
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.FEEDS,
            src_label="RECEPTACLE", dst_label="PANEL",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is False
        assert "feeds_role_direction_wrong" in result.reason
        results["connectivity_direction_outlet_to_panel"] = "PASS"
    except Exception as e:
        results["connectivity_direction_outlet_to_panel"] = f"FAIL: {e}"

    # Option C: when DISTRIBUTION involved, direction NOT enforced
    # Panel FEEDS Conduit (DISTRIBUTION) — Conduit role unknown direction
    # Should PASS (Option C waives direction check)
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.FEEDS,
            src_label="PANEL", dst_label="CONDUIT",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is True
        results["connectivity_distribution_waives_direction"] = "PASS"
    except Exception as e:
        results["connectivity_distribution_waives_direction"] = f"FAIL: {e}"

    # PROTECTED_BY — outlet protected by breaker
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.PROTECTED_BY,
            src_label="RECEPTACLE", dst_label="BREAKER",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is True
        results["connectivity_protected_by_breaker_passes"] = "PASS"
    except Exception as e:
        results["connectivity_protected_by_breaker_passes"] = f"FAIL: {e}"

    # PROTECTED_BY violation — dst is NOT a protection device
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.PROTECTED_BY,
            src_label="RECEPTACLE", dst_label="LIGHT FIXTURE",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is False
        assert "protected_by_dst_not_protection" in result.reason
        results["connectivity_protected_by_invalid_dst"] = "PASS"
    except Exception as e:
        results["connectivity_protected_by_invalid_dst"] = f"FAIL: {e}"

    # CONTROLS — switch controls light fixture
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.CONTROLS,
            src_label="SWITCH", dst_label="LIGHT FIXTURE",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is True
        results["connectivity_switch_controls_light_passes"] = "PASS"
    except Exception as e:
        results["connectivity_switch_controls_light_passes"] = f"FAIL: {e}"

    # CONTROLS violation — src is not a CONTROL
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.CONTROLS,
            src_label="RECEPTACLE", dst_label="LIGHT FIXTURE",
            src_discipline="electrical", dst_discipline="electrical",
        )
        result = cc.check(proposal)
        assert result.passed is False
        assert "controls_src_not_control" in result.reason
        results["connectivity_controls_invalid_src"] = "PASS"
    except Exception as e:
        results["connectivity_controls_invalid_src"] = f"FAIL: {e}"

    # RETURNS_TO — Floor drain returns to grease trap
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.RETURNS_TO,
            src_label="FLOOR DRAIN", dst_label="GREASE TRAP",
        )
        result = cc.check(proposal)
        assert result.passed is True
        results["connectivity_returns_floor_drain_to_grease_trap"] = "PASS"
    except Exception as e:
        results["connectivity_returns_floor_drain_to_grease_trap"] = f"FAIL: {e}"

    # Missing context → SOFT_SKIP
    try:
        proposal = make_mep_edge(edge_type=EdgeType.FEEDS)  # no labels
        result = cc.check(proposal)
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert result.reason == "missing_fixture_context"
        results["connectivity_missing_context_soft_skip"] = "PASS"
    except Exception as e:
        results["connectivity_missing_context_soft_skip"] = f"FAIL: {e}"

    # Partial context → SOFT_SKIP
    try:
        proposal = make_mep_edge(edge_type=EdgeType.FEEDS, src_label="PANEL")
        result = cc.check(proposal)
        assert result.passed is False
        assert result.reason == "partial_fixture_context"
        results["connectivity_partial_context_soft_skip"] = "PASS"
    except Exception as e:
        results["connectivity_partial_context_soft_skip"] = f"FAIL: {e}"

    # validate_edge integration — wrong direction at high confidence → UR
    try:
        proposal = make_mep_edge(
            edge_type=EdgeType.FEEDS,
            src_label="RECEPTACLE", dst_label="PANEL",
            src_discipline="electrical", dst_discipline="electrical",
            confidence=0.95,
        )
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.UNKNOWN_REVIEW
        soft_v = r.constraint.details.get("soft_violations", [])
        assert any(v["name"] == "connectivity_consistency" for v in soft_v)
        results["connectivity_validate_edge_downgrades_wrong_direction"] = "PASS"
    except Exception as e:
        results["connectivity_validate_edge_downgrades_wrong_direction"] = f"FAIL: {e}"

    # Registry now 3 constraints
    try:
        names = {c.name for c in CONSTRAINT_REGISTRY}
        assert "discipline_compatibility" in names
        assert "layer_separation" in names
        assert "connectivity_consistency" in names
        results["connectivity_registry_updated"] = "PASS"
    except Exception as e:
        results["connectivity_registry_updated"] = f"FAIL: {e}"

    # applies_to scoping for M-4
    try:
        assert EdgeType.FEEDS in cc.applies_to
        assert EdgeType.RETURNS_TO in cc.applies_to
        assert EdgeType.CONNECTED_TO in cc.applies_to
        assert EdgeType.PROTECTED_BY in cc.applies_to
        assert EdgeType.CONTROLS in cc.applies_to
        assert EdgeType.TERMINATES_AT in cc.applies_to
        assert EdgeType.ANNOTATED_BY not in cc.applies_to
        assert EdgeType.ADJACENT_TO not in cc.applies_to
        assert EdgeType.BELONGS_TO_SHEET not in cc.applies_to
        results["connectivity_applies_to_correct"] = "PASS"
    except Exception as e:
        results["connectivity_applies_to_correct"] = f"FAIL: {e}"

    # =====================================================
    # M-5 SheetHierarchyConstraint
    # =====================================================
    from aspire_orchestrator.services.blueprint.semantic_edges.constraints import (
        SheetHierarchyConstraint, normalize_sheet_code, parse_callout,
    )

    def make_callout_edge(*, callout_text=None, sheet_registry=None,
                          target_sheet=None, edge_type=EdgeType.REFERENCES_DETAIL,
                          confidence=0.85):
        provenance = {
            "extractor": "test", "wave": "20.14.5M", "pipeline_version": "1.0",
            "sheet_id": "s1", "generator_window": "test",
            "bbox": [0, 0, 10, 10],
        }
        if sheet_registry is not None:
            provenance["project_sheet_codes"] = sheet_registry
        evidence: dict[str, object] = {
            "callout_text": callout_text or "",
            "target_sheet_code": target_sheet or "",
        }
        return EdgeProposal(
            edge_type=edge_type,
            suite_id="s", project_id="p",
            src_entity_id="p1", src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id="sheet-target",
            dst_entity_type=EntityType.SHEET,
            locality_scope=LocalityScope.CROSS_SHEET_REF,
            spatial_distance=None, confidence=confidence,
            evidence=evidence, provenance=provenance,
            extraction_method="test_v1",
        )

    # Callout parser — basic formats
    try:
        assert parse_callout("4/A-501") == ("4", "A-501")
        assert parse_callout("4/A501") == ("4", "A-501")  # normalized
        assert parse_callout("4-A-501") == ("4", "A-501")
        assert parse_callout("DETAIL 4 ON A-501") == ("4", "A-501")
        assert parse_callout("SEE DETAIL 4 / A-501") == ("4", "A-501")
        assert parse_callout("SEE A-501") == (None, "A-501")
        assert parse_callout("A-501/4") == ("4", "A-501")
        # alphanumeric sheet codes
        assert parse_callout("3/M101.1") == ("3", "M-101.1") or \
               parse_callout("3/M101.1") == ("3", "M101.1")
        assert parse_callout("no callout here") is None
        assert parse_callout("") is None
        assert parse_callout(None) is None
        results["sheet_callout_parser"] = "PASS"
    except Exception as e:
        results["sheet_callout_parser"] = f"FAIL: {e}"

    # Sheet code normalizer
    try:
        assert normalize_sheet_code("a-501") == "A-501"
        assert normalize_sheet_code("a501") == "A-501"
        assert normalize_sheet_code("M101.1") == "M101.1"  # decimal preserved
        assert normalize_sheet_code(" A-501 ") == "A-501"
        results["sheet_code_normalizer"] = "PASS"
    except Exception as e:
        results["sheet_code_normalizer"] = f"FAIL: {e}"

    sh = SheetHierarchyConstraint()

    # Valid callout — sheet exists in registry
    try:
        proposal = make_callout_edge(
            callout_text="4/A-501",
            sheet_registry=["A-101", "A-201", "A-501", "M-101"],
        )
        result = sh.check(proposal)
        assert result.passed is True, f"reason={result.reason}"
        results["sheet_valid_callout_passes"] = "PASS"
    except Exception as e:
        results["sheet_valid_callout_passes"] = f"FAIL: {e}"

    # Broken reference — sheet not in registry → SOFT violation (RFI candidate)
    try:
        proposal = make_callout_edge(
            callout_text="4/A-999",  # A-999 doesn't exist
            sheet_registry=["A-101", "A-201", "A-501"],
        )
        result = sh.check(proposal)
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert result.reason == "broken_sheet_reference"
        assert result.evidence.get("rfi_recommended") is True
        results["sheet_broken_reference_violation"] = "PASS"
    except Exception as e:
        results["sheet_broken_reference_violation"] = f"FAIL: {e}"

    # Mismatched binding — callout says A-501 but generator bound to A-502
    try:
        proposal = make_callout_edge(
            callout_text="4/A-501",
            sheet_registry=["A-101", "A-501", "A-502"],
            target_sheet="A-502",
        )
        result = sh.check(proposal)
        assert result.passed is False
        assert result.reason == "callout_dst_mismatch"
        results["sheet_dst_mismatch_violation"] = "PASS"
    except Exception as e:
        results["sheet_dst_mismatch_violation"] = f"FAIL: {e}"

    # Missing sheet registry → SOFT_SKIP
    try:
        proposal = make_callout_edge(callout_text="4/A-501")
        result = sh.check(proposal)
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert result.reason == "missing_sheet_registry"
        results["sheet_missing_registry_soft_skip"] = "PASS"
    except Exception as e:
        results["sheet_missing_registry_soft_skip"] = f"FAIL: {e}"

    # Missing callout text → SOFT_SKIP
    try:
        proposal = make_callout_edge(sheet_registry=["A-501"])
        result = sh.check(proposal)
        assert result.passed is False
        assert result.severity == ConstraintSeverity.SOFT
        assert result.reason == "missing_callout_text"
        results["sheet_missing_text_soft_skip"] = "PASS"
    except Exception as e:
        results["sheet_missing_text_soft_skip"] = f"FAIL: {e}"

    # Unrecognized format → SOFT_SKIP
    try:
        proposal = make_callout_edge(
            callout_text="some random gibberish 12345 xyz",
            sheet_registry=["A-501"],
        )
        result = sh.check(proposal)
        assert result.passed is False
        assert result.reason == "callout_text_format_unrecognized"
        results["sheet_unrecognized_format_soft_skip"] = "PASS"
    except Exception as e:
        results["sheet_unrecognized_format_soft_skip"] = f"FAIL: {e}"

    # validate_edge — broken reference at high conf → UR
    try:
        proposal = make_callout_edge(
            callout_text="4/A-999",
            sheet_registry=["A-101", "A-501"],
            confidence=0.95,
        )
        r = validate_edge(proposal)
        assert r.resolution_state == ResolutionState.UNKNOWN_REVIEW
        soft_v = r.constraint.details.get("soft_violations", [])
        assert any(v["name"] == "sheet_hierarchy" for v in soft_v)
        results["sheet_validate_edge_broken_ref_downgrades"] = "PASS"
    except Exception as e:
        results["sheet_validate_edge_broken_ref_downgrades"] = f"FAIL: {e}"

    # applies_to scoping
    try:
        assert EdgeType.REFERENCES_DETAIL in sh.applies_to
        assert EdgeType.CALLS_OUT in sh.applies_to
        assert EdgeType.BELONGS_TO_SHEET not in sh.applies_to
        assert EdgeType.ANNOTATED_BY not in sh.applies_to
        assert EdgeType.FEEDS not in sh.applies_to
        results["sheet_applies_to_correct"] = "PASS"
    except Exception as e:
        results["sheet_applies_to_correct"] = f"FAIL: {e}"

    # Registry now has 4 constraints
    try:
        names = {c.name for c in CONSTRAINT_REGISTRY}
        assert "discipline_compatibility" in names
        assert "layer_separation" in names
        assert "connectivity_consistency" in names
        assert "sheet_hierarchy" in names
        assert len(names) == 4
        results["sheet_registry_complete"] = "PASS"
    except Exception as e:
        results["sheet_registry_complete"] = f"FAIL: {e}"

    # --- Report ---
    print("=== M-1 + M-2 constraints smoke ===")
    n_pass = sum(1 for v in results.values() if v == "PASS")
    for name, v in results.items():
        print(f"  [{v[:4]}] {name}")
    print(f"\n{n_pass}/{len(results)} pass")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
