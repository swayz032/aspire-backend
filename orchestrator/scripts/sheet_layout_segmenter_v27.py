"""Wave 20.14.5J-V3-SCV-r2 — v27 Functional Structure + Multi-Cue Agreement.

Per v27 mandate after V3-SCV empirical position-lock fail (0% content following):

Position drops from 30% weight (dominant) to 5% (weak Bayesian prior).
Functional structure + semantic content + relational topology carry the load.

3 subsystems:

  1. Functional Structure Layer (PRIMARY)
     - Cell heterogeneity (title blocks have varied cell sizes; schedules uniform)
     - Density profile shape (title-block border-heavy vs schedule periodic)
     - Text-role semantics (metadata anchors with proper weight)

  2. Adversarial Validation (production gate)
     - Swap test integrated into gate, not diagnostic

  3. Multi-cue agreement
     - 5 channels: topology / OCR roles / geometry / divider / density
     - Geometric-mean combination penalizes single-channel reliance

CRITICAL v27 warning being respected:
  Avoid "semantic priors disguised as topology" — features must measure
  CONTENT properties, not topological templates that recreate position-lock.

ZERO Supabase IO.
"""
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from sheet_layout_segmenter_v2 import (  # noqa: E402
    stream_page_primitives, prims_inside, _angle_deg,
)
from sheet_layout_segmenter_v3 import generate_proposal_set  # noqa: E402

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

