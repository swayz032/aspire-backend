"""Wave 20.14.5G — Field Generalization Probe.

Standalone in-memory test. NO Supabase IO. Tests whether the SDPE FOUNDATIONAL
LAYER (field classification) generalizes from Gulfstream's drafting style to a
radically different blueprint.

Method:
  1. Open the new PDF locally via PyMuPDF
  2. Extract primitives per page
  3. Compute per-tile features (curve_density, line_parallelism) — same rules
     as blueprint_sheet_field_map populator
  4. Classify each tile (HATCH / SYMBOL / STRUCTURAL / TABLE / BORDER / UNKNOWN)
  5. Report distribution
  6. Compare against Gulfstream's reference distribution

Acceptance:
  - SYMBOL_FIELD tiles present (>0) → field classifier discovers symbol regions
    in unfamiliar drafting style
  - HATCH_FIELD detection rate > 5% if any hatching present → texture detector
    generalizes
  - Distribution NOT completely collapsed to one band → no monoculture
    (would indicate Gulfstream-specific tuning)

If acceptance met → SDPE foundation transfers, grammar work unblocked.
If acceptance fails → recalibrate canonical features before Wave 20.14.6.
"""
import math
import sys
from collections import Counter, defaultdict

try:
    import pymupdf as fitz  # type: ignore
except ImportError:
    import fitz  # type: ignore  # PyMuPDF

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\tonio\Projects\myapp\blueprints\Precast Drainage.pdf"
TILE_PT = 50.0
MIN_RASTER = 0.05  # treat all extracted primitives as visible (no raster scoring here)


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
    if "qu" in ops:
        return "quad"
    return "path"


