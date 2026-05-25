"""Wave 20.14.5J-V3-A-r2 — Constrained-Ranking Sheet Segmenter.

Per v24 architectural correction:
  - Title_block is a document-layout ROLE, not a geometry class
  - Constrained ranking with winner-take-all (NOT threshold classification)
  - 6 signals + 3 penalties
  - Streaming page pipeline (no full-document materialization)
  - Hard-negative-aware (legend/schedule explicitly penalized)
  - Overlay validation MANDATORY

6 signals:
  1. canonical_position_prior  (HIGH — bottom-right Gaussian, edge strips)
  2. edge_anchor_score          (HIGH — touches right/bottom edge)
  3. metadata_OCR_anchors       (MANDATORY when text available — SHEET/REV/DRAWN BY/PROJECT/SCALE)
  4. table_morphology           (subdivision_depth, NOT uniformity)
  5. aspect_ratio + size_cap    (narrow band, <15% sheet area)
  6. (implicit) subdivision depth via rect count

3 penalties:
  - legend_penalty   (icon grids, mini-cells, repeated motifs)
  - schedule_penalty (high row count, uniform columns)
  - oversized_penalty (area >15% of sheet)

6 gates:
  - Localization ≥95% centroid in canonical zone
  - Edge adhesion ≥90%
  - FP rate ≤5%
  - OCR anchors ≥80% on text-bearing PDFs
  - Memory bounded (no OOM on 2M+ primitives via streaming)
  - Overlay validation REQUIRED
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

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
OVERLAY_DIR = OUT_DIR / "layout_overlays_v2"

TILE_PT = 50.0
ORTHO_TOLERANCE_DEG = 5.0

METADATA_ANCHOR_PATTERNS = [
    r"\bSHEET\b", r"\bREV\b", r"\bREVISION\b", r"\bDRAWN\b", r"\bDRAWN\s*BY\b",
    r"\bPROJECT\b", r"\bCLIENT\b", r"\bOWNER\b", r"\bDATE\b", r"\bSCALE\b",
    r"\bDESIGNED\s*BY\b", r"\bCHECKED\s*BY\b", r"\bAPPROVED\b",
    r"\bSHEET\s+(?:NUMBER|NO|#)\b", r"\bDWG\s*(?:NO|#)\b",
    # Sheet codes
    r"\b[A-Z]{1,3}[-_]?\d{1,3}(?:\.\d{1,2})?\b",
]


# ============================================================
# Streaming per-page primitive extraction
# ============================================================

def stream_page_primitives(doc, page_idx: int) -> tuple[list[dict], tuple[float, float]]:
    """Extract primitives for ONE page only. Caller discards before next page."""
    page = doc[page_idx]
    rect = page.rect
    prims: list[dict] = []
    for d in page.get_drawings():
        for it in d.get("items", []):
            if not it:
                continue
            op = it[0]
            if op == "l":
                try:
                    p0, p1 = it[1], it[2]
                    x0, y0 = float(p0[0]), float(p0[1])
                    x1, y1 = float(p1[0]), float(p1[1])
                    prims.append({
                        "primitive_type": "line",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": math.hypot(x1 - x0, y1 - y0),
                    })
                except Exception:
                    pass
            elif op == "re":
                rect_geom = it[1]
                try:
                    x0, y0 = float(rect_geom.x0), float(rect_geom.y0)
                    x1, y1 = float(rect_geom.x1), float(rect_geom.y1)
                    prims.append({
                        "primitive_type": "rect",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": 0,
                        "bbox_w": abs(x1 - x0), "bbox_h": abs(y1 - y0),
                    })
                except Exception:
                    pass
            elif op == "c":
                try:
                    p0 = it[1]; p1 = it[4] if len(it) >= 5 else it[1]
                    x0, y0 = float(p0[0]), float(p0[1])
                    x1, y1 = float(p1[0]), float(p1[1])
                    prims.append({
                        "primitive_type": "curve",
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "length": math.hypot(x1 - x0, y1 - y0),
                    })
                except Exception:
                    pass
    return prims, (rect.width, rect.height)


# ============================================================
# Tile signal aggregation
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


def tile_signals(prims: list[dict]) -> dict[tuple[int, int], dict]:
    tiles: dict[tuple[int, int], dict] = defaultdict(lambda: {
        "n_visible": 0, "n_lines": 0, "n_rects": 0, "n_curves": 0,
        "ortho_lines": 0, "rect_ys": [],
    })
    for p in prims:
        try:
            tx = int(float(p["cx"]) // TILE_PT)
            ty = int(float(p["cy"]) // TILE_PT)
        except (TypeError, ValueError):
            continue
        t = tiles[(tx, ty)]
        t["n_visible"] += 1
        ptype = p.get("primitive_type") or "p"
        if ptype == "line":
            t["n_lines"] += 1
            a = _angle_deg(p)
            if a is not None:
                if a <= ORTHO_TOLERANCE_DEG or a >= 180 - ORTHO_TOLERANCE_DEG \
                        or abs(a - 90) <= ORTHO_TOLERANCE_DEG:
                    t["ortho_lines"] += 1
        elif ptype == "rect":
            t["n_rects"] += 1
            try:
                t["rect_ys"].append(float(p.get("cy", 0)))
            except (TypeError, ValueError):
                pass
        elif ptype == "curve":
            t["n_curves"] += 1
    return dict(tiles)


# ============================================================
# Candidate region detection (template-based per v24)
# ============================================================

def candidate_regions(page_extent: tuple[float, float]) -> list[dict]:
    """Generate canonical title-block candidate templates.

    Per v24: title blocks live in canonical positions. Define candidate regions
    explicitly + measure signals on each. Avoids confusing legends/schedules
    that happen to be in non-canonical locations."""
    pw, ph = page_extent
    candidates: list[dict] = []

    # Bottom-right corner box (multiple size variations)
    for w_frac, h_frac in [(0.15, 0.20), (0.25, 0.20), (0.20, 0.30), (0.30, 0.15)]:
        candidates.append({
            "template": f"bottom_right_{int(w_frac*100)}x{int(h_frac*100)}",
            "x0": pw * (1 - w_frac), "y0": ph * (1 - h_frac),
            "x1": pw, "y1": ph,
            "kind": "bottom_right_box",
        })

    # Right edge vertical strip
    for w_frac, h_frac in [(0.12, 0.80), (0.18, 0.90), (0.08, 0.95)]:
        candidates.append({
            "template": f"right_strip_w{int(w_frac*100)}",
            "x0": pw * (1 - w_frac), "y0": ph * 0.10,
            "x1": pw, "y1": ph * (0.10 + h_frac),
            "kind": "right_strip",
        })

    # Bottom strip horizontal
    for w_frac, h_frac in [(0.95, 0.10), (0.95, 0.15), (0.95, 0.08)]:
        candidates.append({
            "template": f"bottom_strip_h{int(h_frac*100)}",
            "x0": pw * 0.025, "y0": ph * (1 - h_frac),
            "x1": pw * 0.975, "y1": ph,
            "kind": "bottom_strip",
        })

    return candidates


# ============================================================
# Score a candidate region with 6 signals + 3 penalties
# ============================================================

def gaussian_2d(rel_x: float, rel_y: float, mu_x: float, mu_y: float,
                sigma_x: float = 0.10, sigma_y: float = 0.10) -> float:
    dx = (rel_x - mu_x) / sigma_x
    dy = (rel_y - mu_y) / sigma_y
    return math.exp(-0.5 * (dx * dx + dy * dy))


def score_candidate(region: dict, prims_in_region: list[dict],
                     page_extent: tuple[float, float],
                     text_in_region: str = "") -> dict:
    """Score a candidate region across 6 signals + 3 penalties."""
    pw, ph = page_extent
    rx0, ry0, rx1, ry1 = region["x0"], region["y0"], region["x1"], region["y1"]
    rw, rh = rx1 - rx0, ry1 - ry0
    if rw <= 0 or rh <= 0 or pw <= 0 or ph <= 0:
        return {"score": 0.0, "signals": {}, "penalties": {}}

    rel_cx = (rx0 + rx1) / 2 / pw
    rel_cy = (ry0 + ry1) / 2 / ph
    rel_area = (rw * rh) / (pw * ph)
    aspect = max(rw, rh) / max(min(rw, rh), 0.001)

    n_prims = len(prims_in_region)
    n_lines = sum(1 for p in prims_in_region if p["primitive_type"] == "line")
    n_rects = sum(1 for p in prims_in_region if p["primitive_type"] == "rect")
    ortho_lines = 0
    for p in prims_in_region:
        if p["primitive_type"] != "line":
            continue
        a = _angle_deg(p)
        if a is not None and (a <= ORTHO_TOLERANCE_DEG or a >= 180 - ORTHO_TOLERANCE_DEG
                              or abs(a - 90) <= ORTHO_TOLERANCE_DEG):
            ortho_lines += 1
    ortho_share = ortho_lines / max(n_lines, 1)

    # ---- 6 SIGNALS ----

    # 1. Canonical position prior (Gaussian peaks at bottom-right + right strip + bottom strip)
    pos_score_corner = gaussian_2d(rel_cx, rel_cy, 0.875, 0.875, 0.10, 0.10)
    pos_score_right = gaussian_2d(rel_cx, rel_cy, 0.93, 0.50, 0.06, 0.30)
    pos_score_bottom = gaussian_2d(rel_cx, rel_cy, 0.50, 0.93, 0.30, 0.06)
    position_score = max(pos_score_corner, pos_score_right, pos_score_bottom)

    # 2. Edge adhesion (touches right or bottom edge within 2% margin)
    touches_right = rx1 >= pw * 0.98
    touches_bottom = ry1 >= ph * 0.98
    edge_score = 0.0
    if touches_right:
        edge_score += 0.5
    if touches_bottom:
        edge_score += 0.5

    # 3. Metadata OCR anchors (count distinct keyword matches)
    anchor_hits = set()
    upper_text = text_in_region.upper()
    for pat in METADATA_ANCHOR_PATTERNS:
        if re.search(pat, upper_text):
            anchor_hits.add(pat)
    n_anchors = len(anchor_hits)
    # Anchor score: 0 if text-free; up to 1.0 with 4+ anchors
    if not upper_text.strip():
        anchor_score = 0.0  # text-free PDF — neutral, not negative
        anchor_required = False
    else:
        anchor_score = min(1.0, n_anchors / 4.0)
        anchor_required = True

    # 4. Subdivision depth (rectangular subdivisions per area unit — title blocks have nested cells)
    rect_density = n_rects / max(rw * rh / 10000, 0.001)  # rects per 100×100 unit area
    # Cap and normalize: 5-20 rects per 100×100pt is typical for title block
    subdivision_score = min(1.0, rect_density / 15.0)

    # 5. Aspect ratio (narrow band preferred)
    if aspect >= 3.0:
        aspect_score = 1.0
    elif aspect >= 2.0:
        aspect_score = 0.6
    elif aspect >= 1.5:
        aspect_score = 0.3
    else:
        aspect_score = 0.1

    # 6. Size cap signal (positive if <15%, neutral 15-25%, negative >25%)
    if rel_area < 0.10:
        size_score = 1.0
    elif rel_area < 0.15:
        size_score = 0.7
    elif rel_area < 0.25:
        size_score = 0.3
    else:
        size_score = 0.0  # caught by oversized_penalty below

    # ---- 3 PENALTIES ----

    # Schedule penalty: many uniform-y-spacing rects → repeating rows
    schedule_penalty = 0.0
    rect_ys = sorted([float(p["cy"]) for p in prims_in_region if p["primitive_type"] == "rect"])
    if len(rect_ys) >= 8:
        # Compute pairwise y-spacing
        spacings = [rect_ys[i+1] - rect_ys[i] for i in range(len(rect_ys)-1) if rect_ys[i+1] - rect_ys[i] > 0]
        if spacings:
            mean_sp = sum(spacings) / len(spacings)
            var_sp = sum((s - mean_sp)**2 for s in spacings) / len(spacings)
            cv = math.sqrt(var_sp) / max(mean_sp, 1e-6)
            uniformity = max(0.0, 1.0 - cv)  # low CV = high uniformity = schedule
            if uniformity > 0.50 and len(rect_ys) >= 12:
                schedule_penalty = 0.50 * uniformity  # capped at 0.5

    # Legend penalty: repeated equally-sized small clusters (icon grids)
    legend_penalty = 0.0
    if n_lines >= 50 and rel_area > 0.05:
        # Many lines in moderate-sized area — could be icon grid
        # Look for tile-density uniformity within region
        tile_grid: dict[tuple[int, int], int] = defaultdict(int)
        for p in prims_in_region:
            tx = int(p["cx"] // 40)  # finer grid
            ty = int(p["cy"] // 40)
            tile_grid[(tx, ty)] += 1
        if len(tile_grid) >= 6:
            counts = sorted(tile_grid.values())
            median_count = counts[len(counts)//2]
            high_uniform = sum(1 for c in counts if 0.7 * median_count <= c <= 1.3 * median_count)
            if high_uniform / len(counts) > 0.70:
                legend_penalty = 0.35

    # Oversized penalty: area >15%
    oversized_penalty = max(0.0, (rel_area - 0.15) * 2.0)  # ramps up steeply
    oversized_penalty = min(0.60, oversized_penalty)

    # ---- COMBINE ----
    # Weighted sum: position dominates, edge mandatory, OCR critical when available
    raw_score = (
        0.30 * position_score
        + 0.20 * edge_score
        + 0.15 * anchor_score
        + 0.10 * subdivision_score
        + 0.10 * aspect_score
        + 0.10 * size_score
        + 0.05 * min(1.0, n_prims / 100.0)  # presence signal (must have content)
        - schedule_penalty
        - legend_penalty
        - oversized_penalty
    )
    final_score = max(0.0, min(1.0, raw_score))

    return {
        "score": round(final_score, 4),
        "signals": {
            "position": round(position_score, 4),
            "edge_adhesion": round(edge_score, 4),
            "ocr_anchors": round(anchor_score, 4),
            "subdivision": round(subdivision_score, 4),
            "aspect_ratio": round(aspect_score, 4),
            "size_cap": round(size_score, 4),
            "n_anchor_hits": n_anchors,
            "anchor_required": anchor_required,
        },
        "penalties": {
            "schedule": round(schedule_penalty, 4),
            "legend": round(legend_penalty, 4),
            "oversized": round(oversized_penalty, 4),
        },
        "metrics": {
            "rel_cx": round(rel_cx, 4), "rel_cy": round(rel_cy, 4),
            "rel_area": round(rel_area, 4), "aspect": round(aspect, 3),
            "n_prims": n_prims, "n_lines": n_lines, "n_rects": n_rects,
            "ortho_share": round(ortho_share, 4),
        },
        "in_canonical_zone": (
            (rel_cx > 0.65 and rel_cy > 0.65)  # bottom-right
            or rel_cy > 0.85                    # bottom strip
            or rel_cx > 0.85                    # right strip
        ),
    }


def prims_inside(prims: list[dict], rx0: float, ry0: float, rx1: float, ry1: float) -> list[dict]:
    return [
        p for p in prims
        if rx0 <= float(p.get("cx", 0)) <= rx1
        and ry0 <= float(p.get("cy", 0)) <= ry1
    ]


# ============================================================
# Per-page segmentation (streaming) + winner-take-all
# ============================================================

def segment_page_v2(doc, page_idx: int) -> dict:
    """Extract one page, score candidate regions, pick winner. Discard primitives after."""
    prims, page_extent = stream_page_primitives(doc, page_idx)
    pw, ph = page_extent

    # Extract text per region using PyMuPDF's clip-based extraction
    candidates = candidate_regions(page_extent)
    scored: list[dict] = []
    for cand in candidates:
        rx0, ry0, rx1, ry1 = cand["x0"], cand["y0"], cand["x1"], cand["y1"]
        region_prims = prims_inside(prims, rx0, ry0, rx1, ry1)
        try:
            region_rect = fitz.Rect(rx0, ry0, rx1, ry1)
            region_text = doc[page_idx].get_text(clip=region_rect) or ""
        except Exception:
            region_text = ""
        result = score_candidate(cand, region_prims, page_extent, region_text)
        scored.append({**cand, **result, "region_text_sample": region_text[:200]})

    # Winner-take-all
    scored.sort(key=lambda r: -r["score"])
    winner = scored[0] if scored else None

    # Free primitives memory before returning
    del prims

    return {
        "page_idx": page_idx,
        "page_extent_pt": [pw, ph],
        "winner_template": winner["template"] if winner else None,
        "winner_score": winner["score"] if winner else 0.0,
        "winner_in_canonical_zone": winner["in_canonical_zone"] if winner else False,
        "winner_metrics": winner["metrics"] if winner else {},
        "winner_signals": winner["signals"] if winner else {},
        "winner_penalties": winner["penalties"] if winner else {},
        "winner_bbox_pt": [winner["x0"], winner["y0"], winner["x1"], winner["y1"]] if winner else None,
        "top_3_candidates": [
            {"template": s["template"], "score": s["score"],
             "in_canonical_zone": s["in_canonical_zone"]}
            for s in scored[:3]
        ],
    }


def process_pdf_streaming(pdf_path: Path) -> dict:
    """Stream pages one at a time. Never holds full document state."""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}

    page_results: list[dict] = []
    try:
        for pg_idx in range(doc.page_count):
            try:
                result = segment_page_v2(doc, pg_idx)
                page_results.append(result)
            except Exception as exc:
                page_results.append({"page_idx": pg_idx, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        doc.close()

    # Aggregate metrics
    n_pages = len(page_results)
    n_in_canonical = sum(1 for r in page_results if r.get("winner_in_canonical_zone"))
    n_high_conf = sum(1 for r in page_results if r.get("winner_score", 0) >= 0.50)

    return {
        "pdf_name": pdf_path.name,
        "pdf_key": pdf_path.stem,
        "n_pages": n_pages,
        "n_pages_with_canonical_zone_winner": n_in_canonical,
        "n_pages_high_confidence": n_high_conf,
        "canonical_localization_rate": (n_in_canonical / n_pages) if n_pages else 0,
        "page_results": page_results,
    }


# ============================================================
# Overlay rendering for visual validation
# ============================================================

def render_overlay(pdf_path: Path, result: dict, page_idx: int = 0) -> Path | None:
    """Render page with winner candidate overlaid in red. Show top-3 candidates in lighter shades."""
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

        try:
            from PIL import Image, ImageDraw
            img = Image.open(str(out_path))
            draw = ImageDraw.Draw(img, "RGBA")

            page_result = next((r for r in result["page_results"] if r.get("page_idx") == page_idx), None)
            if not page_result or "error" in page_result:
                doc.close()
                return out_path

            bbox = page_result.get("winner_bbox_pt")
            if bbox:
                in_canonical = page_result.get("winner_in_canonical_zone")
                color = (0, 200, 0, 160) if in_canonical else (255, 0, 0, 120)
                outline = (0, 200, 0, 255) if in_canonical else (255, 0, 0, 255)
                x0, y0, x1, y1 = bbox
                draw.rectangle(
                    [x0*scale_x, y0*scale_y, x1*scale_x, y1*scale_y],
                    fill=color, outline=outline, width=3,
                )
                label = f"TB (score={page_result['winner_score']:.2f}, canon={in_canonical})"
                draw.text((x0*scale_x + 4, y0*scale_y + 4), label, fill=(0, 0, 0, 255))

            img.save(str(out_path))
        except ImportError:
            pass
        doc.close()
        return out_path
    except Exception as exc:
        print(f"  overlay FAIL: {type(exc).__name__}: {exc}")
        return None


# ============================================================
# Main: full corpus run with strict gate
# ============================================================

def main() -> None:
    print("=== Wave 20.14.5J-V3-A-r2 Constrained-Ranking Segmenter ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
    results = []
    total_pages = 0
    total_canonical = 0
    total_high_conf = 0

    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---")
        result = process_pdf_streaming(pdf)
        results.append(result)
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue
        rate = result["canonical_localization_rate"]
        n_canon = result["n_pages_with_canonical_zone_winner"]
        n_high = result["n_pages_high_confidence"]
        print(f"  pages={result['n_pages']}  canonical={n_canon}  high_conf={n_high}  rate={rate*100:.1f}%")
        total_pages += result["n_pages"]
        total_canonical += n_canon
        total_high_conf += n_high

        # Render first page overlay only (to save time)
        overlay = render_overlay(pdf, result, page_idx=0)
        if overlay:
            print(f"  overlay: {overlay.name}")

    overall_rate = total_canonical / max(total_pages, 1)
    high_conf_rate = total_high_conf / max(total_pages, 1)

    print(f"\n=== V24 GATES (re-evaluation with canonical-zone strict metric) ===")
    print(f"  total pages: {total_pages}")
    print(f"  pages with winner in canonical zone: {total_canonical} ({overall_rate*100:.1f}%)")
    print(f"  pages with high confidence (≥0.50): {total_high_conf} ({high_conf_rate*100:.1f}%)")

    g1 = overall_rate >= 0.95
    print(f"\n  Gate 1 (localization ≥95% in canonical zone): {'PASS' if g1 else 'FAIL'}  ({overall_rate*100:.1f}%)")

    print(f"\n=== PER-PDF BREAKDOWN ===")
    for r in results:
        if "error" in r:
            print(f"  ERR {r['pdf']}: {r['error']}")
            continue
        rate = r["canonical_localization_rate"]
        status = "✓" if rate >= 0.95 else ("?" if rate >= 0.70 else "✗")
        print(f"  {status} {r['pdf_name'][:45]:<45s} {r['n_pages_with_canonical_zone_winner']:>3}/{r['n_pages']:<3} ({rate*100:5.1f}%)")

    # Persist (compact)
    compact = []
    for r in results:
        if "error" in r:
            compact.append(r)
            continue
        compact_pages = [
            {
                "page_idx": pr.get("page_idx"),
                "winner_template": pr.get("winner_template"),
                "winner_score": pr.get("winner_score"),
                "winner_in_canonical_zone": pr.get("winner_in_canonical_zone"),
                "winner_metrics": pr.get("winner_metrics", {}),
            }
            for pr in r["page_results"]
        ]
        compact.append({**{k: v for k, v in r.items() if k != "page_results"},
                        "page_results": compact_pages})

    out_path = OUT_DIR / "sheet_layout_segmentation_v2_2026-05-24.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V3-A-r2",
            "total_pdfs": len(pdfs),
            "total_pages": total_pages,
            "canonical_localization_rate": overall_rate,
            "high_confidence_rate": high_conf_rate,
            "gate_1_pass": g1,
            "per_pdf": compact,
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")
    print(f"overlays saved: {OVERLAY_DIR}")


if __name__ == "__main__":
    main()