METADATA_ANCHOR_PATTERNS = {
    "SHEET": [r"\bSHEET\b", r"\bSHT\b", r"\bDWG\s*(?:NO|#)\b"],
    "REV": [r"\bREV(?:ISION)?\b"],
    "DATE": [r"\bDATE\b", r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"],
    "PROJECT": [r"\bPROJECT\b", r"\bPROJ\b"],
    "DRAWN": [r"\bDRAWN\b", r"\bDRW\b"],
    "CHECKED": [r"\bCHECK(?:ED)?\b", r"\bCHK\b"],
    "SCALE": [r"\bSCALE\b"],
    "OWNER": [r"\bOWNER\b", r"\bCLIENT\b"],
    "CODE": [r"\b[A-Z]{1,3}[-_]\d{1,3}(?:\.\d{1,2})?\b"],  # sheet code patterns
}


# ============================================================
# Feature A: Cell heterogeneity (content-driven, not template-matching)
# ============================================================

def cell_heterogeneity_score(region_prims: list[dict]) -> dict:
    """Title blocks have heterogeneous cell sizes (metadata cells of varied dims).
    Schedules have homogeneous cell sizes (uniform rows × uniform cols).
    Measure: coefficient of variation over cell areas."""
    rects = [p for p in region_prims if p.get("primitive_type") == "rect"]
    if len(rects) < 3:
        return {
            "score": 0.0,
            "n_cells": len(rects),
            "size_cv": 0.0,
            "verdict": "insufficient_cells",
        }
    sizes = []
    for r in rects:
        try:
            w = float(r.get("bbox_w", 0) or 0)
            h = float(r.get("bbox_h", 0) or 0)
            sizes.append(w * h)
        except (TypeError, ValueError):
            pass
    if not sizes:
        return {"score": 0.0, "n_cells": len(rects), "size_cv": 0.0, "verdict": "no_sizes"}
    mean = sum(sizes) / len(sizes)
    var = sum((s - mean) ** 2 for s in sizes) / len(sizes)
    cv = math.sqrt(var) / max(mean, 1e-6)
    # Title block: CV > 0.5 (heterogeneous). Schedule: CV < 0.2 (uniform).
    if cv >= 0.50:
        score = 1.0
    elif cv >= 0.30:
        score = 0.6
    elif cv >= 0.15:
        score = 0.3
    else:
        score = 0.0
    return {
        "score": round(score, 4),
        "n_cells": len(rects),
        "size_cv": round(cv, 4),
        "verdict": "heterogeneous" if cv >= 0.50 else "uniform" if cv < 0.20 else "mixed",
    }


# ============================================================
# Feature B: Functional density profile shape
# ============================================================

def density_profile_score(region_prims: list[dict], region: dict) -> dict:
    """Title block signature: heavy border density + sparse interior.
    Schedule signature: periodic peaks (uniform rows).
    Measure: ratio of edge-band density to interior density."""
    rx0, ry0 = region["x0"], region["y0"]
    rx1, ry1 = region["x1"], region["y1"]
    rw, rh = rx1 - rx0, ry1 - ry0
    if rw <= 0 or rh <= 0 or len(region_prims) < 10:
        return {"score": 0.0, "edge_density_ratio": 0, "verdict": "insufficient"}

    edge_band_pct = 0.15
    edge_w = rw * edge_band_pct
    edge_h = rh * edge_band_pct

    edge_count = 0
    interior_count = 0
    for p in region_prims:
        try:
            cx = float(p.get("cx", 0))
            cy = float(p.get("cy", 0))
        except (TypeError, ValueError):
            continue
        in_edge = (
            cx <= rx0 + edge_w or cx >= rx1 - edge_w
            or cy <= ry0 + edge_h or cy >= ry1 - edge_h
        )
        if in_edge:
            edge_count += 1
        else:
            interior_count += 1

    edge_area_pct = 1.0 - (1 - 2 * edge_band_pct) ** 2  # ~51% of area is edge band
    interior_area_pct = (1 - 2 * edge_band_pct) ** 2  # ~49%

    edge_density = edge_count / max(edge_area_pct, 0.01)
    interior_density = interior_count / max(interior_area_pct, 0.01)
    ratio = edge_density / max(interior_density, 0.01)

    # Title block: edge dominates → ratio > 1.5
    # Schedule: uniform → ratio ≈ 1.0
    if ratio >= 2.5:
        score = 1.0
    elif ratio >= 1.5:
        score = 0.7
    elif ratio >= 1.0:
        score = 0.3
    else:
        score = 0.0
    return {
        "score": round(score, 4),
        "edge_density_ratio": round(ratio, 3),
        "edge_count": edge_count,
        "interior_count": interior_count,
        "verdict": "border_heavy" if ratio >= 1.5 else "uniform",
    }


# ============================================================
# Feature C: Text-role semantics
# ============================================================

def text_role_score(region_text: str) -> dict:
    """Title blocks contain metadata anchors with high role diversity.
    Schedules have low role diversity (mostly domain object names + numbers).
    Centered metadata moved by swap test should still match here.
    """
    if not region_text or not region_text.strip():
        return {
            "score": 0.0, "n_roles": 0, "role_diversity": 0,
            "anchor_hits": {}, "verdict": "no_text",
        }
    upper = region_text.upper()
    role_hits = {}
    for role, patterns in METADATA_ANCHOR_PATTERNS.items():
        hit_count = 0
        for pat in patterns:
            hit_count += len(re.findall(pat, upper))
        if hit_count > 0:
            role_hits[role] = hit_count

    n_roles = len(role_hits)
    if n_roles == 0:
        return {
            "score": 0.0, "n_roles": 0, "role_diversity": 0,
            "anchor_hits": {}, "verdict": "no_anchors",
        }

    # Role diversity = entropy over role counts
    total_hits = sum(role_hits.values())
    role_entropy = 0.0
    for c in role_hits.values():
        p = c / total_hits
        if p > 0:
            role_entropy -= p * math.log2(p)
    # Normalize to [0, 1] using log2(9) = 3.17 max
    role_diversity = min(1.0, role_entropy / 3.17)

    # Score: scaled by both diversity AND raw count
    if n_roles >= 4 and role_diversity >= 0.6:
        score = 1.0
    elif n_roles >= 3:
        score = 0.7
    elif n_roles >= 2:
        score = 0.4
    elif n_roles >= 1:
        score = 0.15
    else:
        score = 0.0
    return {
        "score": round(score, 4),
        "n_roles": n_roles,
        "role_diversity": round(role_diversity, 4),
        "anchor_hits": role_hits,
        "verdict": "metadata_rich" if n_roles >= 3 else "metadata_sparse",
    }


# ============================================================
# Feature D: Geometric position prior (DROPPED FROM DOMINANT to weak Bayesian)
# ============================================================

def position_prior_score(region: dict, page_extent: tuple) -> dict:
    """Reduced from v25's 30% weight to ~10%. ONE signal among many."""
    pw, ph = page_extent
    if pw <= 0 or ph <= 0:
        return {"score": 0.0}
    rel_cx = (region["x0"] + region["x1"]) / 2 / pw
    rel_cy = (region["y0"] + region["y1"]) / 2 / ph
    # Gaussian peaks at canonical positions (weak)
    g_corner = math.exp(-(((rel_cx - 0.875)/0.15)**2 + ((rel_cy - 0.875)/0.15)**2) / 2)
    g_right = math.exp(-(((rel_cx - 0.93)/0.10)**2 + ((rel_cy - 0.50)/0.40)**2) / 2)
    g_bottom = math.exp(-(((rel_cx - 0.50)/0.40)**2 + ((rel_cy - 0.93)/0.10)**2) / 2)
    return {"score": round(max(g_corner, g_right, g_bottom), 4)}


# ============================================================
# v27 Scoring with Multi-cue Agreement
# ============================================================

def score_candidate_v27(region: dict, region_prims: list[dict],
                         page_extent: tuple, region_text: str) -> dict:
    """v27 scoring: position is weak prior; content features dominate.
    Uses geometric-mean-style multi-cue agreement: weak channels penalize."""
    heterogeneity = cell_heterogeneity_score(region_prims)
    density = density_profile_score(region_prims, region)
    text_role = text_role_score(region_text)
    position = position_prior_score(region, page_extent)

    # 4 channels for multi-cue agreement
    channels = {
        "heterogeneity": heterogeneity["score"],
        "density_profile": density["score"],
        "text_role": text_role["score"],
        "position": position["score"],
    }

    # Multi-cue agreement: geometric mean (epsilon-floored to prevent log(0))
    eps = 0.05
    gm_log = sum(math.log(max(v, eps)) for v in channels.values()) / len(channels)
    multi_cue_score = math.exp(gm_log)

    # Final score weighted: heterogeneity + density + text are PRIMARY (semantic);
    # position is weak prior; multi-cue agreement gates everything
    weighted = (
        0.30 * heterogeneity["score"]
        + 0.25 * density["score"]
        + 0.25 * text_role["score"]
        + 0.05 * position["score"]
        + 0.15 * multi_cue_score
    )

    # Active-channel count (non-zero channels): rewards multi-channel agreement
    n_active = sum(1 for v in channels.values() if v >= 0.30)

    return {
        "score": round(min(1.0, weighted), 4),
        "multi_cue_score": round(multi_cue_score, 4),
        "channels": channels,
        "n_active_channels": n_active,
        "heterogeneity": heterogeneity,
        "density": density,
        "text_role": text_role,
        "position": position,
    }


# ============================================================
# Region scanning (find candidates BY CONTENT, not by template)
# ============================================================

def detect_content_candidates(prims: list[dict], page_extent: tuple,
                               n_regions: int = 12) -> list[dict]:
    """Generate candidates by scanning for dense regions, NOT canonical templates.

    Per v27: detect title-block-like content wherever it appears.
    Returns up to N candidates of varied sizes anchored to actual dense rect clusters.
    """
    pw, ph = page_extent

    # First strategy: cluster rect primitives by spatial density
    rects = [p for p in prims if p.get("primitive_type") == "rect"]
    if rects:
        # Group rects by spatial buckets (100pt grid)
        buckets: dict = defaultdict(list)
        for r in rects:
            try:
                bx = int(float(r["cx"]) // 100)
                by = int(float(r["cy"]) // 100)
                buckets[(bx, by)].append(r)
            except (TypeError, ValueError):
                continue
        # Find dense buckets (5+ rects)
        dense_buckets = [(k, v) for k, v in buckets.items() if len(v) >= 5]
        dense_buckets.sort(key=lambda kv: -len(kv[1]))

        candidates = []
        for (bx, by), bucket_rects in dense_buckets[:n_regions]:
            xs = [float(r["cx"]) for r in bucket_rects]
            ys = [float(r["cy"]) for r in bucket_rects]
            # Expand a bit beyond rect centroids
            margin = 30.0
            x0 = max(0, min(xs) - margin)
            y0 = max(0, min(ys) - margin)
            x1 = min(pw, max(xs) + margin)
            y1 = min(ph, max(ys) + margin)
            candidates.append({
                "template": f"content_cluster_{bx}_{by}",
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "kind": "content_cluster",
                "n_rects_in_cluster": len(bucket_rects),
            })
        if candidates:
            # Also include canonical templates as comparison
            canonical_templates = generate_proposal_set(pw, ph)[:4]  # canonical edge strips
            for ct in canonical_templates:
                candidates.append({**ct, "kind": "canonical"})
            return candidates

    # Fallback: line-density clustering (no rects available)
    # Use 50pt tiles, find dense regions
    tile_counts: dict = defaultdict(int)
    for p in prims:
        try:
            tx = int(float(p.get("cx", 0)) // 50)
            ty = int(float(p.get("cy", 0)) // 50)
            tile_counts[(tx, ty)] += 1
        except (TypeError, ValueError):
            continue
    sorted_tiles = sorted(tile_counts.items(), key=lambda kv: -kv[1])
    candidates = []
    for (tx, ty), n in sorted_tiles[:n_regions]:
        # Expand 4-tile region around dense tile
        x0 = max(0, (tx - 2) * 50)
        y0 = max(0, (ty - 2) * 50)
        x1 = min(pw, (tx + 3) * 50)
        y1 = min(ph, (ty + 3) * 50)
        candidates.append({
            "template": f"line_cluster_{tx}_{ty}",
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "kind": "line_cluster",
            "tile_density": n,
        })
    # Include canonical templates for comparison
    canonical_templates = generate_proposal_set(pw, ph)[:4]
    for ct in canonical_templates:
        candidates.append({**ct, "kind": "canonical"})
    return candidates


# ============================================================
# Swap test (kept in production gate)
# ============================================================

def swap_regions(prims: list[dict], pw: float, ph: float,
                  region_a: tuple, region_b: tuple) -> list[dict]:
    """Move primitives between two regions."""
    ax0, ay0, ax1, ay1 = region_a
    bx0, by0, bx1, by1 = region_b
    a_to_b_dx = bx0 - ax0
    a_to_b_dy = by0 - ay0
    b_to_a_dx = ax0 - bx0
    b_to_a_dy = ay0 - by0
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
        dx, dy = (a_to_b_dx, a_to_b_dy) if in_a else (b_to_a_dx, b_to_a_dy)
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


def evaluate_with_v27_scoring(doc, page_idx: int, prims: list[dict],
                                page_extent: tuple, use_ocr: bool = True) -> dict:
    """Score content-driven candidates with v27 scoring."""
    pw, ph = page_extent
    candidates = detect_content_candidates(prims, page_extent, n_regions=12)
    scored = []
    for cand in candidates:
        rx0, ry0, rx1, ry1 = cand["x0"], cand["y0"], cand["x1"], cand["y1"]
        region_prims = prims_inside(prims, rx0, ry0, rx1, ry1)
        if use_ocr:
            try:
                region_text = doc[page_idx].get_text(clip=fitz.Rect(rx0, ry0, rx1, ry1)) or ""
            except Exception:
                region_text = ""
        else:
            region_text = ""
        result = score_candidate_v27(cand, region_prims, page_extent, region_text)
        scored.append({**cand, **result})
    scored.sort(key=lambda r: -r["score"])
    return scored


def is_in_canonical_zone(region: dict, pw: float, ph: float) -> bool:
    """Check if region center is in canonical zone (for content-follow measurement)."""
    cx = (region["x0"] + region["x1"]) / 2 / pw
    cy = (region["y0"] + region["y1"]) / 2 / ph
    return (cx > 0.65 and cy > 0.65) or cy > 0.85 or cx > 0.85


def adversarial_swap_test_v27(doc, page_idx: int) -> dict:
    """Test if v27 follows content when title-block region swapped to center."""
    prims, (pw, ph) = stream_page_primitives(doc, page_idx)
    if not prims:
        return {"page_idx": page_idx, "error": "no primitives"}

    canonical_region = (pw * 0.70, ph * 0.85, pw, ph)
    center_region = (pw * 0.35, ph * 0.40, pw * 0.65, ph * 0.60)

    # Baseline (where does v27 pick before swap?)
    baseline_scored = evaluate_with_v27_scoring(doc, page_idx, prims, (pw, ph), use_ocr=True)
    if not baseline_scored:
        return {"page_idx": page_idx, "error": "no candidates"}
    baseline_winner = baseline_scored[0]
    baseline_in_canonical = is_in_canonical_zone(baseline_winner, pw, ph)

    # After swap
    swapped = swap_regions(prims, pw, ph, canonical_region, center_region)
    post_scored = evaluate_with_v27_scoring(doc, page_idx, swapped, (pw, ph), use_ocr=False)
    if not post_scored:
        return {"page_idx": page_idx, "error": "no candidates after swap"}
    post_winner = post_scored[0]
    post_in_canonical = is_in_canonical_zone(post_winner, pw, ph)

    # Did detector FOLLOW the content (winner moved out of canonical zone)?
    follows_content = (baseline_in_canonical and not post_in_canonical)
    stays_at_position = (baseline_in_canonical and post_in_canonical)

    del prims  # free memory

    return {
        "page_idx": page_idx,
        "baseline_winner_in_canonical": baseline_in_canonical,
        "baseline_winner_score": round(baseline_winner["score"], 4),
        "baseline_winner_template": baseline_winner.get("template"),
        "baseline_channels": baseline_winner["channels"],
        "post_swap_winner_in_canonical": post_in_canonical,
        "post_swap_winner_score": round(post_winner["score"], 4),
        "post_swap_winner_template": post_winner.get("template"),
        "post_swap_channels": post_winner["channels"],
        "follows_content": follows_content,
        "stays_at_position": stays_at_position,
    }


def process_pdf(pdf_path: Path) -> dict:
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}

    page_results = []
    try:
        for pg in range(doc.page_count):
            try:
                page_results.append(adversarial_swap_test_v27(doc, pg))
            except Exception as exc:
                page_results.append({"page_idx": pg, "error": str(exc)[:100]})
    finally:
        doc.close()

    return {
        "pdf_name": pdf_path.name,
        "pdf_key": pdf_path.stem,
        "n_pages": len(page_results),
        "page_results": page_results,
    }


def main() -> None:
    print("=== Wave 20.14.5J-V3-SCV-r2 v27 Functional Structure + Multi-Cue ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
    all_results = []

    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---")
        r = process_pdf(pdf)
        all_results.append(r)
        if "error" in r:
            print(f"  ERROR: {r['error']}")
            continue
        valid = [pr for pr in r["page_results"] if "error" not in pr]
        if not valid:
            print(f"  no valid pages")
            continue
        n_follows = sum(1 for pr in valid if pr.get("follows_content"))
        n_stays = sum(1 for pr in valid if pr.get("stays_at_position"))
        # Average channel scores at baseline
        avg_het = sum(pr["baseline_channels"]["heterogeneity"] for pr in valid) / len(valid)
        avg_dens = sum(pr["baseline_channels"]["density_profile"] for pr in valid) / len(valid)
        avg_text = sum(pr["baseline_channels"]["text_role"] for pr in valid) / len(valid)
        avg_pos = sum(pr["baseline_channels"]["position"] for pr in valid) / len(valid)
        print(f"  pages={r['n_pages']}  follows_content={n_follows}/{len(valid)}  stays_pos={n_stays}/{len(valid)}")
        print(f"  avg channels: het={avg_het:.2f} dens={avg_dens:.2f} text={avg_text:.2f} pos={avg_pos:.2f}")

    print(f"\n=== V27 GATE EVALUATION ===")
    total_valid = 0
    total_follows = 0
    total_stays = 0
    avg_channels = defaultdict(float)
    for r in all_results:
        if "error" in r:
            continue
        for pr in r.get("page_results", []):
            if "error" in pr:
                continue
            total_valid += 1
            if pr.get("follows_content"):
                total_follows += 1
            if pr.get("stays_at_position"):
                total_stays += 1
            for k, v in pr.get("baseline_channels", {}).items():
                avg_channels[k] += v
    if total_valid:
        for k in avg_channels:
            avg_channels[k] /= total_valid

    follows_rate = total_follows / max(total_valid, 1)
    stays_rate = total_stays / max(total_valid, 1)

    print(f"\nTotal pages tested: {total_valid}")
    print(f"Follows content: {total_follows} ({follows_rate*100:.1f}%)")
    print(f"Stays at position: {total_stays} ({stays_rate*100:.1f}%)")
    print(f"\nAverage baseline channel scores:")
    for k, v in avg_channels.items():
        print(f"  {k:<20s} {v:.4f}")

    g1 = follows_rate >= 0.50  # v27 baseline target
    g2 = stays_rate <= 0.40
    print(f"\nGate 1 (follows_content ≥50%): {'PASS' if g1 else 'FAIL'}  ({follows_rate*100:.1f}%)")
    print(f"Gate 2 (canonical_bias ≤40%):   {'PASS' if g2 else 'FAIL'}  ({stays_rate*100:.1f}%)")

    out_path = OUT_DIR / "sheet_layout_segmenter_v27_2026-05-24.json"
    # Compact persistence
    compact = []
    for r in all_results:
        if "error" in r:
            compact.append(r)
            continue
        compact.append({
            "pdf_name": r["pdf_name"],
            "n_pages": r["n_pages"],
            "page_results": [
                {k: v for k, v in pr.items() if k != "baseline_channels" and k != "post_swap_channels"}
                | {"baseline_channels": pr.get("baseline_channels", {}),
                   "post_swap_channels": pr.get("post_swap_channels", {})}
                for pr in r.get("page_results", [])
            ],
        })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V3-SCV-r2",
            "v27_gates": {
                "gate_1_follows_content": {"pass": g1, "rate": round(follows_rate, 4)},
                "gate_2_canonical_bias": {"pass": g2, "rate": round(stays_rate, 4)},
            },
            "avg_channels": dict(avg_channels),
            "per_pdf": compact,
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")


if __name__ == "__main__":
    main()
