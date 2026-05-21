"""Wave 11g-v2 Track A — Phase 2: Vector Primitive Extraction.

Lifts PyMuPDF ``page.get_drawings()`` output into a flat list of canonical
:class:`VectorPrimitive` records that are stable across:

* translation — coordinates are normalised against each primitive's own
  bounding box so two copies of the same outlet on different parts of a
  sheet produce the same ``primitive_id``;
* rotation — ``transform_signature`` encodes the *shape* of the contour
  via radial-distance signature buckets that are invariant under 90/180/
  270 rotations (and approximately invariant under arbitrary rotation
  modulo bucketing noise);
* scale — coordinates are also normalised against the long-side of the
  bbox so a scaled copy hashes identically.

Track B (WL graph hashing) consumes these primitives via
:mod:`vector_graph_builder`. Returned dataclasses are frozen so callers
cannot mutate them between extraction and graphing.

Law #9: never log raw points. Only ``primitive_id[:8]`` + counts.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Literal

import fitz

logger = logging.getLogger(__name__)


PrimitiveType = Literal["line", "polyline", "curve", "circle", "rect", "path"]


# Number of radial buckets in the transform signature. 36 = 10° buckets.
# A primitive rotated 90° round-shifts its signature by 9 slots — the
# canonical signature is the lexicographically smallest rotation, which
# makes it rotation-invariant.
_RADIAL_BUCKETS = 36
# Quantise normalised coords to this resolution for primitive_id hashing.
# Higher = more discriminating, lower = more tolerant of noise.
_QUANT_GRID = 64


@dataclass(frozen=True)
class VectorPrimitive:
    """A single canonical drawing primitive lifted off a PDF page.

    ``primitive_id``: sha1 of the **normalised** geometry. Two copies of
        the same symbol drawn at different positions share a primitive_id.
    ``transform_signature``: rotation-invariant hash of the shape's
        contour — used by Track B for fast WL relabelling.
    ``hash``: sha1 of canonical_repr including stroke_width + fill state
        — primitives that differ only in stroke share a primitive_id but
        get different ``hash`` values.
    """

    primitive_id: str
    type: PrimitiveType
    points: list[tuple[float, float]]
    stroke_width: float
    bbox: tuple[float, float, float, float]
    page_number: int
    transform_signature: str
    fill: bool
    hash: str


def _bbox_of(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _normalise(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Translate to bbox origin and scale to unit long-side."""
    if not points:
        return []
    x0, y0, x1, y1 = _bbox_of(points)
    long_side = max(x1 - x0, y1 - y0, 1e-6)
    return [((px - x0) / long_side, (py - y0) / long_side) for px, py in points]


def _quantise(points: list[tuple[float, float]], grid: int = _QUANT_GRID) -> list[tuple[int, int]]:
    return [(int(px * (grid - 1)), int(py * (grid - 1))) for px, py in points]


def _rotation_invariant_signature(points: list[tuple[float, float]]) -> str:
    """Radial-distance signature, canonicalised by lex-min rotation.

    Build a histogram of (angle, distance) of every point relative to the
    centroid; sort each bucket independently; then pick the rotation of
    the bucket sequence with the smallest hash. This is invariant under
    arbitrary rotation modulo bucket resolution.
    """
    if len(points) < 2:
        return hashlib.sha1(b"degenerate").hexdigest()[:16]

    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    buckets: list[list[int]] = [[] for _ in range(_RADIAL_BUCKETS)]
    for px, py in points:
        dx = px - cx
        dy = py - cy
        dist = math.hypot(dx, dy)
        # Quantise distance to 64 levels (matches _QUANT_GRID).
        d_quant = int(min(63, max(0, round(dist * 63))))
        angle = math.atan2(dy, dx)
        # Map [-pi, pi) -> [0, _RADIAL_BUCKETS)
        idx = int(((angle + math.pi) / (2 * math.pi)) * _RADIAL_BUCKETS) % _RADIAL_BUCKETS
        buckets[idx].append(d_quant)

    # Per-bucket: sort distances so within-bucket point order doesn't matter.
    canon = [tuple(sorted(b)) for b in buckets]

    # Pick the lex-smallest rotation of the bucket ring => rotation-invariant.
    best = canon
    for shift in range(1, _RADIAL_BUCKETS):
        rotated = canon[shift:] + canon[:shift]
        if rotated < best:
            best = rotated

    body = "|".join(",".join(str(d) for d in bucket) for bucket in best)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _primitive_id(ptype: PrimitiveType, normalised_points: list[tuple[float, float]]) -> str:
    qpoints = _quantise(normalised_points)
    body = f"{ptype}|" + ";".join(f"{x},{y}" for x, y in qpoints)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _full_hash(
    primitive_id: str,
    stroke_width: float,
    fill: bool,
    transform_signature: str,
) -> str:
    body = f"{primitive_id}|sw={round(stroke_width, 2)}|fill={int(fill)}|ts={transform_signature}"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def _classify_segment_type(items: list, num_items: int) -> PrimitiveType:
    if num_items == 0:
        return "path"
    ops = [it[0] for it in items if isinstance(it, (list, tuple)) and len(it) > 0]
    if not ops:
        return "path"
    if len(ops) == 1 and ops[0] == "re":
        return "rect"
    if all(o == "l" for o in ops):
        return "line" if len(ops) == 1 else "polyline"
    if all(o == "c" for o in ops):
        return "curve"
    return "path"


