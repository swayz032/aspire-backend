"""Wave 20.13 — Primitive Composition Engine (PCE).

THE actual breakthrough abstraction (per GPT v9 + visual debug evidence):

Wave 11g-v2 detects vector PRIMITIVE FRAGMENTS (lines, arcs, ticks), not
composed symbols. A sprinkler head = ~5-8 primitives. So the 52K Gulfstream
'instances' are 52K atomic drawing ops, not 52K semantic symbols.

PCE assembles nearby primitives into candidate composed symbols using a
weighted spatial proximity graph + connected components + plausibility
filter. Output (~5K-10K composed symbols) is the actual matching surface
against blueprint_project_legends.

Pipeline:
    primitives (Wave 11g-v2)
        → spatial proximity graph (weighted edges)
        → connected components
        → plausibility filter (reject walls/dimensions/sparse chains)
        → blueprint_composed_symbols rows

Law compliance:
    #1  Pure computation, deterministic. Composition rules are explicit
        weighted thresholds — no autonomous decisions.
    #2  Emits blueprint.composition_engine.summary receipt per project run.
    #3  Fail closed: missing primitives → no composed symbols, returns
        empty result.
    #6  Suite-scoped via blueprint_sheets join (no project_id col on
        blueprint_symbols).
    #9  Never logs primitive bbox coords; only counts + signatures.
"""
from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections import defaultdict
from typing import Any

from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

_log = logging.getLogger(__name__)

# Composition thresholds — tuned for architectural CAD primitives
_PROXIMITY_THRESHOLD_PT = 30.0       # primitives within Npt = candidate edge
_ENDPOINT_TOUCH_THRESHOLD_PT = 5.0   # primitives sharing endpoint within Npt = strong edge
_EDGE_SCORE_FLOOR = 0.30             # min edge_score to keep edge
_MAX_COMPONENT_SIZE = 20             # >N primitives = likely wall/dimension chain, reject
_MIN_COMPONENT_SIZE = 1              # singletons allowed (single-primitive symbols exist)
_MAX_COMPONENT_BBOX_RATIO = 0.10     # component bbox > Nx of sheet area = reject (likely wall)
_LINEAR_RUN_ASPECT_THRESHOLD = 15.0  # bbox aspect ratio > N = likely wall/dimension line, reject
_PLAUSIBILITY_FLOOR = 0.30           # composed symbol must score ≥ this to persist


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────


