"""Wave 20.14.5J-V3-PRG-r10 — Raster Bridge v1 (Production Raster Ingestion Layer).

CRITICAL DISCIPLINE:
  PRG is modality-agnostic. This bridge adapts TO PRG, never the other way.
  PRG's primitive schema is the contract. Raster output must satisfy it exactly.

Architecture:
  PDF Upload
    -> [Input Classifier] vector vs raster vs mixed
    -> [Raster Path] image normalization -> line extraction -> endpoint snap
    -> [Synthetic Primitive Graph] -> PRG schema-compatible primitives
    -> [PRG] (unchanged from r8)

What this bridge does NOT do tonight:
  - OCR anchor extraction (PaddleOCR/Tesseract unavailable; deferred to r10.1)
  - GPU acceleration
  - Learned line reconstruction
  - Confidence calibration (r12 work)

Failure taxonomy (mandatory):
  NO_LINES         line extractor returned nothing
  LOW_CONTRAST     image normalize failed
  ROTATION_FAIL    deskew couldn't determine orientation
  GRAPH_FRAGMENT   too few primitives for PRG to find any cluster
  OVERMERGE        line merge collapsed structure
  OCR_DEFERRED     OCR not available in v1 (informational only)
  NO_BASELINE      PRG ran but found no baseline (architecture-level)
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    import pymupdf as fitz
except ImportError:
    import fitz

sys.path.insert(0, str(Path(__file__).parent))

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
RASTER_CACHE = Path(r"C:\Users\tonio\Projects\myapp\blueprints\raster_r9")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

# ============================================================
# Configuration
# ============================================================

DEFAULT_DPI = 200.0
MAX_IMAGE_DIM = 3000  # downsample very large rasters
PDF_NATIVE_DPI = 72.0


@dataclass
class RasterTelemetry:
    pdf_name: str
    page_idx: int
    vector_mode: bool
    dpi: float
    line_count: int = 0
    lines_after_merge: int = 0
    quad_count: int = 0
    ocr_spans: int = 0
    cluster_count: int = 0
    largest_cluster_share: float = 0.0
    graph_density: float = 0.0
    titleblock_confidence: float = 0.0
    failure_mode: Optional[str] = None
    elapsed_ms: float = 0.0
    skew_angle_deg: float = 0.0


# ============================================================
# Input Classifier
# ============================================================

def classify_pdf_input(pdf_path: Path) -> dict:
    """Decide vector vs raster vs mixed. Returns dict with per-page modality."""
    doc = fitz.open(str(pdf_path))
    per_page = []
    vector_pages = 0
    raster_pages = 0
    try:
        for pg in range(doc.page_count):
            page = doc[pg]
            drawings = page.get_drawings()
            has_drawings = len(drawings) > 0
            n_prims = sum(len(d.get("items", [])) for d in drawings)
            images = page.get_images(full=False)
            has_images = len(images) > 0
            # Mode: vector if drawings present and >50 primitives. Raster otherwise.
            if has_drawings and n_prims > 50:
                mode = "vector"
                vector_pages += 1
            elif has_images and not has_drawings:
                mode = "raster"
                raster_pages += 1
            elif has_drawings and has_images:
                mode = "mixed"
                raster_pages += 1  # treat mixed as raster path for safety
            else:
                mode = "raster"
                raster_pages += 1
            per_page.append({
                "page_idx": pg,
                "mode": mode,
                "n_drawings": len(drawings),
                "n_primitives": n_prims,
                "n_images": len(images),
            })
    finally:
        doc.close()
    return {
        "total_pages": len(per_page),
        "vector_pages": vector_pages,
        "raster_pages": raster_pages,
        "per_page": per_page,
    }


# ============================================================
# Stage 1 — Image Normalization
# ============================================================

def render_pdf_page_to_image(doc, page_idx: int, dpi: float = DEFAULT_DPI) -> np.ndarray:
    """Render a PDF page to a numpy grayscale image."""
    page = doc[page_idx]
    zoom = dpi / PDF_NATIVE_DPI
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    # Downsample if too large
    if max(img.shape) > MAX_IMAGE_DIM:
        scale = MAX_IMAGE_DIM / max(img.shape)
        new_w = int(img.shape[1] * scale)
        new_h = int(img.shape[0] * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        dpi = dpi * scale  # effective dpi
    return img, dpi


def estimate_skew_angle(binary: np.ndarray) -> float:
    """Estimate dominant skew via Hough on edges. Returns degrees in [-45, 45]."""
    edges = cv2.Canny(binary, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=200)
    if lines is None or len(lines) < 5:
        return 0.0
    angles = []
    for line in lines[:200]:
        rho, theta = line[0]
        deg = math.degrees(theta) - 90.0
        # Normalize to [-45, 45]
        while deg > 45:
            deg -= 90
        while deg < -45:
            deg += 90
        angles.append(deg)
    if not angles:
        return 0.0
    # Median is robust to outliers
    return float(np.median(angles))


def normalize_image(img: np.ndarray, deskew: bool = True) -> tuple[np.ndarray, float]:
    """Grayscale -> adaptive threshold -> optional deskew -> denoise. Returns binary + skew."""
    # Contrast normalization
    img_norm = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)
    # Adaptive threshold for variable-illumination scans
    binary = cv2.adaptiveThreshold(
        img_norm, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 25, 10
    )
    # Light morphological close to bridge small gaps
    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    # Deskew
    skew = 0.0
    if deskew:
        skew = estimate_skew_angle(binary)
        if abs(skew) > 0.5:
            h, w = binary.shape
            M = cv2.getRotationMatrix2D((w / 2, h / 2), skew, 1.0)
            binary = cv2.warpAffine(
                binary, M, (w, h), flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE
            )
    return binary, skew


# ============================================================
# Stage 2 — Structural Line Extraction
# ============================================================

def extract_lines_hough(binary: np.ndarray, dpi: float) -> list[tuple[float, float, float, float]]:
    """Probabilistic Hough — fast, good for long structural lines."""
    # Scale-aware parameters
    min_line_len = max(int(0.05 * dpi), 8)         # ~0.05 inch minimum
    max_line_gap = max(int(0.02 * dpi), 3)
    threshold = max(int(0.04 * dpi), 30)
    lines = cv2.HoughLinesP(
        binary, rho=1, theta=np.pi / 180,
        threshold=threshold,
        minLineLength=min_line_len,
        maxLineGap=max_line_gap,
    )
    if lines is None:
        return []
    out = []
    for line in lines:
        x0, y0, x1, y1 = line[0]
        out.append((float(x0), float(y0), float(x1), float(y1)))
    return out


def extract_lines_contour(binary: np.ndarray, dpi: float
                          ) -> list[tuple[float, float, float, float]]:
    """Contour-based line extraction — catches what Hough misses on broken structure."""
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_TC89_L1)
    out = []
    min_len_sq = (0.04 * dpi) ** 2
    for c in contours:
        if len(c) < 2:
            continue
        # Approximate contour to polyline
        epsilon = 1.5
        approx = cv2.approxPolyDP(c, epsilon, False)
        pts = approx.reshape(-1, 2)
        for i in range(len(pts) - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            if (x1 - x0) ** 2 + (y1 - y0) ** 2 < min_len_sq:
                continue
            out.append((x0, y0, x1, y1))
    return out


# ============================================================
# Stage 3 — Line Merging
# ============================================================

def angle_of(x0, y0, x1, y1) -> float:
    """Angle in degrees, normalized to [0, 180)."""
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return 0.0
    return math.degrees(math.atan2(dy, dx)) % 180


def is_orthogonal_angle(angle: float, tol: float = 5.0) -> bool:
    return angle <= tol or angle >= 180 - tol or abs(angle - 90) <= tol


def merge_collinear_lines(lines: list[tuple[float, float, float, float]],
                            tol_perp: float = 2.5,
                            gap_tol: float = 8.0
                          ) -> list[tuple[float, float, float, float]]:
    """Merge collinear segments that overlap or are within gap_tol of each other."""
    if not lines:
        return []
    # Bucket by angle bin (every 3 degrees) and perpendicular distance bucket
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x0, y0, x1, y1) in enumerate(lines):
        a = angle_of(x0, y0, x1, y1)
        a_bin = int(a // 3)
        # Perpendicular distance from origin for normalized line direction
        # For an axis-aligned line, this is just y (horizontal) or x (vertical)
        # General: |y0*dx - x0*dy| / length
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        perp = abs(y0 * dx - x0 * dy) / length
        p_bin = int(perp // tol_perp)
        buckets[(a_bin, p_bin)].append(i)
        # Also bucket adjacent perp bins for robustness
        buckets[(a_bin, p_bin + 1)].append(i)

    used = set()
    merged = []
    for key, idxs in buckets.items():
        # Dedupe candidates
        cands = sorted(set(idxs))
        for i in cands:
            if i in used:
                continue
            # Greedy merge with other cands in same bucket
            x0, y0, x1, y1 = lines[i]
            cluster = [i]
            for j in cands:
                if j == i or j in used:
                    continue
                jx0, jy0, jx1, jy1 = lines[j]
                # Check overlap/proximity along line direction
                dx, dy = x1 - x0, y1 - y0
                length = math.hypot(dx, dy)
                if length < 1e-6:
                    continue
                # Project j's endpoints onto i's direction
                ux, uy = dx / length, dy / length
                # Distances along i from i's start
                t_i0 = 0.0
                t_i1 = length
                t_j0 = (jx0 - x0) * ux + (jy0 - y0) * uy
                t_j1 = (jx1 - x0) * ux + (jy1 - y0) * uy
                # Perpendicular offset of j from i
                px = (jx0 - x0) - t_j0 * ux
                py = (jy0 - y0) - t_j0 * uy
                perp_off = math.hypot(px, py)
                if perp_off > tol_perp:
                    continue
                # Overlap or gap small enough?
                lo, hi = min(t_j0, t_j1), max(t_j0, t_j1)
                if lo > t_i1 + gap_tol or hi < t_i0 - gap_tol:
                    continue
                # Extend i to include j
                t_i0 = min(t_i0, lo)
                t_i1 = max(t_i1, hi)
                # Update endpoints
                x0 = (lines[i][0] if t_i0 == 0.0
                      else lines[i][0] + t_i0 * ux)
                y0 = (lines[i][1] if t_i0 == 0.0
                      else lines[i][1] + t_i0 * uy)
                x1 = lines[i][0] + t_i1 * ux
                y1 = lines[i][1] + t_i1 * uy
                cluster.append(j)
            for j in cluster:
                used.add(j)
            merged.append((x0, y0, x1, y1))
    return merged


# ============================================================
# Stage 4 — Endpoint Snapping (DPI-aware)
# ============================================================

def snap_endpoints(lines: list[tuple[float, float, float, float]],
                    snap_radius: float = 6.0
                  ) -> list[tuple[float, float, float, float]]:
    """Snap close endpoints to shared points so PRG's 4-cycle detector can find quads."""
    if not lines:
        return []
    # Collect all endpoints
    eps = []
    for x0, y0, x1, y1 in lines:
        eps.append((x0, y0))
        eps.append((x1, y1))
    # Quantize
    bins: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x, y) in enumerate(eps):
        key = (round(x / snap_radius), round(y / snap_radius))
        bins[key].append(i)
    # Compute canonical position per bin (centroid)
    canonical: dict[tuple[int, int], tuple[float, float]] = {}
    for key, idxs in bins.items():
        cx = sum(eps[i][0] for i in idxs) / len(idxs)
        cy = sum(eps[i][1] for i in idxs) / len(idxs)
        canonical[key] = (cx, cy)
    # Map each endpoint to its canonical
    snapped_eps = []
    for x, y in eps:
        key = (round(x / snap_radius), round(y / snap_radius))
        snapped_eps.append(canonical[key])
    # Reconstruct lines
    snapped_lines = []
    for i in range(len(lines)):
        x0, y0 = snapped_eps[2 * i]
        x1, y1 = snapped_eps[2 * i + 1]
        if math.hypot(x1 - x0, y1 - y0) < 1.0:
            continue  # collapsed to zero-length
        snapped_lines.append((x0, y0, x1, y1))
    return snapped_lines