def _points_from_items(items: list) -> tuple[list[tuple[float, float]], bool]:
    """Walk PyMuPDF drawing items, returning (points, looks_like_circle).

    A "looks_like_circle" hint is used to upgrade a 4-bezier path into
    type='circle'.
    """
    points: list[tuple[float, float]] = []
    bezier_count = 0
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 1:
            continue
        op = item[0]
        try:
            if op == "l":
                for p in item[1:]:
                    points.append((float(p.x), float(p.y)))
            elif op == "c":
                bezier_count += 1
                for p in item[1:]:
                    points.append((float(p.x), float(p.y)))
            elif op == "re":
                r = item[1]
                points.extend([
                    (float(r.x0), float(r.y0)),
                    (float(r.x1), float(r.y0)),
                    (float(r.x1), float(r.y1)),
                    (float(r.x0), float(r.y1)),
                ])
            elif op == "qu":
                q = item[1]
                points.extend([
                    (float(q.ul.x), float(q.ul.y)),
                    (float(q.ur.x), float(q.ur.y)),
                    (float(q.lr.x), float(q.lr.y)),
                    (float(q.ll.x), float(q.ll.y)),
                ])
        except Exception:  # noqa: BLE001
            continue
    looks_like_circle = bezier_count == 4 and all(
        it[0] == "c" for it in items if isinstance(it, (list, tuple)) and len(it) > 0
    )
    return points, looks_like_circle


def _filtered_rgb(color) -> bool:
    """Whether the drawing has a real (non-None) RGB fill colour."""
    if color is None:
        return False
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        return True
    return False


def extract_primitives(page: fitz.Page) -> list[VectorPrimitive]:
    """Lift every drawing on ``page`` into a list of :class:`VectorPrimitive`.

    Returns an empty list when the page has no drawings — callers that
    need raster fallback should consult :func:`document_classifier.classify_page`
    and decide explicitly (do NOT silently fall through; Law #3).
    """
    try:
        drawings = page.get_drawings()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "vector_extractor.drawings_failed page=%s err=%s",
            page.number + 1, exc.__class__.__name__,
        )
        return []

    primitives: list[VectorPrimitive] = []
    page_number = page.number + 1
    for d in drawings:
        items = d.get("items") or []
        if not items:
            continue
        points, looks_like_circle = _points_from_items(items)
        if not points:
            continue
        stroke_width = float(d.get("width") or 0.0)
        fill = _filtered_rgb(d.get("fill"))
        bbox = _bbox_of(points)
        ptype: PrimitiveType = _classify_segment_type(items, len(items))
        if looks_like_circle:
            ptype = "circle"

        normalised = _normalise(points)
        transform_sig = _rotation_invariant_signature(normalised)
        pid = _primitive_id(ptype, normalised)
        full_hash = _full_hash(pid, stroke_width, fill, transform_sig)

        primitives.append(VectorPrimitive(
            primitive_id=pid,
            type=ptype,
            points=points,
            stroke_width=stroke_width,
            bbox=bbox,
            page_number=page_number,
            transform_signature=transform_sig,
            fill=fill,
            hash=full_hash,
        ))

    logger.debug(
        "vector_extractor.ok page=%s primitives=%s sample_ids=%s",
        page_number, len(primitives),
        [p.primitive_id[:8] for p in primitives[:5]],
    )
    return primitives


__all__ = ["VectorPrimitive", "PrimitiveType", "extract_primitives"]
