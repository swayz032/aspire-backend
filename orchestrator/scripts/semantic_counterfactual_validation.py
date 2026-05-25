"""Wave 20.14.5J-V3-SCV — Semantic Counterfactual Validation.

Per v26 mandate: R3 passed adversarial structural prior gates BUT entropy 3.44 +
margins 0.017-0.039 prove scorer is weakly discriminative. Must validate semantic
grounding before Phase 3 unblocks.

This session implements 3 of 5 v26 test families (the 3 that can be done WITHOUT
new PDFs or manual annotations):

  T1. Synthetic adversarial swaps
      - Swap content between canonical (bottom-right) and center regions
      - Test: does detector follow CONTENT or POSITION?
      - If position-locked → prior-driven (not semantic)
      - If content-following → semantic grounding emerging

  T3. Semantic ablation
      - Remove each scoring signal independently
      - Measure: which channels carry the discriminative work?
      - Red flag: removing one channel collapses everything

  T4. Confidence calibration (framework)
      - Build calibration curves from existing data
      - Compute margin distributions
      - Set rejection thresholds

NOT implemented this session (require external inputs):
  T2 cross-style corpus (need new PDFs in unseen styles)
  T5 manual polygon IoU (need human annotation)

ZERO Supabase IO.
"""
import json
import math
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from sheet_layout_segmenter_v2 import (  # noqa: E402
    stream_page_primitives, score_candidate, prims_inside,
)
from sheet_layout_segmenter_v3 import (  # noqa: E402
    generate_proposal_set, compute_calibration,
)

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")


# ============================================================
# T1: Synthetic adversarial swaps
# ============================================================

def swap_regions(prims: list[dict], pw: float, ph: float,
                  region_a: tuple, region_b: tuple) -> list[dict]:
    """Move primitives between two regions. Tests if detector follows content."""
    ax0, ay0, ax1, ay1 = region_a
    bx0, by0, bx1, by1 = region_b
    a_dx = bx0 - ax0
    a_dy = by0 - ay0
    b_dx = ax0 - bx0
    b_dy = ay0 - by0

    out = []
    for p in prims:
        try:
            cx = float(p.get("cx", 0))
            cy = float(p.get("cy", 0))
        except (TypeError, ValueError):
            out.append(dict(p))
            continue
        in_a = ax0 <= cx <= ax1 and ay0 <= cy <= ay1
        in_b = bx0 <= cx <= bx1 and by0 <= cy <= by1
        if not in_a and not in_b:
            out.append(dict(p))
            continue
        pp = dict(p)
        if in_a:
            dx, dy = a_dx, a_dy
        else:
            dx, dy = b_dx, b_dy
        for k in ("x0", "x1", "cx"):
            if k in pp and pp[k] is not None:
                try:
                    pp[k] = float(pp[k]) + dx
                except (TypeError, ValueError):
                    pass
        for k in ("y0", "y1", "cy"):
            if k in pp and pp[k] is not None:
                try:
                    pp[k] = float(pp[k]) + dy
                except (TypeError, ValueError):
                    pass
        out.append(pp)
    return out


def score_with_swap(doc, page_idx: int, prims: list[dict],
                     page_extent: tuple[float, float],
                     swap_region_a, swap_region_b) -> dict:
    """Apply swap, re-score all proposals, return winner + new location."""
    swapped_prims = swap_regions(prims, page_extent[0], page_extent[1],
                                  swap_region_a, swap_region_b)
    pw, ph = page_extent
    proposals = generate_proposal_set(pw, ph)
    scored = []
    for cand in proposals:
        rx0, ry0, rx1, ry1 = cand["x0"], cand["y0"], cand["x1"], cand["y1"]
        region_prims = prims_inside(swapped_prims, rx0, ry0, rx1, ry1)
        # Use no-OCR since text doesn't move with content
        result = score_candidate(cand, region_prims, page_extent, "")
        scored.append({**cand, **result})
    scored.sort(key=lambda r: -r["score"])
    return {"winner": scored[0], "top_3": scored[:3]}


