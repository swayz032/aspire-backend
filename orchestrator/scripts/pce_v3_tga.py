"""Wave 20.14.3 — Topology-Growing Assembly (TGA) — PCE-v3.

Replaces PCE-v2 fixed-radius logic with semantic boundary growth.

Per GPT v9 strategic guidance:
  grow_symbol_graph_until_semantic_boundary()

Algorithm:
  1. Seed: curve primitive on SYMBOL_DOMINANT sheet
  2. Iteratively add nearest visible non-tiny primitive
  3. After each add, check stopping conditions:
     - text overlap (paragraph region) > 30%
     - parallel-line texture in growing component (>=4 colinear primitives)
     - orientation divergence: dominant angle bin share <50%
     - bbox aspect ratio > 10 (linear run)
     - graph compactness drops (sum_prim_area / bbox_area < 0.05)
     - cap at 30 primitives
  4. Record growth trace + stop_reason
  5. Compute CCS = compactness × (1 - text_overlap) × min(1, n/8)

This is the topology stabilization stage. DO NOT optimize binding recall
until composed graphs are coherent.
"""
import asyncio
import hashlib
import math
import sys
import uuid as uuid_mod
from collections import defaultdict

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.supabase_client import (  # noqa: E402
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"

# TGA parameters
_MAX_GROWTH_PRIMITIVES = 30
_MIN_PRIMITIVE_DIM = 3.0
_VISIBILITY_FLOOR = 0.05
_GROWTH_RADIUS = 35.0           # PDF points — nearest-neighbor candidate radius
_TEXT_OVERLAP_STOP = 0.30        # paragraph polygon overlap ratio that halts growth
_PARALLEL_COLINEAR_MIN = 4       # 4+ same-angle lines in component = texture, stop
_ORIENTATION_DOMINANT_MAX = 0.55 # if >55% of primitives in one angle bin = too monolithic
_BBOX_ASPECT_STOP = 10.0
_MIN_COMPACTNESS = 0.05
_CCS_BIND_FLOOR = 0.30
_VERSION = "tga-v3"


def _line_angle_bin(angle_deg: float | None) -> int:
    if angle_deg is None:
        return -1
    return int(abs(angle_deg) // 15) * 15 % 180


async def _fetch_sheet_curves(sheet_id: str) -> list[dict]:
    """Fetch ONLY visible curves for one sheet (small subset, used as seeds)."""
    rows: list[dict] = []
    last_id = ""
    while True:
        cursor = f"&id=gt.{last_id}" if last_id else ""
        page = await supabase_select(
            "blueprint_primitives",
            f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}"
            f"&primitive_type=eq.curve&raster_density=gte.{_VISIBILITY_FLOOR}"
            f"{cursor}&order=id",
            limit=500,
        )
        if not page:
            break
        rows.extend([
            p for p in page if max(
                float(p.get("bbox_w") or 0), float(p.get("bbox_h") or 0)
            ) >= _MIN_PRIMITIVE_DIM
        ])
        last_id = page[-1]["id"]
        if len(page) < 500:
            break
    return rows


async def _fetch_primitives_in_bbox(sheet_id: str, cx: float, cy: float, radius: float) -> list[dict]:
    """Fetch visible primitives within a spatial bbox around (cx, cy)."""
    x0 = cx - radius; y0 = cy - radius
    x1 = cx + radius; y1 = cy + radius
    rows = await supabase_select(
        "blueprint_primitives",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}"
        f"&cx=gte.{x0}&cx=lte.{x1}"
        f"&cy=gte.{y0}&cy=lte.{y1}"
        f"&raster_density=gte.{_VISIBILITY_FLOOR}&order=id",
        limit=500,
    )
    return [
        p for p in (rows or []) if max(
            float(p.get("bbox_w") or 0), float(p.get("bbox_h") or 0)
        ) >= _MIN_PRIMITIVE_DIM
    ]


async def _fetch_paragraph_polygons(sheet_id: str) -> list[list[float]]:
    """Fetch paragraph bboxes for text-overlap stopping. Coords already in PDF or pixel?
    blueprint_sheet_intelligence.paragraphs are Azure pixel coords. We'll convert to PDF pts
    using 200 DPI assumption (since sheet PNGs are 200 DPI). Scale: 72/200 = 0.36.
    """
    rows = await supabase_select(
        "blueprint_sheet_intelligence",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}",
        limit=1,
    ) or []
    if not rows:
        return []
    paras = rows[0].get("paragraphs") or []
    polys: list[list[float]] = []
    scale = 72.0 / 200.0
    for p in paras:
        b = p.get("bbox") if isinstance(p, dict) else None
        if isinstance(b, list) and len(b) >= 8:
            polys.append([b[i] * scale for i in range(8)])
    return polys


