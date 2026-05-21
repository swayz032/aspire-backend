"""Wave 11g-v2 Track A — Phase 1 tests: document_classifier.

Covers:
  * VECTOR_NATIVE: >= 30 drawings, low raster
  * RASTER_SCAN:   no drawings + large embedded image
  * HYBRID:        moderate drawings + significant raster
  * fail-closed:   empty page returns RASTER_SCAN, confidence=0.0
  * smoke:         a real fitz page built with fitz.Shape extracts and
                   classifies without raising.
"""
from __future__ import annotations

from dataclasses import dataclass

import fitz
import pytest

from aspire_orchestrator.services.blueprint.document_classifier import (
    PageClassification,
    classify_page,
)


# ─────────────────────────────────────────────────────────────────────
# Test doubles — minimal stand-in for fitz.Page
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _FakeRect:
    width: float
    height: float


class _FakePage:
    def __init__(
        self,
        *,
        drawings: list[dict] | None = None,
        images: list[tuple] | None = None,
        image_rects: dict[int, list] | None = None,
        text_blocks: list[tuple] | None = None,
        width: float = 612.0,
        height: float = 792.0,
        number: int = 0,
    ) -> None:
        self._drawings = drawings or []
        self._images = images or []
        self._image_rects = image_rects or {}
        self._text_blocks = text_blocks or []
        self.rect = _FakeRect(width, height)
        self.number = number

    def get_drawings(self) -> list[dict]:
        return self._drawings

    def get_images(self, full: bool = False) -> list[tuple]:
        return self._images

    def get_image_rects(self, xref: int) -> list:
        return self._image_rects.get(xref, [])

    def get_text(self, mode: str):
        if mode == "blocks":
            return self._text_blocks
        return ""


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────
def test_vector_native_high_drawing_low_raster() -> None:
    drawings = [{"items": [("l",)]} for _ in range(50)]
    page = _FakePage(drawings=drawings, images=[])
    pc = classify_page(page)  # type: ignore[arg-type]
    assert isinstance(pc, PageClassification)
    assert pc.page_mode == "VECTOR_NATIVE"
    assert pc.drawing_count == 50
    assert pc.image_count == 0
    assert pc.raster_density == 0.0
    assert pc.classification_confidence >= 0.5


def test_raster_scan_no_drawings_large_image() -> None:
    images = [(1, 0, 612, 792, 0, "", "", 0, "", "")]
    image_rects = {1: [fitz.Rect(0, 0, 612, 792)]}
    page = _FakePage(drawings=[], images=images, image_rects=image_rects)
    pc = classify_page(page)  # type: ignore[arg-type]
    assert pc.page_mode == "RASTER_SCAN"
    assert pc.drawing_count == 0
    assert pc.image_count == 1
    assert pc.raster_density == 1.0
    assert pc.classification_confidence > 0.5


def test_hybrid_moderate_drawings_plus_raster() -> None:
    drawings = [{"items": [("l",)]} for _ in range(20)]
    images = [(1, 0, 612, 792, 0, "", "", 0, "", "")]
    image_rects = {1: [fitz.Rect(0, 0, 612, 400)]}  # ~half of letter page
    page = _FakePage(drawings=drawings, images=images, image_rects=image_rects)
    pc = classify_page(page)  # type: ignore[arg-type]
    assert pc.page_mode == "HYBRID"
    assert pc.drawing_count == 20
    assert pc.image_count == 1
    assert pc.raster_density >= 0.2
    assert pc.classification_confidence >= 0.5


def test_empty_page_fails_closed() -> None:
    page = _FakePage(drawings=[], images=[])
    pc = classify_page(page)  # type: ignore[arg-type]
    assert pc.page_mode == "RASTER_SCAN"
    assert pc.classification_confidence == 0.0
    assert pc.drawing_count == 0
    assert pc.image_count == 0


def test_smoke_real_fitz_page_with_shape() -> None:
    """Construct a real PyMuPDF page and ensure classifier doesn't raise.

    We draw ~40 line segments using fitz.Shape, which should give us
    enough vector primitives to land in VECTOR_NATIVE.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    shape = page.new_shape()
    for i in range(40):
        x = 50 + (i % 10) * 50
        y = 50 + (i // 10) * 50
        shape.draw_line(fitz.Point(x, y), fitz.Point(x + 20, y + 20))
    shape.finish(color=(0, 0, 0), width=0.5)
    shape.commit()

    pc = classify_page(page)
    assert pc.page_number == 1
    assert pc.drawing_count >= 1
    # Real shape commits coalesce into fewer drawings, so we don't
    # assert mode — just that the classifier returns a valid record.
    assert pc.page_mode in ("VECTOR_NATIVE", "VECTOR_DEGRADED", "HYBRID", "RASTER_SCAN")
    assert 0.0 <= pc.classification_confidence <= 1.0


def test_page_number_is_one_indexed() -> None:
    page = _FakePage(drawings=[{"items": [("l",)]}] * 50, number=4)
    pc = classify_page(page)  # type: ignore[arg-type]
    assert pc.page_number == 5  # number+1


@pytest.mark.parametrize("count", [0, 1, 4])
def test_low_drawing_count_lands_in_raster_scan(count: int) -> None:
    drawings = [{"items": [("l",)]} for _ in range(count)]
    page = _FakePage(drawings=drawings, images=[])
    pc = classify_page(page)  # type: ignore[arg-type]
    assert pc.page_mode == "RASTER_SCAN"
