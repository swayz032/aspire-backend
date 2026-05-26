"""Phase 5 N-2.5 — Live OCR-only baseline audit on Gulfstream.

8 composed_symbols × 288 legends = 2,304 candidate pairs.
Run BindingEvaluator with OCRAffinityScorer only. Capture 4 metrics:

  1. Stage 1 kill rate (which gates fired, on how many pairs)
  2. VERIFIED volume (score >= 0.70)
  3. UNKNOWN_REVIEW volume (0.50 <= score < 0.70)
  4. False-positive sample of VERIFIED for sanity-check

Run:
    python scripts/win_debug/binding_live_audit.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))

from aspire_orchestrator.services.blueprint.semantic_edges.binding import (  # noqa: E402
    BindingEvaluator, BindingProposal, BindingTier, HardGateReason,
    LEGEND_HISTOGRAM_KEY, MOTIF_HISTOGRAM_KEY,
    MotifGeometryFeatures, OCRAffinityScorer, PrimitiveHistogram,
    TopologySimilarityScorer,
    classify_legend_geometry, classify_motif_geometry,
)

PROJECT_ID = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE_ID = PROJECT_ID  # acceptable for audit script; real call would carry suite_id


# -- discipline inference from sheet code prefix -----------------
DISCIPLINE_PREFIX = {
    "P":  "plumbing",
    "FP": "fire",
    "M":  "mechanical",
    "E":  "electrical",
    "A":  "architectural",
    "S":  "structural",
    "C":  "civil",
}


def infer_discipline(sheet_code: str | None) -> str | None:
    """Sheet code 'FP-101' -> 'fire'. None / empty -> None."""
    if not sheet_code:
        return None
    sheet_code = sheet_code.strip().upper()
    # try 2-letter prefix first ('FP'), then 1-letter ('P', 'M', 'E')
    m = re.match(r"^([A-Z]{1,2})", sheet_code)
    if not m:
        return None
    pref = m.group(1)
    if pref in DISCIPLINE_PREFIX:
        return DISCIPLINE_PREFIX[pref]
    # fall back to first letter only
    if pref[0] in DISCIPLINE_PREFIX:
        return DISCIPLINE_PREFIX[pref[0]]
    return None


def main() -> int:
    motifs = json.loads((HERE / "_motifs.json").read_text(encoding="utf-8"))
    legends = json.loads((HERE / "_legends.json").read_text(encoding="utf-8"))
    ocr_by_sheet = json.loads((HERE / "_ocr.json").read_text(encoding="utf-8"))

    # N-3.1 — motif primitive histograms (legend side blocked on coord transform)
    motif_hist_path = HERE / "_motif_histograms.json"
    motif_histograms = (
        json.loads(motif_hist_path.read_text(encoding="utf-8"))
        if motif_hist_path.exists() else {}
    )
    # Strip the _mapping comment if present
    motif_histograms = {k: v for k, v in motif_histograms.items() if not k.startswith("_")}

    print(f"loaded {len(motifs)} motifs, {len(legends)} legends, "
          f"{len(ocr_by_sheet)} sheets with OCR, "
          f"{len(motif_histograms)} motif histograms")

    # N-3.1 — evaluator now runs OCR + Topology
    evaluator = BindingEvaluator(scorers=[
        OCRAffinityScorer(),
        TopologySimilarityScorer(),
    ])

    counts = Counter()
    gate_hits = Counter()
    verified_samples = []
    ur_samples = []
    band_hits = Counter()

    # Enrich motifs with inferred discipline + sheet OCR + geometry class
    enriched_motifs = []
    for m in motifs:
        sheet_ocr = ocr_by_sheet.get(m["motif_sheet_id"], [])
        bbox = m.get("motif_bbox") or {}
        feats = MotifGeometryFeatures(
            primitive_count=m.get("primitive_count", 0),
            bbox_width=float(bbox.get("w", 0.0)),
            bbox_height=float(bbox.get("h", 0.0)),
        )
        motif_geom = classify_motif_geometry(feats)
        hist_data = motif_histograms.get(m["motif_id"])
        motif_hist = PrimitiveHistogram.from_dict(hist_data) if hist_data else None
        enriched_motifs.append({
            **m,
            "discipline": infer_discipline(m.get("sheet_code")),
            "ocr_texts": tuple(sheet_ocr),
            "geometry": motif_geom.value,
            "histogram": motif_hist,
        })

    motif_hist_coverage = sum(
        1 for m in enriched_motifs if m["histogram"] is not None
    )
    legend_hist_coverage = 0  # N-3.1: legend side blocked on coord transform
    print(f"\nHistogram coverage:")
    print(f"  motifs  : {motif_hist_coverage}/{len(enriched_motifs)} "
          f"({100*motif_hist_coverage/len(enriched_motifs):.0f}%)")
    print(f"  legends : {legend_hist_coverage}/{len(legends)} "
          f"(0% — blocked on raster->PDF coord transform, N-3.2 scope)")

    # Pre-classify all legends (cache geometry for speed)
    legend_geom_cache = {}
    for legend in legends:
        legend_geom_cache[legend["legend_id"]] = classify_legend_geometry(
            symbol_id=legend.get("legend_symbol_id"),
            label=legend.get("legend_label"),
        ).value

    # Print motif geometry distribution + a sample of legend classification
    print("\nMotif geometry distribution:")
    mg = Counter(m["geometry"] for m in enriched_motifs)
    for g, c in mg.most_common():
        print(f"  {g:<12} : {c}")
    print("\nLegend geometry distribution (288 legends):")
    lg = Counter(legend_geom_cache.values())
    for g, c in lg.most_common():
        print(f"  {g:<12} : {c}")

    # Cross product
    total_pairs = 0
    # N-3.1 telemetry
    topology_scores_by_tier: dict[BindingTier, list[float]] = {t: [] for t in BindingTier}
    topology_kill_count = 0  # OCR >= 0.8 AND topology < 0.5
    topology_signal_pairs = 0  # pairs where topology actually had data
    for motif in enriched_motifs:
        for legend in legends:
            total_pairs += 1
            extra: dict = {}
            if motif["histogram"] is not None:
                extra[MOTIF_HISTOGRAM_KEY] = motif["histogram"]
            # legend histogram: deferred to N-3.2 (coord transform)
            proposal = BindingProposal(
                suite_id=SUITE_ID,
                project_id=PROJECT_ID,
                motif_id=motif["motif_id"],
                motif_entity_type="composed_symbol",
                motif_sheet_id=motif["motif_sheet_id"],
                motif_entity_geometry=motif["geometry"],
                motif_discipline=motif["discipline"],
                motif_nearby_ocr_text=motif["ocr_texts"],
                motif_extra=extra,
                legend_id=legend["legend_id"],
                legend_label=legend.get("legend_label"),
                legend_symbol_id=legend.get("legend_symbol_id"),
                legend_discipline=(
                    legend.get("legend_discipline")
                    if legend.get("legend_discipline") not in (None, "unknown")
                    else infer_discipline(legend.get("legend_sheet_code"))
                ),
                legend_entity_geometry=legend_geom_cache[legend["legend_id"]],
                legend_project_id=PROJECT_ID,
                legend_superseded=False,
            )
            score = evaluator.evaluate(proposal)
            counts[score.tier] += 1

            if not score.gate_passed:
                gate_hits[score.gate_reason] += 1
            else:
                # Record band hit
                ocr_result = score.per_factor_scores.get("ocr_affinity")
                if ocr_result:
                    band_hits[ocr_result.reasoning] += 1
                # N-3.1 telemetry — separate topology score by tier
                topo_result = score.per_factor_scores.get("topology")
                if topo_result:
                    topology_scores_by_tier[score.tier].append(topo_result.score)
                    if topo_result.reasoning != "insufficient_data":
                        topology_signal_pairs += 1
                        if ocr_result and ocr_result.score >= 0.8 and topo_result.score < 0.5:
                            topology_kill_count += 1

            if score.tier == BindingTier.VERIFIED and len(verified_samples) < 50:
                verified_samples.append({
                    "motif_sheet": motif.get("sheet_code") or motif["motif_sheet_id"][:8],
                    "motif_discipline": motif["discipline"],
                    "legend_symbol_id": legend.get("legend_symbol_id"),
                    "legend_label": legend.get("legend_label"),
                    "legend_sheet": legend.get("legend_sheet_code"),
                    "score": round(score.final_score, 3),
                    "reasoning": (
                        score.per_factor_scores["ocr_affinity"].reasoning
                        if "ocr_affinity" in score.per_factor_scores else ""
                    ),
                    "evidence": (
                        score.per_factor_scores["ocr_affinity"].evidence
                        if "ocr_affinity" in score.per_factor_scores else {}
                    ),
                })
            elif score.tier == BindingTier.UNKNOWN_REVIEW and len(ur_samples) < 20:
                ur_samples.append({
                    "motif_sheet": motif.get("sheet_code") or motif["motif_sheet_id"][:8],
                    "legend_symbol_id": legend.get("legend_symbol_id"),
                    "legend_label": legend.get("legend_label"),
                    "score": round(score.final_score, 3),
                    "reasoning": (
                        score.per_factor_scores["ocr_affinity"].reasoning
                        if "ocr_affinity" in score.per_factor_scores else ""
                    ),
                })

    # ---- Report ---------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"PHASE 5 OCR-ONLY BASELINE AUDIT — Gulfstream (project {PROJECT_ID[:8]})")
    print("=" * 70)
    print(f"\nTotal candidate pairs : {total_pairs}")
    print(f"  motifs              : {len(motifs)}")
    print(f"  legends             : {len(legends)}")

    print("\n--- Stage 1 HARD-gate kill rate ---")
    total_killed = sum(gate_hits.values())
    kill_pct = (total_killed / total_pairs * 100.0) if total_pairs else 0.0
    print(f"  Total killed at Stage 1 : {total_killed:>5}  ({kill_pct:5.1f}%)")
    for reason in HardGateReason:
        hit = gate_hits.get(reason, 0)
        pct = (hit / total_pairs * 100.0) if total_pairs else 0.0
        print(f"    {reason.value:<35} : {hit:>5}  ({pct:5.1f}%)")

    survived = total_pairs - total_killed
    print(f"\n  Survived Stage 1 -> Stage 2 : {survived}  "
          f"({survived / total_pairs * 100.0:.1f}%)")

    print("\n--- Stage 2 OCR-affinity band hits (Stage-1-survivors only) ---")
    for band, hit in sorted(band_hits.items(), key=lambda kv: -kv[1]):
        pct = (hit / survived * 100.0) if survived else 0.0
        print(f"    {band:<35} : {hit:>5}  ({pct:5.1f}%)")

    print("\n--- Stage 3 tier distribution (all 2,304 pairs) ---")
    for tier in BindingTier:
        c = counts.get(tier, 0)
        pct = (c / total_pairs * 100.0) if total_pairs else 0.0
        print(f"    {tier.value:<20} : {c:>5}  ({pct:5.1f}%)")

    # ---- N-3.1 TOPOLOGY TELEMETRY ----
    print("\n--- Topology telemetry (N-3.1) ---")
    print(f"  Pairs with topology signal      : {topology_signal_pairs:>5}  "
          f"({100*topology_signal_pairs/max(survived,1):.1f}% of survivors)")
    print(f"  Topology kills (OCR>=0.8 & topo<0.5): {topology_kill_count:>5}")
    if topology_signal_pairs == 0:
        print("  (No topology signal recorded — legend histograms blocked")
        print("   on raster->PDF coord transform; scorer fell back to")
        print("   insufficient_data=1.0 identity for all pairs. Audit is")
        print("   the N-2.7 baseline, NOT a topology-active measurement.)")
    print("\n  Mean topology score by tier:")
    for tier in BindingTier:
        scores = topology_scores_by_tier.get(tier, [])
        if scores:
            mean = sum(scores) / len(scores)
            print(f"    {tier.value:<20} : mean={mean:.3f}  n={len(scores)}")
        else:
            print(f"    {tier.value:<20} : n=0")

    # Force ASCII-safe stdout on Windows cp1252
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("\n--- VERIFIED samples (first 50) ---")
    if not verified_samples:
        print("    (none)")
    for s in verified_samples[:50]:
        print(f"    [{s['score']}] motif@{s['motif_sheet']} (d={s['motif_discipline']})")
        print(f"           -> legend symbol_id={s['legend_symbol_id']!r}"
              f"  label={s['legend_label']!r}")
        print(f"           band={s['reasoning']}  evidence={s['evidence']}")

    print("\n--- UNKNOWN_REVIEW samples (first 20) ---")
    if not ur_samples:
        print("    (none)")
    for s in ur_samples[:20]:
        print(f"    [{s['score']}] motif@{s['motif_sheet']}"
              f" -> {s['legend_symbol_id']!r} ({s['reasoning']})")

    # Write JSON report alongside for L-7 receipt persistence later
    report = {
        "project_id": PROJECT_ID,
        "total_pairs": total_pairs,
        "stage1_kills": {r.value: gate_hits.get(r, 0) for r in HardGateReason},
        "stage2_band_hits": dict(band_hits),
        "tier_distribution": {t.value: counts.get(t, 0) for t in BindingTier},
        "verified_count": counts.get(BindingTier.VERIFIED, 0),
        "unknown_review_count": counts.get(BindingTier.UNKNOWN_REVIEW, 0),
        "rejected_count": counts.get(BindingTier.REJECTED, 0),
        "verified_samples": verified_samples,
        "ur_samples": ur_samples,
    }
    (HERE / "_audit_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8",
    )
    print(f"\nFull report → {(HERE / '_audit_report.json').name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