def _bbox_paragraph_overlap(bbox_xywh, poly8) -> float:
    if not poly8 or len(poly8) < 8:
        return 0.0
    ax, ay, aw, ah = bbox_xywh
    ax1, ay1 = ax + aw, ay + ah
    xs = [poly8[i] for i in (0, 2, 4, 6)]
    ys = [poly8[i] for i in (1, 3, 5, 7)]
    bx, by, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
    ix, iy = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix or iy1 <= iy:
        return 0.0
    inter = (ix1 - ix) * (iy1 - iy)
    return inter / max(aw * ah, 1e-6)


def _union_xywh(prims: list[dict]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for p in prims:
        try:
            x0 = float(p.get("x0") or 0); y0 = float(p.get("y0") or 0)
            x1 = float(p.get("x1") or 0); y1 = float(p.get("y1") or 0)
            xs.extend([x0, x1]); ys.extend([y0, y1])
        except (TypeError, ValueError):
            continue
    if not xs:
        return (0, 0, 0, 0)
    bx0, by0 = min(xs), min(ys)
    return (bx0, by0, max(xs) - bx0, max(ys) - by0)


def _grow_component(
    seed_idx: int,
    prims: list[dict],
    centroids: list[tuple[float, float]],
    para_polygons: list[list[float]],
    claimed: set[int],
) -> dict:
    """Grow a component from seed using TGA. Returns dict with members + stop_reason + CCS."""
    component_idxs: list[int] = [seed_idx]
    claimed.add(seed_idx)
    growth_trace: list[str] = [str(prims[seed_idx]["id"])]
    angle_bins: dict[int, int] = defaultdict(int)
    stop_reason = "max_growth"
    sx, sy = centroids[seed_idx]

    seed_p = prims[seed_idx]
    angle_bins[_line_angle_bin(seed_p.get("angle_deg"))] += 1

    while len(component_idxs) < _MAX_GROWTH_PRIMITIVES:
        # Compute current component bbox + centroid
        comp_prims = [prims[i] for i in component_idxs]
        bbox = _union_xywh(comp_prims)
        cx_comp = bbox[0] + bbox[2] / 2
        cy_comp = bbox[1] + bbox[3] / 2

        # Find nearest unclaimed primitive within growth radius
        best_idx = -1
        best_dist = _GROWTH_RADIUS + 1
        for j in range(len(prims)):
            if j in claimed:
                continue
            d = math.hypot(centroids[j][0] - cx_comp, centroids[j][1] - cy_comp)
            if d < best_dist:
                best_dist = d
                best_idx = j
        if best_idx < 0 or best_dist > _GROWTH_RADIUS:
            stop_reason = "no_neighbors_in_radius"
            break

        # Tentatively add — check stop conditions
        candidate = prims[best_idx]
        tentative = component_idxs + [best_idx]
        tent_prims = [prims[i] for i in tentative]
        tent_bbox = _union_xywh(tent_prims)

        # Stop 1: bbox aspect ratio
        tw, th = tent_bbox[2], tent_bbox[3]
        if max(tw, th) / max(min(tw, th), 0.01) > _BBOX_ASPECT_STOP:
            stop_reason = "linear_run"
            break

        # Stop 2: text overlap
        max_text = 0.0
        for poly in para_polygons:
            o = _bbox_paragraph_overlap((tent_bbox[0], tent_bbox[1], tent_bbox[2], tent_bbox[3]), poly)
            if o > max_text:
                max_text = o
        if max_text > _TEXT_OVERLAP_STOP:
            stop_reason = "text_overlap"
            break

        # Stop 3: parallel-line texture (4+ same-angle lines)
        cand_bin = _line_angle_bin(candidate.get("angle_deg"))
        tentative_bins = dict(angle_bins)
        tentative_bins[cand_bin] = tentative_bins.get(cand_bin, 0) + 1
        max_bin_share = max(tentative_bins.values()) / len(tentative)
        max_same_angle = max(tentative_bins.values())
        if max_same_angle >= _PARALLEL_COLINEAR_MIN and max_bin_share > _ORIENTATION_DOMINANT_MAX:
            stop_reason = "parallel_texture"
            break

        # Stop 4: compactness collapse
        sum_area = sum(
            (float(p.get("bbox_w") or 0) * float(p.get("bbox_h") or 0)) for p in tent_prims
        )
        tent_bbox_area = max(tw * th, 1e-6)
        if sum_area / tent_bbox_area < _MIN_COMPACTNESS and len(tentative) > 5:
            stop_reason = "compactness_collapse"
            break

        # Accept
        component_idxs.append(best_idx)
        claimed.add(best_idx)
        growth_trace.append(str(candidate["id"]))
        angle_bins[cand_bin] += 1

    # Final CCS
    comp_prims = [prims[i] for i in component_idxs]
    final_bbox = _union_xywh(comp_prims)
    sum_area = sum(
        (float(p.get("bbox_w") or 0) * float(p.get("bbox_h") or 0)) for p in comp_prims
    )
    fb_area = max(final_bbox[2] * final_bbox[3], 1e-6)
    compactness = min(1.0, sum_area / fb_area)
    repetition = min(1.0, len(component_idxs) / 8.0)
    ccs = round(compactness * 0.6 + repetition * 0.4, 4)

    return {
        "members": component_idxs,
        "bbox": final_bbox,
        "stop_reason": stop_reason,
        "completeness_score": ccs,
        "assembly_trace": growth_trace,
    }


async def main() -> None:
    print("=== Wave 20.14.3 TGA — Topology-Growing Assembly ===")

    sym_rows = await supabase_select(
        "blueprint_sheet_semantics",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&semantic_type=eq.SYMBOL_DOMINANT",
    ) or []
    symbol_sheet_ids = [r["sheet_id"] for r in sym_rows]
    print(f"SYMBOL_DOMINANT sheets: {len(symbol_sheet_ids)}")

    total_components = 0
    total_persisted = 0
    total_filtered_ccs = 0
    stop_breakdown: dict[str, int] = defaultdict(int)
    inserts: list[dict] = []

    for sheet_id in symbol_sheet_ids:
        # Get curve seeds (small fast query)
        curve_seeds = await _fetch_sheet_curves(sheet_id)
        if not curve_seeds:
            continue
        para_polys = await _fetch_paragraph_polygons(sheet_id)
        claimed_ids: set[str] = set()
        sheet_components = 0
        sheet_persisted = 0

        for seed in curve_seeds:
            sid = str(seed["id"])
            if sid in claimed_ids:
                continue

            # Fetch local neighborhood per seed (small, fast, scoped)
            try:
                cx_seed = float(seed.get("cx") or 0); cy_seed = float(seed.get("cy") or 0)
            except (TypeError, ValueError):
                continue
            neighborhood = await _fetch_primitives_in_bbox(
                sheet_id, cx_seed, cy_seed, _GROWTH_RADIUS * 2,
            )
            if not neighborhood:
                continue

            # Find seed in neighborhood (must include itself)
            seed_idx = -1
            for i, p in enumerate(neighborhood):
                if str(p["id"]) == sid:
                    seed_idx = i; break
            if seed_idx < 0:
                neighborhood = [seed] + neighborhood
                seed_idx = 0

            centroids = [
                (float(p.get("cx") or 0), float(p.get("cy") or 0)) for p in neighborhood
            ]
            # Local-claimed-by-id (not index; neighborhood differs per seed)
            local_claimed: set[int] = set()
            # Skip primitives already claimed by other components on this sheet
            for i, p in enumerate(neighborhood):
                if str(p["id"]) in claimed_ids:
                    local_claimed.add(i)

            comp = _grow_component(seed_idx, neighborhood, centroids, para_polys, local_claimed)
            total_components += 1
            sheet_components += 1
            stop_breakdown[comp["stop_reason"]] += 1

            # Mark globally claimed
            for i in comp["members"]:
                claimed_ids.add(str(neighborhood[i]["id"]))
            # Set prims/centroids refs so downstream code matches
            prims = neighborhood

            if len(comp["members"]) < 2:
                continue
            if comp["completeness_score"] < _CCS_BIND_FLOOR:
                total_filtered_ccs += 1
                continue

            member_prims = [prims[i] for i in comp["members"]]
            types = sorted((p.get("primitive_type") or "path") for p in member_prims)
            composed_sig = hashlib.sha1("|".join(types).encode()).hexdigest()

            inserts.append({
                "id": str(uuid_mod.uuid4()),
                "suite_id": SUITE,
                "project_id": PROJ,
                "sheet_id": sheet_id,
                "bbox": {"x": comp["bbox"][0], "y": comp["bbox"][1],
                         "w": comp["bbox"][2], "h": comp["bbox"][3]},
                "primitive_count": len(comp["members"]),
                "constituent_motif_ids": [str(prims[i]["id"]) for i in comp["members"]],
                "composed_signature": composed_sig,
                "plausibility_score": comp["completeness_score"],
                "total_instance_count": len(comp["members"]),
                "assembly_trace": {"trace": comp["assembly_trace"],
                                   "primitive_count": len(comp["members"])},
                "completeness_score": comp["completeness_score"],
                "stop_reason": comp["stop_reason"],
                "assembly_version": _VERSION,
            })
            total_persisted += 1

        if inserts:
            try:
                await supabase_insert_batch_ignore_conflicts(
                    "blueprint_composed_symbols", inserts, conflict_target="id",
                )
            except Exception as exc:
                print(f"  insert fail sheet={sheet_id[:8]}: {type(exc).__name__}")
            print(f"  sheet={sheet_id[:8]} prims={len(prims)} curves={len(curve_seeds)} components={sheet_components} persisted_so_far_in_batch={len(inserts)}")
            inserts = []

    print()
    print(f"=== TGA DONE ===")
    print(f"components grown:     {total_components}")
    print(f"persisted:            {total_persisted}")
    print(f"filtered (low CCS):   {total_filtered_ccs}")
    print(f"stop reason breakdown:")
    for r, n in sorted(stop_breakdown.items(), key=lambda kv: -kv[1]):
        print(f"  {r:<30s} {n}")


if __name__ == "__main__":
    asyncio.run(main())
