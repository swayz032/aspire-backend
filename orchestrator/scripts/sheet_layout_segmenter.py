"""Wave 20.14.5J-V3-A — Structural Sheet Segmentation (sheet_layout_graph).

L1 of v23 visual-semantic bootstrap. Partition each sheet into semantic
macro-regions BEFORE any OCR or candidate extraction.

Operates at the VECTOR GEOMETRY LEVEL (not raster). Inputs: raw primitives
per page. Outputs: zoned regions classified as:
  - title_block
  - border
  - plan_field
  - schedule_or_legend
  - notes_or_detail
  - unknown

Method (3 signal families):

  A. Line-density field per tile:
     - orientation_entropy (Shannon over 18 angle bins)
     - stroke_density (visible primitives per tile area)
     - orthogonal_dominance (share of lines at 0/90 mod 180)
     - rectangular_enclosure_density (axis-aligned rect count)

  B. Connected line-cluster graphing:
     - identify dense rectangular subdivision patterns (= title blocks/schedules)
     - cluster aspect ratio + repetition signatures

  C. Repetition mining:
     - tiles with high repetition + low entropy → schedule_or_legend
     - tiles with spatial continuity + mixed orientation → plan_field
     - tiles with high density + small bbox + bottom/right position → title_block
     - tiles with low density + edge position → border

Acceptance per v23: ≥95% title block localization on 15-PDF corpus.

ZERO Supabase IO. Pure local PyMuPDF + geometry analysis.
"""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from canonicalize_local import extract_lines, canonicalize  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
OVERLAY_DIR = OUT_DIR / "layout_overlays"

TILE_PT = 50.0
ORTHO_TOLERANCE_DEG = 5.0  # within ±5° of 0/90 counts as orthogonal


# ============================================================
# Per-tile signal computation
# ============================================================