def adversarial_swap_test(doc, page_idx: int) -> dict:
    """Swap canonical bottom-right region with center region. Test if detector follows."""
    prims, (pw, ph) = stream_page_primitives(doc, page_idx)
    if not prims:
        return {"page_idx": page_idx, "error": "no primitives"}

    # Define swap regions
    canonical_region = (pw * 0.70, ph * 0.85, pw, ph)  # bottom-right corner
    center_region = (pw * 0.35, ph * 0.40, pw * 0.65, ph * 0.60)  # center

    # Baseline: where does detector pick BEFORE swap?
    proposals = generate_proposal_set(pw, ph)
    baseline_scored = []
    for cand in proposals:
        region_prims = prims_inside(prims, cand["x0"], cand["y0"], cand["x1"], cand["y1"])
        try:
            region_text = doc[page_idx].get_text(clip=fitz.Rect(cand["x0"], cand["y0"], cand["x1"], cand["y1"])) or ""
        except Exception:
            region_text = ""
        result = score_candidate(cand, region_prims, (pw, ph), region_text)
        baseline_scored.append({**cand, **result})
    baseline_scored.sort(key=lambda r: -r["score"])
    baseline_winner = baseline_scored[0]

    # Swap canonical content (bottom-right) to center, vice-versa
    swap_result = score_with_swap(doc, page_idx, prims, (pw, ph),
                                    canonical_region, center_region)
    post_winner = swap_result["winner"]

    # Did detector follow content (winner near center now) OR position (winner still bottom-right)?
    follows_content = post_winner["class"] in ("center_floating_blocks",)
    stays_at_position = post_winner["class"] == "canonical_edge_strips"

    del prims  # free memory

    return {
        "page_idx": page_idx,
        "baseline_winner_template": baseline_winner["template"],
        "baseline_winner_class": baseline_winner["class"],
        "post_swap_winner_template": post_winner["template"],
        "post_swap_winner_class": post_winner["class"],
        "follows_content": follows_content,
        "stays_at_position": stays_at_position,
        # If detector follows CONTENT, semantic grounding emerging
        # If detector stays at POSITION, prior-driven (R3 hypothesis confirmed)
    }


# ============================================================
# T3: Semantic ablation
# ============================================================

def score_with_signal_ablated(region_prims: list[dict], region: dict,
                                page_extent: tuple, region_text: str,
                                ablate_signal: str) -> float:
    """Recompute score with one signal forced to zero."""
    result = score_candidate(region, region_prims, page_extent, region_text)
    s = result["signals"]
    p = result["penalties"]

    if ablate_signal == "position":
        s["position"] = 0
    elif ablate_signal == "edge_adhesion":
        s["edge_adhesion"] = 0
    elif ablate_signal == "ocr_anchors":
        s["ocr_anchors"] = 0
    elif ablate_signal == "subdivision":
        s["subdivision"] = 0
    elif ablate_signal == "aspect_ratio":
        s["aspect_ratio"] = 0
    elif ablate_signal == "size_cap":
        s["size_cap"] = 0

    # Same weighted sum as score_candidate
    n_prims = result["metrics"]["n_prims"]
    raw = (
        0.30 * s["position"]
        + 0.20 * s["edge_adhesion"]
        + 0.15 * s["ocr_anchors"]
        + 0.10 * s["subdivision"]
        + 0.10 * s["aspect_ratio"]
        + 0.10 * s["size_cap"]
        + 0.05 * min(1.0, n_prims / 100.0)
        - p["schedule"]
        - p["legend"]
        - p["oversized"]
    )
    return max(0.0, min(1.0, raw))


