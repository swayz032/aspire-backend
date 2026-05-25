"""Phase 1.2 — Semantic Pruner v2.

Architectural shift:
  v1 (zone-limited):   OCR finds zones -> extract ONLY in zones -> ~124 prims/page (too few)
  v2 (Azure-affinity): Extract full-page primitives -> score by Azure-OCR-token-proximity ->
                       keep top-K per spatial bucket -> 500-1500 prims/page

Score = sum of:
  + base proximity to ANY OCR token (weighted by token confidence)
  + bonus for proximity to POSITIVE keyword tokens (SHEET, REV, PROJECT, sheet IDs)
  + bonus for table-cell-sized orthogonal lines
  - penalty for proximity to DIMENSION chains (X'-Y")
  - penalty for proximity to SCHEDULE/EQUIPMENT/PANEL tokens
  - penalty for long isolated wall-like lines
  - penalty for page-border-grazing lines

Then: spatial-bucket top-K (uniform coverage, not just dense regions).
PRG remains untouched. Azure DI remains the OCR engine.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

# ============================================================
# Token classification (Azure spans -> typed sets)
# ============================================================

POSITIVE_TOKENS = {
    "SHEET", "DRAWN", "PROJECT", "REVISION", "REV", "SCALE", "DATE",
    "CHECKED", "DESIGNED", "APPROVED", "DRAWING", "DWG", "TITLE",
    "ARCHITECT", "ENGINEER", "CLIENT", "OWNER", "DESIGNER",
    "FIRM", "ADDRESS", "JOB", "PHASE",
}

NEGATIVE_TOKENS = {
    "EQUIPMENT", "SCHEDULE", "DOOR", "WINDOW", "PANEL", "KEYNOTE",
    "FIXTURE", "ROOM", "FINISH", "MATERIAL", "TYPICAL", "TYP",
    "DETAIL", "SECTION", "ELEVATION",
}

SHEET_ID_RE = re.compile(r"\b[A-Z]{1,2}[-\.]?\d{1,3}(?:\.\d{1,2})?\b")
DIMENSION_RE = re.compile(r"\b\d{1,3}['\-]\d{1,3}\"?\b|\b\d{1,3}\.\d{1,2}'\b")


@dataclass
class TokenIndex:
    """Spatial index over Azure OCR tokens, partitioned by semantic class."""
    positive_spans: list[tuple[int, int, int, int, float]]  # (cx, cy, w, h, conf)
    negative_spans: list[tuple[int, int, int, int, float]]
    sheet_id_spans: list[tuple[int, int, int, int, float, str]]
    dimension_spans: list[tuple[int, int, int, int, float]]
    all_spans: list[tuple[int, int, int, int, float]]


def classify_ocr_spans(ocr_spans) -> TokenIndex:
    pos, neg, sid, dim, all_ = [], [], [], [], []
    for s in ocr_spans:
        x0, y0, x1, y1 = s.bbox_px
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        w = max(1, x1 - x0)
        h = max(1, y1 - y0)
        conf = s.confidence
        text = (s.text or "").upper().strip()
        all_.append((cx, cy, w, h, conf))
        # Classification (mutually-exclusive priority order)
        if DIMENSION_RE.search(text):
            dim.append((cx, cy, w, h, conf))
        elif SHEET_ID_RE.search(text):
            sid_match = SHEET_ID_RE.search(text)
            sid.append((cx, cy, w, h, conf, sid_match.group(0)))
        elif any(t in text for t in POSITIVE_TOKENS):
            pos.append((cx, cy, w, h, conf))
        elif any(t in text for t in NEGATIVE_TOKENS):
            neg.append((cx, cy, w, h, conf))
    return TokenIndex(pos, neg, sid, dim, all_)


# ============================================================
# Spatial grid index for fast proximity queries
# ============================================================

class SpatialGrid:
    """Bucket tokens by 100px cells for O(1) proximity lookups."""
    def __init__(self, items: list, cell_size: int = 100):
        self.cell_size = cell_size
        self.buckets: dict[tuple[int, int], list] = defaultdict(list)
        for item in items:
            cx, cy = item[0], item[1]
            self.buckets[(cx // cell_size, cy // cell_size)].append(item)

    def nearest_dist(self, cx_px: float, cy_px: float, max_radius_px: int = 200
                       ) -> tuple[float, float]:
        """Return (distance_to_nearest, confidence_of_nearest). +inf if nothing within radius."""
        cell_radius = (max_radius_px // self.cell_size) + 1
        gx0, gy0 = int(cx_px // self.cell_size), int(cy_px // self.cell_size)
        best_d = float("inf")
        best_conf = 0.0
        for dx in range(-cell_radius, cell_radius + 1):
            for dy in range(-cell_radius, cell_radius + 1):
                for item in self.buckets.get((gx0 + dx, gy0 + dy), []):
                    ix, iy, _, _, conf = item[:5]
                    d = math.hypot(cx_px - ix, cy_px - iy)
                    if d < best_d:
                        best_d = d
                        best_conf = conf
        return best_d, best_conf


# ============================================================
# Primitive scorer (the actual Phase 1.2 unlock)
# ============================================================

def score_primitive(
    p_px_x: float, p_px_y: float, length_px: float,
    is_orthogonal: bool,
    near_page_edge: bool,
    pos_grid: SpatialGrid,
    neg_grid: SpatialGrid,
    sid_grid: SpatialGrid,
    dim_grid: SpatialGrid,
    all_grid: SpatialGrid,
) -> float:
    score = 0.0

    # Base: proximity to ANY OCR token (text-ness signal)
    d_any, conf_any = all_grid.nearest_dist(p_px_x, p_px_y, max_radius_px=150)
    if d_any < 20:
        score += 1.0 * conf_any
    elif d_any < 50:
        score += 0.6 * conf_any
    elif d_any < 100:
        score += 0.2 * conf_any

    # Bonus: proximity to POSITIVE title-block tokens
    d_pos, conf_pos = pos_grid.nearest_dist(p_px_x, p_px_y, max_radius_px=120)
    if d_pos < 30:
        score += 2.5 * conf_pos
    elif d_pos < 80:
        score += 1.0 * conf_pos

    # Bonus: proximity to SHEET ID pattern (highest signal)
    d_sid, conf_sid = sid_grid.nearest_dist(p_px_x, p_px_y, max_radius_px=150)
    if d_sid < 40:
        score += 3.0 * conf_sid
    elif d_sid < 100:
        score += 1.5 * conf_sid

    # Penalty: proximity to DIMENSION chains
    d_dim, conf_dim = dim_grid.nearest_dist(p_px_x, p_px_y, max_radius_px=80)
    if d_dim < 25:
        score -= 2.0 * conf_dim
    elif d_dim < 50:
        score -= 1.0 * conf_dim

    # Penalty: proximity to SCHEDULE/EQUIPMENT/PANEL tokens
    d_neg, conf_neg = neg_grid.nearest_dist(p_px_x, p_px_y, max_radius_px=100)
    if d_neg < 40:
        score -= 1.0 * conf_neg
    elif d_neg < 80:
        score -= 0.5 * conf_neg

    # Geometric modulators
    if 8 < length_px < 80 and is_orthogonal:
        score += 0.6  # table-cell-sized orthogonal line
    elif length_px > 250:
        score -= 0.4  # likely wall / drawing geometry
    elif length_px < 5:
        score -= 1.0  # tiny fragment, likely noise

    if near_page_edge and length_px > 100:
        score -= 0.8  # long line near page edge = border, not title block

    return score


# ============================================================
# Spatial-bucket top-K pruner (uniform coverage)
# ============================================================

def prune_to_target(
    scored_primitives: list[tuple[int, float, float, float]],  # (idx, cx_px, cy_px, score)
    target_count: int = 1000,
    bucket_size_px: int = 200,
) -> list[int]:
    """Keep top-K per spatial bucket so coverage is uniform across the page.

    Returns kept primitive indices."""
    if len(scored_primitives) <= target_count:
        return [t[0] for t in scored_primitives]

    buckets: dict[tuple[int, int], list[tuple[float, int]]] = defaultdict(list)
    for idx, cx, cy, score in scored_primitives:
        bx = int(cx // bucket_size_px)
        by = int(cy // bucket_size_px)
        buckets[(bx, by)].append((score, idx))

    n_buckets = max(len(buckets), 1)
    per_bucket = max(8, target_count // n_buckets)

    kept = []
    for key, items in buckets.items():
        items.sort(reverse=True)  # high score first
        for score, idx in items[:per_bucket]:
            kept.append((score, idx))

    # If still over target, drop globally lowest
    if len(kept) > target_count:
        kept.sort(reverse=True)
        kept = kept[:target_count]

    kept_idx = [idx for _, idx in kept]
    kept_idx.sort()  # preserve relative order
    return kept_idx


# ============================================================
# Top-level: score-and-prune
# ============================================================

def semantic_prune(
    primitives: list[dict],  # PRG schema, with px coords on the rendered image
    ocr_spans,  # list[OCRSpan]
    pw_px: int, ph_px: int,
    target_count: int = 1000,
    edge_margin_px: int = 40,
) -> tuple[list[dict], dict]:
    """Score every primitive by Azure-affinity, keep top-K per spatial bucket.
    Returns (kept_primitives, stats_dict)."""
    if not primitives:
        return [], {"original": 0, "kept": 0, "method": "empty"}

    tokens = classify_ocr_spans(ocr_spans)
    pos_grid = SpatialGrid(tokens.positive_spans)
    neg_grid = SpatialGrid(tokens.negative_spans)
    sid_grid = SpatialGrid(tokens.sheet_id_spans)
    dim_grid = SpatialGrid(tokens.dimension_spans)
    all_grid = SpatialGrid(tokens.all_spans)

    scored = []
    for i, p in enumerate(primitives):
        try:
            cx_px = float(p.get("cx_px", p["cx"]))
            cy_px = float(p.get("cy_px", p["cy"]))
            length_px = float(p.get("length_px", p.get("length", 0)))
            angle = float(p.get("angle", 0))
            is_ortho = abs(angle % 90) < 5 or abs(angle % 90 - 90) < 5
        except (KeyError, TypeError, ValueError):
            continue
        near_edge = (
            cx_px < edge_margin_px or cy_px < edge_margin_px
            or cx_px > pw_px - edge_margin_px
            or cy_px > ph_px - edge_margin_px
        )
        score = score_primitive(
            cx_px, cy_px, length_px, is_ortho, near_edge,
            pos_grid, neg_grid, sid_grid, dim_grid, all_grid,
        )
        scored.append((i, cx_px, cy_px, score))

    if not scored:
        return [], {"original": len(primitives), "kept": 0, "method": "no_valid"}

    kept_indices = prune_to_target(scored, target_count=target_count)
    kept = [primitives[i] for i in kept_indices]

    score_values = [s[3] for s in scored]
    return kept, {
        "original": len(primitives),
        "kept": len(kept),
        "method": "azure_affinity_spatial_topk",
        "target": target_count,
        "n_pos_tokens": len(tokens.positive_spans),
        "n_neg_tokens": len(tokens.negative_spans),
        "n_sheet_id_tokens": len(tokens.sheet_id_spans),
        "n_dimension_tokens": len(tokens.dimension_spans),
        "score_min": round(min(score_values), 2),
        "score_max": round(max(score_values), 2),
        "score_mean": round(sum(score_values) / len(score_values), 2),
        "score_median": round(sorted(score_values)[len(score_values) // 2], 2),
    }
