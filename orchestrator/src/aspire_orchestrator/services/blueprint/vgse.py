"""Wave 11g/h/i/j — Vector Graph Symbol Extraction (VGSE).

Production module that classifies a blueprint PDF, extracts vector-native
geometric motifs, and emits per-motif detections with deterministic
signature hashes that survive across projects from the same architect.

This is the core of Aspire's symbol-detection moat: NOT a trained model
that classifies pixels, but a structural extractor that names every CAD
symbol by what it geometrically IS.

Pipeline:
  1. classify_page()   → VECTOR_NATIVE | VECTOR_DEGRADED | HYBRID | RASTER_SCAN
  2. For vector pages: extract_motifs() → list of (signature_hash, count, bboxes)
  3. resolve_names()   → lookup each signature in (a) tenant memory, (b) page legend
                          → returns canonical_name or 'unknown_motif_<sig8>'
  4. emit detections into blueprint_symbols with provenance='vgse_*'

Called from drew_blueprint._async_see_pipeline as the PRIMARY detector;
Modal YOLO is fallback for RASTER_SCAN sheets only.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import fitz

logger = logging.getLogger(__name__)


# Heuristic thresholds (matches scripts/vector_classify_fixtures.py)
_PRIMITIVES_PER_PAGE_VECTOR_NATIVE = 500
_PRIMITIVES_PER_PAGE_DEGRADED = 50
_IMAGE_PAGE_AREA_RATIO_HYBRID = 0.20

# Motif filter defaults
DEFAULT_MIN_ITEMS = 3
DEFAULT_MIN_COUNT = 5
DEFAULT_MIN_ENTROPY = 1.5
DEFAULT_MAX_SIZE_PT = 40.0
DEFAULT_MIN_LONG_SIDE_PT = 2.0
DEFAULT_MAX_LONG_SIDE_PT = 80.0
DEFAULT_QUANTIZE_GRID = 12

_LEGEND_KEYWORDS = [
    r"\bLEGEND\b", r"\bSYMBOL\s+LEGEND\b", r"\bSYMBOL\s+LIST\b",
    r"\bSYMBOLS?\b\s+AND\s+ABBREVIATIONS", r"\bFIXTURE\s+SCHEDULE\b",
    r"\bLIGHTING\s+FIXTURE\s+SCHEDULE\b", r"\bELECTRICAL\s+LEGEND\b",
    r"\bPLUMBING\s+LEGEND\b", r"\bABBREVIATIONS?\b", r"\bKEY\s+NOTES?\b",
    r"\bPANEL\s+SCHEDULE\b", r"\bPANELBOARD\s+\w+\s+SCHEDULE\b",
    r"\bFIXTURE\s+TYPE\b", r"\bRECEPTACLE\s+SCHEDULE\b",
    r"\bDEVICE\s+SCHEDULE\b", r"\bEQUIPMENT\s+SCHEDULE\b",
]
_LEGEND_RE = re.compile("|".join(_LEGEND_KEYWORDS), re.IGNORECASE)


@dataclass(frozen=True)
class MotifDetection:
    """One repeated symbol type discovered on a sheet."""
    signature_hash: str
    count: int
    sample_item_count: int
    sample_size_pt: tuple[float, float]
    spatial_entropy: float
    bboxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    canonical_name: str = ""
    name_source: str = "unknown"  # 'tenant_memory' | 'legend' | 'unanchored'


@dataclass
class VGSEPageResult:
    page_number: int
    category: str  # VECTOR_NATIVE | VECTOR_DEGRADED | HYBRID | RASTER_SCAN | UNKNOWN_LOW_CONTENT
    primitive_count: int
    page_size_pt: tuple[float, float]
    motifs: list[MotifDetection] = field(default_factory=list)
    legend_entries: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Stage A: PDF Classification
# ─────────────────────────────────────────────────────────────────────

def classify_page(page: fitz.Page) -> dict:
    """Classify a page by structural content. See vector_classify_fixtures.py."""
    drawings = page.get_drawings()
    primitive_count = sum(len(d.get("items", [])) for d in drawings)

    images = page.get_images(full=True)
    page_w, page_h = page.rect.width, page.rect.height
    page_area = max(1.0, page_w * page_h)
    raster_area = 0.0
    for img_info in images:
        try:
            xref = img_info[0]
            rects = page.get_image_rects(xref)
            for r in rects:
                raster_area += max(0.0, r.width) * max(0.0, r.height)
        except Exception:  # noqa: BLE001
            continue
    raster_ratio = min(1.0, raster_area / page_area)

    if primitive_count >= _PRIMITIVES_PER_PAGE_VECTOR_NATIVE and raster_ratio < _IMAGE_PAGE_AREA_RATIO_HYBRID:
        category = "VECTOR_NATIVE"
    elif primitive_count >= _PRIMITIVES_PER_PAGE_VECTOR_NATIVE:
        category = "HYBRID"
    elif _PRIMITIVES_PER_PAGE_DEGRADED <= primitive_count < _PRIMITIVES_PER_PAGE_VECTOR_NATIVE:
        category = "VECTOR_DEGRADED"
    elif primitive_count < _PRIMITIVES_PER_PAGE_DEGRADED and len(images) > 0:
        category = "RASTER_SCAN"
    else:
        category = "UNKNOWN_LOW_CONTENT"
    return {
        "category": category,
        "primitive_count": primitive_count,
        "page_size_pt": (page_w, page_h),
        "image_count": len(images),
        "raster_coverage_pct": round(raster_ratio * 100, 1),
    }


# ─────────────────────────────────────────────────────────────────────
# Stage B+C+D: Motif extraction
# ─────────────────────────────────────────────────────────────────────

def _drawing_bbox(items) -> tuple[float, float, float, float] | None:
    xs, ys = [], []
    for item in items:
        op = item[0]
        try:
            if op == "l":
                for p in item[1:]:
                    xs.append(p.x); ys.append(p.y)
            elif op == "c":
                for p in item[1:]:
                    xs.append(p.x); ys.append(p.y)
            elif op == "re":
                r = item[1]
                xs.extend([r.x0, r.x1]); ys.extend([r.y0, r.y1])
            elif op == "qu":
                q = item[1]
                xs.extend([q.ul.x, q.ur.x, q.ll.x, q.lr.x])
                ys.extend([q.ul.y, q.ur.y, q.ll.y, q.lr.y])
        except Exception:  # noqa: BLE001
            continue
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _drawing_signature(items, bbox: tuple[float, float, float, float], grid: int = DEFAULT_QUANTIZE_GRID) -> str:
    x0, y0, x1, y1 = bbox
    w = max(1e-6, x1 - x0)
    h = max(1e-6, y1 - y0)

    def q(p) -> str:
        return f"{int(((p.x - x0) / w) * (grid - 1))},{int(((p.y - y0) / h) * (grid - 1))}"

    def qr(r) -> str:
        return (f"{int(((r.x0 - x0) / w) * (grid - 1))},{int(((r.y0 - y0) / h) * (grid - 1))}-"
                f"{int(((r.x1 - x0) / w) * (grid - 1))},{int(((r.y1 - y0) / h) * (grid - 1))}")

    parts: list[str] = []
    for item in items:
        op = item[0]
        try:
            if op == "l":
                parts.append(f"l:{q(item[1])}>{q(item[2])}")
            elif op == "c":
                parts.append(f"c:{q(item[1])}>{q(item[2])}>{q(item[3])}>{q(item[4])}")
            elif op == "re":
                parts.append(f"re:{qr(item[1])}")
            elif op == "qu":
                q1 = item[1]
                parts.append(
                    f"qu:{int(((q1.ul.x - x0) / w) * (grid - 1))},{int(((q1.ul.y - y0) / h) * (grid - 1))}-"
                    f"{int(((q1.lr.x - x0) / w) * (grid - 1))},{int(((q1.lr.y - y0) / h) * (grid - 1))}"
                )
            else:
                parts.append(op)
        except Exception:  # noqa: BLE001
            parts.append("?")

    aspect = w / max(1e-6, h)
    ab = "tall" if aspect < 0.5 else "square" if aspect < 1.5 else "wide" if aspect < 3.0 else "veryWide"
    body = f"{ab}|" + "|".join(parts)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _spatial_entropy(bboxes: list[tuple[float, float, float, float]],
                     page_size: tuple[float, float], grid: int = 8) -> float:
    if not bboxes:
        return 0.0
    page_w, page_h = page_size
    cells: dict[tuple[int, int], int] = {}
    for x0, y0, x1, y1 in bboxes:
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        gx = min(grid - 1, max(0, int(cx / max(1e-6, page_w) * grid)))
        gy = min(grid - 1, max(0, int(cy / max(1e-6, page_h) * grid)))
        cells[(gx, gy)] = cells.get((gx, gy), 0) + 1
    total = sum(cells.values())
    if total == 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in cells.values() if c > 0)


def extract_motifs(page: fitz.Page,
                  min_items: int = DEFAULT_MIN_ITEMS,
                  min_count: int = DEFAULT_MIN_COUNT,
                  min_entropy: float = DEFAULT_MIN_ENTROPY,
                  max_size_pt: float = DEFAULT_MAX_SIZE_PT) -> list[MotifDetection]:
    """Extract real-symbol motifs from a vector PDF page."""
    drawings = page.get_drawings()
    page_w, page_h = page.rect.width, page.rect.height
    sig_to_instances: dict[str, list[dict]] = defaultdict(list)

    for d in drawings:
        items = d.get("items", [])
        if not items:
            continue
        bbox = _drawing_bbox(items)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        w, h = x1 - x0, y1 - y0
        long_side = max(w, h)
        if long_side < DEFAULT_MIN_LONG_SIDE_PT or long_side > DEFAULT_MAX_LONG_SIDE_PT:
            continue
        sig = _drawing_signature(items, bbox)
        sig_to_instances[sig].append({
            "bbox": bbox,
            "item_count": len(items),
            "size_pt": (round(w, 2), round(h, 2)),
        })

    motifs: list[MotifDetection] = []
    for sig, instances in sig_to_instances.items():
        if len(instances) < min_count:
            continue
        first = instances[0]
        if first["item_count"] < min_items:
            continue
        if max(first["size_pt"]) > max_size_pt:
            continue
        bboxes = [i["bbox"] for i in instances]
        entropy = _spatial_entropy(bboxes, (page_w, page_h))
        if entropy < min_entropy:
            continue
        motifs.append(MotifDetection(
            signature_hash=sig,
            count=len(instances),
            sample_item_count=first["item_count"],
            sample_size_pt=first["size_pt"],
            spatial_entropy=round(entropy, 2),
            bboxes=bboxes,
        ))
    motifs.sort(key=lambda m: -m.count)
    return motifs


# ─────────────────────────────────────────────────────────────────────
# Stage 11h: Legend extraction (best-effort)
# ─────────────────────────────────────────────────────────────────────

def find_legend_regions(page: fitz.Page) -> list[fitz.Rect]:
    page_rect = page.rect
    page_w, page_h = page_rect.width, page_rect.height
    text_blocks = page.get_text("blocks")
    regions: list[fitz.Rect] = []
    for block in text_blocks:
        if len(block) < 5:
            continue
        x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        if not isinstance(text, str) or not _LEGEND_RE.search(text):
            continue
        margin_x = 60.0
        expand_down = min(page_h * 0.45, 360.0)
        regions.append(fitz.Rect(
            max(0.0, x0 - margin_x), max(0.0, y0 - 8.0),
            min(page_w, x1 + margin_x), min(page_h, y1 + expand_down),
        ))
    merged: list[fitz.Rect] = []
    for r in regions:
        joined = False
        for i, m in enumerate(merged):
            if not (r & m).is_empty:
                merged[i] = fitz.Rect(m | r); joined = True; break
        if not joined:
            merged.append(r)
    return merged


def extract_legend_signatures(page: fitz.Page) -> dict[str, str]:
    """Return {signature_hash: label} for symbols inside legend regions.

    Best-effort. Returns empty dict when no clean matches found. The production
    pipeline gracefully falls back to tenant memory + 'unknown_motif' naming.
    """
    regions = find_legend_regions(page)
    if not regions:
        return {}

    raw_text = page.get_text("dict")
    text_spans: list[dict] = []
    for block in raw_text.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = (span.get("text") or "").strip()
                if len(t) < 2:
                    continue
                bb = span.get("bbox")
                if not bb or len(bb) < 4:
                    continue
                text_spans.append({"text": t, "bbox": bb})

    sig_to_label: dict[str, str] = {}
    for d in page.get_drawings():
        items = d.get("items", [])
        if not items:
            continue
        bbox = _drawing_bbox(items)
        if bbox is None:
            continue
        # In legend region?
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if not any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in regions):
            continue
        sig = _drawing_signature(items, bbox)
        # Find nearest non-heading text span to the right
        drawing_cy = (bbox[1] + bbox[3]) / 2
        best: tuple[float, str] | None = None
        for span in text_spans:
            sx0, sy0, sx1, sy1 = span["bbox"]
            if _LEGEND_RE.search(span["text"]):
                continue
            if sx0 < bbox[0]:
                continue
            sy_mid = (sy0 + sy1) / 2
            if abs(sy_mid - drawing_cy) > 12.0:
                continue
            dx = sx0 - bbox[2]
            if dx < -2.0 or dx > 120.0:
                continue
            score = dx + abs(sy_mid - drawing_cy) * 0.5
            if best is None or score < best[0]:
                best = (score, span["text"])
        if best and sig not in sig_to_label:
            sig_to_label[sig] = best[1]
    return sig_to_label


# ─────────────────────────────────────────────────────────────────────
# Page-level entry point
# ─────────────────────────────────────────────────────────────────────

def process_page(page: fitz.Page) -> VGSEPageResult:
    """One-call extraction: classify, mine motifs, attempt legend matching."""
    classification = classify_page(page)
    result = VGSEPageResult(
        page_number=page.number + 1,
        category=classification["category"],
        primitive_count=classification["primitive_count"],
        page_size_pt=classification["page_size_pt"],
    )
    if classification["category"] in ("RASTER_SCAN", "UNKNOWN_LOW_CONTENT"):
        return result  # caller falls back to Modal YOLO

    motifs = extract_motifs(page)
    legend = extract_legend_signatures(page)
    result.legend_entries = [{"signature_hash": s, "label": l} for s, l in legend.items()]

    # Resolve names against page legend (tenant memory lookup happens upstream)
    enriched: list[MotifDetection] = []
    for m in motifs:
        if m.signature_hash in legend:
            enriched.append(MotifDetection(
                signature_hash=m.signature_hash,
                count=m.count,
                sample_item_count=m.sample_item_count,
                sample_size_pt=m.sample_size_pt,
                spatial_entropy=m.spatial_entropy,
                bboxes=m.bboxes,
                canonical_name=legend[m.signature_hash],
                name_source="legend",
            ))
        else:
            enriched.append(MotifDetection(
                signature_hash=m.signature_hash,
                count=m.count,
                sample_item_count=m.sample_item_count,
                sample_size_pt=m.sample_size_pt,
                spatial_entropy=m.spatial_entropy,
                bboxes=m.bboxes,
                canonical_name=f"unknown_motif_{m.signature_hash[:8]}",
                name_source="unanchored",
            ))
    result.motifs = enriched
    return result
