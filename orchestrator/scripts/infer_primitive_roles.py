"""Wave 20.14.5 — Probabilistic Primitive Role Inference (PRI).

Per GPT v9: 'Do NOT hardcode roles aggressively. Infer probabilistic
primitive roles. Many construction primitives are ambiguous in isolation.'

Role taxonomy:
  boundary       — long perimeter lines (>50pt, isolated endpoint)
  connector      — short lines between anchors (both endpoints touching)
  decorative     — primitives in dense parallel-line clusters (hatching)
  support        — long structural lines (long but not boundary/connector)
  anchor         — curves/compact primitives forming symbol cores
  symmetry_axis  — lines passing through cluster centroids
  feed_line      — short lines extending from cluster
  text_scaffold  — primitives overlapping paragraph regions

Output: blueprint_primitives.role_scores jsonb (probabilistic distribution)
                            .dominant_role text (highest score)
                            .role_confidence numeric (top score - 2nd score)

This becomes foundational for WL graph signatures (Wave 20.14.6) and
construction topology grammars (Wave 20.14.7).
"""
import asyncio
import json
import math
import sys
from collections import defaultdict

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"

_BOUNDARY_LONG_PT = 50.0          # length threshold for boundary candidates
_CONNECTOR_MAX_PT = 25.0          # length cap for connectors
_ENDPOINT_TOUCH_PT = 4.0          # endpoint adjacency threshold
_PARALLEL_NEIGHBORS = 4            # cluster size for decorative classification
_PARALLEL_ANGLE_TOLERANCE = 8.0    # degrees — same-angle clustering
_TEXT_OVERLAP_FLOOR = 0.30


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 180
    return min(d, 180 - d)


def _bbox_paragraph_overlap(xywh, poly8) -> float:
    if not poly8 or len(poly8) < 8:
        return 0.0
    ax, ay, aw, ah = xywh
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


async def _fetch_paragraph_polys(sheet_id: str) -> list[list[float]]:
    rows = await supabase_select(
        "blueprint_sheet_intelligence",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}",
        limit=1,
    ) or []
    if not rows:
        return []
    polys: list[list[float]] = []
    scale = 72.0 / 200.0  # PNG (200 DPI) → PDF pts
    for p in (rows[0].get("paragraphs") or []):
        b = p.get("bbox") if isinstance(p, dict) else None
        if isinstance(b, list) and len(b) >= 8:
            polys.append([b[i] * scale for i in range(8)])
    return polys


def _infer_roles(
    prim: dict,
    neighbors: list[dict],
    para_polys: list[list[float]],
) -> dict[str, float]:
    """Probabilistic role scores 0..1 for one primitive given its local neighborhood."""
    scores = {
        "boundary": 0.0,
        "connector": 0.0,
        "decorative": 0.0,
        "support": 0.0,
        "anchor": 0.0,
        "symmetry_axis": 0.0,
        "feed_line": 0.0,
        "text_scaffold": 0.0,
    }

    ptype = prim.get("primitive_type") or "path"
    try:
        x0 = float(prim.get("x0") or 0); y0 = float(prim.get("y0") or 0)
        x1 = float(prim.get("x1") or 0); y1 = float(prim.get("y1") or 0)
        bw = float(prim.get("bbox_w") or 0); bh = float(prim.get("bbox_h") or 0)
    except (TypeError, ValueError):
        return scores
    length = float(prim.get("length") or max(bw, bh))
    angle = prim.get("angle_deg")

    # 1. Anchor: curves are strong anchor candidates
    if ptype == "curve":
        scores["anchor"] = 0.85
    elif ptype == "rect":
        scores["anchor"] = 0.40  # rects can be fixture boxes

    # 2. Text scaffold: overlaps paragraph region
    for poly in para_polys:
        o = _bbox_paragraph_overlap((min(x0, x1), min(y0, y1), bw, bh), poly)
        if o > scores["text_scaffold"]:
            scores["text_scaffold"] = o
    if scores["text_scaffold"] > _TEXT_OVERLAP_FLOOR:
        scores["text_scaffold"] = min(1.0, scores["text_scaffold"] + 0.30)

    # 3. Parallel decoration: many same-angle neighbors of similar length
    if ptype == "line" and angle is not None:
        try:
            ang = float(angle)
            same_angle_count = 0
            for n in neighbors:
                if n.get("primitive_type") != "line":
                    continue
                na = n.get("angle_deg")
                if na is None:
                    continue
                try:
                    if _angle_diff(ang, float(na)) <= _PARALLEL_ANGLE_TOLERANCE:
                        same_angle_count += 1
                except (TypeError, ValueError):
                    continue
            if same_angle_count >= _PARALLEL_NEIGHBORS:
                scores["decorative"] = min(1.0, same_angle_count / 10.0)
        except (TypeError, ValueError):
            pass

    # 4. Length-based: boundary / support / connector / feed_line
    if ptype == "line":
        if length >= _BOUNDARY_LONG_PT:
            # Long line — count endpoint touches in neighborhood
            endpoint_touches = 0
            endpoints = [(x0, y0), (x1, y1)]
            for n in neighbors:
                try:
                    nx0 = float(n.get("x0") or 0); ny0 = float(n.get("y0") or 0)
                    nx1 = float(n.get("x1") or 0); ny1 = float(n.get("y1") or 0)
                    n_pts = [(nx0, ny0), (nx1, ny1), ((nx0 + nx1) / 2, (ny0 + ny1) / 2)]
                    for ep in endpoints:
                        for np in n_pts:
                            if math.hypot(ep[0] - np[0], ep[1] - np[1]) <= _ENDPOINT_TOUCH_PT:
                                endpoint_touches += 1; break
                except (TypeError, ValueError):
                    continue
            # Few endpoint touches = boundary candidate; many = support
            if endpoint_touches <= 1:
                scores["boundary"] = min(1.0, length / 200.0 + 0.30)
            elif endpoint_touches >= 4:
                scores["support"] = min(1.0, length / 200.0 + 0.20)
            else:
                scores["support"] = min(0.8, length / 200.0)
        elif length < _CONNECTOR_MAX_PT:
            # Short line — connector if both endpoints touch neighbors
            ep_touch_a = ep_touch_b = False
            for n in neighbors:
                try:
                    nx0 = float(n.get("x0") or 0); ny0 = float(n.get("y0") or 0)
                    nx1 = float(n.get("x1") or 0); ny1 = float(n.get("y1") or 0)
                    n_pts = [(nx0, ny0), (nx1, ny1), ((nx0 + nx1) / 2, (ny0 + ny1) / 2)]
                    for np in n_pts:
                        if not ep_touch_a and math.hypot(x0 - np[0], y0 - np[1]) <= _ENDPOINT_TOUCH_PT:
                            ep_touch_a = True
                        if not ep_touch_b and math.hypot(x1 - np[0], y1 - np[1]) <= _ENDPOINT_TOUCH_PT:
                            ep_touch_b = True
                    if ep_touch_a and ep_touch_b:
                        break
                except (TypeError, ValueError):
                    continue
            if ep_touch_a and ep_touch_b:
                scores["connector"] = 0.85
            elif ep_touch_a or ep_touch_b:
                scores["feed_line"] = 0.70

    # Normalize so dominant score becomes confident; allow ties.
    total = sum(scores.values())
    if total > 0:
        # Soft normalization — preserve relative magnitudes
        for k in scores:
            scores[k] = round(scores[k], 3)

    return scores


