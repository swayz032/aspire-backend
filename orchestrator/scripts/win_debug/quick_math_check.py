"""Standalone math-check for L-5 confidence bands.
Bypasses 20-min scipy/pytest discovery overhead on Windows for rapid math validation.
Re-implements only the changed scoring logic — no scipy, no pytest, no supabase imports.
Run: `python scripts/win_debug/quick_math_check.py`"""
import math
import json
from pathlib import Path


# --- Replicate the EXACT logic from the fixed code ---

def inside_zone_score(overlap: float) -> float:
    """Copy of InsideZoneGenerator._score from inside_zone.py (post-fix)."""
    if overlap >= 0.95:
        return 0.95
    if overlap >= 0.75:
        return 0.85
    if overlap >= 0.55:
        return 0.72
    if overlap >= 0.35:
        return 0.55
    return 0.0


def annotated_by_base(distance: float, radius: float) -> float:
    """Copy of base-band logic from AnnotatedByGenerator._score."""
    ratio = distance / max(radius, 1e-6)
    if ratio <= 0.25:
        return 0.92
    if ratio <= 0.50:
        return 0.75
    if ratio <= 0.75:
        return 0.55
    return 0.40


def alignment_factor(src_cx, src_cy, dst_cx, dst_cy, tolerance=8.0):
    dx = abs(src_cx - dst_cx)
    dy = abs(src_cy - dst_cy)
    if dy <= tolerance and dx > tolerance:
        return "horizontal", 1.0
    if dx <= tolerance and dy > tolerance:
        return "vertical", 0.85
    if dx <= tolerance and dy <= tolerance:
        return "horizontal", 1.0
    return "diagonal", 0.5


def bbox_overlap_ratio(inner, outer):
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    dx = max(0.0, min(ix1, ox1) - max(ix0, ox0))
    dy = max(0.0, min(iy1, oy1) - max(iy0, oy0))
    overlap = dx * dy
    inner_area = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    return min(1.0, overlap / inner_area) if inner_area > 0 else 0.0


# --- Replicate the validator confidence-band logic from taxonomy.py ---

ANNOTATED_BY_FLOOR = 0.5
ANNOTATED_BY_AMBIGUITY = 0.3   # default = floor - 0.2

INSIDE_ZONE_FLOOR = 0.7
INSIDE_ZONE_AMBIGUITY = 0.5    # default = floor - 0.2


def resolve(conf: float, floor: float, ambiguity: float) -> str:
    if conf >= floor:
        return "VERIFIED"
    if conf >= ambiguity:
        return "UNKNOWN_REVIEW"
    return "REJECTED"


# --- Test 1: inside_zone partial overlap ---

prim_bbox = [0, 0, 30, 30]
zone_bbox = [10, 10, 30, 30]
overlap = bbox_overlap_ratio(prim_bbox, zone_bbox)
score = inside_zone_score(overlap)
state = resolve(score, INSIDE_ZONE_FLOOR, INSIDE_ZONE_AMBIGUITY)

inside_zone_result = {
    "overlap_ratio": round(overlap, 4),
    "score": score,
    "resolution_state": state,
    "expected": "UNKNOWN_REVIEW",
    "pass": state == "UNKNOWN_REVIEW",
}


# --- Test 2: annotated_by far distance ---

prim_bbox = [100, 100, 102, 102]
span_bbox = [158, 99, 168, 103]
prim_cx = (prim_bbox[0] + prim_bbox[2]) / 2  # 101
prim_cy = (prim_bbox[1] + prim_bbox[3]) / 2  # 101
span_cx = (span_bbox[0] + span_bbox[2]) / 2  # 163
span_cy = (span_bbox[1] + span_bbox[3]) / 2  # 101
distance = math.hypot(span_cx - prim_cx, span_cy - prim_cy)
radius = 80.0
base = annotated_by_base(distance, radius)
align_label, align_f = alignment_factor(prim_cx, prim_cy, span_cx, span_cy)
zone_bonus = 1.0   # no zone in test
confidence = min(1.0, base * align_f * zone_bonus)
state = resolve(confidence, ANNOTATED_BY_FLOOR, ANNOTATED_BY_AMBIGUITY)

annotated_by_result = {
    "distance": round(distance, 2),
    "radius": radius,
    "ratio": round(distance / radius, 4),
    "base": base,
    "alignment": align_label,
    "align_factor": align_f,
    "zone_bonus": zone_bonus,
    "confidence": round(confidence, 4),
    "resolution_state": state,
    "expected": "UNKNOWN_REVIEW",
    "pass": state == "UNKNOWN_REVIEW",
}


# --- Write results ---
out = {
    "test_inside_zone_partial_overlap_routes_to_unknown_review": inside_zone_result,
    "test_annotated_by_far_distance_drops_to_unknown_review": annotated_by_result,
    "both_pass": inside_zone_result["pass"] and annotated_by_result["pass"],
}

print(json.dumps(out, indent=2))
Path("_quick_math_result.json").write_text(json.dumps(out, indent=2))
