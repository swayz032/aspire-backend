"""Wave 20.14.5J-V3-PSAL — Primitive Semantic Attribution Layer (v28).

Per v28 architectural pivot: 5 iterations of region-level scoring exhausted.
Different system class required.

CORE MECHANISM:
  Each primitive gets a P(title_block_role) computed from LOCAL CONTEXT
  (not absolute position). When title-block content moves under swap,
  the local context moves WITH it, so high-P primitives shift location.
  Detected title block = connected cluster of high-P primitives.

This is the architectural mechanism that enables content-following.

Features per primitive (LOCAL, not absolute):
  - local_orthogonal_neighbor_count (lines at 0°/90° within 30pt)
  - local_short_line_ratio (lines <20pt within 50pt radius)
  - local_rect_count (rectangles within 50pt)
  - local_density (primitive count within 30pt)
  - local_alignment_consistency (low angle variance = table structure)
  - local_text_proximity (distance to nearest text span)
  - border_distance (ONE feature among many, NOT dominant)

Role classes (focus on title_block detection for Gate A):
  - title_metadata_cell (target class)
  - drawing_geometry (negative class — main drawing area)
  - border_frame (negative — sheet border line)
  - other

v28 acceptance gates:
  A: Semantic relocation ≥70% (PRIMARY)
  B: Border invariance
  C: Primitive attribution accuracy ≥80%
  D: Counterfactual robustness ≥90%

ZERO Supabase IO. Streaming pipeline preserved.
"""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from sheet_layout_segmenter_v2 import stream_page_primitives  # noqa: E402

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

ORTHO_TOLERANCE_DEG = 5.0
NEIGHBOR_RADIUS_PT = 30.0
WIDE_RADIUS_PT = 50.0
GRID_CELL_SIZE = 50.0  # matches widest radius -> 1 cell-ring sufficient
MAX_PRIMITIVES_PER_PAGE = 8000  # subsample for tractable runtime
SUBSET_PDFS_ONLY = True  # process small PDFs first; skip GAVNN 39pp + 21030 13pp initially
SKIP_LIST = {"GAVNN_ADDENDUM_1.pdf", "21030 ENG_Rev1 240801_Signed.pdf"}


# ============================================================
# Spatial grid index for O(N) neighborhood queries
# ============================================================

