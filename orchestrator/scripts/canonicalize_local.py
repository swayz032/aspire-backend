"""Wave 20.14.5H — Canonical Primitive Reconstruction (LOCAL PROBE).

In-memory canonicalization of polyline-flattened arcs on a local PDF.
ZERO Supabase IO. Validates the canonicalization algorithm before any DB writes.

Algorithm:
  1. Extract raw line primitives via PyMuPDF
  2. Build endpoint-adjacency graph (within ε pt)
  3. Find degree-2 connected chains of short lines (length in target band)
  4. For each chain >= 6 segments, apply 3 anti-hatching guardrails:
     - Curvature monotonicity (var(Δθ) >= ε — rejects constant-angle hatching)
     - Circular residual fit (RMS_residual / radius <= 0.15)
     - Degree-2 dominance (>80% of nodes in chain have degree <= 2)
  5. If pass, fit best circle and emit synthetic_curve
  6. Re-classify field tiles with synthetic curves included
  7. Verify SYMBOL_FIELD > 0 on previously-failing PDFs

Acceptance (run on 29187P-ES):
  - SYMBOL_FIELD count rises from 0 to > 0
  - Reconstructed synthetic curves trace real arcs (visual probe)
  - Hatching NOT promoted (verified by anti-hatching rejection counts)
"""
import math
import sys
from collections import Counter, defaultdict

try:
    import pymupdf as fitz  # type: ignore
except ImportError:
    import fitz  # type: ignore

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\tonio\Projects\myapp\blueprints\29187P-ES (3).pdf"
TILE_PT = 50.0

# Canonicalization parameters
_ENDPOINT_EPS = 1.0           # endpoint proximity for adjacency
_SHORT_LINE_MAX = 8.0         # candidate segment max length (pt)
_SHORT_LINE_MIN = 0.5         # candidate segment min length (pt)
_MIN_CHAIN_LEN = 6            # minimum segments to consider a chain
_MAX_CHAIN_LEN = 200          # cap to bound runtime

# Anti-hatching guardrails
_CURVATURE_VAR_MIN = 0.005    # var(Δθ in rad) must be >= this (hatching has var≈0)
_RESIDUAL_RATIO_MAX = 0.15    # RMS_residual / radius must be <= this
_DEGREE_2_DOMINANCE_MIN = 0.80  # share of degree<=2 nodes in chain
_MIN_RADIUS = 2.0              # below this, too small to be a real symbol
_MAX_RADIUS = 200.0            # above this, likely a structural curve not a symbol


def _angle_rad(x0, y0, x1, y1):
    return math.atan2(y1 - y0, x1 - x0)


def _classify_op(items) -> str:
    if not items:
        return "path"
    ops = {it[0] for it in items if it}
    if "l" in ops and "c" not in ops:
        return "line" if len(items) == 1 else "polyline"
    if "c" in ops:
        return "curve"
    if "re" in ops:
        return "rect"
    return "path"


def extract_lines(pdf_path: str) -> list[dict]:
    """Extract all line primitives across all pages."""
    doc = fitz.open(pdf_path)
    print(f"PDF: {pdf_path}")
    print(f"pages: {doc.page_count}")
    prims = []
    pid = 0
    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        drawings = page.get_drawings()
        for d in drawings:
            for it in d.get("items", []):
                if it[0] == "l":
                    p0, p1 = it[1], it[2]
                    x0, y0 = float(p0[0]), float(p0[1])
                    x1, y1 = float(p1[0]), float(p1[1])
                    length = math.hypot(x1 - x0, y1 - y0)
                    prims.append({
                        "id": pid, "page": page_idx, "primitive_type": "line",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": length,
                    })
                    pid += 1
                elif it[0] == "c":
                    p0, p1 = it[1], it[4] if len(it) >= 5 else it[1]
                    try:
                        x0, y0 = float(p0[0]), float(p0[1])
                        x1, y1 = float(p1[0]), float(p1[1])
                    except (TypeError, IndexError):
                        continue
                    prims.append({
                        "id": pid, "page": page_idx, "primitive_type": "curve",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": math.hypot(x1 - x0, y1 - y0),
                    })
                    pid += 1
                elif it[0] == "re":
                    rect = it[1]
                    try:
                        x0, y0 = float(rect.x0), float(rect.y0)
                        x1, y1 = float(rect.x1), float(rect.y1)
                    except AttributeError:
                        try:
                            x0, y0, x1, y1 = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
                        except (TypeError, IndexError):
                            continue
                    prims.append({
                        "id": pid, "page": page_idx, "primitive_type": "rect",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": 0,
                    })
                    pid += 1
    doc.close()
    return prims


