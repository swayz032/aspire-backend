"""Wave 20.14.5J-V3-PRG — Primitive Relational Graph (v29).

ARCHITECTURAL INVERSION: regions are OUTPUTS, not inputs.

Stage 1 — Primitive Typing (NOT scoring)
Stage 2 — Relational Graph (typed edges between primitives)
Stage 3 — Motif Mining (discover graph patterns)
Stage 4 — Region Emergence (regions derived from motif clusters)

The semantic separator (locked):
    Title blocks = small-scale closed-cell administrative structures.
    Drawings = open polylines, long segments, partial intersections.

MANDATORY GATE: Border-only ablation collapse.
    Remove edge_frame primitives → re-run detection.
    If detector collapses → position prior still dominates → FAIL.
    If detector survives → semantics carry load → architectural success.

Minimum viable PRG: focus on microcell motif (closed quads connected by
row_alignment edges). One motif type is enough to test the inversion.
"""
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))
from sheet_layout_segmenter_v2 import stream_page_primitives  # noqa: E402

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

# ============================================================
# Constants — semantic discriminators
# ============================================================
SHORT_LINE_MAX = 60.0       # r2: widened from 25 to capture larger title cells in electrical/plumbing sheets
LONG_LINE_MIN = 120.0       # r2: bumped up since SHORT_LINE_MAX widened
CORNER_RADIUS = 6.0         # r7: widened from 3pt — endpoint quantization too tight under coord transformation (NULL_COLLAPSE fix)
EDGE_FRAME_MARGIN = 15.0    # within this distance of page edge = frame candidate
ROW_ALIGN_TOL = 4.0         # y-axis (or x) coincidence for row alignment
GRID_CELL = 40.0
MAX_PRIMS_PER_PAGE = 6000   # tractability cap
# r2: swap region (single source of truth for validity + swap_regions call)
SWAP_REGION_REL = (0.65, 0.80, 1.0, 1.0)        # bottom-right corner where title blocks actually live
SWAP_DESTINATION_REL = (0.30, 0.35, 0.65, 0.65) # center destination

SUBSET_PDFS = {
    "2101LP-R1-LP1-1of1 (11) (7).pdf",
    "21030 ENG-C2.2 GSM.pdf",
    "29187P-ES (3).pdf",
    "Concrete blueprints Mangonia Park.pdf",
    "E-1 (1).pdf",
    "E-2 (1).pdf",
    "E-3 (1).pdf",
    "P-1 (1).pdf",
    "Precast Drainage.pdf",
    "Revised light pole plan.pdf",
}

# r6 BLIND HOLDOUT — declared here, NOT inspected during r6 tuning
# Will only be run ONCE at end of r6 after tuning gates pass.
# Cross-project (GAVNN = Gulfstream, different firm from Mangonia)
# + a heavy multi-page Mangonia file that wasn't in r5 corpus.
HOLDOUT_PDFS = {
    "GAVNN_ADDENDUM_1.pdf",                  # 39pp, Gulfstream project, cross-firm
    "21030 ENG_Rev1 240801_Signed.pdf",      # 13pp, different ENG file
    "21030 ENG-C2.2 GSM (1).pdf",            # near-duplicate, prevents memorization check
}

# r6 TUNING SET (visible during instrumentation work)
TUNING_PDFS = SUBSET_PDFS - HOLDOUT_PDFS

# r6 mode flag — set False to run holdout (only AFTER tuning gates pass)
R6_USE_TUNING_SET = True
# r7: run on FULL CORPUS including holdout + new additions (residential bathroom)
R7_FULL_CORPUS = True


# ============================================================
# Spatial grid for O(N) neighborhood
# ============================================================