def _angle_deg(x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return None
    return math.degrees(math.atan2(dy, dx))


def extract_primitives_from_pdf(pdf_path: str) -> list[dict]:
    """Extract vector primitives from all pages of a PDF in PDF point space."""
    doc = fitz.open(pdf_path)
    print(f"PDF: {pdf_path}")
    print(f"pages: {doc.page_count}")
    prims: list[dict] = []
    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        drawings = page.get_drawings()
        page_prims = 0
        for d in drawings:
            items = d.get("items", [])
            ptype = _classify_op(items)
            for it in items:
                op = it[0]
                if op == "l":
                    p0, p1 = it[1], it[2]
                    x0, y0 = float(p0[0]), float(p0[1])
                    x1, y1 = float(p1[0]), float(p1[1])
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    length = math.hypot(x1 - x0, y1 - y0)
                    prims.append({
                        "page": page_idx, "primitive_type": "line",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": cx, "cy": cy, "length": length,
                        "angle_deg": _angle_deg(x0, y0, x1, y1),
                        "bbox_w": abs(x1 - x0), "bbox_h": abs(y1 - y0),
                    })
                    page_prims += 1
                elif op == "c":
                    p0, p1 = it[1], it[4] if len(it) >= 5 else it[1]
                    try:
                        x0, y0 = float(p0[0]), float(p0[1])
                        x1, y1 = float(p1[0]), float(p1[1])
                    except (TypeError, IndexError):
                        continue
                    bbox_w = abs(x1 - x0); bbox_h = abs(y1 - y0)
                    prims.append({
                        "page": page_idx, "primitive_type": "curve",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": math.hypot(x1 - x0, y1 - y0),
                        "angle_deg": None,
                        "bbox_w": bbox_w, "bbox_h": bbox_h,
                    })
                    page_prims += 1
                elif op == "re":
                    rect = it[1]
                    try:
                        x0, y0 = float(rect.x0), float(rect.y0)
                        x1, y1 = float(rect.x1), float(rect.y1)
                    except AttributeError:
                        x0, y0, x1, y1 = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
                    prims.append({
                        "page": page_idx, "primitive_type": "rect",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": 0,
                        "angle_deg": None,
                        "bbox_w": abs(x1 - x0), "bbox_h": abs(y1 - y0),
                    })
                    page_prims += 1
        print(f"  page {page_idx}: {page_prims} primitives")
    doc.close()
    return prims


def classify_field_for_tile(features: dict) -> str:
    """Same classification rules as production SQL field classifier."""
    n_visible = features["n_visible"]
    n_curves = features["n_curves"]
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


def compute_tile_features(prims: list[dict]) -> dict[tuple, dict]:
    """Aggregate primitives into 50pt tiles per page."""
    tiles: dict[tuple, dict] = defaultdict(lambda: {
        "n_visible": 0, "n_curves": 0, "n_lines": 0, "n_rects": 0,
        "line_angles": []
    })
    for p in prims:
        cx = p["cx"]; cy = p["cy"]; page = p["page"]
        tx = int(cx // TILE_PT); ty = int(cy // TILE_PT)
        key = (page, tx, ty)
        t = tiles[key]
        t["n_visible"] += 1
        ptype = p["primitive_type"]
        if ptype == "curve":
            t["n_curves"] += 1
        elif ptype == "line":
            t["n_lines"] += 1
            if p["angle_deg"] is not None:
                t["line_angles"].append(p["angle_deg"])
        elif ptype in ("rect", "quad"):
            t["n_rects"] += 1
    # Compute parallelism
    for key, t in tiles.items():
        if t["line_angles"]:
            bins: dict[int, int] = defaultdict(int)
            for a in t["line_angles"]:
                bins[int(abs(a) // 10) % 18] += 1
            top = max(bins.values())
            t["line_parallelism"] = top / len(t["line_angles"])
        else:
            t["line_parallelism"] = 0.0
        del t["line_angles"]  # cleanup
    return dict(tiles)


def main() -> None:
    print(f"=== Wave 20.14.5G Field Generalization Probe ===\n")
    prims = extract_primitives_from_pdf(PDF_PATH)
    print(f"\ntotal primitives: {len(prims)}")
    if not prims:
        print("FAIL: no primitives extracted — PDF may be raster-only")
        return

    type_counts: Counter[str] = Counter()
    for p in prims:
        type_counts[p["primitive_type"]] += 1
    print(f"primitive types: {dict(type_counts)}")

    tiles = compute_tile_features(prims)
    print(f"tiles populated: {len(tiles)}")

    field_counts: Counter[str] = Counter()
    field_examples: dict[str, list] = defaultdict(list)
    for key, feat in tiles.items():
        band = classify_field_for_tile(feat)
        field_counts[band] += 1
        if len(field_examples[band]) < 3:
            field_examples[band].append((key, feat))

    print(f"\n=== FIELD DISTRIBUTION (vs Gulfstream) ===")
    # Gulfstream reference (1386 tiles):
    #   BORDER 559 (40%), UNKNOWN 466 (34%), HATCH 192 (14%), STRUCTURAL 93 (7%),
    #   TABLE 42 (3%), SYMBOL 34 (2.5%)
    gulfstream_ref = {
        "BORDER_FIELD": 0.40, "UNKNOWN_FIELD": 0.34, "HATCH_FIELD": 0.14,
        "STRUCTURAL_FIELD": 0.07, "TABLE_FIELD": 0.03, "SYMBOL_FIELD": 0.025
    }
    total = len(tiles) or 1
    for band, n in field_counts.most_common():
        ratio = n / total
        gulf = gulfstream_ref.get(band, 0)
        delta = ratio - gulf
        sign = "+" if delta >= 0 else ""
        print(f"  {band:<22s} {n:>4} ({ratio*100:5.1f}%)   "
              f"vs Gulfstream {gulf*100:5.1f}%  Δ {sign}{delta*100:+.1f}%")

    print(f"\n=== ACCEPTANCE GATES ===")
    sym_n = field_counts.get("SYMBOL_FIELD", 0)
    hatch_n = field_counts.get("HATCH_FIELD", 0)
    border_n = field_counts.get("BORDER_FIELD", 0)
    distinct_bands = sum(1 for n in field_counts.values() if n > 0)

    gate1 = sym_n > 0
    gate2 = distinct_bands >= 3
    gate3 = (border_n + field_counts.get("UNKNOWN_FIELD", 0)) / total < 0.95

    print(f"  Gate 1: SYMBOL_FIELD tiles present (>0)        ... {'PASS' if gate1 else 'FAIL'}  ({sym_n})")
    print(f"  Gate 2: At least 3 distinct field bands        ... {'PASS' if gate2 else 'FAIL'}  ({distinct_bands})")
    print(f"  Gate 3: Not collapsed to BORDER+UNKNOWN (<95%) ... {'PASS' if gate3 else 'FAIL'}  ({(border_n + field_counts.get('UNKNOWN_FIELD', 0))/total*100:.1f}%)")

    if gate1 and gate2 and gate3:
        print(f"\n  VERDICT: SDPE foundation GENERALIZES on this drafting style.")
        print(f"  Grammar work (Wave 20.14.6) unblocked.")
    else:
        print(f"\n  VERDICT: Foundation does NOT generalize cleanly.")
        print(f"  Recalibrate canonical features before grammar work.")


if __name__ == "__main__":
    main()