async def main() -> None:
    print("=== Wave 20.14.5 Primitive Role Inference (PRI) ===")

    # Process only SYMBOL_DOMINANT sheets first (most valuable)
    sym = await supabase_select(
        "blueprint_sheet_semantics",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&semantic_type=eq.SYMBOL_DOMINANT",
    ) or []
    sheet_ids = [r["sheet_id"] for r in sym]
    print(f"SYMBOL_DOMINANT sheets to process: {len(sheet_ids)}")

    total_processed = 0
    role_counts: dict[str, int] = defaultdict(int)

    from aspire_orchestrator.services.supabase_client import supabase_update

    for sheet_id in sheet_ids:
        para_polys = await _fetch_paragraph_polys(sheet_id)

        # Fetch visible primitives in chunks (cursor pagination)
        last_id = ""
        sheet_processed = 0
        while True:
            cur = f"&id=gt.{last_id}" if last_id else ""
            page = await supabase_select(
                "blueprint_primitives",
                f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}"
                f"&raster_density=gte.0.05{cur}&order=id",
                limit=500,
            )
            if not page:
                break

            # Local neighborhood lookup — index by centroid grid (50pt buckets)
            grid: dict[tuple[int, int], list[dict]] = defaultdict(list)
            for p in page:
                try:
                    cx = float(p.get("cx") or 0); cy = float(p.get("cy") or 0)
                    grid[(int(cx // 50), int(cy // 50))].append(p)
                except (TypeError, ValueError):
                    continue

            for p in page:
                try:
                    cx = float(p.get("cx") or 0); cy = float(p.get("cy") or 0)
                except (TypeError, ValueError):
                    continue
                gx, gy = int(cx // 50), int(cy // 50)
                neighbors = []
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        neighbors.extend(grid.get((gx + dx, gy + dy), []))
                neighbors = [n for n in neighbors if n["id"] != p["id"]][:80]

                scores = _infer_roles(p, neighbors, para_polys)
                if not any(v > 0.01 for v in scores.values()):
                    last_id = page[-1]["id"]
                    continue
                dominant = max(scores.items(), key=lambda kv: kv[1])
                confidence = dominant[1] - sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else dominant[1]
                role_counts[dominant[0]] += 1

                try:
                    await supabase_update(
                        "blueprint_primitives",
                        f"id=eq.{p['id']}&suite_id=eq.{SUITE}",
                        {
                            "role_scores": scores,
                            "dominant_role": dominant[0],
                            "role_confidence": round(confidence, 3),
                        },
                    )
                    total_processed += 1
                    sheet_processed += 1
                except Exception:
                    pass

            last_id = page[-1]["id"]
            if len(page) < 500:
                break

        print(f"  sheet={sheet_id[:8]}: {sheet_processed} primitives roled")

    print()
    print(f"=== PRI DONE — {total_processed} primitives roled ===")
    print(f"role distribution:")
    for r, n in sorted(role_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {r:<18s} {n}")


if __name__ == "__main__":
    asyncio.run(main())