def build_grid(prims, cell=GRID_CELL):
    g = defaultdict(list)
    for i, p in enumerate(prims):
        try:
            tx = int(float(p.get("cx", 0)) // cell)
            ty = int(float(p.get("cy", 0)) // cell)
            g[(tx, ty)].append(i)
        except (TypeError, ValueError):
            pass
    return g


def neighbors(idx, prims, grid, radius, cell=GRID_CELL):
    p = prims[idx]
    try:
        cx, cy = float(p["cx"]), float(p["cy"])
    except (KeyError, TypeError, ValueError):
        return []
    n = int(math.ceil(radius / cell))
    tx, ty = int(cx // cell), int(cy // cell)
    out = []
    for dx in range(-n, n + 1):
        for dy in range(-n, n + 1):
            for ni in grid.get((tx + dx, ty + dy), []):
                if ni == idx:
                    continue
                np_ = prims[ni]
                try:
                    if math.hypot(float(np_["cx"]) - cx, float(np_["cy"]) - cy) <= radius:
                        out.append(ni)
                except (KeyError, TypeError, ValueError):
                    pass
    return out


def endpoints(p):
    try:
        return (float(p["x0"]), float(p["y0"])), (float(p["x1"]), float(p["y1"]))
    except (KeyError, TypeError, ValueError):
        return None, None


def is_orthogonal(p):
    e0, e1 = endpoints(p)
    if e0 is None:
        return False
    dx, dy = e1[0] - e0[0], e1[1] - e0[1]
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return False
    angle = math.degrees(math.atan2(dy, dx)) % 180
    return angle <= 5 or angle >= 175 or abs(angle - 90) <= 5


# ============================================================
# STAGE 1 — Primitive Typing
# ============================================================

def type_primitives(prims, page_extent):
    """Assign type set to each primitive. Returns list of type-sets."""
    pw, ph = page_extent
    types_per_prim = [set() for _ in prims]
    grid = build_grid(prims)

    # Direct typing from primitive properties
    for i, p in enumerate(prims):
        ptype = p.get("primitive_type")
        length = float(p.get("length", 0) or 0)

        # Native rects/quads are immediate closed_quad members
        if ptype in ("rect", "quad"):
            types_per_prim[i].add("closed_quad_member")
            # Cell-sized rect?
            try:
                w = abs(float(p["x1"]) - float(p["x0"]))
                h = abs(float(p["y1"]) - float(p["y0"]))
                if max(w, h) <= 60:
                    types_per_prim[i].add("microcell")
            except (KeyError, TypeError, ValueError):
                pass

        if ptype == "line":
            if length < SHORT_LINE_MAX:
                types_per_prim[i].add("short_line")
            if length > LONG_LINE_MIN:
                types_per_prim[i].add("long_line")

            # Edge frame: long orthogonal line within margin of page edge
            if length > 100 and is_orthogonal(p):
                e0, e1 = endpoints(p)
                if e0:
                    near_edge = (
                        min(e0[0], e1[0]) < EDGE_FRAME_MARGIN
                        or max(e0[0], e1[0]) > pw - EDGE_FRAME_MARGIN
                        or min(e0[1], e1[1]) < EDGE_FRAME_MARGIN
                        or max(e0[1], e1[1]) > ph - EDGE_FRAME_MARGIN
                    )
                    if near_edge:
                        types_per_prim[i].add("edge_frame_member")

    # Detect closed quads from line cycles (short orthogonal lines forming 4-corner loops)
    closed_quads_via_lines = detect_closed_quad_lines(prims, types_per_prim, grid)
    for line_idx in closed_quads_via_lines:
        types_per_prim[line_idx].add("closed_quad_member")
        types_per_prim[line_idx].add("microcell")

    # Repetition members: parallel short lines with similar spacing
    for i, ts in enumerate(types_per_prim):
        if "short_line" not in ts:
            continue
        if has_parallel_siblings(i, prims, grid):
            types_per_prim[i].add("repetition_member")

    return types_per_prim, grid


def detect_closed_quad_lines(prims, types_per_prim, grid):
    """Find lines that participate in a 4-edge closed loop (small cell)."""
    closed = set()
    # Build endpoint index: which lines share endpoints?
    endpoint_buckets = defaultdict(list)
    for i, p in enumerate(prims):
        if p.get("primitive_type") != "line":
            continue
        length = float(p.get("length", 0) or 0)
        if length >= SHORT_LINE_MAX or length < 2:
            continue
        if not is_orthogonal(p):
            continue
        e0, e1 = endpoints(p)
        if e0 is None:
            continue
        # Quantize endpoint to small grid for fuzzy matching
        for ep in (e0, e1):
            key = (round(ep[0] / CORNER_RADIUS), round(ep[1] / CORNER_RADIUS))
            endpoint_buckets[key].append((i, ep))

    # For each line, try to find a 4-cycle
    visited_starts = set()
    for line_idx, p in enumerate(prims):
        if p.get("primitive_type") != "line":
            continue
        length = float(p.get("length", 0) or 0)
        if length >= SHORT_LINE_MAX or length < 2:
            continue
        if not is_orthogonal(p):
            continue
        if line_idx in closed:
            continue
        e0, e1 = endpoints(p)
        if e0 is None:
            continue
        # Find lines sharing e1 endpoint
        key1 = (round(e1[0] / CORNER_RADIUS), round(e1[1] / CORNER_RADIUS))
        for nb_idx, nb_ep in endpoint_buckets.get(key1, []):
            if nb_idx == line_idx or nb_idx in visited_starts:
                continue
            nb_p = prims[nb_idx]
            nb_e0, nb_e1 = endpoints(nb_p)
            if nb_e0 is None:
                continue
            # Other endpoint of nb
            other_nb = nb_e0 if nb_ep == nb_e1 else nb_e1
            # Find third line sharing other_nb
            key2 = (round(other_nb[0] / CORNER_RADIUS), round(other_nb[1] / CORNER_RADIUS))
            for nb2_idx, nb2_ep in endpoint_buckets.get(key2, []):
                if nb2_idx in (line_idx, nb_idx):
                    continue
                nb2_p = prims[nb2_idx]
                nb2_e0, nb2_e1 = endpoints(nb2_p)
                if nb2_e0 is None:
                    continue
                other_nb2 = nb2_e0 if nb2_ep == nb2_e1 else nb2_e1
                # Find fourth line closing back to e0
                key3 = (round(other_nb2[0] / CORNER_RADIUS), round(other_nb2[1] / CORNER_RADIUS))
                key_close = (round(e0[0] / CORNER_RADIUS), round(e0[1] / CORNER_RADIUS))
                for nb3_idx, nb3_ep in endpoint_buckets.get(key3, []):
                    if nb3_idx in (line_idx, nb_idx, nb2_idx):
                        continue
                    nb3_p = prims[nb3_idx]
                    nb3_e0, nb3_e1 = endpoints(nb3_p)
                    if nb3_e0 is None:
                        continue
                    other_nb3 = nb3_e0 if nb3_ep == nb3_e1 else nb3_e1
                    if (round(other_nb3[0] / CORNER_RADIUS), round(other_nb3[1] / CORNER_RADIUS)) == key_close:
                        # Found a 4-cycle
                        closed.add(line_idx)
                        closed.add(nb_idx)
                        closed.add(nb2_idx)
                        closed.add(nb3_idx)
                        break
                if line_idx in closed:
                    break
            if line_idx in closed:
                break
        visited_starts.add(line_idx)
    return closed


def has_parallel_siblings(idx, prims, grid):
    p = prims[idx]
    e0, e1 = endpoints(p)
    if e0 is None:
        return False
    dx, dy = e1[0] - e0[0], e1[1] - e0[1]
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return False
    angle = math.degrees(math.atan2(dy, dx)) % 180
    length = math.hypot(dx, dy)
    sib_count = 0
    for ni in neighbors(idx, prims, grid, 50.0):
        np_ = prims[ni]
        if np_.get("primitive_type") != "line":
            continue
        n_e0, n_e1 = endpoints(np_)
        if n_e0 is None:
            continue
        n_dx, n_dy = n_e1[0] - n_e0[0], n_e1[1] - n_e0[1]
        n_len = math.hypot(n_dx, n_dy)
        if abs(n_len - length) > 5:
            continue
        n_angle = math.degrees(math.atan2(n_dy, n_dx)) % 180
        if abs(n_angle - angle) < 3:
            sib_count += 1
            if sib_count >= 2:
                return True
    return False


# ============================================================
# STAGE 2 — Relational Graph
# ============================================================

def build_relational_graph(prims, types_per_prim, grid):
    """Edges between primitives.
    r6: also returns edge_types[(i,j)] = type label for provenance audit."""
    microcell_indices = [
        i for i, t in enumerate(types_per_prim)
        if "closed_quad_member" in t
    ]
    if not microcell_indices:
        return {}, microcell_indices, {}

    edges = defaultdict(set)
    edge_types = {}  # (min(i,j), max(i,j)) -> type label
    micro_grid = defaultdict(list)
    for i in microcell_indices:
        try:
            cx, cy = float(prims[i]["cx"]), float(prims[i]["cy"])
            micro_grid[(int(cx // 30), int(cy // 30))].append(i)
        except (KeyError, TypeError, ValueError):
            pass

    for i in microcell_indices:
        try:
            cx, cy = float(prims[i]["cx"]), float(prims[i]["cy"])
        except (KeyError, TypeError, ValueError):
            continue
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                for j in micro_grid.get((int(cx // 30) + dx, int(cy // 30) + dy), []):
                    if j <= i:
                        continue
                    try:
                        jcx, jcy = float(prims[j]["cx"]), float(prims[j]["cy"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    d = math.hypot(jcx - cx, jcy - cy)
                    if d > 60:
                        continue
                    etype = None
                    if abs(jcy - cy) <= ROW_ALIGN_TOL:
                        etype = "row_alignment"
                    elif abs(jcx - cx) <= ROW_ALIGN_TOL:
                        etype = "column_alignment"
                    elif d < 40:
                        etype = "adjacency"
                    if etype:
                        edges[i].add(j)
                        edges[j].add(i)
                        key = (min(i, j), max(i, j))
                        edge_types[key] = etype
    return edges, microcell_indices, edge_types


# ============================================================
# STAGE 3 — Motif Mining
# ============================================================

def mine_admin_motifs(edges, microcell_indices, prims, types_per_prim,
                       min_motif_size=5):
    """Find connected microcell clusters with admin-row-column structure.

    A motif is admin-row-column if:
    - &gt;= min_motif_size microcells
    - average within-cluster row or column alignment &gt;= 50%
    """
    visited = set()
    motifs = []
    for seed in microcell_indices:
        if seed in visited:
            continue
        # BFS connected component
        comp = []
        stack = [seed]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.append(cur)
            for nb in edges.get(cur, []):
                if nb not in visited:
                    stack.append(nb)
        if len(comp) < min_motif_size:
            continue
        # Measure row/column alignment within cluster
        align_score = measure_alignment(comp, prims)
        # Exclude pure edge_frame clusters (they're border frames, not title block content)
        frame_share = sum(
            1 for c in comp if "edge_frame_member" in types_per_prim[c]
        ) / len(comp)
        if frame_share > 0.7:
            continue
        motifs.append({
            "indices": comp,
            "size": len(comp),
            "alignment_score": align_score,
            "frame_share": frame_share,
        })
    return motifs


def measure_alignment(comp_indices, prims):
    """Fraction of cells that share row or column with another cell in cluster."""
    coords = []
    for i in comp_indices:
        try:
            coords.append((float(prims[i]["cx"]), float(prims[i]["cy"])))
        except (KeyError, TypeError, ValueError):
            pass
    if len(coords) < 3:
        return 0.0
    aligned_pairs = 0
    total_pairs = 0
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            total_pairs += 1
            if (abs(coords[i][1] - coords[j][1]) < ROW_ALIGN_TOL
                or abs(coords[i][0] - coords[j][0]) < ROW_ALIGN_TOL):
                aligned_pairs += 1
    return aligned_pairs / max(total_pairs, 1)


# ============================================================
# STAGE 4 — Region Emergence
# ============================================================

def compute_edge_class_distribution(cluster_indices, edges, edge_types):
    """r6: count edges within cluster by type. Returns dict + entropy."""
    counts = defaultdict(int)
    member = set(cluster_indices)
    total = 0
    for i in cluster_indices:
        for j in edges.get(i, set()):
            if j > i and j in member:
                key = (i, j)
                t = edge_types.get(key, "unknown")
                counts[t] += 1
                total += 1
    if total == 0:
        return {}, 0.0
    # Shannon entropy over edge-type distribution
    entropy = 0.0
    for c in counts.values():
        p = c / total
        if p > 0:
            entropy -= p * math.log2(p)
    return dict(counts), round(entropy, 3)


def compute_boundary_compactness(xs, ys):
    """r6: rough boundary compactness = perimeter / sqrt(area). Lower = more compact."""
    if len(xs) < 3:
        return 0.0
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    area = w * h
    if area <= 0:
        return 0.0
    perimeter = 2 * (w + h)
    return round(perimeter / math.sqrt(area), 3)


def emerge_region(motifs, prims, edges=None, edge_types=None):
    """Title block = motif with highest size × alignment_score product.
    Region bbox emerges from primitive centroids.
    r4: returns primitive_ids for provenance tracking + n_motifs.
    r6: returns edge_class_distribution + entropy + boundary_compactness."""
    if not motifs:
        return None
    for m in motifs:
        m["emergence_score"] = m["size"] * (0.3 + m["alignment_score"]) * (1 - m["frame_share"])
    motifs.sort(key=lambda m: -m["emergence_score"])
    winner = motifs[0]
    xs, ys = [], []
    primitive_ids = []
    for i in winner["indices"]:
        try:
            xs.append(float(prims[i]["cx"]))
            ys.append(float(prims[i]["cy"]))
            pid = prims[i].get("primitive_id")
            if pid is not None:
                primitive_ids.append(pid)
        except (KeyError, TypeError, ValueError):
            pass
    if not xs:
        return None
    # r4: detect SPLIT — 2+ motifs with comparable emergence scores
    split_candidates = [m for m in motifs[:3]
                         if m["emergence_score"] >= winner["emergence_score"] * 0.7]
    # r6: edge class distribution + entropy + boundary compactness
    edge_class_counts = {}
    edge_entropy = 0.0
    if edges is not None and edge_types is not None:
        edge_class_counts, edge_entropy = compute_edge_class_distribution(
            winner["indices"], edges, edge_types
        )
    boundary_compactness = compute_boundary_compactness(xs, ys)
    return {
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "centroid": [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2],
        "n_primitives": winner["size"],
        "alignment_score": round(winner["alignment_score"], 3),
        "frame_share": round(winner["frame_share"], 3),
        "emergence_score": round(winner["emergence_score"], 3),
        "primitive_ids": primitive_ids,
        "n_motifs": len(motifs),
        "n_competitive_motifs": len(split_candidates),
        "edge_class_counts": edge_class_counts,
        "edge_entropy": edge_entropy,
        "boundary_compactness": boundary_compactness,
    }


# ============================================================
# Full PRG pipeline
# ============================================================

def run_prg(prims, page_extent, exclude_edge_frame=False):
    """4-stage pipeline. Returns emerged region or None."""
    types_per_prim, grid = type_primitives(prims, page_extent)
    if exclude_edge_frame:
        keep = [
            i for i, t in enumerate(types_per_prim)
            if "edge_frame_member" not in t
        ]
        if len(keep) < len(prims):
            prims_filtered = [prims[i] for i in keep]
            types_filtered = [types_per_prim[i] for i in keep]
            grid = build_grid(prims_filtered)
            edges, microcell_indices, edge_types = build_relational_graph(
                prims_filtered, types_filtered, grid
            )
            motifs = mine_admin_motifs(
                edges, microcell_indices, prims_filtered, types_filtered
            )
            return emerge_region(motifs, prims_filtered, edges, edge_types)
    edges, microcell_indices, edge_types = build_relational_graph(prims, types_per_prim, grid)
    motifs = mine_admin_motifs(edges, microcell_indices, prims, types_per_prim)
    return emerge_region(motifs, prims, edges, edge_types)


# ============================================================
# Swap test + border ablation
# ============================================================

def swap_regions(prims, pw, ph, region_a, region_b):
    ax0, ay0, ax1, ay1 = region_a
    bx0, by0, bx1, by1 = region_b
    a_dx, a_dy = bx0 - ax0, by0 - ay0
    b_dx, b_dy = ax0 - bx0, ay0 - by0
    out = []
    for p in prims:
        try:
            cx, cy = float(p.get("cx", 0)), float(p.get("cy", 0))
        except (TypeError, ValueError):
            out.append(dict(p))
            continue
        in_a = ax0 <= cx <= ax1 and ay0 <= cy <= ay1
        in_b = bx0 <= cx <= bx1 and by0 <= cy <= by1
        if not in_a and not in_b:
            out.append(dict(p))
            continue
        pp = dict(p)
        dx, dy = (a_dx, a_dy) if in_a else (b_dx, b_dy)
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


def in_canonical_zone(cx, cy, pw, ph):
    rx, ry = cx / max(pw, 1), cy / max(ph, 1)
    return (rx > 0.6 and ry > 0.6) or ry > 0.82 or rx > 0.82


def in_swap_region(cx, cy, pw, ph):
    """r2: strict validity — baseline must be in actual swap rectangle, not broad canonical zone."""
    x0, y0, x1, y1 = SWAP_REGION_REL
    return pw * x0 <= cx <= pw * x1 and ph * y0 <= cy <= ph * y1


def in_destination_region(cx, cy, pw, ph):
    x0, y0, x1, y1 = SWAP_DESTINATION_REL
    return pw * x0 <= cx <= pw * x1 and ph * y0 <= cy <= ph * y1


def pick_alternate_quadrant(base_bbox, pw, ph):
    """r3: choose a destination quadrant that doesn't overlap baseline bbox."""
    bx0, by0, bx1, by1 = base_bbox
    bcx = (bx0 + bx1) / 2
    bcy = (by0 + by1) / 2
    # Pick opposite quadrant
    if bcx >= pw / 2 and bcy >= ph / 2:
        # baseline in BR -> dest TL
        return (pw * 0.05, ph * 0.05, pw * 0.35, ph * 0.30)
    elif bcx >= pw / 2 and bcy < ph / 2:
        # BR-top -> dest BL
        return (pw * 0.05, ph * 0.65, pw * 0.35, ph * 0.95)
    elif bcx < pw / 2 and bcy >= ph / 2:
        # baseline BL -> dest TR
        return (pw * 0.65, ph * 0.05, pw * 0.95, ph * 0.30)
    else:
        # baseline TL -> dest BR
        return (pw * 0.65, ph * 0.65, pw * 0.95, ph * 0.95)


def bbox_iou(a, b):
    """Intersection-over-Union for two [x0,y0,x1,y1] bboxes."""
    if not a or not b:
        return 0.0
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a_area = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    b_area = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = a_area + b_area - inter
    return inter / max(union, 1e-9)


def classify_trajectory(baseline, post_swap, swap_geometry, primitive_recall):
    """r4: 6-class trajectory taxonomy.
    FOLLOW: post_swap bbox overlaps destination-projected baseline bbox
    STAY: post_swap bbox overlaps origin baseline bbox
    DRIFT_NEAR: centroid near destination but IoU low
    SPLIT_CLUSTER: 2+ comparable motifs after swap
    THIRD_REGION: post_swap far from both origin and destination
    NULL_COLLAPSE: no post_swap cluster
    """
    if post_swap is None:
        return "NULL_COLLAPSE"
    if not baseline or not swap_geometry:
        return "THIRD_REGION"

    # Project baseline bbox to destination using TL-to-TL offset (matches swap_regions exactly)
    # BUG FIX: prior version used center-to-center, mismatched swap_regions's TL-to-TL transform
    # when src and dst rectangles differ in size, causing IoU=0 for perfect-recall clusters.
    sx0, sy0, sx1, sy1 = swap_geometry["src"]
    dx0, dy0, dx1, dy1 = swap_geometry["dst"]
    dx_offset = dx0 - sx0
    dy_offset = dy0 - sy0
    bbox = baseline["bbox"]
    projected_baseline_bbox = [
        bbox[0] + dx_offset, bbox[1] + dy_offset,
        bbox[2] + dx_offset, bbox[3] + dy_offset,
    ]

    iou_follow = bbox_iou(post_swap["bbox"], projected_baseline_bbox)
    iou_stay = bbox_iou(post_swap["bbox"], baseline["bbox"])
    n_comp_motifs = post_swap.get("n_competitive_motifs", 1)

    # STAY check FIRST — high iou_stay means post-swap is at original position, period.
    # primitive_recall can be high simultaneously when swap failed to displace the cluster
    # (transport failure). That is STAY, not FOLLOW.
    if iou_stay >= 0.30:
        return "STAY"
    if n_comp_motifs >= 2 and iou_follow < 0.30:
        return "SPLIT_CLUSTER"
    # FOLLOW requires real overlap with projected destination — NOT just recall
    if iou_follow >= 0.30:
        return "FOLLOW"
    # DRIFT — primitives moved enough that recall is non-trivial, even if IoU low
    psc = post_swap["centroid"]
    proj_cx = (projected_baseline_bbox[0] + projected_baseline_bbox[2]) / 2
    proj_cy = (projected_baseline_bbox[1] + projected_baseline_bbox[3]) / 2
    d_to_dest = math.hypot(psc[0] - proj_cx, psc[1] - proj_cy)
    if primitive_recall >= 0.30 and d_to_dest <= 200:
        return "DRIFT_NEAR"
    if d_to_dest <= 150:
        return "DRIFT_NEAR"
    return "THIRD_REGION"


def process_page(doc, page_idx):
    prims, (page_rect_w, page_rect_h) = stream_page_primitives(doc, page_idx)
    if not prims:
        return {"page_idx": page_idx, "error": "no primitives"}
    if len(prims) > MAX_PRIMS_PER_PAGE:
        stride = max(1, len(prims) // MAX_PRIMS_PER_PAGE)
        prims = prims[::stride][:MAX_PRIMS_PER_PAGE]
    # r4: assign stable primitive_id BEFORE any processing
    for i, p in enumerate(prims):
        p["primitive_id"] = i
    # r4 TRANSPORT FIX: PyMuPDF page.rect lies for some PDFs (E-1/E-2/E-3/P-1
    # have content far exceeding page.rect.height). Use actual primitive
    # extents as effective page geometry to avoid malformed swap rectangles.
    prim_xs = [float(p["cx"]) for p in prims if p.get("cx") is not None]
    prim_ys = [float(p["cy"]) for p in prims if p.get("cy") is not None]
    if prim_xs and prim_ys:
        content_x0, content_y0 = min(prim_xs), min(prim_ys)
        content_x1, content_y1 = max(prim_xs), max(prim_ys)
        # Effective page = bounding box of all primitives (with small margin)
        margin = 20.0
        pw = content_x1 - content_x0 + 2 * margin
        ph = content_y1 - content_y0 + 2 * margin
        # Translate all primitives so origin is at (0,0) in content space — this
        # makes all downstream geometry (canonical_zone, swap, etc.) reliable.
        shift_x = margin - content_x0
        shift_y = margin - content_y0
        if abs(shift_x) > 1 or abs(shift_y) > 1:
            for p in prims:
                for k in ("x0", "x1", "cx"):
                    if k in p and p[k] is not None:
                        try:
                            p[k] = float(p[k]) + shift_x
                        except (TypeError, ValueError):
                            pass
                for k in ("y0", "y1", "cy"):
                    if k in p and p[k] is not None:
                        try:
                            p[k] = float(p[k]) + shift_y
                        except (TypeError, ValueError):
                            pass
    else:
        pw, ph = page_rect_w, page_rect_h

    # Baseline detection
    baseline = run_prg(prims, (pw, ph))
    # Border ablation: remove edge frame
    ablated = run_prg(prims, (pw, ph), exclude_edge_frame=True)
    # r3: content-bbox-adaptive swap test
    # Swap region = baseline's own bbox (with 20pt padding); dest = opposite quadrant
    swapped = None
    swap_geometry = None
    if baseline:
        bbox = baseline["bbox"]
        pad = 20.0
        swap_src = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                     min(pw, bbox[2] + pad), min(ph, bbox[3] + pad))
        swap_dst = pick_alternate_quadrant(bbox, pw, ph)
        swap_geometry = {"src": [round(x, 1) for x in swap_src],
                          "dst": [round(x, 1) for x in swap_dst]}
        swapped = swap_regions(prims, pw, ph, swap_src, swap_dst)
    post_swap = run_prg(swapped, (pw, ph)) if swapped else None

    result = {"page_idx": page_idx}
    if baseline:
        bc = baseline["centroid"]
        result["baseline_centroid"] = [round(c, 1) for c in bc]
        result["baseline_in_canonical"] = in_canonical_zone(bc[0], bc[1], pw, ph)
        # r2: STRICT validity — baseline must be in actual swap region for test to count
        result["baseline_in_swap_region"] = in_swap_region(bc[0], bc[1], pw, ph)
        result["baseline_size"] = baseline["n_primitives"]
        result["baseline_align"] = baseline["alignment_score"]
        result["page_dims"] = [round(pw, 1), round(ph, 1)]
    else:
        result["baseline_found"] = False
        return result
    result["baseline_found"] = True

    # Border ablation gate
    if ablated:
        result["ablation_survives"] = True
        result["ablation_centroid"] = [round(c, 1) for c in ablated["centroid"]]
        # Did ablation stay near baseline? (semantic stability)
        d = math.hypot(
            ablated["centroid"][0] - baseline["centroid"][0],
            ablated["centroid"][1] - baseline["centroid"][1],
        )
        result["ablation_drift"] = round(d, 1)
        result["ablation_stable"] = d < 60  # within 60pt of baseline
    else:
        result["ablation_survives"] = False
        result["ablation_stable"] = False

    # r4: provenance + trajectory classification + IoU
    if post_swap and swap_geometry:
        psc = post_swap["centroid"]
        result["post_swap_centroid"] = [round(c, 1) for c in psc]
        result["swap_geometry"] = swap_geometry
        # Centroid-based metrics (kept for trajectory comparison only)
        dx0, dy0, dx1, dy1 = swap_geometry["dst"]
        sx0, sy0, sx1, sy1 = swap_geometry["src"]
        in_dst = dx0 <= psc[0] <= dx1 and dy0 <= psc[1] <= dy1
        in_src = sx0 <= psc[0] <= sx1 and sy0 <= psc[1] <= sy1
        result["post_swap_in_destination"] = in_dst
        result["post_swap_in_baseline_area"] = in_src
        result["valid_swap_test"] = True

        # === r4 provenance: primitive_recall ===
        baseline_pids = set(baseline.get("primitive_ids", []))
        post_swap_pids = set(post_swap.get("primitive_ids", []))
        overlap_pids = baseline_pids & post_swap_pids
        if baseline_pids:
            result["primitive_recall"] = round(len(overlap_pids) / len(baseline_pids), 3)
        else:
            result["primitive_recall"] = 0.0
        # === r5 NEW: precision + contamination ===
        if post_swap_pids:
            result["primitive_precision"] = round(len(overlap_pids) / len(post_swap_pids), 3)
            result["contamination_count"] = len(post_swap_pids - baseline_pids)
            result["contamination_share"] = round(
                len(post_swap_pids - baseline_pids) / len(post_swap_pids), 3
            )
        else:
            result["primitive_precision"] = 0.0
            result["contamination_count"] = 0
            result["contamination_share"] = 0.0
        result["baseline_primitives"] = len(baseline_pids)
        result["post_swap_primitives"] = len(post_swap_pids)
        result["overlap_primitives"] = len(overlap_pids)

        # === r4 IoU: projected baseline → destination (TL-to-TL, matches swap_regions) ===
        dxo = dx0 - sx0
        dyo = dy0 - sy0
        bbox = baseline["bbox"]
        projected_bbox = [bbox[0] + dxo, bbox[1] + dyo, bbox[2] + dxo, bbox[3] + dyo]
        result["iou_follow"] = round(bbox_iou(post_swap["bbox"], projected_bbox), 3)
        result["iou_stay"] = round(bbox_iou(post_swap["bbox"], bbox), 3)

        # === r6 edge provenance + cluster purity metrics ===
        result["baseline_edge_classes"] = baseline.get("edge_class_counts", {})
        result["baseline_edge_entropy"] = baseline.get("edge_entropy", 0.0)
        result["baseline_compactness"] = baseline.get("boundary_compactness", 0.0)
        result["post_swap_edge_classes"] = post_swap.get("edge_class_counts", {})
        result["post_swap_edge_entropy"] = post_swap.get("edge_entropy", 0.0)
        result["post_swap_compactness"] = post_swap.get("boundary_compactness", 0.0)
        # Edge class delta (which classes appeared/disappeared post-swap)
        b_classes = set(baseline.get("edge_class_counts", {}).keys())
        ps_classes = set(post_swap.get("edge_class_counts", {}).keys())
        result["edge_classes_lost"] = list(b_classes - ps_classes)
        result["edge_classes_gained"] = list(ps_classes - b_classes)

        # === r4 trajectory class ===
        traj = classify_trajectory(baseline, post_swap, swap_geometry,
                                     result["primitive_recall"])
        result["trajectory_class"] = traj

        # Binary metrics derived from trajectory (no longer centroid-only)
        result["follows_content"] = traj == "FOLLOW"
        result["stays_at_position"] = traj == "STAY"
    else:
        result["follows_content"] = False
        result["stays_at_position"] = False
        result["valid_swap_test"] = False
        result["trajectory_class"] = "NULL_COLLAPSE" if baseline else "NO_BASELINE"
        result["primitive_recall"] = 0.0
        result["primitive_precision"] = 0.0
        result["contamination_count"] = 0
        result["contamination_share"] = 0.0
        result["iou_follow"] = 0.0
        result["iou_stay"] = 0.0
    del prims
    del swapped
    return result


def process_pdf(pdf_path):
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": str(exc)[:120]}
    page_results = []
    try:
        for pg in range(doc.page_count):
            try:
                pr = process_page(doc, pg)
                page_results.append(pr)
                print(f"    page {pg}: base={pr.get('baseline_found', False)} "
                      f"traj={pr.get('trajectory_class', '?'):<14s} "
                      f"recall={pr.get('primitive_recall', 0):.2f} "
                      f"prec={pr.get('primitive_precision', 0):.2f} "
                      f"contam={pr.get('contamination_share', 0):.2f} "
                      f"iou_F={pr.get('iou_follow', 0):.2f}", flush=True)
            except Exception as exc:
                page_results.append({"page_idx": pg, "error": str(exc)[:100]})
                print(f"    page {pg}: ERROR {str(exc)[:80]}", flush=True)
    finally:
        doc.close()
    return {"pdf_name": pdf_path.name, "n_pages": len(page_results),
            "page_results": page_results}


def main():
    print("=== Wave 20.14.5J-V3-PRG — r7 FULL CORPUS RUN (NULL_COLLAPSE fix + residential addition) ===\n", flush=True)
    if R7_FULL_CORPUS:
        pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
        set_label = "FULL CORPUS (no exclusions)"
    else:
        active_set = TUNING_PDFS if R6_USE_TUNING_SET else HOLDOUT_PDFS
        set_label = "TUNING" if R6_USE_TUNING_SET else "HOLDOUT (BLIND RUN)"
        pdfs = sorted(p for p in BLUEPRINTS.glob("*.pdf") if p.name in active_set)
    print(f"set={set_label}  processing {len(pdfs)} PDFs", flush=True)
    print(f"CORNER_RADIUS={CORNER_RADIUS}pt (r7 NULL_COLLAPSE fix from 3pt)\n", flush=True)
    all_results = []
    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---", flush=True)
        r = process_pdf(pdf)
        all_results.append(r)

    print("\n=== V29 GATE EVALUATION ===", flush=True)
    total_pages = 0
    base_found = 0
    base_in_canon = 0
    ablation_survives = 0
    ablation_stable = 0
    follows = 0
    stays = 0
    valid_tests = 0
    per_pdf_follows = defaultdict(lambda: {"valid": 0, "follows": 0, "stays": 0, "base_found": 0})
    for r in all_results:
        pdf_name = r.get("pdf_name", "?")
        for pr in r.get("page_results", []):
            if "error" in pr:
                continue
            total_pages += 1
            if pr.get("baseline_found"):
                base_found += 1
                per_pdf_follows[pdf_name]["base_found"] += 1
                if pr.get("baseline_in_canonical"):
                    base_in_canon += 1
                if pr.get("ablation_survives"):
                    ablation_survives += 1
                if pr.get("ablation_stable"):
                    ablation_stable += 1
                if pr.get("valid_swap_test"):
                    valid_tests += 1
                    per_pdf_follows[pdf_name]["valid"] += 1
                    if pr.get("follows_content"):
                        follows += 1
                        per_pdf_follows[pdf_name]["follows"] += 1
                    if pr.get("stays_at_position"):
                        stays += 1
                        per_pdf_follows[pdf_name]["stays"] += 1

    print(f"total pages tested: {total_pages}")
    print(f"baseline_found:     {base_found}  ({base_found/max(total_pages,1)*100:.1f}%)")
    print(f"baseline_in_canon:  {base_in_canon}  ({base_in_canon/max(base_found,1)*100:.1f}% of found)")
    print(f"\n--- BORDER-ONLY ABLATION (mandatory v29 gate) ---")
    print(f"ablation_survives:  {ablation_survives}  ({ablation_survives/max(base_found,1)*100:.1f}% of found)")
    print(f"ablation_stable:    {ablation_stable}  ({ablation_stable/max(base_found,1)*100:.1f}% of found)")
    survival_rate = ablation_survives / max(base_found, 1)
    stability_rate = ablation_stable / max(base_found, 1)
    print(f"\nGate (ablation survival ?=70%):   {'PASS' if survival_rate >= 0.70 else 'FAIL'}  ({survival_rate*100:.1f}%)")
    print(f"Gate (ablation stability ?=70%):  {'PASS' if stability_rate >= 0.70 else 'FAIL'}  ({stability_rate*100:.1f}%)")
    # === r4 TRAJECTORY TAXONOMY + r5 per-class precision/recall ===
    traj_counts = defaultdict(int)
    iou_follow_sum = 0.0
    iou_stay_sum = 0.0
    recall_sum = 0.0
    precision_sum = 0.0
    contamination_sum = 0.0
    per_class_recall = defaultdict(list)
    per_class_precision = defaultdict(list)
    per_class_iou = defaultdict(list)
    per_class_contamination = defaultdict(list)
    for r in all_results:
        for pr in r.get("page_results", []):
            if "error" in pr:
                continue
            traj = pr.get("trajectory_class")
            if traj:
                traj_counts[traj] += 1
                per_class_recall[traj].append(pr.get("primitive_recall", 0))
                per_class_precision[traj].append(pr.get("primitive_precision", 0))
                per_class_iou[traj].append(pr.get("iou_follow", 0))
                per_class_contamination[traj].append(pr.get("contamination_share", 0))
            iou_follow_sum += pr.get("iou_follow", 0)
            iou_stay_sum += pr.get("iou_stay", 0)
            recall_sum += pr.get("primitive_recall", 0)
            precision_sum += pr.get("primitive_precision", 0)
            contamination_sum += pr.get("contamination_share", 0)

    print(f"\n--- r4 TRAJECTORY TAXONOMY ---")
    for cls in ("FOLLOW", "STAY", "DRIFT_NEAR", "SPLIT_CLUSTER", "THIRD_REGION", "NULL_COLLAPSE", "NO_BASELINE"):
        c = traj_counts.get(cls, 0)
        pct = c / max(base_found, 1) * 100 if cls != "NO_BASELINE" else c / max(total_pages, 1) * 100
        print(f"  {cls:<18s}  {c:3d}  ({pct:.1f}%)")

    follows_rate = traj_counts.get("FOLLOW", 0) / max(base_found, 1)
    stays_rate = traj_counts.get("STAY", 0) / max(base_found, 1)
    print(f"\n--- r4 ACCEPTANCE GATES (IoU-based, not centroid) ---")
    print(f"stays_at_position:   {stays_rate*100:.1f}%   (gate: <=5%)  {'PASS' if stays_rate <= 0.05 else 'FAIL'}")
    print(f"follows_content:     {follows_rate*100:.1f}%  (gate: >=70%) {'PASS' if follows_rate >= 0.70 else 'FAIL'}")
    if base_found:
        avg_recall = recall_sum / base_found
        avg_precision = precision_sum / base_found
        avg_contam = contamination_sum / base_found
        avg_iou_f = iou_follow_sum / base_found
        print(f"avg primitive_recall:    {avg_recall:.3f}  (gate: >=0.60)  {'PASS' if avg_recall >= 0.60 else 'FAIL'}")
        print(f"avg primitive_precision: {avg_precision:.3f}  (gate: >=0.75)  {'PASS' if avg_precision >= 0.75 else 'FAIL'}")
        print(f"avg contamination_share: {avg_contam:.3f}")
        print(f"avg IoU(follow):         {avg_iou_f:.3f}  (gate: >=0.50)  {'PASS' if avg_iou_f >= 0.50 else 'FAIL'}")

    # === r5 DIAGNOSTIC: per-class precision (the breakthrough question) ===
    print(f"\n--- r5 PER-CLASS DIAGNOSTIC (recall + precision + IoU per trajectory) ---")
    for cls in ("FOLLOW", "DRIFT_NEAR", "SPLIT_CLUSTER", "THIRD_REGION", "STAY", "NULL_COLLAPSE"):
        recalls = per_class_recall.get(cls, [])
        precisions = per_class_precision.get(cls, [])
        ious = per_class_iou.get(cls, [])
        contams = per_class_contamination.get(cls, [])
        if not recalls:
            continue
        avg_r = sum(recalls) / len(recalls)
        avg_p = sum(precisions) / len(precisions)
        avg_i = sum(ious) / len(ious)
        avg_c = sum(contams) / len(contams)
        diag = ""
        if cls in ("DRIFT_NEAR", "THIRD_REGION") and avg_r > 0.6 and avg_p < 0.30:
            diag = " <- CLUSTER GREED (model fix needed)"
        elif cls in ("DRIFT_NEAR", "THIRD_REGION") and avg_r > 0.6 and avg_p > 0.70:
            diag = " <- BBOX EVAL ISSUE (eval fix needed)"
        elif cls == "SPLIT_CLUSTER" and avg_p > 0.5:
            diag = " <- pure but split (motif merge needed)"
        print(f"  {cls:<14s} n={len(recalls)}  recall={avg_r:.2f}  prec={avg_p:.2f}  contam={avg_c:.2f}  iou={avg_i:.2f}{diag}")
    print(f"\nTrajectory: v26=0%/100% / v28=7.7% / v29r3=52.9%/0% / v29r4_IoU={follows_rate*100:.1f}%/{stays_rate*100:.1f}%")
    print(f"\n--- PER-PDF BREAKDOWN (mission gate: >=3 distinct PDFs with follows>0) ---")
    pdfs_with_follows = 0
    for pdf_name, stats in sorted(per_pdf_follows.items()):
        marker = ""
        if stats["valid"] > 0:
            if stats["follows"] > 0:
                pdfs_with_follows += 1
                marker = " <-- FOLLOWS"
            print(f"  {pdf_name[:40]:<40s}  base={stats['base_found']}  valid={stats['valid']}  follows={stats['follows']}  stays={stats['stays']}{marker}")
        else:
            print(f"  {pdf_name[:40]:<40s}  base={stats['base_found']}  valid={stats['valid']}  (no valid swap tests)")
    print(f"\nMISSION GATE: {pdfs_with_follows} distinct PDFs with follows>0  --  {'PASS >=3' if pdfs_with_follows >= 3 else 'FAIL'}")

    # === r6 EDGE PROVENANCE AUDIT ===
    print(f"\n--- r6 EDGE PROVENANCE AUDIT (cluster edge composition) ---")
    baseline_class_totals = defaultdict(int)
    post_swap_class_totals = defaultdict(int)
    baseline_entropy_sum = 0.0
    post_swap_entropy_sum = 0.0
    baseline_compact_sum = 0.0
    post_swap_compact_sum = 0.0
    edge_class_pages = 0
    classes_lost_counts = defaultdict(int)
    classes_gained_counts = defaultdict(int)
    for r in all_results:
        for pr in r.get("page_results", []):
            if "error" in pr or not pr.get("baseline_found"):
                continue
            edge_class_pages += 1
            for cls, cnt in (pr.get("baseline_edge_classes") or {}).items():
                baseline_class_totals[cls] += cnt
            for cls, cnt in (pr.get("post_swap_edge_classes") or {}).items():
                post_swap_class_totals[cls] += cnt
            baseline_entropy_sum += pr.get("baseline_edge_entropy", 0.0)
            post_swap_entropy_sum += pr.get("post_swap_edge_entropy", 0.0)
            baseline_compact_sum += pr.get("baseline_compactness", 0.0)
            post_swap_compact_sum += pr.get("post_swap_compactness", 0.0)
            for c in pr.get("edge_classes_lost") or []:
                classes_lost_counts[c] += 1
            for c in pr.get("edge_classes_gained") or []:
                classes_gained_counts[c] += 1

    if edge_class_pages:
        b_total = sum(baseline_class_totals.values()) or 1
        ps_total = sum(post_swap_class_totals.values()) or 1
        print(f"  baseline edge composition (n={edge_class_pages}):")
        for cls in sorted(baseline_class_totals, key=lambda c: -baseline_class_totals[c]):
            print(f"    {cls:<22s} {baseline_class_totals[cls]:5d}  ({baseline_class_totals[cls]/b_total*100:.1f}%)")
        print(f"  post-swap edge composition:")
        for cls in sorted(post_swap_class_totals, key=lambda c: -post_swap_class_totals[c]):
            print(f"    {cls:<22s} {post_swap_class_totals[cls]:5d}  ({post_swap_class_totals[cls]/ps_total*100:.1f}%)")
        print(f"  classes lost in any page:   {dict(classes_lost_counts)}")
        print(f"  classes gained in any page: {dict(classes_gained_counts)}")
        print(f"\n  avg edge_entropy   baseline={baseline_entropy_sum/edge_class_pages:.3f}  post_swap={post_swap_entropy_sum/edge_class_pages:.3f}")
        print(f"  avg compactness    baseline={baseline_compact_sum/edge_class_pages:.3f}  post_swap={post_swap_compact_sum/edge_class_pages:.3f}")
        delta_compact = (post_swap_compact_sum - baseline_compact_sum) / edge_class_pages
        print(f"  delta compactness: {delta_compact:+.3f}  (positive = post-swap cluster less compact / more sprawled)")
    print("", flush=True)

    out = OUT_DIR / "prg_v29_2026-05-24.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V3-PRG-v29",
            "total_pages": total_pages,
            "baseline_found": base_found,
            "baseline_in_canonical": base_in_canon,
            "ablation_survives": ablation_survives,
            "ablation_stable": ablation_stable,
            "follows_content": follows,
            "stays_at_position": stays,
            "valid_swap_tests": valid_tests,
            "pdfs_with_follows": pdfs_with_follows,
            "rates": {
                "ablation_survival": round(survival_rate, 4),
                "ablation_stability": round(stability_rate, 4),
                "follows": round(follows_rate, 4),
                "stays": round(stays_rate, 4),
            },
            "per_pdf_follows": dict(per_pdf_follows),
            "per_pdf": all_results,
        }, f, indent=2)
    print(f"\nsaved: {out}", flush=True)


if __name__ == "__main__":
    main()