# ============================================================
# Stage 5 — Coordinate Space Conversion (raster -> PDF pt)
# ============================================================

def raster_lines_to_pdf_primitives(lines: list[tuple[float, float, float, float]],
                                     dpi: float,
                                     page_w_pt: float, page_h_pt: float
                                   ) -> list[dict]:
    """Convert raster-space lines (pixels) to PDF-coordinate primitives in PRG schema."""
    scale = PDF_NATIVE_DPI / dpi
    out = []
    for x0, y0, x1, y1 in lines:
        px0 = x0 * scale
        py0 = y0 * scale
        px1 = x1 * scale
        py1 = y1 * scale
        length = math.hypot(px1 - px0, py1 - py0)
        out.append({
            "primitive_type": "line",
            "x0": px0, "y0": py0, "x1": px1, "y1": py1,
            "cx": (px0 + px1) / 2, "cy": (py0 + py1) / 2,
            "length": length,
            "source": "raster_reconstructed",
            "confidence": 0.7,
        })
    return out


# ============================================================
# Full Raster Reconstruction Pipeline
# ============================================================

def reconstruct_page(pdf_path: Path, page_idx: int, dpi: float = DEFAULT_DPI
                     ) -> tuple[list[dict], tuple[float, float], RasterTelemetry]:
    """End-to-end: PDF page -> synthetic PRG primitives."""
    t0 = time.time()
    telem = RasterTelemetry(
        pdf_name=pdf_path.name, page_idx=page_idx,
        vector_mode=False, dpi=dpi,
    )
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        page_w_pt = page.rect.width
        page_h_pt = page.rect.height
        img, effective_dpi = render_pdf_page_to_image(doc, page_idx, dpi)
        telem.dpi = effective_dpi

        # Stage 1: normalize
        if img.std() < 8:
            telem.failure_mode = "LOW_CONTRAST"
            telem.elapsed_ms = (time.time() - t0) * 1000
            return [], (page_w_pt, page_h_pt), telem
        binary, skew = normalize_image(img, deskew=True)
        telem.skew_angle_deg = skew

        # Stage 2: line extraction
        hough_lines = extract_lines_hough(binary, effective_dpi)
        contour_lines = extract_lines_contour(binary, effective_dpi)
        all_lines = hough_lines + contour_lines
        telem.line_count = len(all_lines)
        if not all_lines:
            telem.failure_mode = "NO_LINES"
            telem.elapsed_ms = (time.time() - t0) * 1000
            return [], (page_w_pt, page_h_pt), telem

        # Stage 3: merge collinear
        merged = merge_collinear_lines(all_lines)
        telem.lines_after_merge = len(merged)

        # Stage 4: snap endpoints (DPI-aware)
        # snap radius = 0.02 inch = ~4px @ 200dpi -> after conversion to pt = ~1.4pt
        # Choose snap such that after PDF conversion >=CORNER_RADIUS (6pt) ÷ scale
        # scale = 72/dpi -> snap_px = 6 / scale = 6 * dpi / 72
        snap_px = max(3.0, 0.5 * 6.0 * effective_dpi / PDF_NATIVE_DPI)
        snapped = snap_endpoints(merged, snap_radius=snap_px)

        # Cap to prevent graph explosion
        if len(snapped) > 50000:
            # Aggressive size-based pruning
            snapped.sort(key=lambda L: -((L[2] - L[0]) ** 2 + (L[3] - L[1]) ** 2))
            snapped = snapped[:50000]

        # Stage 5: convert to PDF coordinates + PRG schema
        primitives = raster_lines_to_pdf_primitives(
            snapped, effective_dpi, page_w_pt, page_h_pt
        )
        if not primitives:
            telem.failure_mode = "GRAPH_FRAGMENT"
        telem.elapsed_ms = (time.time() - t0) * 1000
        return primitives, (page_w_pt, page_h_pt), telem
    finally:
        doc.close()


