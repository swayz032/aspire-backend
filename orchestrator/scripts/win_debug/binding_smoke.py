"""Lightweight smoke test for Phase 5 N-1 + N-2 binding evaluator. No scipy.
Bypasses 20-min pytest discovery on Windows. Run:
    python scripts/win_debug/binding_smoke.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    from aspire_orchestrator.services.blueprint.semantic_edges.binding import (
        BindingEvaluator, BindingProposal, BindingTier, BindingWeights,
        GeometryClass, HardGateReason, MotifGeometryFeatures,
        OCRAffinityScorer, ScorerResult,
        UNKNOWN_REVIEW_THRESHOLD, VERIFIED_THRESHOLD,
        classify_legend_geometry, classify_motif_geometry,
        geometry_gate_compatible, map_tier,
    )

    UID_SUITE = "00000000-0000-0000-0000-000000000001"
    UID_PROJECT = "00000000-0000-0000-0000-000000000002"

    def make_proposal(
        *,
        motif_discipline=None, legend_discipline=None,
        motif_geom="point", legend_geom="point",
        ocr_texts=(),
        legend_label=None, legend_symbol_id=None,
        legend_superseded=False,
        legend_project_id=None,
    ):
        return BindingProposal(
            suite_id=UID_SUITE, project_id=UID_PROJECT,
            motif_id="motif-1", motif_entity_type="composed_symbol",
            motif_sheet_id="sheet-1",
            motif_entity_geometry=motif_geom,
            motif_discipline=motif_discipline,
            motif_nearby_ocr_text=tuple(ocr_texts),
            legend_id="legend-1",
            legend_label=legend_label,
            legend_symbol_id=legend_symbol_id,
            legend_discipline=legend_discipline,
            legend_entity_geometry=legend_geom,
            legend_project_id=legend_project_id,
            legend_superseded=legend_superseded,
        )

    results: dict[str, str] = {}

    # ---- Thresholds locked ----
    try:
        assert VERIFIED_THRESHOLD == 0.70
        assert UNKNOWN_REVIEW_THRESHOLD == 0.50
        results["thresholds_locked"] = "PASS"
    except Exception as e:
        results["thresholds_locked"] = f"FAIL: {e}"

    # ---- map_tier ----
    try:
        assert map_tier(0.95) == BindingTier.VERIFIED
        assert map_tier(0.70) == BindingTier.VERIFIED
        assert map_tier(0.69) == BindingTier.UNKNOWN_REVIEW
        assert map_tier(0.50) == BindingTier.UNKNOWN_REVIEW
        assert map_tier(0.49) == BindingTier.REJECTED
        assert map_tier(0.0) == BindingTier.REJECTED
        results["tier_mapping_thresholds"] = "PASS"
    except Exception as e:
        results["tier_mapping_thresholds"] = f"FAIL: {e}"

    # ---- BindingProposal geometry validation ----
    try:
        try:
            BindingProposal(
                suite_id=UID_SUITE, project_id=UID_PROJECT,
                motif_id="m", motif_entity_type="primitive",
                motif_sheet_id="s",
                motif_entity_geometry="bogus_geometry",
            )
            results["proposal_geometry_validation"] = "FAIL: accepted bogus geometry"
        except ValueError:
            results["proposal_geometry_validation"] = "PASS"
    except Exception as e:
        results["proposal_geometry_validation"] = f"FAIL: {e}"

    # ---- OCRAffinityScorer: exact symbol_id match → 1.0 ----
    scorer = OCRAffinityScorer()
    try:
        p = make_proposal(
            ocr_texts=("QR-1",),
            legend_symbol_id="QR-1", legend_label="QR Pendant Sprinkler",
        )
        r = scorer.compute(p)
        assert r.score == 1.0
        assert r.reasoning == "exact_symbol_id_match"
        results["ocr_exact_symbol_id_match"] = "PASS"
    except Exception as e:
        results["ocr_exact_symbol_id_match"] = f"FAIL: {e}"

    # ---- OCRAffinityScorer: exact label match → 1.0 ----
    try:
        p = make_proposal(
            ocr_texts=("QR PENDANT SPRINKLER",),
            legend_symbol_id="QR-1", legend_label="QR Pendant Sprinkler",
        )
        r = scorer.compute(p)
        assert r.score == 1.0
        assert r.reasoning == "exact_label_match"
        results["ocr_exact_label_match"] = "PASS"
    except Exception as e:
        results["ocr_exact_label_match"] = f"FAIL: {e}"

    # ---- substring: symbol_id appears INSIDE OCR text → 0.85 ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1 EXISTING UPRIGHT",),  # symbol_id inside OCR
            legend_symbol_id="QR-1", legend_label="QR Sprinkler",
        )
        r = scorer.compute(p)
        # Should hit substring band, not label match (label != OCR text)
        assert r.score == 0.85, f"got {r.score} ({r.reasoning})"
        assert "substring" in r.reasoning
        results["ocr_substring_band"] = "PASS"
    except Exception as e:
        results["ocr_substring_band"] = f"FAIL: {e}"

    # ---- token match band — 0.70 (post-tiny-id-guard) ----
    try:
        # symbol_id "FCO PIPE" tokenizes to {FCO, PIPE} — both len>=3.
        # OCR text contains "FCO" → token-band hits at 0.70.
        p = make_proposal(
            ocr_texts=("FCO PROTOTYPE",),
            legend_symbol_id="FCO PIPE", legend_label="something else",
        )
        r = scorer.compute(p)
        assert r.score == 0.70, f"got {r.score} ({r.reasoning})"
        results["ocr_token_band"] = "PASS"
    except Exception as e:
        results["ocr_token_band"] = f"FAIL: {e}"

    # ---- token band DROPS 2-char ids (tiny-id guard) ----
    try:
        # symbol_id "QR" — len=2 < MIN_TOKEN_LEN. Substring band must
        # NOT fire; token band tokenizes to {} (QR dropped). Result: 0.0
        p = make_proposal(
            ocr_texts=("QR-1 EXISTING",),
            legend_symbol_id="QR", legend_label=None,
        )
        r = scorer.compute(p)
        assert r.score == 0.0, f"got {r.score} ({r.reasoning})"
        results["ocr_tiny_id_guard_drops_2char"] = "PASS"
    except Exception as e:
        results["ocr_tiny_id_guard_drops_2char"] = f"FAIL: {e}"

    # ---- substring band still works for len>=3 ----
    try:
        p = make_proposal(
            ocr_texts=("FOOBAR EXISTING",),
            legend_symbol_id="BAR", legend_label="STUFF",
        )
        r = scorer.compute(p)
        assert r.score == 0.85, f"got {r.score} ({r.reasoning})"
        assert "substring" in r.reasoning
        results["ocr_substring_band_len3_ok"] = "PASS"
    except Exception as e:
        results["ocr_substring_band_len3_ok"] = f"FAIL: {e}"

    # ---- keyword-overlap band (post-guard) ----
    try:
        # label="PENDANT SPRINKLER" -> tokens {PENDANT, SPRINKLER}, both >=3
        # OCR "PENDANT HEAD" -> tokens {PENDANT, HEAD}; shared={PENDANT}; ratio=1/2
        # → 0.40 + 0.20*0.5 = 0.50
        p = make_proposal(
            ocr_texts=("PENDANT HEAD",),
            legend_symbol_id="UNRELATED", legend_label="PENDANT SPRINKLER",
        )
        r = scorer.compute(p)
        assert 0.49 <= r.score <= 0.51, f"got {r.score} ({r.reasoning})"
        results["ocr_keyword_overlap_band"] = "PASS"
    except Exception as e:
        results["ocr_keyword_overlap_band"] = f"FAIL: {e}"

    # ---- no overlap → 0.0 ----
    try:
        p = make_proposal(
            ocr_texts=("SOMETHING TOTALLY UNRELATED",),
            legend_symbol_id="QR-1", legend_label="Sprinkler",
        )
        r = scorer.compute(p)
        assert r.score == 0.0
        results["ocr_no_overlap_zero"] = "PASS"
    except Exception as e:
        results["ocr_no_overlap_zero"] = f"FAIL: {e}"

    # ---- empty OCR → 0.0 ----
    try:
        p = make_proposal(
            ocr_texts=(),
            legend_symbol_id="QR-1", legend_label="Sprinkler",
        )
        r = scorer.compute(p)
        assert r.score == 0.0
        assert r.reasoning == "no_ocr_text_near_motif"
        results["ocr_no_text_zero"] = "PASS"
    except Exception as e:
        results["ocr_no_text_zero"] = f"FAIL: {e}"

    # ---- best-of-multiple: one strong signal wins over many weak ----
    try:
        p = make_proposal(
            ocr_texts=("RANDOM", "STUFF", "QR-1", "NOISE"),
            legend_symbol_id="QR-1", legend_label="Sprinkler",
        )
        r = scorer.compute(p)
        assert r.score == 1.0
        results["ocr_best_of_multiple_spans"] = "PASS"
    except Exception as e:
        results["ocr_best_of_multiple_spans"] = f"FAIL: {e}"

    # =====================================================
    # BindingEvaluator — 3-stage pipeline
    # =====================================================
    evaluator = BindingEvaluator(scorers=[OCRAffinityScorer()])

    # ---- Stage 1 short-circuit: superseded legend ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler", legend_superseded=True,
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is False
        assert s.gate_reason == HardGateReason.SUPERSEDED_LEGEND
        assert s.final_score == 0.0
        assert s.tier == BindingTier.REJECTED
        # Crucial: no scorers ran
        assert s.per_factor_scores == {}
        results["stage1_short_circuit_superseded"] = "PASS"
    except Exception as e:
        results["stage1_short_circuit_superseded"] = f"FAIL: {e}"

    # ---- Stage 1 short-circuit: project mismatch ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
            legend_project_id="some-other-project",
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is False
        assert s.gate_reason == HardGateReason.PROJECT_MISMATCH
        assert s.per_factor_scores == {}
        results["stage1_short_circuit_project_mismatch"] = "PASS"
    except Exception as e:
        results["stage1_short_circuit_project_mismatch"] = f"FAIL: {e}"

    # ---- Stage 1 short-circuit: discipline mismatch (M-2 reuse) ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
            motif_discipline="plumbing",
            legend_discipline="electrical",
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is False
        assert s.gate_reason == HardGateReason.DISCIPLINE_MISMATCH
        assert s.per_factor_scores == {}    # no OCR work wasted
        results["stage1_short_circuit_discipline"] = "PASS"
    except Exception as e:
        results["stage1_short_circuit_discipline"] = f"FAIL: {e}"

    # ---- Stage 1 short-circuit: geometry incompatibility ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
            motif_geom="linear",         # pipe-shaped motif
            legend_geom="point",         # sprinkler-head legend
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is False
        assert s.gate_reason == HardGateReason.ENTITY_GEOMETRY_INCOMPATIBLE
        results["stage1_short_circuit_geometry"] = "PASS"
    except Exception as e:
        results["stage1_short_circuit_geometry"] = f"FAIL: {e}"

    # ---- Geometry 'unknown' on either side → passes gate ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
            motif_geom="unknown", legend_geom="point",
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is True
        results["stage1_unknown_geometry_passes"] = "PASS"
    except Exception as e:
        results["stage1_unknown_geometry_passes"] = f"FAIL: {e}"

    # ---- Stage 2 + 3: full pipeline VERIFIED ----
    try:
        p = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is True
        assert s.final_score == 1.0     # only scorer returned 1.0
        assert s.tier == BindingTier.VERIFIED
        assert "ocr_affinity" in s.per_factor_scores
        assert s.per_factor_scores["ocr_affinity"].score == 1.0
        results["full_pipeline_verified"] = "PASS"
    except Exception as e:
        results["full_pipeline_verified"] = f"FAIL: {e}"

    # ---- Stage 3: UNKNOWN_REVIEW band (0.50-0.70) ----
    try:
        # Keyword overlap of 1/2 → 0.50 → UR band
        p = make_proposal(
            ocr_texts=("PENDANT HEAD",),
            legend_symbol_id="UNRELATED", legend_label="QR PENDANT",
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is True
        assert s.tier == BindingTier.UNKNOWN_REVIEW
        assert 0.50 <= s.final_score < 0.70
        results["tier_unknown_review"] = "PASS"
    except Exception as e:
        results["tier_unknown_review"] = f"FAIL: {e}"

    # ---- Stage 3: REJECTED band ----
    try:
        p = make_proposal(
            ocr_texts=("UNRELATED",),
            legend_symbol_id="QR-1", legend_label="Sprinkler",
        )
        s = evaluator.evaluate(p)
        assert s.gate_passed is True
        assert s.final_score < 0.50
        assert s.tier == BindingTier.REJECTED
        results["tier_rejected"] = "PASS"
    except Exception as e:
        results["tier_rejected"] = f"FAIL: {e}"

    # =====================================================
    # N-2.7 — Geometry classifier + Gate #4 matrix
    # =====================================================

    # ---- Legend classifier: POINT keyword wins ----
    try:
        c = classify_legend_geometry(symbol_id="P14", label="SERVICE SINK")
        assert c == GeometryClass.POINT, c
        results["legend_classifier_point"] = "PASS"
    except Exception as e:
        results["legend_classifier_point"] = f"FAIL: {e}"

    # ---- Legend classifier: LINEAR ----
    try:
        c = classify_legend_geometry(
            symbol_id="HWS", label="HOT WATER SUPPLY LINE",
        )
        assert c == GeometryClass.LINEAR, c
        results["legend_classifier_linear"] = "PASS"
    except Exception as e:
        results["legend_classifier_linear"] = f"FAIL: {e}"

    # ---- Legend classifier: POLYGON ----
    try:
        c = classify_legend_geometry(symbol_id="ROOM 101", label="OFFICE AREA")
        assert c == GeometryClass.POLYGON, c
        results["legend_classifier_polygon"] = "PASS"
    except Exception as e:
        results["legend_classifier_polygon"] = f"FAIL: {e}"

    # ---- Legend classifier: NONE (glossary) ----
    try:
        c = classify_legend_geometry(symbol_id="EXIST", label="- EXISTING")
        assert c == GeometryClass.NONE, c
        c2 = classify_legend_geometry(symbol_id="CONT", label="CONTINUOUS")
        assert c2 == GeometryClass.NONE, c2
        c3 = classify_legend_geometry(symbol_id="NIC", label="NOT IN CONTRACT")
        assert c3 == GeometryClass.NONE, c3
        results["legend_classifier_none_glossary"] = "PASS"
    except Exception as e:
        results["legend_classifier_none_glossary"] = f"FAIL: {e}"

    # ---- Legend classifier: physical wins over glossary modifier ----
    try:
        # "EXISTING SINK" — has both NONE-kw 'existing' and POINT-kw 'sink'
        # Physical entity wins.
        c = classify_legend_geometry(symbol_id="EX-SINK", label="EXISTING SINK")
        assert c == GeometryClass.POINT, c
        results["legend_classifier_physical_wins_glossary"] = "PASS"
    except Exception as e:
        results["legend_classifier_physical_wins_glossary"] = f"FAIL: {e}"

    # ---- Legend classifier: UNKNOWN (no keyword hit) ----
    try:
        c = classify_legend_geometry(symbol_id="XYZ", label="ZOOGNARF")
        assert c == GeometryClass.UNKNOWN, c
        results["legend_classifier_unknown"] = "PASS"
    except Exception as e:
        results["legend_classifier_unknown"] = f"FAIL: {e}"

    # ---- Motif classifier: small + few primitives → POINT ----
    try:
        f = MotifGeometryFeatures(
            primitive_count=7, bbox_width=27.0, bbox_height=38.0,
        )
        c = classify_motif_geometry(f)
        assert c == GeometryClass.POINT, c
        results["motif_classifier_point"] = "PASS"
    except Exception as e:
        results["motif_classifier_point"] = f"FAIL: {e}"

    # ---- Motif classifier: high aspect → LINEAR ----
    try:
        f = MotifGeometryFeatures(
            primitive_count=20, bbox_width=600.0, bbox_height=8.0,
        )
        c = classify_motif_geometry(f)
        assert c == GeometryClass.LINEAR, c
        results["motif_classifier_linear"] = "PASS"
    except Exception as e:
        results["motif_classifier_linear"] = f"FAIL: {e}"

    # ---- Motif classifier: closed-area dominance → POLYGON ----
    try:
        f = MotifGeometryFeatures(
            primitive_count=120, bbox_width=200.0, bbox_height=150.0,
            closed_area_ratio=0.8,
        )
        c = classify_motif_geometry(f)
        assert c == GeometryClass.POLYGON, c
        results["motif_classifier_polygon"] = "PASS"
    except Exception as e:
        results["motif_classifier_polygon"] = f"FAIL: {e}"

    # ---- Motif classifier: ambiguous → UNKNOWN ----
    try:
        f = MotifGeometryFeatures(
            primitive_count=50, bbox_width=80.0, bbox_height=80.0,
        )
        c = classify_motif_geometry(f)
        assert c == GeometryClass.UNKNOWN, c
        results["motif_classifier_unknown"] = "PASS"
    except Exception as e:
        results["motif_classifier_unknown"] = f"FAIL: {e}"

    # ---- Gate matrix: legend=NONE always rejected ----
    try:
        assert not geometry_gate_compatible(GeometryClass.POINT, GeometryClass.NONE)
        assert not geometry_gate_compatible(GeometryClass.LINEAR, GeometryClass.NONE)
        assert not geometry_gate_compatible(GeometryClass.POLYGON, GeometryClass.NONE)
        assert not geometry_gate_compatible(GeometryClass.UNKNOWN, GeometryClass.NONE)
        results["gate_matrix_none_always_rejected"] = "PASS"
    except Exception as e:
        results["gate_matrix_none_always_rejected"] = f"FAIL: {e}"

    # ---- Gate matrix: UNKNOWN fail-open ----
    try:
        assert geometry_gate_compatible(GeometryClass.UNKNOWN, GeometryClass.POINT)
        assert geometry_gate_compatible(GeometryClass.POINT, GeometryClass.UNKNOWN)
        assert geometry_gate_compatible(GeometryClass.UNKNOWN, GeometryClass.UNKNOWN)
        results["gate_matrix_unknown_fail_open"] = "PASS"
    except Exception as e:
        results["gate_matrix_unknown_fail_open"] = f"FAIL: {e}"

    # ---- Gate matrix: physical mismatch rejected ----
    try:
        assert not geometry_gate_compatible(GeometryClass.POINT, GeometryClass.LINEAR)
        assert not geometry_gate_compatible(GeometryClass.LINEAR, GeometryClass.POLYGON)
        assert not geometry_gate_compatible(GeometryClass.POLYGON, GeometryClass.POINT)
        results["gate_matrix_physical_mismatch_rejected"] = "PASS"
    except Exception as e:
        results["gate_matrix_physical_mismatch_rejected"] = f"FAIL: {e}"

    # ---- Gate matrix: physical match allowed ----
    try:
        assert geometry_gate_compatible(GeometryClass.POINT, GeometryClass.POINT)
        assert geometry_gate_compatible(GeometryClass.LINEAR, GeometryClass.LINEAR)
        assert geometry_gate_compatible(GeometryClass.POLYGON, GeometryClass.POLYGON)
        results["gate_matrix_physical_match_ok"] = "PASS"
    except Exception as e:
        results["gate_matrix_physical_match_ok"] = f"FAIL: {e}"

    # ---- Evaluator Stage 1: glossary legend now short-circuits ----
    try:
        e = BindingEvaluator(scorers=[OCRAffinityScorer()])
        p = make_proposal(
            ocr_texts=("EXISTING",),
            legend_symbol_id="EXIST", legend_label="- EXISTING",
            legend_geom="none",
        )
        s = e.evaluate(p)
        assert s.gate_passed is False
        assert s.gate_reason == HardGateReason.ENTITY_GEOMETRY_INCOMPATIBLE
        assert s.per_factor_scores == {}
        results["evaluator_stage1_none_legend_killed"] = "PASS"
    except Exception as e:
        results["evaluator_stage1_none_legend_killed"] = f"FAIL: {e}"

    # ---- Weighted product: w=0 ignores the factor ----
    try:
        zero_weight = BindingWeights(ocr_affinity=0.0)
        eval_with_zero = BindingEvaluator(
            scorers=[OCRAffinityScorer()], weights=zero_weight,
        )
        p = make_proposal(
            ocr_texts=("UNRELATED",),
            legend_symbol_id="QR-1", legend_label="Sprinkler",
        )
        s = eval_with_zero.evaluate(p)
        # ocr scored 0.0 — but w=0 means we treat factor as identity,
        # so empty product = 1.0 → VERIFIED
        assert s.final_score == 1.0
        assert s.tier == BindingTier.VERIFIED
        # Receipt still records the actual scorer output (0.0)
        assert s.per_factor_scores["ocr_affinity"].score == 0.0
        results["weighted_product_w0_ignores"] = "PASS"
    except Exception as e:
        results["weighted_product_w0_ignores"] = f"FAIL: {e}"

    # ---- Replay hash determinism ----
    try:
        p1 = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
        )
        p2 = make_proposal(
            ocr_texts=("QR-1",), legend_symbol_id="QR-1",
            legend_label="Sprinkler",
        )
        s1 = evaluator.evaluate(p1)
        s2 = evaluator.evaluate(p2)
        assert s1.replay_hash == s2.replay_hash
        import re
        assert re.fullmatch(r"[a-f0-9]{24}", s1.replay_hash)
        results["replay_hash_deterministic"] = "PASS"
    except Exception as e:
        results["replay_hash_deterministic"] = f"FAIL: {e}"

    # ---- Evaluator rejects duplicate scorer names ----
    try:
        try:
            BindingEvaluator(scorers=[OCRAffinityScorer(), OCRAffinityScorer()])
            results["evaluator_dupe_name_rejected"] = "FAIL: accepted duplicates"
        except ValueError:
            results["evaluator_dupe_name_rejected"] = "PASS"
    except Exception as e:
        results["evaluator_dupe_name_rejected"] = f"FAIL: {e}"

    # ---- Evaluator rejects empty scorer list ----
    try:
        try:
            BindingEvaluator(scorers=[])
            results["evaluator_empty_scorers_rejected"] = "FAIL: accepted empty"
        except ValueError:
            results["evaluator_empty_scorers_rejected"] = "PASS"
    except Exception as e:
        results["evaluator_empty_scorers_rejected"] = f"FAIL: {e}"

    # ---- Report ---------------------------------------------------------
    print("=== Phase 5 N-1 + N-2 binding smoke ===")
    n_pass = sum(1 for v in results.values() if v == "PASS")
    for name, v in results.items():
        print(f"  [{v[:4]}] {name}")
    print(f"\n{n_pass}/{len(results)} pass")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