def _angle_deg(p):
    """Angle of a line primitive in degrees, normalized to [0, 180)."""
    try:
        dx = float(p["x1"]) - float(p["x0"])
        dy = float(p["y1"]) - float(p["y0"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return None
    a = math.degrees(math.atan2(dy, dx))
    return a % 180


def _shannon_entropy(counts: list[int]) -> float:
    """Shannon entropy of a discrete distribution."""
    total = sum(counts)
    if total <= 0:
        return 0.0
    ent = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            ent -= p * math.log2(p)
    return ent


def compute_tile_signals(prims: list[dict], page_extent: tuple[float, float]
                          ) -> dict[tuple[int, int], dict]:
    """Aggregate primitives into tiles + compute signal vector per tile."""
    tiles: dict[tuple[int, int], dict] = defaultdict(lambda: {
        "n_visible": 0, "n_lines": 0, "n_curves": 0, "n_rects": 0,
        "line_angles": [], "rect_sizes": [],
    })
    for p in prims:
        try:
            cx = float(p.get("cx", 0))
            cy = float(p.get("cy", 0))
        except (TypeError, ValueError):
            continue
        tx = int(cx // TILE_PT)
        ty = int(cy // TILE_PT)
        key = (tx, ty)
        t = tiles[key]
        t["n_visible"] += 1
        ptype = p.get("primitive_type") or "p"
        if ptype == "line":
            t["n_lines"] += 1
            a = _angle_deg(p)
            if a is not None:
                t["line_angles"].append(a)
        elif ptype in ("curve", "synthetic_curve"):
            t["n_curves"] += 1
        elif ptype in ("rect", "quad"):
            t["n_rects"] += 1
            try:
                bw = float(p.get("bbox_w", 0) or 0)
                bh = float(p.get("bbox_h", 0) or 0)
                t["rect_sizes"].append(max(bw, bh))
            except (TypeError, ValueError):
                pass

    # Compute derived signals per tile
    pw, ph = page_extent
    for key, t in list(tiles.items()):
        tx, ty = key
        n_total = t["n_visible"]
        n_lines = t["n_lines"]
        angles = t["line_angles"]

        # Stroke density (visible per pt²)
        stroke_density = n_total / (TILE_PT * TILE_PT)

        # Orientation entropy (Shannon over 10° angle bins)
        if angles:
            bins = [0] * 18
            for a in angles:
                bins[int(a // 10)] += 1
            ori_entropy = _shannon_entropy(bins)
        else:
            ori_entropy = 0.0

        # Orthogonal dominance: share of lines within ±5° of 0 or 90
        if angles:
            ortho_n = sum(
                1 for a in angles
                if a <= ORTHO_TOLERANCE_DEG or a >= 180 - ORTHO_TOLERANCE_DEG
                or abs(a - 90) <= ORTHO_TOLERANCE_DEG
            )
            ortho_share = ortho_n / len(angles)
        else:
            ortho_share = 0.0

        # Rectangular enclosure density
        rect_density = t["n_rects"] / (TILE_PT * TILE_PT) if n_total > 0 else 0

        # Position relative to page
        if pw > 0 and ph > 0:
            rel_x = (tx * TILE_PT + TILE_PT / 2) / pw
            rel_y = (ty * TILE_PT + TILE_PT / 2) / ph
        else:
            rel_x = rel_y = 0.5

        t["stroke_density"] = round(stroke_density, 5)
        t["ori_entropy"] = round(ori_entropy, 4)
        t["ortho_share"] = round(ortho_share, 4)
        t["rect_density"] = round(rect_density, 5)
        t["rel_x"] = round(rel_x, 4)
        t["rel_y"] = round(rel_y, 4)
        # Cleanup non-serializable lists for output
        del t["line_angles"]
        del t["rect_sizes"]

    return dict(tiles)


# ============================================================
# Connected component analysis on tile grid
# ============================================================

def cluster_high_density_tiles(tile_signals: dict, density_threshold: float = 0.008
                                ) -> list[set[tuple[int, int]]]:
    """Connected-component clustering of tiles above density threshold."""
    high_density = {
        k for k, v in tile_signals.items()
        if v["stroke_density"] >= density_threshold and v["ortho_share"] >= 0.50
    }
    clusters = []
    visited: set[tuple[int, int]] = set()
    for seed in high_density:
        if seed in visited:
            continue
        component = set()
        stack = [seed]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            component.add(cur)
            tx, ty = cur
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    n = (tx + dx, ty + dy)
                    if n in high_density and n not in visited:
                        stack.append(n)
        if len(component) >= 2:  # require min cluster size
            clusters.append(component)
    return clusters


def cluster_bbox(cluster: set[tuple[int, int]]) -> dict:
    """Bounding box + dimensions of a tile cluster (in tile coords + pt)."""
    if not cluster:
        return {}
    txs = [t[0] for t in cluster]
    tys = [t[1] for t in cluster]
    tx_min, tx_max = min(txs), max(txs)
    ty_min, ty_max = min(tys), max(tys)
    return {
        "tx_min": tx_min, "tx_max": tx_max,
        "ty_min": ty_min, "ty_max": ty_max,
        "n_tiles": len(cluster),
        "tile_width": tx_max - tx_min + 1,
        "tile_height": ty_max - ty_min + 1,
        "pt_x0": tx_min * TILE_PT,
        "pt_y0": ty_min * TILE_PT,
        "pt_x1": (tx_max + 1) * TILE_PT,
        "pt_y1": (ty_max + 1) * TILE_PT,
        "pt_width": (tx_max - tx_min + 1) * TILE_PT,
        "pt_height": (ty_max - ty_min + 1) * TILE_PT,
    }


# ============================================================
# Zone classification
# ============================================================

def classify_cluster(bbox: dict, cluster: set[tuple[int, int]],
                      tile_signals: dict, page_extent: tuple[float, float]) -> dict:
    """Classify a cluster as title_block / schedule_or_legend / plan_field / etc."""
    pw, ph = page_extent
    if pw <= 0 or ph <= 0:
        return {"zone_type": "unknown", "confidence": 0.0}

    # Position signals
    rel_cx = (bbox["pt_x0"] + bbox["pt_x1"]) / 2 / pw
    rel_cy = (bbox["pt_y0"] + bbox["pt_y1"]) / 2 / ph
    rel_w = bbox["pt_width"] / pw
    rel_h = bbox["pt_height"] / ph

    # Aggregate signal stats over cluster
    densities = [tile_signals[t]["stroke_density"] for t in cluster]
    ortho_shares = [tile_signals[t]["ortho_share"] for t in cluster]
    entropies = [tile_signals[t]["ori_entropy"] for t in cluster]

    avg_density = sum(densities) / len(densities)
    avg_ortho = sum(ortho_shares) / len(ortho_shares)
    avg_entropy = sum(entropies) / len(entropies)

    # === RULES ===
    # Title block: bottom OR right edge band, narrow rectangular cluster,
    # very high orthogonal dominance, dense
    title_block_score = 0.0
    if rel_cy > 0.80:  # bottom band
        title_block_score += 0.30
    if rel_cx > 0.70:  # right band
        title_block_score += 0.30
    if avg_ortho >= 0.80:
        title_block_score += 0.20
    if avg_density >= 0.020:
        title_block_score += 0.20
    # Narrow strip preference (title blocks are typically rectangular bands)
    aspect = max(rel_w, rel_h) / max(min(rel_w, rel_h), 0.001)
    if aspect >= 2.0:
        title_block_score += 0.10
    # Cap and apply size cap (title blocks not >50% of sheet)
    if rel_w * rel_h > 0.40:
        title_block_score -= 0.30

    # Schedule/legend: high density + high orthogonal + smaller cluster,
    # NOT bottom-right specifically
    schedule_score = 0.0
    if avg_ortho >= 0.70:
        schedule_score += 0.25
    if avg_density >= 0.015:
        schedule_score += 0.20
    if bbox["n_tiles"] <= 30:
        schedule_score += 0.15
    if rel_cx > 0.50 and rel_cy < 0.80:  # right but not corner
        schedule_score += 0.15

    # Border: at sheet edge, mostly empty, low density
    border_score = 0.0
    if (rel_cx < 0.05 or rel_cx > 0.95 or rel_cy < 0.05 or rel_cy > 0.95):
        border_score += 0.40
    if avg_density < 0.003:
        border_score += 0.30

    # Plan field: large cluster, mixed orientation (lower ortho_share),
    # not in extreme corner
    plan_field_score = 0.0
    if bbox["n_tiles"] >= 30:
        plan_field_score += 0.30
    if avg_ortho < 0.70:
        plan_field_score += 0.20
    if 0.20 <= rel_cx <= 0.70 and 0.20 <= rel_cy <= 0.80:
        plan_field_score += 0.30

    # Pick highest scoring class
    scores = {
        "title_block": max(0.0, title_block_score),
        "schedule_or_legend": max(0.0, schedule_score),
        "border": max(0.0, border_score),
        "plan_field": max(0.0, plan_field_score),
    }
    best = max(scores.items(), key=lambda kv: kv[1])
    return {
        "zone_type": best[0] if best[1] > 0.30 else "unknown",
        "confidence": round(best[1], 3),
        "all_scores": {k: round(v, 3) for k, v in scores.items()},
        "metrics": {
            "rel_cx": round(rel_cx, 3), "rel_cy": round(rel_cy, 3),
            "rel_w": round(rel_w, 3), "rel_h": round(rel_h, 3),
            "avg_density": round(avg_density, 5),
            "avg_ortho": round(avg_ortho, 4),
            "avg_entropy": round(avg_entropy, 4),
        },
    }


# ============================================================
# Per-page layout graph
# ============================================================

def segment_page(prims: list[dict], page_extent: tuple[float, float]) -> dict:
    """Produce sheet_layout_graph for one page."""
    tile_signals = compute_tile_signals(prims, page_extent)
    clusters = cluster_high_density_tiles(tile_signals)

    zones: list[dict] = []
    for cluster in clusters:
        bbox = cluster_bbox(cluster)
        if not bbox:
            continue
        classification = classify_cluster(bbox, cluster, tile_signals, page_extent)
        zones.append({**bbox, **classification})

    return {
        "n_tiles_total": len(tile_signals),
        "n_clusters": len(clusters),
        "page_extent_pt": [page_extent[0], page_extent[1]],
        "zones": zones,
    }


# ============================================================
# Per-PDF orchestration
# ============================================================

def process_pdf(pdf_path: Path) -> dict:
    """Extract primitives + canonicalize + segment each page."""
    try:
        prims = extract_lines(str(pdf_path))
        synthetic, _, _ = canonicalize(prims)
        all_prims = prims + synthetic
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}

    # Group primitives per page
    by_page: dict[int, list[dict]] = defaultdict(list)
    for p in all_prims:
        by_page[p["page"]].append(p)

    # Per-page extents
    page_extents: dict[int, tuple[float, float]] = {}
    for page, prims_on_page in by_page.items():
        if not prims_on_page:
            continue
        max_cx = max(float(p.get("cx", 0)) for p in prims_on_page)
        max_cy = max(float(p.get("cy", 0)) for p in prims_on_page)
        page_extents[page] = (max_cx, max_cy)

    # Open doc for actual page dimensions (more accurate than primitive extent)
    try:
        doc = fitz.open(str(pdf_path))
        for pg in range(doc.page_count):
            rect = doc[pg].rect
            page_extents[pg] = (rect.width, rect.height)
        doc.close()
    except Exception:
        pass

    # Segment each page
    layouts: dict[int, dict] = {}
    for page, prims_on_page in by_page.items():
        extent = page_extents.get(page, (1.0, 1.0))
        layouts[page] = segment_page(prims_on_page, extent)

    # Title block detection summary
    n_pages_with_title_block = sum(
        1 for layout in layouts.values()
        if any(z["zone_type"] == "title_block" for z in layout["zones"])
    )
    n_total_pages = len(layouts)

    return {
        "pdf_name": pdf_path.name,
        "pdf_key": pdf_path.stem,
        "n_pages": n_total_pages,
        "n_pages_with_title_block": n_pages_with_title_block,
        "title_block_localization_rate": (
            n_pages_with_title_block / n_total_pages if n_total_pages else 0
        ),
        "layouts_per_page": {str(k): v for k, v in layouts.items()},
    }


# ============================================================
# Render visual overlay (sanity check)
# ============================================================

def render_overlay(pdf_path: Path, layout: dict, page_idx: int = 0) -> Path | None:
    """Render first page with zones overlaid (for visual validation)."""
    try:
        doc = fitz.open(str(pdf_path))
        page = doc[page_idx]
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat)
        page_w, page_h = page.rect.width, page.rect.height
        scale_x = pix.width / page_w
        scale_y = pix.height / page_h

        OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OVERLAY_DIR / f"{pdf_path.stem.replace(' ', '_').replace('(', '_').replace(')', '_')}_page{page_idx:02d}.png"
        pix.save(str(out_path))

        # Draw zones with PIL
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.open(str(out_path))
            draw = ImageDraw.Draw(img, "RGBA")
            color_map = {
                "title_block": (255, 0, 0, 100),  # red
                "schedule_or_legend": (0, 0, 255, 100),  # blue
                "plan_field": (0, 200, 0, 60),  # green
                "border": (128, 128, 128, 80),  # gray
                "unknown": (255, 200, 0, 60),  # yellow
            }
            page_layout = layout["layouts_per_page"].get(str(page_idx), {})
            for zone in page_layout.get("zones", []):
                x0 = zone["pt_x0"] * scale_x
                y0 = zone["pt_y0"] * scale_y
                x1 = zone["pt_x1"] * scale_x
                y1 = zone["pt_y1"] * scale_y
                color = color_map.get(zone["zone_type"], (200, 200, 200, 60))
                draw.rectangle([x0, y0, x1, y1], fill=color, outline=color[:3] + (255,), width=2)
                label = f"{zone['zone_type']} {zone['confidence']:.2f}"
                draw.text((x0 + 4, y0 + 4), label, fill=(0, 0, 0, 255))
            img.save(str(out_path))
        except ImportError:
            pass
        doc.close()
        return out_path
    except Exception as exc:
        print(f"  overlay FAIL: {type(exc).__name__}: {exc}")
        return None


# ============================================================
# Main: process corpus + measure acceptance
# ============================================================

def main() -> None:
    print("=== Wave 20.14.5J-V3-A Structural Sheet Segmentation ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
    results = []

    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---")
        result = process_pdf(pdf)
        results.append(result)
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue
        print(f"  pages: {result['n_pages']}  title_block detected: "
              f"{result['n_pages_with_title_block']}/{result['n_pages']} "
              f"({result['title_block_localization_rate']*100:.1f}%)")

        # Render first page overlay
        overlay_path = render_overlay(pdf, result, page_idx=0)
        if overlay_path:
            print(f"  overlay: {overlay_path.name}")

    print(f"\n=== ACCEPTANCE: Title block localization ≥95% per v23 ===\n")
    total_pages = sum(r.get("n_pages", 0) for r in results if "error" not in r)
    pages_with_tb = sum(r.get("n_pages_with_title_block", 0) for r in results if "error" not in r)
    overall_rate = pages_with_tb / max(total_pages, 1)
    print(f"  corpus total pages: {total_pages}")
    print(f"  pages with title_block detected: {pages_with_tb}")
    print(f"  overall rate: {overall_rate*100:.1f}%   "
          f"{'PASS' if overall_rate >= 0.95 else 'FAIL'}")

    print(f"\n=== PER-PDF BREAKDOWN ===")
    for r in results:
        if "error" in r:
            continue
        rate = r["title_block_localization_rate"]
        status = "✓" if rate >= 0.95 else ("?" if rate >= 0.50 else "✗")
        print(f"  {status} {r['pdf_name'][:45]:<45s} {r['n_pages_with_title_block']:>3}/{r['n_pages']:<3} ({rate*100:5.1f}%)")

    # Persist results (drop tile-level detail to keep size reasonable)
    compact_results = []
    for r in results:
        if "error" in r:
            compact_results.append(r)
            continue
        compact_layouts = {}
        for page, layout in r["layouts_per_page"].items():
            compact_layouts[page] = {
                "n_tiles_total": layout["n_tiles_total"],
                "n_clusters": layout["n_clusters"],
                "page_extent_pt": layout["page_extent_pt"],
                "zones": layout["zones"],
            }
        compact_results.append({**r, "layouts_per_page": compact_layouts})

    out_path = OUT_DIR / "sheet_layout_segmentation_2026-05-24.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V3-A",
            "total_pdfs": len(pdfs),
            "total_pages": total_pages,
            "title_block_localization_rate": overall_rate,
            "per_pdf": compact_results,
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")
    print(f"overlays saved to: {OVERLAY_DIR}")


if __name__ == "__main__":
    main()