# ============================================================
# PRG Compatibility Adapter
# ============================================================

def run_prg_on_synthetic(primitives: list[dict], page_extent: tuple[float, float]):
    """Run PRG against synthetic primitives. Returns baseline + ablation results."""
    from prg_v29 import run_prg  # local import — PRG unchanged
    if not primitives:
        return None, None
    for i, p in enumerate(primitives):
        p["primitive_id"] = i
    baseline = run_prg(primitives, page_extent)
    ablated = run_prg(primitives, page_extent, exclude_edge_frame=True)
    return baseline, ablated


# ============================================================
# Benchmark — Run on r9 rasterized PDFs
# ============================================================

def main():
    print("=== Wave 20.14.5J-V3-PRG-r10 RASTER BRIDGE v1 ===\n", flush=True)
    print("OCR anchors DEFERRED (no PaddleOCR/Tesseract available in env).\n", flush=True)
    print("Tonight's goal: synthetic primitives -> PRG produces non-zero baseline_found\n", flush=True)

    if not RASTER_CACHE.exists() or not list(RASTER_CACHE.glob("*.pdf")):
        print("ERROR: no rasterized cache. Run prg_r9_raster_falsification.py first.", flush=True)
        return

    raster_pdfs = sorted(RASTER_CACHE.glob("raster150_*.pdf"))
    print(f"processing {len(raster_pdfs)} rasterized PDFs from r9 cache\n", flush=True)

    all_telem = []
    all_baseline = 0
    all_pages = 0
    total_primitives = 0
    failure_counts: dict[str, int] = defaultdict(int)

    for pdf in raster_pdfs:
        print(f"=== {pdf.name} ===", flush=True)
        # Get page count
        doc = fitz.open(str(pdf))
        n_pages = doc.page_count
        doc.close()
        # Cap to first 6 pages per PDF to manage runtime tonight
        n_test_pages = min(n_pages, 6)
        for pg in range(n_test_pages):
            primitives, extent, telem = reconstruct_page(pdf, pg, dpi=150)
            baseline, ablated = run_prg_on_synthetic(primitives, extent)
            base_found = baseline is not None
            if base_found:
                telem.cluster_count = baseline.get("n_motifs", 0)
                telem.titleblock_confidence = round(
                    baseline.get("emergence_score", 0.0), 3
                )
                all_baseline += 1
            elif not telem.failure_mode:
                telem.failure_mode = "NO_BASELINE"
            all_pages += 1
            total_primitives += len(primitives)
            if telem.failure_mode:
                failure_counts[telem.failure_mode] += 1
            all_telem.append(asdict(telem))
            print(
                f"  page {pg}: lines={telem.line_count}->{telem.lines_after_merge}  "
                f"prims={len(primitives)}  base_found={base_found}  "
                f"conf={telem.titleblock_confidence:.2f}  "
                f"failure={telem.failure_mode}  {telem.elapsed_ms:.0f}ms",
                flush=True,
            )

    print("\n=== RASTER BRIDGE v1 VERDICT ===")
    print(f"total raster pages processed:    {all_pages}")
    print(f"PRG baseline_found from synthetic: {all_baseline}  ({all_baseline/max(all_pages,1)*100:.1f}%)")
    print(f"total synthetic primitives:        {total_primitives}")
    print(f"avg primitives/page:               {total_primitives/max(all_pages,1):.0f}")
    print(f"\nFAILURE TAXONOMY:")
    for mode, c in sorted(failure_counts.items(), key=lambda x: -x[1]):
        print(f"  {mode:<20s} {c}")
    print(f"\nr9 raster baseline_found:  0.0%  (no primitives at all)")
    print(f"r10 raster baseline_found: {all_baseline/max(all_pages,1)*100:.1f}%  (synthetic primitives)")
    drop_recovery = all_baseline / max(all_pages, 1) * 100
    if drop_recovery > 50:
        print(f"\nVERDICT: Raster Bridge v1 RECOVERS substantial raster baseline (target was >0%).")
    elif drop_recovery > 0:
        print(f"\nVERDICT: Raster Bridge v1 produces primitives that PRG accepts (target was >0% baseline).")
    else:
        print(f"\nVERDICT: Synthetic primitives extracted but PRG produced no baseline. Investigate.")

    out_path = OUT_DIR / "raster_bridge_v1_2026-05-25.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V3-PRG-r10-raster-bridge-v1",
            "total_pages": all_pages,
            "baseline_found": all_baseline,
            "total_primitives": total_primitives,
            "failure_taxonomy": dict(failure_counts),
            "per_page": all_telem,
        }, f, indent=2)
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