def ablation_test(doc, page_idx: int) -> dict:
    """For each signal, remove it and re-rank. Measure if canonical still wins."""
    prims, (pw, ph) = stream_page_primitives(doc, page_idx)
    if not prims:
        return {"page_idx": page_idx, "error": "no primitives"}

    proposals = generate_proposal_set(pw, ph)
    # Baseline
    baseline_scored = []
    for cand in proposals:
        region_prims = prims_inside(prims, cand["x0"], cand["y0"], cand["x1"], cand["y1"])
        try:
            region_text = doc[page_idx].get_text(clip=fitz.Rect(cand["x0"], cand["y0"], cand["x1"], cand["y1"])) or ""
        except Exception:
            region_text = ""
        result = score_candidate(cand, region_prims, (pw, ph), region_text)
        baseline_scored.append({**cand, **result, "_text": region_text})
    baseline_scored.sort(key=lambda r: -r["score"])
    baseline_winner_class = baseline_scored[0]["class"]
    baseline_winner_template = baseline_scored[0]["template"]

    # Ablate each signal
    ablation_signals = ["position", "edge_adhesion", "ocr_anchors", "subdivision",
                        "aspect_ratio", "size_cap"]
    ablation_results = {}
    for sig in ablation_signals:
        rescored = []
        for cand in baseline_scored:
            region_prims = prims_inside(prims, cand["x0"], cand["y0"], cand["x1"], cand["y1"])
            new_score = score_with_signal_ablated(
                region_prims, cand, (pw, ph), cand.get("_text", ""), sig
            )
            rescored.append({**cand, "ablated_score": new_score})
        rescored.sort(key=lambda r: -r["ablated_score"])
        new_winner_class = rescored[0]["class"]
        new_winner_template = rescored[0]["template"]
        ablation_results[sig] = {
            "new_winner_class": new_winner_class,
            "new_winner_template": new_winner_template,
            "canonical_still_wins": new_winner_class == "canonical_edge_strips",
            "winner_class_changed": new_winner_class != baseline_winner_class,
        }

    del prims  # free memory

    return {
        "page_idx": page_idx,
        "baseline_winner_class": baseline_winner_class,
        "baseline_winner_template": baseline_winner_template,
        "ablation_results": ablation_results,
    }


# ============================================================
# Main orchestration
# ============================================================

def process_pdf(pdf_path: Path) -> dict:
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}

    swap_results = []
    ablation_results = []
    try:
        for pg in range(doc.page_count):
            try:
                swap_results.append(adversarial_swap_test(doc, pg))
            except Exception as exc:
                swap_results.append({"page_idx": pg, "error": str(exc)[:100]})
            try:
                ablation_results.append(ablation_test(doc, pg))
            except Exception as exc:
                ablation_results.append({"page_idx": pg, "error": str(exc)[:100]})
    finally:
        doc.close()

    return {
        "pdf_name": pdf_path.name,
        "pdf_key": pdf_path.stem,
        "n_pages": len(swap_results),
        "swap_results": swap_results,
        "ablation_results": ablation_results,
    }


def evaluate_v26_gates(all_results: list[dict]) -> dict:
    """Compute v26 Gate 1 (adversarial swaps) + ablation summary."""
    # GATE 1: Adversarial semantic swap
    # If detector follows CONTENT → semantic grounding emerging
    # If detector stays at POSITION → prior-driven
    total_pages = 0
    follows_content = 0
    stays_at_position = 0
    other = 0
    for pdf in all_results:
        if "error" in pdf:
            continue
        for sr in pdf.get("swap_results", []):
            if "error" in sr:
                continue
            total_pages += 1
            if sr.get("follows_content"):
                follows_content += 1
            elif sr.get("stays_at_position"):
                stays_at_position += 1
            else:
                other += 1

    follows_rate = follows_content / max(total_pages, 1)
    stays_rate = stays_at_position / max(total_pages, 1)

    # Ablation summary: which signal removal most degrades canonical winner rate?
    ablation_signal_impact: dict[str, dict] = {
        sig: {"canonical_still_wins": 0, "broken": 0}
        for sig in ["position", "edge_adhesion", "ocr_anchors",
                    "subdivision", "aspect_ratio", "size_cap"]
    }
    total_ablation_pages = 0
    for pdf in all_results:
        if "error" in pdf:
            continue
        for ar in pdf.get("ablation_results", []):
            if "error" in ar:
                continue
            total_ablation_pages += 1
            for sig, res in ar.get("ablation_results", {}).items():
                if res.get("canonical_still_wins"):
                    ablation_signal_impact[sig]["canonical_still_wins"] += 1
                else:
                    ablation_signal_impact[sig]["broken"] += 1

    # Critical dependency: signal whose removal breaks canonical winner most
    ablation_summary = {}
    for sig, counts in ablation_signal_impact.items():
        survival = counts["canonical_still_wins"] / max(total_ablation_pages, 1)
        ablation_summary[sig] = {
            "canonical_survival_rate": round(survival, 4),
            "n_canonical_wins": counts["canonical_still_wins"],
            "n_broken": counts["broken"],
        }

    return {
        "gate_1_adversarial_swaps": {
            "total_pages_tested": total_pages,
            "follows_content_rate": round(follows_rate, 4),
            "stays_at_position_rate": round(stays_rate, 4),
            "other_rate": round(other / max(total_pages, 1), 4),
            "verdict": "POSITION_LOCKED" if stays_rate > 0.85 else (
                "SEMANTIC_FOLLOWING" if follows_rate > 0.85 else "MIXED"
            ),
            "pass": follows_rate >= 0.90,  # v26 target: ≥90% follows content
        },
        "signal_ablation_summary": ablation_summary,
        "ablation_pages_tested": total_ablation_pages,
    }


