"""Wave 11g-v2 Track A — Phase 2 tests: vector_extractor.

Covers:
  * 3 lines + 2 circles → 5 primitives (counts by type)
  * Polyline with 4 segments → polyline type
  * transform_signature is rotation-invariant (rotate 90° → same signature)
  * primitive_id is deterministic (same input → same id)
  * Empty page → empty primitive list
"""
from __future__ import annotations

import fitz

from aspire_orchestrator.services.blueprint.vector_extractor import (
    VectorPrimitive,
    _normalise,
    _primitive_id,
    _rotation_invariant_signature,
    extract_primitives,
)


def _new_page() -> fitz.Page:
    doc = fitz.open()
    return doc.new_page(width=612, height=792)


def test_three_lines_two_circles() -> None:
    page = _new_page()
    # 3 separate lines (separate Shape commits => separate drawings)
    for i in range(3):
        s = page.new_shape()
        s.draw_line(fitz.Point(100 + i * 30, 100), fitz.Point(140 + i * 30, 140))
        s.finish(color=(0, 0, 0), width=0.5)
        s.commit()
    # 2 separate circles
    for i in range(2):
        s = page.new_shape()
        s.draw_circle(fitz.Point(300 + i * 50, 300), 10)
        s.finish(color=(0, 0, 0), width=0.5)
        s.commit()

    primitives = extract_primitives(page)
    assert len(primitives) == 5
    types = sorted(p.type for p in primitives)
    # PyMuPDF circles emit 4-bezier paths, classified as "circle".
    assert types.count("line") == 3
    assert types.count("circle") == 2


def test_polyline_classification() -> None:
    page = _new_page()
    s = page.new_shape()
    s.draw_polyline([
        fitz.Point(100, 100),
        fitz.Point(140, 100),
        fitz.Point(140, 140),
        fitz.Point(180, 140),
        fitz.Point(180, 180),
    ])
    s.finish(color=(0, 0, 0), width=0.5)
    s.commit()

    primitives = extract_primitives(page)
    assert len(primitives) == 1
    # 4-segment polyline → "polyline" (not "line")
    assert primitives[0].type == "polyline"


def test_transform_signature_rotation_invariant() -> None:
    """Rotate a primitive 90° and confirm the transform_signature is stable."""
    # Build a square outline as 4 points on the unit circle, then rotate 90°.
    import math

    pts_orig = [(math.cos(a), math.sin(a)) for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2)]
    pts_rotated = [(-y, x) for x, y in pts_orig]  # rotate 90° CCW

    sig_a = _rotation_invariant_signature(_normalise(pts_orig))
    sig_b = _rotation_invariant_signature(_normalise(pts_rotated))
    assert sig_a == sig_b


def test_transform_signature_differs_for_different_shapes() -> None:
    """Two genuinely different shapes should produce different signatures."""
    pts_square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    pts_triangle = [(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)]
    sig_square = _rotation_invariant_signature(_normalise(pts_square))
    sig_triangle = _rotation_invariant_signature(_normalise(pts_triangle))
    assert sig_square != sig_triangle


def test_primitive_id_deterministic() -> None:
    """Same inputs => same primitive_id."""
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    a = _primitive_id("line", pts)
    b = _primitive_id("line", pts)
    assert a == b
    # Different type => different id even with same points.
    c = _primitive_id("curve", pts)
    assert a != c


def test_empty_page_returns_empty() -> None:
    page = _new_page()
    primitives = extract_primitives(page)
    assert primitives == []


def test_primitive_id_translation_invariant() -> None:
    """Same shape drawn at a different position => same primitive_id."""
    page = _new_page()
    # Two identical L-shapes at different positions.
    s1 = page.new_shape()
    s1.draw_polyline([fitz.Point(100, 100), fitz.Point(140, 100), fitz.Point(140, 140)])
    s1.finish(color=(0, 0, 0), width=0.5)
    s1.commit()

    s2 = page.new_shape()
    s2.draw_polyline([fitz.Point(400, 400), fitz.Point(440, 400), fitz.Point(440, 440)])
    s2.finish(color=(0, 0, 0), width=0.5)
    s2.commit()

    primitives = extract_primitives(page)
    assert len(primitives) == 2
    assert primitives[0].primitive_id == primitives[1].primitive_id


def test_primitive_dataclass_is_frozen() -> None:
    page = _new_page()
    s = page.new_shape()
    s.draw_line(fitz.Point(10, 10), fitz.Point(20, 20))
    s.finish(color=(0, 0, 0), width=0.5)
    s.commit()
    prims = extract_primitives(page)
    assert len(prims) == 1
    p = prims[0]
    assert isinstance(p, VectorPrimitive)
    try:
        p.type = "curve"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("VectorPrimitive must be frozen")


def test_page_number_one_indexed() -> None:
    page = _new_page()
    s = page.new_shape()
    s.draw_line(fitz.Point(10, 10), fitz.Point(20, 20))
    s.finish(color=(0, 0, 0), width=0.5)
    s.commit()
    prims = extract_primitives(page)
    assert prims[0].page_number == 1
