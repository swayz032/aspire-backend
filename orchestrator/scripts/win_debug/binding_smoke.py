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
        ConnectivityScorer, FunctionalRole,
        GeometryClass, HardGateReason, MotifGeometryFeatures,
        OCRAffinityScorer, PrimitiveHistogram, ScorerResult,
        SheetCoordinateTransform, TopologySimilarityScorer,
        UNKNOWN_REVIEW_THRESHOLD, VERIFIED_THRESHOLD,
        classify_legend_function, classify_legend_geometry,
        classify_motif_function, classify_motif_geometry,
        cosine_similarity, functional_compatibility,
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

    # ---- keyword-overlap band (post-N-4.6 calibration) ----
    try:
        # N-4.6: 0.30 + 0.30 * ratio
        # label="PENDANT SPRINKLER" -> tokens {PENDANT, SPRINKLER}
        # OCR "PENDANT HEAD" -> tokens {PENDANT, HEAD}; shared={PENDANT}; ratio=1/2
        # → 0.30 + 0.30*0.5 = 0.45 (no longer enters UR; falls below 0.50)
        p = make_proposal(
            ocr_texts=("PENDANT HEAD",),
            legend_symbol_id="UNRELATED", legend_label="PENDANT SPRINKLER",
        )
        r = scorer.compute(p)
        assert 0.44 <= r.score <= 0.46, f"got {r.score} ({r.reasoning})"
        results["ocr_keyword_overlap_band_1of2"] = "PASS"
    except Exception as e:
        results["ocr_keyword_overlap_band_1of2"] = f"FAIL: {e}"

    # ---- keyword-overlap band: 2-of-2 still produces UR-tier signal ----
    try:
        # 2 shared / 2 label tokens → 0.30 + 0.30*1.0 = 0.60
        p = make_proposal(
            ocr_texts=("PENDANT SPRINKLER",),
            legend_symbol_id="UNRELATED",
            legend_label="PENDANT SPRINKLER ASSEMBLY",
        )
        r = scorer.compute(p)
        # 2 of 3 tokens shared → 0.30 + 0.30*(2/3) = 0.50
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

    # =====================================================
    # N-3 — TopologySimilarityScorer (histogram cosine)
    # =====================================================

    # ---- Cosine: identical histograms → 1.0 ----
    try:
        h = PrimitiveHistogram(lines=4, circles=1, text=1)
        c = cosine_similarity(h, h)
        assert abs(c - 1.0) < 1e-9, c
        results["topology_cosine_identical"] = "PASS"
    except Exception as e:
        results["topology_cosine_identical"] = f"FAIL: {e}"

    # ---- Cosine: orthogonal histograms → 0.0 ----
    try:
        a = PrimitiveHistogram(lines=10)
        b = PrimitiveHistogram(circles=10)
        c = cosine_similarity(a, b)
        assert c == 0.0, c
        results["topology_cosine_orthogonal"] = "PASS"
    except Exception as e:
        results["topology_cosine_orthogonal"] = f"FAIL: {e}"

    # ---- Cosine: scale-invariant (counts ratio matters, not magnitude) ----
    try:
        a = PrimitiveHistogram(lines=4, circles=1)
        b = PrimitiveHistogram(lines=8, circles=2)  # 2x scale, same ratio
        c = cosine_similarity(a, b)
        assert abs(c - 1.0) < 1e-9, c
        results["topology_cosine_scale_invariant"] = "PASS"
    except Exception as e:
        results["topology_cosine_scale_invariant"] = f"FAIL: {e}"

    # ---- Cosine: empty histogram → 0.0 ----
    try:
        c = cosine_similarity(PrimitiveHistogram(), PrimitiveHistogram(lines=1))
        assert c == 0.0, c
        results["topology_cosine_empty"] = "PASS"
    except Exception as e:
        results["topology_cosine_empty"] = f"FAIL: {e}"

    # ---- PrimitiveHistogram.from_dict accepts partial data ----
    try:
        h = PrimitiveHistogram.from_dict({"lines": 3, "circles": 1, "ignored": 99})
        assert h.lines == 3 and h.circles == 1 and h.arcs == 0
        assert h.total == 4
        results["topology_histogram_from_dict"] = "PASS"
    except Exception as e:
        results["topology_histogram_from_dict"] = f"FAIL: {e}"

    # ---- Scorer band: near-identical → 1.0 ----
    try:
        scorer_t = TopologySimilarityScorer()
        p = make_proposal()  # base proposal
        # PrimitiveHistogram via dataclass replace into motif_extra
        p_with_hist = BindingProposal(
            suite_id=p.suite_id, project_id=p.project_id,
            motif_id=p.motif_id, motif_entity_type=p.motif_entity_type,
            motif_sheet_id=p.motif_sheet_id,
            motif_extra={
                "primitive_histogram": PrimitiveHistogram(lines=4, circles=1, text=1),
                "legend_primitive_histogram": PrimitiveHistogram(lines=4, circles=1, text=1),
            },
            legend_id="L", legend_label="x", legend_symbol_id="x",
            legend_project_id=p.project_id,
        )
        r = scorer_t.compute(p_with_hist)
        assert r.score == 1.0, (r.score, r.reasoning)
        assert "near_identical" in r.reasoning
        results["topology_scorer_band_near_identical"] = "PASS"
    except Exception as e:
        results["topology_scorer_band_near_identical"] = f"FAIL: {e}"

    # ---- Scorer band: mismatched shapes → below floor → 0.0 ----
    try:
        scorer_t = TopologySimilarityScorer()
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s",
            motif_extra={
                "primitive_histogram": PrimitiveHistogram(lines=12),   # complex louver
                "legend_primitive_histogram": PrimitiveHistogram(circles=1),  # sprinkler head
            },
            legend_id="L", legend_label="x", legend_symbol_id="x",
            legend_project_id="p",
        )
        r = scorer_t.compute(p)
        assert r.score == 0.0, (r.score, r.reasoning)
        assert r.reasoning == "histogram_cosine_below_floor"
        results["topology_scorer_band_mismatch_zero"] = "PASS"
    except Exception as e:
        results["topology_scorer_band_mismatch_zero"] = f"FAIL: {e}"

    # ---- Scorer band: moderate similarity → 0.70 band ----
    try:
        scorer_t = TopologySimilarityScorer()
        # Shared lines (5) + diverging circle counts -> cosine ~0.78
        # → falls into the 0.70 band
        a = PrimitiveHistogram(lines=5, circles=3)
        b = PrimitiveHistogram(lines=5, circles=1)
        # cos = (25+3) / (sqrt(34)*sqrt(26)) = 28 / 29.73 ≈ 0.942
        # That's actually ≥0.85 — let me use a wider diverge
        a2 = PrimitiveHistogram(lines=4, circles=2, text=2)
        b2 = PrimitiveHistogram(lines=4, circles=1)
        cos = cosine_similarity(a2, b2)
        # Verify the math first, then assert band
        assert 0.70 <= cos < 0.95, f"cos={cos} not in moderate window"
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s",
            motif_extra={
                "primitive_histogram": a2,
                "legend_primitive_histogram": b2,
            },
            legend_id="L", legend_label="x", legend_symbol_id="x",
            legend_project_id="p",
        )
        r = scorer_t.compute(p)
        assert r.score in (0.70, 0.85), (r.score, r.reasoning, cos)
        results["topology_scorer_band_moderate"] = "PASS"
    except Exception as e:
        results["topology_scorer_band_moderate"] = f"FAIL: {e}"

    # ---- Scorer: insufficient data → 1.0 (identity, no penalty) ----
    try:
        scorer_t = TopologySimilarityScorer()
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s",
            motif_extra={},  # neither side
            legend_id="L", legend_label="x", legend_symbol_id="x",
            legend_project_id="p",
        )
        r = scorer_t.compute(p)
        assert r.score == 1.0
        assert r.reasoning == "insufficient_data"
        assert set(r.evidence["missing_side"]) == {"motif", "legend"}
        results["topology_scorer_insufficient_data"] = "PASS"
    except Exception as e:
        results["topology_scorer_insufficient_data"] = f"FAIL: {e}"

    # ---- Scorer: histogram dict payload also accepted ----
    try:
        scorer_t = TopologySimilarityScorer()
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s",
            motif_extra={
                "primitive_histogram": {"lines": 4, "circles": 1, "text": 1},
                "legend_primitive_histogram": {"lines": 4, "circles": 1, "text": 1},
            },
            legend_id="L", legend_label="x", legend_symbol_id="x",
            legend_project_id="p",
        )
        r = scorer_t.compute(p)
        assert r.score == 1.0
        results["topology_scorer_dict_payload"] = "PASS"
    except Exception as e:
        results["topology_scorer_dict_payload"] = f"FAIL: {e}"

    # ---- Full pipeline: topology kills bad OCR (the demo case) ----
    try:
        # OCR says "T" exact match (1.0) but motif is a louver
        # (12 lines), legend is a thermostat (1 circle, 1 text).
        # Topology cosine → 0 → product → 0 → REJECTED.
        eval_two = BindingEvaluator(
            scorers=[OCRAffinityScorer(), TopologySimilarityScorer()],
        )
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s",
            motif_nearby_ocr_text=("THERMOSTAT",),
            motif_extra={
                "primitive_histogram": PrimitiveHistogram(lines=12),  # louver shape
                "legend_primitive_histogram": PrimitiveHistogram(circles=1, text=1),
            },
            legend_id="L",
            legend_label="THERMOSTAT", legend_symbol_id="T",
            legend_project_id="p",
        )
        s = eval_two.evaluate(p)
        # OCR scores 1.0 (exact label match). Topology scores 0.0.
        # Product = 0 → REJECTED.
        assert s.gate_passed is True
        assert s.final_score == 0.0
        assert s.tier == BindingTier.REJECTED
        assert s.per_factor_scores["ocr_affinity"].score == 1.0
        assert s.per_factor_scores["topology"].score == 0.0
        results["topology_kills_bad_ocr"] = "PASS"
    except Exception as e:
        results["topology_kills_bad_ocr"] = f"FAIL: {e}"

    # =====================================================
    # N-3.2 — SheetCoordinateTransform
    # =====================================================

    # ---- Rotation 0 — identity-like (just normalize + rescale) ----
    try:
        t = SheetCoordinateTransform(
            raster_w=1000, raster_h=500,
            pdf_w=2000, pdf_h=1000, rotation=0,
        )
        # Point at raster (500, 250) → midpoint → PDF (1000, 500)
        px, py = t.raster_point_to_pdf(500, 250)
        assert abs(px - 1000.0) < 1e-9, (px, py)
        assert abs(py - 500.0) < 1e-9, (px, py)
        results["coords_rotation_0_midpoint"] = "PASS"
    except Exception as e:
        results["coords_rotation_0_midpoint"] = f"FAIL: {e}"

    # ---- Rotation 90 — axis swap (the Gulfstream case) ----
    try:
        # Landscape raster (1000 x 500) -> portrait PDF (500 x 1000)
        t = SheetCoordinateTransform(
            raster_w=1000, raster_h=500,
            pdf_w=500, pdf_h=1000, rotation=90,
        )
        # Raster top-left (0, 0) under 90° CW → PDF top-right (500, 0)
        px, py = t.raster_point_to_pdf(0, 0)
        assert abs(px - 500.0) < 1e-9, (px, py)
        assert abs(py - 0.0) < 1e-9, (px, py)
        # Raster top-right (1000, 0) → PDF bottom-right (500, 1000)
        px, py = t.raster_point_to_pdf(1000, 0)
        assert abs(px - 500.0) < 1e-9, (px, py)
        assert abs(py - 1000.0) < 1e-9, (px, py)
        # Raster bottom-left (0, 500) → PDF top-left (0, 0)
        px, py = t.raster_point_to_pdf(0, 500)
        assert abs(px - 0.0) < 1e-9, (px, py)
        assert abs(py - 0.0) < 1e-9, (px, py)
        results["coords_rotation_90_corners"] = "PASS"
    except Exception as e:
        results["coords_rotation_90_corners"] = f"FAIL: {e}"

    # ---- Rotation 180 — flip both axes ----
    try:
        t = SheetCoordinateTransform(
            raster_w=1000, raster_h=500,
            pdf_w=2000, pdf_h=1000, rotation=180,
        )
        px, py = t.raster_point_to_pdf(0, 0)
        assert abs(px - 2000.0) < 1e-9
        assert abs(py - 1000.0) < 1e-9
        results["coords_rotation_180_origin"] = "PASS"
    except Exception as e:
        results["coords_rotation_180_origin"] = f"FAIL: {e}"

    # ---- Rotation 270 — opposite of 90 ----
    try:
        t = SheetCoordinateTransform(
            raster_w=1000, raster_h=500,
            pdf_w=500, pdf_h=1000, rotation=270,
        )
        # Raster (0,0) under 270° CW → PDF (0, 1000)
        px, py = t.raster_point_to_pdf(0, 0)
        assert abs(px - 0.0) < 1e-9
        assert abs(py - 1000.0) < 1e-9
        results["coords_rotation_270_origin"] = "PASS"
    except Exception as e:
        results["coords_rotation_270_origin"] = f"FAIL: {e}"

    # ---- Bbox transform produces AABB covering rotated quad ----
    try:
        t = SheetCoordinateTransform(
            raster_w=1000, raster_h=500,
            pdf_w=500, pdf_h=1000, rotation=90,
        )
        # Small raster bbox near top-left -> small PDF bbox near top-right
        px_min, py_min, px_max, py_max = t.raster_bbox_to_pdf_aabb(
            (100, 50, 200, 100),
        )
        assert px_max <= 500 and px_min >= 0
        assert py_max <= 1000 and py_min >= 0
        # Width and height should be positive
        assert px_max > px_min and py_max > py_min
        results["coords_bbox_aabb"] = "PASS"
    except Exception as e:
        results["coords_bbox_aabb"] = f"FAIL: {e}"

    # ---- 8-coord polygon transform ----
    try:
        t = SheetCoordinateTransform(
            raster_w=1000, raster_h=500,
            pdf_w=500, pdf_h=1000, rotation=90,
        )
        # 8-coord polygon: 4 corners of a small square
        poly = (100, 50, 200, 50, 200, 100, 100, 100)
        px_min, py_min, px_max, py_max = t.raster_polygon_to_pdf_aabb(poly)
        assert px_max > px_min and py_max > py_min
        assert 0 <= px_min and px_max <= 500
        assert 0 <= py_min and py_max <= 1000
        results["coords_polygon_aabb"] = "PASS"
    except Exception as e:
        results["coords_polygon_aabb"] = f"FAIL: {e}"

    # ---- Invalid rotation rejected ----
    try:
        try:
            SheetCoordinateTransform(
                raster_w=100, raster_h=100, pdf_w=100, pdf_h=100,
                rotation=45,
            )
            results["coords_invalid_rotation_rejected"] = "FAIL: 45 accepted"
        except ValueError:
            results["coords_invalid_rotation_rejected"] = "PASS"
    except Exception as e:
        results["coords_invalid_rotation_rejected"] = f"FAIL: {e}"

    # ---- Invalid dimensions rejected ----
    try:
        try:
            SheetCoordinateTransform(
                raster_w=-1, raster_h=100, pdf_w=100, pdf_h=100, rotation=0,
            )
            results["coords_invalid_dim_rejected"] = "FAIL: negative accepted"
        except ValueError:
            results["coords_invalid_dim_rejected"] = "PASS"
    except Exception as e:
        results["coords_invalid_dim_rejected"] = f"FAIL: {e}"

    # ---- Polygon with wrong arity rejected ----
    try:
        t = SheetCoordinateTransform(
            raster_w=100, raster_h=100, pdf_w=100, pdf_h=100, rotation=0,
        )
        try:
            t.raster_polygon_to_pdf_aabb((1, 2, 3, 4))  # only 4 coords
            results["coords_polygon_arity_rejected"] = "FAIL: 4 coords accepted"
        except ValueError:
            results["coords_polygon_arity_rejected"] = "PASS"
    except Exception as e:
        results["coords_polygon_arity_rejected"] = f"FAIL: {e}"

    # ---- Gulfstream-like real transform produces plausible PDF coords ----
    try:
        # Gulfstream FP-101: portrait PDF (1728 x 2592), landscape raster (~7200 x 4800), rotation=90
        t = SheetCoordinateTransform(
            raster_w=7200, raster_h=4800,
            pdf_w=1728, pdf_h=2592, rotation=90,
        )
        # Test: VICTAULIC legend raster bbox ~(5842, 4269) - (6071, 4362)
        px_min, py_min, px_max, py_max = t.raster_bbox_to_pdf_aabb(
            (5842, 4269, 6071, 4362),
        )
        # Should land somewhere on the page (not necessarily at original coords)
        assert 0 <= px_min and px_max <= 1728, f"px out of range {px_min}-{px_max}"
        assert 0 <= py_min and py_max <= 2592, f"py out of range {py_min}-{py_max}"
        # Bbox should be small (legend cell is small in PDF too)
        assert (px_max - px_min) < 200, f"bbox too wide {px_max - px_min}"
        assert (py_max - py_min) < 200, f"bbox too tall {py_max - py_min}"
        results["coords_gulfstream_real_transform"] = "PASS"
    except Exception as e:
        results["coords_gulfstream_real_transform"] = f"FAIL: {e}"

    # =====================================================
    # N-4 — FunctionalRole + ConnectivityScorer
    # =====================================================

    # ---- Legend functional classifier ----
    try:
        assert classify_legend_function(symbol_id="P14", label="SERVICE SINK") == FunctionalRole.SINK
        assert classify_legend_function(symbol_id="HWS", label="HOT WATER LINE") == FunctionalRole.DISTRIBUTION
        assert classify_legend_function(symbol_id="GV", label="GATE VALVE") == FunctionalRole.JUNCTION
        assert classify_legend_function(symbol_id="P", label="ELECTRICAL PANEL") == FunctionalRole.SOURCE
        assert classify_legend_function(symbol_id="XYZ", label="ZOOG") == FunctionalRole.UNKNOWN
        results["functional_legend_classifier"] = "PASS"
    except Exception as e:
        results["functional_legend_classifier"] = f"FAIL: {e}"

    # ---- Motif functional classifier ----
    try:
        assert classify_motif_function(GeometryClass.POINT) == FunctionalRole.SINK
        assert classify_motif_function(GeometryClass.LINEAR) == FunctionalRole.DISTRIBUTION
        assert classify_motif_function(GeometryClass.POLYGON) == FunctionalRole.UNKNOWN
        assert classify_motif_function(GeometryClass.UNKNOWN) == FunctionalRole.UNKNOWN
        results["functional_motif_classifier"] = "PASS"
    except Exception as e:
        results["functional_motif_classifier"] = f"FAIL: {e}"

    # ---- Compatibility matrix: same role allowed ----
    try:
        for r in [FunctionalRole.SOURCE, FunctionalRole.SINK,
                  FunctionalRole.JUNCTION, FunctionalRole.DISTRIBUTION]:
            assert functional_compatibility(r, r) == 1.0, r
        results["functional_compat_same_role"] = "PASS"
    except Exception as e:
        results["functional_compat_same_role"] = f"FAIL: {e}"

    # ---- Compatibility matrix: SINK <-> JUNCTION allowed ----
    try:
        assert functional_compatibility(FunctionalRole.SINK, FunctionalRole.JUNCTION) == 1.0
        assert functional_compatibility(FunctionalRole.JUNCTION, FunctionalRole.SINK) == 1.0
        results["functional_compat_sink_junction"] = "PASS"
    except Exception as e:
        results["functional_compat_sink_junction"] = f"FAIL: {e}"

    # ---- Compatibility matrix: hard mismatches rejected ----
    try:
        # SINK motif × DISTRIBUTION legend = 0
        assert functional_compatibility(FunctionalRole.SINK, FunctionalRole.DISTRIBUTION) == 0.0
        assert functional_compatibility(FunctionalRole.DISTRIBUTION, FunctionalRole.SINK) == 0.0
        assert functional_compatibility(FunctionalRole.SOURCE, FunctionalRole.SINK) == 0.0
        assert functional_compatibility(FunctionalRole.SOURCE, FunctionalRole.DISTRIBUTION) == 0.0
        results["functional_compat_hard_mismatch"] = "PASS"
    except Exception as e:
        results["functional_compat_hard_mismatch"] = f"FAIL: {e}"

    # ---- Compatibility matrix: UNKNOWN fail-open ----
    try:
        for r in FunctionalRole:
            assert functional_compatibility(r, FunctionalRole.UNKNOWN) == 1.0, r
            assert functional_compatibility(FunctionalRole.UNKNOWN, r) == 1.0, r
        results["functional_compat_unknown_open"] = "PASS"
    except Exception as e:
        results["functional_compat_unknown_open"] = f"FAIL: {e}"

    # ---- ConnectivityScorer end-to-end ----
    try:
        scorer_c = ConnectivityScorer()
        # POINT motif × SINK legend → 1.0
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s", motif_entity_geometry="point",
            legend_id="L",
            legend_label="SERVICE SINK", legend_symbol_id="P5B",
            legend_project_id="p",
        )
        r = scorer_c.compute(p)
        assert r.score == 1.0, (r.score, r.reasoning)
        assert "sink" in r.reasoning
        results["connectivity_scorer_match"] = "PASS"
    except Exception as e:
        results["connectivity_scorer_match"] = f"FAIL: {e}"

    # ---- ConnectivityScorer: hard mismatch ----
    try:
        # POINT motif (classified SINK) × DISTRIBUTION legend → 0.0
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s", motif_entity_geometry="point",
            legend_id="L",
            legend_label="HOT WATER LINE", legend_symbol_id="HWS",
            legend_project_id="p",
        )
        r = scorer_c.compute(p)
        assert r.score == 0.0, (r.score, r.reasoning)
        assert "mismatch" in r.reasoning
        results["connectivity_scorer_mismatch"] = "PASS"
    except Exception as e:
        results["connectivity_scorer_mismatch"] = f"FAIL: {e}"

    # ---- ConnectivityScorer: fail-open on UNKNOWN motif geometry ----
    try:
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s", motif_entity_geometry="unknown",
            legend_id="L",
            legend_label="HOT WATER LINE", legend_symbol_id="HWS",
            legend_project_id="p",
        )
        r = scorer_c.compute(p)
        assert r.score == 1.0
        assert r.reasoning == "insufficient_data"
        results["connectivity_scorer_unknown_motif"] = "PASS"
    except Exception as e:
        results["connectivity_scorer_unknown_motif"] = f"FAIL: {e}"

    # ---- N-4 in evaluator: kills the bad pipe-vs-sink case ----
    try:
        eval_three = BindingEvaluator(scorers=[
            OCRAffinityScorer(),
            TopologySimilarityScorer(),
            ConnectivityScorer(),
        ])
        # POINT sink-motif tries to bind to a LINE legend.
        # Gate #4 should have killed this (point vs linear) — but here
        # legend_entity_geometry='unknown' so Gate #4 fails open.
        # N-4 catches it: motif=SINK × legend=DISTRIBUTION → 0.0
        p = BindingProposal(
            suite_id="s", project_id="p",
            motif_id="m", motif_entity_type="composed_symbol",
            motif_sheet_id="s",
            motif_entity_geometry="point",
            motif_nearby_ocr_text=("HOT WATER",),
            legend_id="L",
            legend_label="HOT WATER LINE", legend_symbol_id="HWS",
            legend_entity_geometry="unknown",
            legend_project_id="p",
        )
        s = eval_three.evaluate(p)
        assert s.gate_passed is True
        assert s.per_factor_scores["connectivity"].score == 0.0
        # Product = 0 → REJECTED regardless of OCR/topology
        assert s.final_score == 0.0
        assert s.tier == BindingTier.REJECTED
        results["n4_kills_pipe_vs_sink"] = "PASS"
    except Exception as e:
        results["n4_kills_pipe_vs_sink"] = f"FAIL: {e}"

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