def build_spatial_grid(prims: list[dict], cell_size: float = GRID_CELL_SIZE
                        ) -> dict[tuple[int, int], list[int]]:
    """Bucket primitive indices into spatial grid for fast neighbor lookup."""
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, p in enumerate(prims):
        try:
            tx = int(float(p.get("cx", 0)) // cell_size)
            ty = int(float(p.get("cy", 0)) // cell_size)
            grid[(tx, ty)].append(i)
        except (TypeError, ValueError):
            pass
    return grid


def neighbors_within(prim_idx: int, prims: list[dict], grid: dict,
                       radius: float, cell_size: float = GRID_CELL_SIZE) -> list[int]:
    """Find indices of primitives within `radius` of prim_idx."""
    p = prims[prim_idx]
    try:
        cx = float(p["cx"])
        cy = float(p["cy"])
    except (KeyError, TypeError, ValueError):
        return []
    n_cells = int(math.ceil(radius / cell_size))
    tx0 = int(cx // cell_size)
    ty0 = int(cy // cell_size)
    candidates = []
    for dx in range(-n_cells, n_cells + 1):
        for dy in range(-n_cells, n_cells + 1):
            for n_idx in grid.get((tx0 + dx, ty0 + dy), []):
                if n_idx == prim_idx:
                    continue
                np_ = prims[n_idx]
                try:
                    ncx = float(np_["cx"])
                    ncy = float(np_["cy"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.hypot(ncx - cx, ncy - cy) <= radius:
                    candidates.append(n_idx)
    return candidates


# ============================================================
# Per-primitive feature extraction
# ============================================================

def _angle_deg(p):
    try:
        dx = float(p["x1"]) - float(p["x0"])
        dy = float(p["y1"]) - float(p["y0"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return None
    return math.degrees(math.atan2(dy, dx)) % 180


def is_orthogonal(angle: float | None) -> bool:
    if angle is None:
        return False
    return (angle <= ORTHO_TOLERANCE_DEG
            or angle >= 180 - ORTHO_TOLERANCE_DEG
            or abs(angle - 90) <= ORTHO_TOLERANCE_DEG)


def primitive_features(prim_idx: int, prims: list[dict], grid: dict,
                        page_extent: tuple) -> dict:
    """Compute LOCAL-CONTEXT features per primitive."""
    p = prims[prim_idx]
    pw, ph = page_extent
    try:
        cx = float(p["cx"])
        cy = float(p["cy"])
    except (KeyError, TypeError, ValueError):
        return {}
    ptype = p.get("primitive_type") or "p"
    length = float(p.get("length", 0) or 0)
    angle = _angle_deg(p)

    # Find neighbors at two radii
    close_nbrs = neighbors_within(prim_idx, prims, grid, NEIGHBOR_RADIUS_PT)
    wide_nbrs = neighbors_within(prim_idx, prims, grid, WIDE_RADIUS_PT)

    # Feature 1: local orthogonal neighbor count (30pt radius)
    orth_count = sum(
        1 for ni in close_nbrs
        if prims[ni].get("primitive_type") == "line"
        and is_orthogonal(_angle_deg(prims[ni]))
    )

    # Feature 2: local short-line ratio (50pt radius, lines <20pt)
    nearby_lines = [ni for ni in wide_nbrs if prims[ni].get("primitive_type") == "line"]
    short_lines = sum(
        1 for ni in nearby_lines
        if float(prims[ni].get("length", 0) or 0) < 20
    )
    short_ratio = short_lines / max(len(nearby_lines), 1)

    # Feature 3: local rect count (50pt radius)
    rect_count = sum(
        1 for ni in wide_nbrs
        if prims[ni].get("primitive_type") in ("rect", "quad")
    )

    # Feature 4: local density (primitive count in 30pt)
    local_density = len(close_nbrs)

    # Feature 5: local alignment consistency (angle variance of nearby lines)
    nearby_angles = [_angle_deg(prims[ni]) for ni in wide_nbrs
                     if prims[ni].get("primitive_type") == "line"]
    nearby_angles = [a for a in nearby_angles if a is not None]
    if len(nearby_angles) >= 3:
        ortho_count = sum(1 for a in nearby_angles if is_orthogonal(a))
        ortho_share = ortho_count / len(nearby_angles)
    else:
        ortho_share = 0.0

    # Feature 6: border distance (normalized — ONE feature, not dominant)
    if pw > 0 and ph > 0:
        dist_right = pw - cx
        dist_bottom = ph - cy
        dist_left = cx
        dist_top = cy
        min_edge_dist = min(dist_right, dist_bottom, dist_left, dist_top)
        border_distance = min(1.0, min_edge_dist / 100.0)  # capped at 100pt
    else:
        border_distance = 1.0

    return {
        "orth_neighbor_count": orth_count,
        "short_line_ratio": round(short_ratio, 4),
        "rect_count_nearby": rect_count,
        "local_density": local_density,
        "ortho_share_nearby": round(ortho_share, 4),
        "border_distance_norm": round(border_distance, 4),
        "is_line": ptype == "line",
        "is_rect": ptype in ("rect", "quad"),
        "is_short_line": ptype == "line" and length < 20,
        "is_long_line": ptype == "line" and length > 50,
    }


# ============================================================
# Per-primitive semantic role probability
# ============================================================

def role_probability(features: dict) -> dict:
    """P(role | local_features) — rule-based but content-driven.

    Title block primitives characterized by:
      - High local short-line ratio (cells made of short orthogonal segments)
      - Many orthogonal neighbors (cell structure)
      - High local density (compact area)
      - Mid-to-high orthogonal share (table-like)

    Drawing geometry characterized by:
      - Low short-line ratio (drawing lines tend to be longer)
      - Lower orthogonal share (varied angles in building geometry)
      - Lower density

    Border frame characterized by:
      - Very close to edge (border_distance ≈ 0)
      - Long isolated lines (low local density)
    """
    if not features:
        return {"title_metadata_cell": 0.0, "drawing_geometry": 0.0,
                "border_frame": 0.0, "other": 1.0}

    # Title metadata cell score
    tmc_score = 0.0
    if features["short_line_ratio"] >= 0.6:
        tmc_score += 0.30
    elif features["short_line_ratio"] >= 0.4:
        tmc_score += 0.15
    if features["orth_neighbor_count"] >= 6:
        tmc_score += 0.25
    elif features["orth_neighbor_count"] >= 3:
        tmc_score += 0.12
    if features["local_density"] >= 15:
        tmc_score += 0.20
    elif features["local_density"] >= 8:
        tmc_score += 0.10
    if features["ortho_share_nearby"] >= 0.7:
        tmc_score += 0.20
    elif features["ortho_share_nearby"] >= 0.5:
        tmc_score += 0.10
    if features["rect_count_nearby"] >= 1:
        tmc_score += 0.10
    # Soft penalty for very-close-to-edge (likely border, not title block interior)
    if features["border_distance_norm"] < 0.05:
        tmc_score *= 0.4  # heavily reduce — likely border line itself

    # Drawing geometry score
    drw_score = 0.0
    if features["is_long_line"]:
        drw_score += 0.25
    if features["short_line_ratio"] < 0.3:
        drw_score += 0.20
    if features["ortho_share_nearby"] < 0.5:
        drw_score += 0.20
    if features["local_density"] < 8:
        drw_score += 0.15
    if features["rect_count_nearby"] == 0:
        drw_score += 0.05

    # Border frame score
    bdr_score = 0.0
    if features["border_distance_norm"] < 0.03:
        bdr_score += 0.40
    if features["is_long_line"] and features["local_density"] < 5:
        bdr_score += 0.30
    if features["ortho_share_nearby"] > 0.85:
        bdr_score += 0.15

    # Normalize (soft, not hard sum-to-one)
    return {
        "title_metadata_cell": round(min(1.0, tmc_score), 4),
        "drawing_geometry": round(min(1.0, drw_score), 4),
        "border_frame": round(min(1.0, bdr_score), 4),
    }


# ============================================================
# Emergent clustering
# ============================================================

def find_title_block_cluster(prims: list[dict], grid: dict,
                              page_extent: tuple,
                              role_threshold: float = 0.40) -> dict:
    """Find connected components of high-P(title_metadata_cell) primitives.
    Returns the LARGEST such cluster as the detected title block."""

    # Score all primitives
    high_p_indices: list[int] = []
    p_scores: dict[int, float] = {}
    for i in range(len(prims)):
        features = primitive_features(i, prims, grid, page_extent)
        roles = role_probability(features)
        p_tmc = roles["title_metadata_cell"]
        if p_tmc >= role_threshold:
            high_p_indices.append(i)
            p_scores[i] = p_tmc

    if not high_p_indices:
        return {
            "found": False,
            "n_high_p_primitives": 0,
        }

    # Cluster via spatial connected components (within 40pt = adjacency radius)
    cluster_radius = 40.0
    visited: set[int] = set()
    clusters: list[set[int]] = []
    for seed in high_p_indices:
        if seed in visited:
            continue
        component: set[int] = set()
        stack = [seed]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            component.add(cur)
            # Find high-P neighbors within cluster radius
            for nbr in neighbors_within(cur, prims, grid, cluster_radius):
                if nbr in p_scores and nbr not in visited:
                    stack.append(nbr)
        if len(component) >= 5:  # ignore tiny clusters
            clusters.append(component)

    if not clusters:
        return {"found": False, "n_high_p_primitives": len(high_p_indices)}

    # Largest cluster (by total P score, not just count)
    cluster_scores = []
    for c in clusters:
        total_p = sum(p_scores[i] for i in c)
        xs = [float(prims[i]["cx"]) for i in c]
        ys = [float(prims[i]["cy"]) for i in c]
        cluster_scores.append({
            "indices": c,
            "total_p": total_p,
            "n": len(c),
            "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "cx": (min(xs) + max(xs)) / 2,
            "cy": (min(ys) + max(ys)) / 2,
        })
    cluster_scores.sort(key=lambda c: -c["total_p"])
    winner = cluster_scores[0]

    return {
        "found": True,
        "n_high_p_primitives": len(high_p_indices),
        "n_clusters": len(clusters),
        "winner": {
            "n_primitives": winner["n"],
            "total_p": round(winner["total_p"], 3),
            "bbox": [round(winner["x0"], 1), round(winner["y0"], 1),
                     round(winner["x1"], 1), round(winner["y1"], 1)],
            "centroid": [round(winner["cx"], 1), round(winner["cy"], 1)],
        },
    }


# ============================================================
# Swap test (the v28 Gate A primary semantic gate)
# ============================================================

def swap_regions(prims: list[dict], pw: float, ph: float,
                  region_a: tuple, region_b: tuple) -> list[dict]:
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


def is_in_canonical_zone(cx: float, cy: float, pw: float, ph: float) -> bool:
    rx = cx / pw if pw > 0 else 0.5
    ry = cy / ph if ph > 0 else 0.5
    return (rx > 0.65 and ry > 0.65) or ry > 0.85 or rx > 0.85


def psal_swap_test(doc, page_idx: int) -> dict:
    """Gate A: detector follows content under swap (target ≥70%)."""
    prims, (pw, ph) = stream_page_primitives(doc, page_idx)
    if not prims:
        return {"page_idx": page_idx, "error": "no primitives"}
    # Subsample if page exceeds budget (deterministic — every Nth primitive)
    n_orig = len(prims)
    if n_orig > MAX_PRIMITIVES_PER_PAGE:
        stride = max(1, n_orig // MAX_PRIMITIVES_PER_PAGE)
        prims = prims[::stride][:MAX_PRIMITIVES_PER_PAGE]

    # Baseline detection
    grid = build_spatial_grid(prims)
    baseline = find_title_block_cluster(prims, grid, (pw, ph))
    if not baseline["found"]:
        return {
            "page_idx": page_idx,
            "baseline_found": False,
            "follows_content": False,
            "stays_at_position": False,
        }
    base_cx = baseline["winner"]["centroid"][0]
    base_cy = baseline["winner"]["centroid"][1]
    baseline_in_canonical = is_in_canonical_zone(base_cx, base_cy, pw, ph)

    # Swap canonical bottom-right region with center
    canonical_region = (pw * 0.70, ph * 0.85, pw, ph)
    center_region = (pw * 0.35, ph * 0.40, pw * 0.65, ph * 0.60)
    swapped = swap_regions(prims, pw, ph, canonical_region, center_region)
    sw_grid = build_spatial_grid(swapped)
    post = find_title_block_cluster(swapped, sw_grid, (pw, ph))
    if not post["found"]:
        return {
            "page_idx": page_idx,
            "baseline_in_canonical": baseline_in_canonical,
            "post_found": False,
            "follows_content": False,
            "stays_at_position": False,
        }
    post_cx = post["winner"]["centroid"][0]
    post_cy = post["winner"]["centroid"][1]
    post_in_canonical = is_in_canonical_zone(post_cx, post_cy, pw, ph)

    # Did detector follow CONTENT (winner left canonical zone)?
    follows_content = baseline_in_canonical and not post_in_canonical
    stays_at_position = baseline_in_canonical and post_in_canonical

    # Free memory
    del prims
    del swapped

    return {
        "page_idx": page_idx,
        "baseline_found": True,
        "baseline_in_canonical": baseline_in_canonical,
        "baseline_centroid": [round(base_cx, 1), round(base_cy, 1)],
        "baseline_n_primitives": baseline["winner"]["n_primitives"],
        "post_in_canonical": post_in_canonical,
        "post_centroid": [round(post_cx, 1), round(post_cy, 1)],
        "post_n_primitives": post["winner"]["n_primitives"],
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
                pr = psal_swap_test(doc, pg)
                page_results.append(pr)
                print(f"    page {pg}: baseline={pr.get('baseline_found', False)} "
                      f"follows={pr.get('follows_content', False)} "
                      f"stays={pr.get('stays_at_position', False)}", flush=True)
            except Exception as exc:
                page_results.append({"page_idx": pg, "error": str(exc)[:100]})
                print(f"    page {pg}: ERROR {str(exc)[:80]}", flush=True)
    finally:
        doc.close()

    return {
        "pdf_name": pdf_path.name,
        "pdf_key": pdf_path.stem,
        "n_pages": len(page_results),
        "page_results": page_results,
    }


def main() -> None:
    print("=== Wave 20.14.5J-V3-PSAL — Primitive Semantic Attribution Layer (v28) ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
    if SUBSET_PDFS_ONLY:
        pdfs = [p for p in pdfs if p.name not in SKIP_LIST]
        print(f"SUBSET MODE: skipping {SKIP_LIST}, processing {len(pdfs)} PDFs\n", flush=True)
    all_results = []

    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---", flush=True)
        r = process_pdf(pdf)
        all_results.append(r)
        if "error" in r:
            print(f"  ERROR: {r['error']}")
            continue
        valid = [pr for pr in r["page_results"] if "error" not in pr and pr.get("baseline_found")]
        if not valid:
            print(f"  no valid pages with baseline detection")
            continue
        n_follows = sum(1 for pr in valid if pr.get("follows_content"))
        n_stays = sum(1 for pr in valid if pr.get("stays_at_position"))
        print(f"  pages={r['n_pages']}  baseline_found={len(valid)}  "
              f"follows={n_follows}/{len(valid)}  stays={n_stays}/{len(valid)}")

    print(f"\n=== V28 GATE A EVALUATION (semantic relocation) ===")
    total_valid = 0
    total_follows = 0
    total_stays = 0
    no_baseline = 0
    for r in all_results:
        if "error" in r:
            continue
        for pr in r.get("page_results", []):
            if "error" in pr:
                continue
            if not pr.get("baseline_found"):
                no_baseline += 1
                continue
            total_valid += 1
            if pr.get("follows_content"):
                total_follows += 1
            if pr.get("stays_at_position"):
                total_stays += 1

    follows_rate = total_follows / max(total_valid, 1)
    stays_rate = total_stays / max(total_valid, 1)

    print(f"  pages with baseline detection: {total_valid}")
    print(f"  pages with no baseline detected: {no_baseline}")
    print(f"  follows content: {total_follows} ({follows_rate*100:.1f}%)")
    print(f"  stays at position: {total_stays} ({stays_rate*100:.1f}%)")

    g_a = follows_rate >= 0.70
    print(f"\nGate A (follows_content ≥70%): {'PASS' if g_a else 'FAIL'}  ({follows_rate*100:.1f}%)")
    print(f"Comparison: v26 SCV=0%, v27 SCV-r2=1.1%, v28 PSAL={follows_rate*100:.1f}%")

    out_path = OUT_DIR / "psal_v28_2026-05-24.json"
    with open(out_path, "w", encoding="utf-8") as f:
        # Compact
        compact = []
        for r in all_results:
            if "error" in r:
                compact.append(r)
                continue
            compact.append({
                "pdf_name": r["pdf_name"],
                "n_pages": r["n_pages"],
                "page_results": r["page_results"],
            })
        json.dump({
            "wave": "20.14.5J-V3-PSAL-v28",
            "gate_a": {"pass": g_a, "follows_content_rate": round(follows_rate, 4)},
            "pages_with_baseline": total_valid,
            "pages_no_baseline": no_baseline,
            "per_pdf": compact,
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")


if __name__ == "__main__":
    main()
