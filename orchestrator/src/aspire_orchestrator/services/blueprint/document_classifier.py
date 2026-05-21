"""Wave 11g-v2 Track A — Phase 1: Document Structure Classifier.

Classifies a PyMuPDF page into one of four structural categories so the
vector-graph pipeline knows whether the page is amenable to symbolic
extraction or must fall back to raster discovery.

Categories
----------
VECTOR_NATIVE
    >= 30 distinct drawings AND raster_density < 0.10. The page is built
    out of CAD primitives; the WL graph pipeline owns it end-to-end.
VECTOR_DEGRADED
    Moderate drawing count but stroke-width spread suggests the file was
    re-rasterised and re-vectorised (lossy round-trip). Still usable but
    with reduced confidence.
HYBRID
    >= 10 drawings AND raster_density >= 0.20. Often a vector sheet with
    embedded reference photos / scanned details. Vector extraction is
    still primary; raster path can supplement.
RASTER_SCAN
    < 5 drawings OR vector_density < 0.05. Effectively a scanned page —
    return RASTER_SCAN with confidence proportional to how strong the
    signal is, and never silently fall through (Law #3: fail closed).

Densities are normalised against page area (in pt²) so the classifier
is page-size agnostic.

Provenance (Law #2): every PageClassification carries
``classification_confidence`` so the caller can record it on a receipt.

PII (Law #9): never log drawing coordinates; only counts and densities.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import fitz

logger = logging.getLogger(__name__)


PageMode = Literal["VECTOR_NATIVE", "VECTOR_DEGRADED", "HYBRID", "RASTER_SCAN"]


# Thresholds — chosen to align with Wave 11g classifier so existing
# fixtures keep their expected category, while introducing density-based
# normalisation for page-size independence.
_VECTOR_NATIVE_MIN_DRAWINGS = 30
_VECTOR_NATIVE_MAX_RASTER_DENSITY = 0.10
_HYBRID_MIN_DRAWINGS = 10
_HYBRID_MIN_RASTER_DENSITY = 0.20
_RASTER_SCAN_MAX_DRAWINGS = 5
_RASTER_SCAN_MAX_VECTOR_DENSITY = 0.05

# A density scale factor: drawings_per_pt2 is naturally tiny. We scale to
# something in roughly the 0.0-1.0 range using a saturating divisor that
# represents "this many drawings per pt² is essentially saturated".
_VECTOR_DENSITY_SATURATION = 0.01  # 0.01 drawings/pt² => density ~1.0


@dataclass(frozen=True)
class PageClassification:
    """Structural classification of a single PDF page.

    All densities are in [0.0, 1.0]. ``classification_confidence`` is in
    [0.0, 1.0] and is 0.0 when the page is empty (fail-closed marker).
    """

    page_number: int
    page_mode: PageMode
    vector_density: float
    raster_density: float
    text_density: float
    drawing_count: int
    image_count: int
    classification_confidence: float


def _page_area(page: fitz.Page) -> float:
    """Return page area in pt², clamped to >= 1.0 to avoid div-by-zero."""
    return max(1.0, float(page.rect.width) * float(page.rect.height))


def _raster_density(page: fitz.Page, page_area: float) -> tuple[float, int]:
    """Sum of raster image areas / page_area. Returns (density, count)."""
    images = page.get_images(full=True)
    if not images:
        return 0.0, 0
    raster_area = 0.0
    for img in images:
        try:
            xref = img[0]
            for rect in page.get_image_rects(xref):
                raster_area += max(0.0, rect.width) * max(0.0, rect.height)
        except Exception:  # noqa: BLE001 — image metadata can be malformed
            continue
    return min(1.0, raster_area / page_area), len(images)


def _text_density(page: fitz.Page, page_area: float) -> float:
    """Sum of text block areas / page_area. Used as a tiebreaker only."""
    try:
        blocks = page.get_text("blocks")
    except Exception:  # noqa: BLE001
        return 0.0
    text_area = 0.0
    for block in blocks:
        if len(block) < 4:
            continue
        x0, y0, x1, y1 = float(block[0]), float(block[1]), float(block[2]), float(block[3])
        w = max(0.0, x1 - x0)
        h = max(0.0, y1 - y0)
        text_area += w * h
    return min(1.0, text_area / page_area)


def _vector_density(drawing_count: int, page_area: float) -> float:
    """Normalised drawing density in [0, 1]."""
    per_pt2 = drawing_count / page_area
    return min(1.0, per_pt2 / _VECTOR_DENSITY_SATURATION)


def _decide_mode(
    drawing_count: int,
    vector_density: float,
    raster_density: float,
) -> tuple[PageMode, float]:
    """Return (page_mode, classification_confidence).

    Confidence reflects how far the page sits inside its bucket — a page
    right at a threshold gets ~0.5, deep inside gets ~1.0.
    """
    # Fail-closed: completely empty page.
    if drawing_count == 0 and raster_density == 0.0 and vector_density == 0.0:
        return "RASTER_SCAN", 0.0

    if drawing_count >= _VECTOR_NATIVE_MIN_DRAWINGS and raster_density < _VECTOR_NATIVE_MAX_RASTER_DENSITY:
        depth = min(1.0, drawing_count / (_VECTOR_NATIVE_MIN_DRAWINGS * 4))
        gap = (_VECTOR_NATIVE_MAX_RASTER_DENSITY - raster_density) / _VECTOR_NATIVE_MAX_RASTER_DENSITY
        confidence = max(0.5, 0.5 * depth + 0.5 * max(0.0, gap))
        return "VECTOR_NATIVE", round(min(1.0, confidence), 4)

    if drawing_count >= _HYBRID_MIN_DRAWINGS and raster_density >= _HYBRID_MIN_RASTER_DENSITY:
        # Confidence climbs with how strongly BOTH signals exceed thresholds.
        d_depth = min(1.0, drawing_count / (_HYBRID_MIN_DRAWINGS * 4))
        r_depth = min(1.0, raster_density / 0.6)
        confidence = round(0.5 * d_depth + 0.5 * r_depth, 4)
        return "HYBRID", max(0.5, confidence)

    if drawing_count <= _RASTER_SCAN_MAX_DRAWINGS or vector_density < _RASTER_SCAN_MAX_VECTOR_DENSITY:
        # Strong raster signal => high confidence RASTER_SCAN. Weak signal
        # everywhere => low confidence (still RASTER_SCAN, fail-closed).
        if raster_density >= 0.3:
            confidence = round(min(1.0, 0.5 + raster_density / 2), 4)
        else:
            confidence = round(max(0.2, raster_density + 0.2), 4)
        return "RASTER_SCAN", confidence

    # Everything else => VECTOR_DEGRADED (moderate drawings, no strong
    # raster, but not enough drawings to be native).
    depth = drawing_count / _VECTOR_NATIVE_MIN_DRAWINGS  # 0.0-1.0 by construction
    confidence = round(max(0.3, min(0.8, depth)), 4)
    return "VECTOR_DEGRADED", confidence


def classify_page(page: fitz.Page) -> PageClassification:
    """Classify a single PyMuPDF page.

    Fail-closed: an empty page returns ``RASTER_SCAN`` with
    ``classification_confidence=0.0`` and is logged at WARNING level so
    operators can investigate.
    """
    try:
        drawings = page.get_drawings()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "document_classifier.drawings_failed page=%s err=%s",
            page.number + 1, exc.__class__.__name__,
        )
        drawings = []

    drawing_count = len(drawings)
    page_area = _page_area(page)
    vector_density = _vector_density(drawing_count, page_area)
    raster_density, image_count = _raster_density(page, page_area)
    text_density = _text_density(page, page_area)
    page_mode, confidence = _decide_mode(drawing_count, vector_density, raster_density)

    if confidence == 0.0:
        logger.warning(
            "document_classifier.fail_closed page=%s mode=%s drawings=%s images=%s",
            page.number + 1, page_mode, drawing_count, image_count,
        )
    else:
        logger.debug(
            "document_classifier.ok page=%s mode=%s drawings=%s images=%s "
            "vd=%.3f rd=%.3f td=%.3f conf=%.3f",
            page.number + 1, page_mode, drawing_count, image_count,
            vector_density, raster_density, text_density, confidence,
        )

    return PageClassification(
        page_number=page.number + 1,
        page_mode=page_mode,
        vector_density=round(vector_density, 4),
        raster_density=round(raster_density, 4),
        text_density=round(text_density, 4),
        drawing_count=drawing_count,
        image_count=image_count,
        classification_confidence=confidence,
    )


__all__ = ["PageClassification", "PageMode", "classify_page"]