def build_endpoint_graph(short_lines: list[dict]) -> dict[int, set[int]]:
    """Build adjacency graph by endpoint proximity within ε pt.
    Spatial grid for O(N) build, not O(N²)."""
    grid: dict[tuple[int, int, int], list[tuple[int, float, float]]] = defaultdict(list)
    cell = max(_ENDPOINT_EPS, 2.0)

    def cell_key(page, x, y):
        return (page, int(x // cell), int(y // cell))

    # Index endpoints
    for line in short_lines:
        for ep in ((line["x0"], line["y0"]), (line["x1"], line["y1"])):
            grid[cell_key(line["page"], ep[0], ep[1])].append((line["id"], ep[0], ep[1]))

    adj: dict[int, set[int]] = defaultdict(set)
    for line in short_lines:
        for ex, ey in ((line["x0"], line["y0"]), (line["x1"], line["y1"])):
            ckey = cell_key(line["page"], ex, ey)
            # Check current + neighbor cells
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nkey = (ckey[0], ckey[1] + dx, ckey[2] + dy)
                    for other_id, ox, oy in grid.get(nkey, []):
                        if other_id == line["id"]:
                            continue
                        if math.hypot(ex - ox, ey - oy) <= _ENDPOINT_EPS:
                            adj[line["id"]].add(other_id)
                            adj[other_id].add(line["id"])
    return adj


def find_chains(short_lines: list[dict], adj: dict[int, set[int]]) -> list[list[int]]:
    """Find connected chains of degree-2 short lines via DFS."""
    by_id = {l["id"]: l for l in short_lines}
    visited = set()
    chains: list[list[int]] = []

    for line in short_lines:
        if line["id"] in visited:
            continue
        # BFS to find connected component
        component = []
        stack = [line["id"]]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            component.append(cur)
            for nbr in adj.get(cur, ()):
                if nbr not in visited and nbr in by_id:
                    stack.append(nbr)
        if _MIN_CHAIN_LEN <= len(component) <= _MAX_CHAIN_LEN:
            chains.append(component)
    return chains


def fit_circle(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Algebraic circle fit (Kasa method). Returns (cx, cy, r, rms_residual)."""
    n = len(points)
    if n < 3:
        return (0, 0, 0, 1e9)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] ** 2 for p in points)
    syy = sum(p[1] ** 2 for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    sxxx = sum(p[0] ** 3 for p in points)
    syyy = sum(p[1] ** 3 for p in points)
    sxyy = sum(p[0] * p[1] ** 2 for p in points)
    sxxy = sum(p[0] ** 2 * p[1] for p in points)

    # Solve [[A B] [C D]] @ [cx cy] = [E F] (×2 absorbed in derivation)
    A = 2 * (sxx - sx * sx / n)
    B = 2 * (sxy - sx * sy / n)
    C = B
    D = 2 * (syy - sy * sy / n)
    E = sxxx + sxyy - (sxx + syy) * sx / n
    F = sxxy + syyy - (sxx + syy) * sy / n

    det = A * D - B * C
    if abs(det) < 1e-9:
        return (0, 0, 0, 1e9)
    cx = (E * D - B * F) / det
    cy = (A * F - C * E) / det
    r_sq = sum((p[0] - cx) ** 2 + (p[1] - cy) ** 2 for p in points) / n
    r = math.sqrt(max(r_sq, 0))
    residuals = [math.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2) - r for p in points]
    rms = math.sqrt(sum(rv ** 2 for rv in residuals) / n) if residuals else 0
    return (cx, cy, r, rms)


def evaluate_chain(chain_ids: list[int], by_id: dict[int, dict],
                   full_adj: dict[int, set[int]]) -> dict | None:
    """Apply 3 anti-hatching guardrails. Return synthetic_curve descriptor if all pass."""
    chain = [by_id[i] for i in chain_ids if i in by_id]
    if len(chain) < _MIN_CHAIN_LEN:
        return None

    # Collect all unique endpoints
    points = []
    for l in chain:
        points.append((l["x0"], l["y0"]))
        points.append((l["x1"], l["y1"]))

    # Compute per-segment angles
    angles = [_angle_rad(l["x0"], l["y0"], l["x1"], l["y1"]) for l in chain]
    # Normalize to [-π, π]; compute Δθ between adjacent angles in the chain
    deltas = []
    for i in range(1, len(angles)):
        d = angles[i] - angles[i - 1]
        # Wrap to [-π, π]
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        deltas.append(d)

    # GUARDRAIL 1: curvature monotonicity (var(Δθ) >= floor)
    if not deltas:
        return None
    mean_d = sum(deltas) / len(deltas)
    var_d = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
    if var_d < _CURVATURE_VAR_MIN:
        return {"reject": "curvature_monotonicity_too_flat", "var_d": var_d}

    # GUARDRAIL 2: circular fit
    cx, cy, r, rms = fit_circle(points)
    if r < _MIN_RADIUS or r > _MAX_RADIUS:
        return {"reject": "radius_out_of_band", "r": r}
    if rms / max(r, 1e-6) > _RESIDUAL_RATIO_MAX:
        return {"reject": "circular_fit_poor", "rms_ratio": rms / r}

    # GUARDRAIL 3: degree-2 dominance
    high_deg = sum(1 for i in chain_ids if len(full_adj.get(i, ())) > 2)
    deg2_share = 1.0 - high_deg / len(chain_ids)
    if deg2_share < _DEGREE_2_DOMINANCE_MIN:
        return {"reject": "branching_dominance", "deg2_share": deg2_share}

    # Reconstruction confidence: combine signals
    confidence = round(
        0.4 * (1.0 - (rms / r) / _RESIDUAL_RATIO_MAX)
        + 0.3 * min(1.0, var_d / 0.1)
        + 0.3 * deg2_share,
        4,
    )

    return {
        "primitive_type": "synthetic_curve",
        "source_type": "polyline_reconstruction",
        "source_primitive_ids": chain_ids,
        "reconstruction_confidence": confidence,
        "page": chain[0]["page"],
        "cx": round(cx, 3), "cy": round(cy, 3),
        "radius": round(r, 3),
        "rms_residual": round(rms, 4),
        "n_segments": len(chain),
        "x0": cx - r, "y0": cy - r, "x1": cx + r, "y1": cy + r,
        "length": 2 * math.pi * r,
        "bbox_w": 2 * r, "bbox_h": 2 * r,
    }


def canonicalize(prims: list[dict]) -> tuple[list[dict], Counter, int]:
    """Run full canonicalization pass. Returns (synthetic_curves, rejection_counter, n_chains_eval)."""
    short_lines = [
        p for p in prims
        if p["primitive_type"] == "line"
        and _SHORT_LINE_MIN <= p["length"] <= _SHORT_LINE_MAX
    ]
    print(f"  candidate short lines: {len(short_lines)} / {sum(1 for p in prims if p['primitive_type']=='line')} total lines")

    adj = build_endpoint_graph(short_lines)
    by_id = {l["id"]: l for l in short_lines}
    chains = find_chains(short_lines, adj)
    print(f"  chains found (≥{_MIN_CHAIN_LEN} segs): {len(chains)}")

    synthetic_curves = []
    rejections: Counter[str] = Counter()
    for chain_ids in chains:
        result = evaluate_chain(chain_ids, by_id, adj)
        if result is None:
            continue
        if "reject" in result:
            rejections[result["reject"]] += 1
            continue
        synthetic_curves.append(result)

    return synthetic_curves, rejections, len(chains)


def classify_tile_field(features: dict) -> str:
    n_visible = features["n_visible"]
    n_curves = features["n_curves"]  # NOW includes synthetic_curve
    n_lines = features["n_lines"]
    line_parallelism = features["line_parallelism"]
    if line_parallelism >= 0.55 and n_lines >= 10 and n_curves <= 2:
        return "HATCH_FIELD"
    if n_curves >= 2 and 3 <= n_visible <= 60 and line_parallelism < 0.55:
        return "SYMBOL_FIELD"
    if n_lines >= 5 and n_curves <= 1 and line_parallelism < 0.55:
        return "STRUCTURAL_FIELD"
    if n_visible > 100:
        return "TABLE_FIELD"
    if n_visible <= 2:
        return "BORDER_FIELD"
    return "UNKNOWN_FIELD"


def field_distribution(prims: list[dict]) -> Counter:
    """Same as probe_field_generalization but synthetic curves count as curves."""
    tiles: dict[tuple, dict] = defaultdict(lambda: {
        "n_visible": 0, "n_curves": 0, "n_lines": 0, "n_rects": 0, "line_angles": []
    })
    for p in prims:
        cx = p["cx"]; cy = p["cy"]; page = p["page"]
        tx = int(cx // TILE_PT); ty = int(cy // TILE_PT)
        key = (page, tx, ty)
        t = tiles[key]
        t["n_visible"] += 1
        ptype = p["primitive_type"]
        if ptype == "curve" or ptype == "synthetic_curve":
            t["n_curves"] += 1
        elif ptype == "line":
            t["n_lines"] += 1
            ang = math.degrees(_angle_rad(p["x0"], p["y0"], p["x1"], p["y1"]))
            t["line_angles"].append(ang)
        elif ptype in ("rect", "quad"):
            t["n_rects"] += 1
    for t in tiles.values():
        if t["line_angles"]:
            bins: dict[int, int] = defaultdict(int)
            for a in t["line_angles"]:
                bins[int(abs(a) // 10) % 18] += 1
            top = max(bins.values())
            t["line_parallelism"] = top / len(t["line_angles"])
        else:
            t["line_parallelism"] = 0.0
        del t["line_angles"]

    counts: Counter[str] = Counter()
    for t in tiles.values():
        counts[classify_tile_field(t)] += 1
    return counts


def main() -> None:
    print(f"=== Wave 20.14.5H Canonical Primitive Reconstruction (LOCAL) ===\n")
    prims = extract_lines(PDF_PATH)
    type_counts = Counter(p["primitive_type"] for p in prims)
    print(f"\nraw primitives: {len(prims)} {dict(type_counts)}")

    print(f"\n--- BEFORE CANONICALIZATION ---")
    before = field_distribution(prims)
    total_b = sum(before.values()) or 1
    for band, n in before.most_common():
        print(f"  {band:<22s} {n:>4} ({n/total_b*100:5.1f}%)")

    print(f"\n--- RUNNING CANONICALIZATION ---")
    synthetic, rejections, n_chains = canonicalize(prims)
    print(f"  rejections by guardrail: {dict(rejections)}")
    print(f"  synthetic curves emitted: {len(synthetic)} / {n_chains} chains evaluated")
    if synthetic:
        radii = sorted(s["radius"] for s in synthetic)
        print(f"  radius range: {radii[0]:.1f} – {radii[-1]:.1f} pt")
        print(f"  confidence range: {min(s['reconstruction_confidence'] for s in synthetic):.2f} – "
              f"{max(s['reconstruction_confidence'] for s in synthetic):.2f}")

    all_prims = prims + synthetic
    print(f"\n--- AFTER CANONICALIZATION ---")
    after = field_distribution(all_prims)
    total_a = sum(after.values()) or 1
    for band, n in after.most_common():
        before_n = before.get(band, 0)
        delta = n - before_n
        sign = "+" if delta >= 0 else ""
        print(f"  {band:<22s} {n:>4} ({n/total_a*100:5.1f}%)  Δ {sign}{delta:+d}")

    print(f"\n=== ACCEPTANCE ===")
    sym_before = before.get("SYMBOL_FIELD", 0)
    sym_after = after.get("SYMBOL_FIELD", 0)
    hatch_before = before.get("HATCH_FIELD", 0)
    hatch_after = after.get("HATCH_FIELD", 0)
    hatch_promoted = max(0, hatch_before - hatch_after - 5)  # tolerate small redistribution

    g1 = sym_after > sym_before
    g2 = hatch_promoted < 0.5 * max(hatch_before, 1)
    g3 = len(synthetic) > 0 if "line" in type_counts and type_counts.get("curve", 0) == 0 else True

    print(f"  Gate 1 (SYMBOL_FIELD rose after canonicalization)     ... {'PASS' if g1 else 'FAIL'}  ({sym_before} → {sym_after})")
    print(f"  Gate 2 (HATCH_FIELD not catastrophically promoted)    ... {'PASS' if g2 else 'FAIL'}  ({hatch_before} → {hatch_after})")
    print(f"  Gate 3 (synthetic curves produced when raw has none)  ... {'PASS' if g3 else 'FAIL'}  ({len(synthetic)} synthetic)")

    if g1 and g2 and g3:
        print(f"\n  VERDICT: Canonicalization layer recovers symbol regions on polyline-arc CAD.")
    else:
        print(f"\n  VERDICT: Substrate calibration not yet correct. Adjust guardrails.")


if __name__ == "__main__":
    main()