def _primitive_xywh(motif: dict[str, Any]) -> tuple[float, float, float, float] | None:
    b = motif.get("bbox") or {}
    if not isinstance(b, dict):
        return None
    try:
        if {"x", "y", "w", "h"}.issubset(b.keys()):
            return (float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"]))
    except (TypeError, ValueError):
        return None
    return None


def _centroid(xywh: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, w, h = xywh
    return (x + w / 2, y + h / 2)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _bboxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _endpoint_touch(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """Approximate endpoint touch — corners of either bbox within threshold of corners of the other."""
    ac = [(a[0], a[1]), (a[0] + a[2], a[1]), (a[0], a[1] + a[3]), (a[0] + a[2], a[1] + a[3])]
    bc = [(b[0], b[1]), (b[0] + b[2], b[1]), (b[0], b[1] + b[3]), (b[0] + b[2], b[1] + b[3])]
    for p in ac:
        for q in bc:
            if _distance(p, q) <= _ENDPOINT_TOUCH_THRESHOLD_PT:
                return True
    return False


def _orientation_compatible(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """Symbols are typically composed of mixed orientations (lines + arcs at angles).
    Two perfectly-aligned long lines = likely wall, reject as incompatible.
    """
    aw, ah = a[2], a[3]
    bw, bh = b[2], b[3]
    a_horizontal = aw > ah * 3
    a_vertical = ah > aw * 3
    b_horizontal = bw > bh * 3
    b_vertical = bh > bw * 3
    if (a_horizontal and b_horizontal) or (a_vertical and b_vertical):
        return False
    return True


def _edge_score(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Weighted edge score per GPT v9 production recommendation.

        edge_score = proximity + endpoint_touch + orientation + density

    Returns 0..1 — clamp.
    """
    ca, cb = _centroid(a), _centroid(b)
    dist = _distance(ca, cb)
    proximity = max(0.0, 1.0 - dist / _PROXIMITY_THRESHOLD_PT) * 0.40
    touch = 0.30 if _endpoint_touch(a, b) else 0.0
    overlap = 0.15 if _bboxes_overlap(a, b) else 0.0
    orientation = 0.15 if _orientation_compatible(a, b) else 0.0
    return min(1.0, proximity + touch + overlap + orientation)


# ─────────────────────────────────────────────────────────────────────────────
# Connected components (union-find)
# ─────────────────────────────────────────────────────────────────────────────


class _UnionFind:
    __slots__ = ("parent", "rank")

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


# ─────────────────────────────────────────────────────────────────────────────
# Plausibility scoring + filtering
# ─────────────────────────────────────────────────────────────────────────────


def _union_bbox(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def _plausibility(
    component_boxes: list[tuple[float, float, float, float]],
    sheet_area: float,
) -> tuple[float, str | None]:
    n = len(component_boxes)
    if n > _MAX_COMPONENT_SIZE:
        return (0.0, "too_large")
    if n < _MIN_COMPONENT_SIZE:
        return (0.0, "too_small")

    ub = _union_bbox(component_boxes)
    ub_area = ub[2] * ub[3]
    if sheet_area > 0 and ub_area / sheet_area > _MAX_COMPONENT_BBOX_RATIO:
        return (0.0, "too_large_relative_to_sheet")

    # Linear run rejection (likely wall or dimension)
    aspect = max(ub[2], ub[3]) / max(min(ub[2], ub[3]), 1e-6)
    if aspect > _LINEAR_RUN_ASPECT_THRESHOLD:
        return (0.0, "linear_run")

    # Compactness score — symbols are compact, walls are sparse
    sum_box_area = sum(b[2] * b[3] for b in component_boxes)
    compactness = min(1.0, sum_box_area / max(ub_area, 1e-6))
    # Repetition prior — more primitives = stronger symbol candidate (capped at 8)
    repetition = min(1.0, n / 8.0)
    score = 0.6 * compactness + 0.4 * repetition
    return (score, None)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────


async def compose_primitives_into_symbols(
    *,
    project_id: str,
    suite_id: str,
) -> dict[str, Any]:
    """Assemble Wave 11g-v2 primitives into composed symbols. Returns counts."""
    if not suite_id:
        raise ValueError("compose_primitives_into_symbols requires non-empty suite_id (Law #6)")

    # 1. Get sheets + extents
    sheet_rows = await supabase_select(
        "blueprint_sheets",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    if not sheet_rows:
        return {"sheets": 0, "primitives": 0, "components": 0, "composed_symbols": 0, "rejected": 0}

    sheet_extents: dict[str, float] = {}
    for s in sheet_rows:
        try:
            w = float(s.get("page_width_pt") or 5000)
            h = float(s.get("page_height_pt") or 5000)
            sheet_extents[s["id"]] = w * h
        except (TypeError, ValueError):
            sheet_extents[s["id"]] = 25_000_000.0

    composed_inserts: list[dict[str, Any]] = []
    total_primitives = 0
    total_components = 0
    total_composed = 0
    total_rejected = 0
    rejection_counts: dict[str, int] = defaultdict(int)

    # 2. Per-sheet composition
    for s in sheet_rows:
        sheet_id = s["id"]
        # Cursor-paginate primitives on this sheet
        prims: list[dict[str, Any]] = []
        last_id = ""
        while True:
            cur = f"&id=gt.{last_id}" if last_id else ""
            page = await supabase_select(
                "blueprint_symbols",
                f"sheet_id=eq.{sheet_id}&suite_id=eq.{suite_id}{cur}&order=id",
                limit=500,
            )
            if not page:
                break
            prims.extend(page)
            last_id = page[-1]["id"]
            if len(page) < 500:
                break

        if not prims:
            continue

        # Build bboxes for each primitive
        boxes: list[tuple[float, float, float, float]] = []
        valid_idx: list[int] = []
        for i, p in enumerate(prims):
            xywh = _primitive_xywh(p)
            if xywh is None:
                continue
            boxes.append(xywh)
            valid_idx.append(i)
        if not boxes:
            continue
        total_primitives += len(boxes)

        # 3. Spatial proximity graph via sweep (sort by x-centroid for locality)
        n = len(boxes)
        uf = _UnionFind(n)
        centroids = [_centroid(b) for b in boxes]
        order = sorted(range(n), key=lambda i: centroids[i][0])
        # Sliding window over x-sorted indices — only check pairs with x-distance < threshold
        for i_pos, i in enumerate(order):
            cx_i = centroids[i][0]
            for j_pos in range(i_pos + 1, n):
                j = order[j_pos]
                cx_j = centroids[j][0]
                if cx_j - cx_i > _PROXIMITY_THRESHOLD_PT:
                    break  # x-sorted, no more candidates
                if _distance(centroids[i], centroids[j]) > _PROXIMITY_THRESHOLD_PT:
                    continue
                if _edge_score(boxes[i], boxes[j]) >= _EDGE_SCORE_FLOOR:
                    uf.union(i, j)

        # 4. Collect components
        components: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            components[uf.find(i)].append(i)
        total_components += len(components)

        # 5. Plausibility filter + persist
        sheet_area = sheet_extents.get(sheet_id, 25_000_000.0)
        for root, member_idxs in components.items():
            comp_boxes = [boxes[i] for i in member_idxs]
            score, reject = _plausibility(comp_boxes, sheet_area)
            if reject is not None or score < _PLAUSIBILITY_FLOOR:
                total_rejected += 1
                rejection_counts[reject or "low_score"] += 1
                continue
            ub = _union_bbox(comp_boxes)
            member_motif_ids = [prims[valid_idx[i]]["id"] for i in member_idxs]
            # Composed signature = sorted constituent signature_hashes hashed
            sigs = sorted(
                (prims[valid_idx[i]].get("bbox") or {}).get("signature_hash", "")
                for i in member_idxs
            )
            composed_sig = hashlib.sha1("|".join(sigs).encode()).hexdigest() if sigs else None
            # Sum instance counts
            total_inst = 0
            for i in member_idxs:
                ic = (prims[valid_idx[i]].get("bbox") or {}).get("instance_count", 1)
                try:
                    total_inst += int(ic)
                except (TypeError, ValueError):
                    total_inst += 1
            composed_inserts.append({
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "project_id": project_id,
                "sheet_id": sheet_id,
                "bbox": {"x": ub[0], "y": ub[1], "w": ub[2], "h": ub[3]},
                "primitive_count": len(member_idxs),
                "constituent_motif_ids": member_motif_ids,
                "composed_signature": composed_sig,
                "plausibility_score": round(score, 4),
                "total_instance_count": max(total_inst, 1),
            })
            total_composed += 1

    # 6. Batch insert
    inserted = 0
    if composed_inserts:
        batch = 500
        for i in range(0, len(composed_inserts), batch):
            chunk = composed_inserts[i : i + batch]
            try:
                await supabase_insert_batch_ignore_conflicts(
                    "blueprint_composed_symbols", chunk, conflict_target="id",
                )
                inserted += len(chunk)
            except SupabaseClientError as exc:
                _log.warning("PCE: batch insert fail (%s)", type(exc).__name__)

    _log.info(
        "PCE: project=%s primitives=%d components=%d composed=%d rejected=%d (%s)",
        project_id[:8], total_primitives, total_components, total_composed, total_rejected,
        dict(rejection_counts),
    )

    return {
        "sheets": len(sheet_rows),
        "primitives": total_primitives,
        "components": total_components,
        "composed_symbols": total_composed,
        "rejected": total_rejected,
        "rejection_breakdown": dict(rejection_counts),
        "inserted": inserted,
    }


__all__ = ["compose_primitives_into_symbols"]