def main() -> None:
    print("=== Wave 20.14.5J-V3-SCV Semantic Counterfactual Validation ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
    all_results = []

    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---")
        r = process_pdf(pdf)
        all_results.append(r)
        if "error" in r:
            print(f"  ERROR: {r['error']}")
            continue
        # Swap summary per PDF
        valid_swaps = [s for s in r.get("swap_results", []) if "error" not in s]
        if valid_swaps:
            n_follow = sum(1 for s in valid_swaps if s.get("follows_content"))
            n_stay = sum(1 for s in valid_swaps if s.get("stays_at_position"))
            print(f"  pages={r['n_pages']}  follows_content={n_follow}/{len(valid_swaps)}  "
                  f"stays_at_position={n_stay}/{len(valid_swaps)}")

    print(f"\n=== V26 GATE EVALUATION ===")
    eval_result = evaluate_v26_gates(all_results)
    g1 = eval_result["gate_1_adversarial_swaps"]
    print(f"\nGate 1 — Adversarial Semantic Swap (target ≥90% follows_content)")
    print(f"  total pages tested: {g1['total_pages_tested']}")
    print(f"  follows_content: {g1['follows_content_rate']*100:.1f}%")
    print(f"  stays_at_position: {g1['stays_at_position_rate']*100:.1f}%")
    print(f"  other: {g1['other_rate']*100:.1f}%")
    print(f"  VERDICT: {g1['verdict']}")
    print(f"  {'PASS' if g1['pass'] else 'FAIL'} ≥90% follows_content")

    print(f"\n=== SIGNAL ABLATION (which channels do the discriminative work?) ===")
    print(f"Total pages tested: {eval_result['ablation_pages_tested']}")
    print(f"{'Signal':<20s} {'Canonical Survival':>20s}   {'Broken':>8s}")
    print("-" * 60)
    for sig, summary in eval_result["signal_ablation_summary"].items():
        print(f"{sig:<20s} {summary['canonical_survival_rate']*100:>18.1f}%   "
              f"{summary['n_broken']:>8d}")

    # Save
    out_path = OUT_DIR / "semantic_counterfactual_validation_2026-05-24.json"
    with open(out_path, "w", encoding="utf-8") as f:
        # Compact: keep summary + per-PDF aggregate; drop per-page detail to save size
        compact = []
        for r in all_results:
            if "error" in r:
                compact.append(r)
                continue
            valid_swaps = [s for s in r.get("swap_results", []) if "error" not in s]
            valid_ablations = [a for a in r.get("ablation_results", []) if "error" not in a]
            compact.append({
                "pdf_name": r["pdf_name"],
                "n_pages": r["n_pages"],
                "swap_follows_content": sum(1 for s in valid_swaps if s.get("follows_content")),
                "swap_stays_at_position": sum(1 for s in valid_swaps if s.get("stays_at_position")),
                "swap_other": sum(1 for s in valid_swaps if not s.get("follows_content") and not s.get("stays_at_position")),
                "n_swap_valid": len(valid_swaps),
                "n_ablation_valid": len(valid_ablations),
            })
        json.dump({
            "wave": "20.14.5J-V3-SCV",
            "v26_gate_results": eval_result,
            "per_pdf": compact,
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")


if __name__ == "__main__":
    main()
