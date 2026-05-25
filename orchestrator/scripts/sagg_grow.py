"""Wave 20.14.6 — SAGG (Semantic Admissibility Graph Growth).

Replaces PCE-v3 TGA per architectural correction 2026-05-24:
"only admit semantically valid expansion" — not "grow then filter".

Each seed (curve anchor, sagg_seed_eligible=true) attempts greedy expansion.
EACH neighbor must pass admissibility BEFORE inclusion:

  Stage 1 — visibility (raster_density >= floor)
  Stage 2 — zoning (paragraph/hatching exclusion — partial, deferred)
  Stage 3 — topology (compactness collapse, orientation entropy)
  Stage 4 — semantic (anchor preserved; growth doesn't text/linear-dominate)

After growth, compute GCS:
  compactness + symmetry + enclosure + anchor_presence + topology_stability
  - texture_affinity - text_affinity - linear_run_score

Only graphs with GCS >= threshold AND purity >= 0.50 survive.

Output: blueprint_composed_symbols rows with sagg_admitted=true.
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

# Admissibility thresholds
_PROXIMITY_PT = 25.0
_MIN_RASTER = 0.05
_MAX_GRAPH_SIZE = 20
_MIN_GRAPH_SIZE = 3            # real symbols are NOT lone primitives; require structural commitment
_MIN_BBOX_AREA = 80.0          # min pt² to filter sub-pixel admissions
_MIN_TOTAL_DIM = 10.0          # min max(w, h) in pt — no microscopic graphs
_COMPACTNESS_FLOOR = 0.18      # bbox aspect-driven; reject if ratio worse than this
_MAX_ASPECT = 4.0              # graph bbox aspect ratio cap
_GCS_THRESHOLD = 0.40
_PURITY_THRESHOLD = 0.50
_LINEAR_RUN_REJECT = 0.70      # if >70% lines parallel, reject growth
_TEXT_PROXIMITY_REJECT = 0.50  # if >50% prims overlap paragraph, reject (deferred until Stage 2 wires)


async def _fetch_seeds_for_sheet(sheet_id: str) -> list[dict]:
    """Per-sheet seed fetch using idx_bp_sagg_seeds(sheet_id, sagg_seed_eligible)."""
    return await supabase_select(
        "blueprint_primitives",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}&sagg_seed_eligible=eq.true",
        limit=1000,
    ) or []


async def _fetch_seed_sheets() -> list[str]:
    """Get distinct sheets containing any SAGG-eligible seed."""
    rows = await supabase_select(
        "blueprint_sheet_semantics",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&semantic_type=eq.SYMBOL_DOMINANT",
    ) or []
    return [r["sheet_id"] for r in rows]


async def _fetch_local_neighborhood(sheet_id: str, cx: float, cy: float, radius: float = 60.0) -> list[dict]:
    """Per-seed spatial neighborhood — uses cx/cy range filter (much narrower than whole sheet)."""
    return await supabase_select(
        "blueprint_primitives",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}"
        f"&raster_density=gte.{_MIN_RASTER}"
        f"&cx=gte.{cx - radius}&cx=lte.{cx + radius}"
        f"&cy=gte.{cy - radius}&cy=lte.{cy + radius}",
        limit=500,
    ) or []


def _bbox_union(prims: list[dict]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for p in prims:
        try:
            xs.extend([float(p.get("x0") or 0), float(p.get("x1") or 0)])
            ys.extend([float(p.get("y0") or 0), float(p.get("y1") or 0)])
        except (TypeError, ValueError):
            pass
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def _centroid(p: dict) -> tuple[float, float]:
    try:
        return (float(p.get("cx") or 0), float(p.get("cy") or 0))
    except (TypeError, ValueError):
        return (0.0, 0.0)


def _grow(seed: dict, neighbors: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Greedy SAGG growth from seed. Returns (admitted_prims, rejection_counter)."""
    admitted = [seed]
    admitted_ids = {seed["id"]}
    cx_s, cy_s = _centroid(seed)
    rejections: dict[str, int] = defaultdict(int)

    # Sort candidates by distance to seed centroid (closest first)
    scored = []
    for n in neighbors:
        if n["id"] in admitted_ids:
            continue
        ncx, ncy = _centroid(n)
        d = math.hypot(ncx - cx_s, ncy - cy_s)
        if d > _PROXIMITY_PT * 2:  # outer cutoff
            continue
        scored.append((d, n))
    scored.sort(key=lambda kv: kv[0])

    seed_field = seed.get("field_type")
    for _, cand in scored:
        if len(admitted) >= _MAX_GRAPH_SIZE:
            rejections["max_size"] += 1; break

        # Stage 2 admissibility (field transition rule)
        cand_field = cand.get("field_type")
        if cand_field in ("HATCH_FIELD", "TEXT_FIELD", "BORDER_FIELD"):
            rejections[f"field_block_{cand_field}"] += 1; continue
        if seed_field == "SYMBOL_FIELD" and cand_field == "STRUCTURAL_FIELD":
            rejections["field_block_structural_escape"] += 1; continue

        # Stage 3: topology admissibility — compactness check before inclusion
        trial = admitted + [cand]
        x0, y0, x1, y1 = _bbox_union(trial)
        w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
        aspect = max(w, h) / max(min(w, h), 1e-6)
        if aspect > _MAX_ASPECT:
            rejections["aspect_collapse"] += 1; continue

        # Stage 4 partial: linear-run check
        line_count = sum(1 for p in trial if p.get("primitive_type") == "line")
        if len(trial) >= 4 and line_count / len(trial) > 0.85:
            rejections["linear_dominance"] += 1; continue

        # Distance to existing graph centroid (NOT just seed) — require adjacency to admitted set
        ax = sum(_centroid(p)[0] for p in admitted) / len(admitted)
        ay = sum(_centroid(p)[1] for p in admitted) / len(admitted)
        cand_cx, cand_cy = _centroid(cand)
        if math.hypot(cand_cx - ax, cand_cy - ay) > _PROXIMITY_PT:
            rejections["too_far_from_centroid"] += 1; continue

        admitted.append(cand)
        admitted_ids.add(cand["id"])

    return admitted, dict(rejections)


def _compute_gcs(prims: list[dict]) -> tuple[float, dict[str, float], float]:
    """Returns (gcs_score, components_dict, purity_score)."""
    if not prims:
        return (0.0, {}, 0.0)

    n = len(prims)
    x0, y0, x1, y1 = _bbox_union(prims)
    w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
    aspect = max(w, h) / max(min(w, h), 1e-6)
    compactness = max(0.0, 1.0 - (aspect - 1.0) / 4.0)  # 1.0 perfect square, 0.0 at aspect 5+

    # Symmetry: distance distribution of primitive centroids around graph centroid
    cx_g = sum(_centroid(p)[0] for p in prims) / n
    cy_g = sum(_centroid(p)[1] for p in prims) / n
    dists = [math.hypot(_centroid(p)[0] - cx_g, _centroid(p)[1] - cy_g) for p in prims]
    mean_d = sum(dists) / len(dists)
    var = sum((d - mean_d) ** 2 for d in dists) / len(dists) if mean_d > 0 else 0
    cv = math.sqrt(var) / max(mean_d, 1e-6)
    symmetry = max(0.0, 1.0 - cv)  # low CV = high symmetry

    # Enclosure: ratio of curves/rects (closed structures)
    closed_types = {"curve", "rect", "quad"}
    closed_n = sum(1 for p in prims if p.get("primitive_type") in closed_types)
    enclosure = closed_n / n

    # Anchor presence: at least one curve >= 5pt
    has_anchor = any(
        p.get("primitive_type") == "curve"
        and max(float(p.get("bbox_w") or 0), float(p.get("bbox_h") or 0)) >= 5
        for p in prims
    )
    anchor_presence = 1.0 if has_anchor else 0.0

    # Topology stability: graph not too sparse vs primitives
    topology_stability = min(1.0, n / 8.0) if n >= 2 else 0.5

    # Texture affinity: share of same-angle lines
    line_angles = [float(p.get("angle_deg")) for p in prims
                   if p.get("primitive_type") == "line" and p.get("angle_deg") is not None]
    if len(line_angles) >= 3:
        bins: dict[int, int] = defaultdict(int)
        for a in line_angles:
            bins[int(abs(a) // 10) % 18] += 1
        texture_affinity = max(bins.values()) / len(line_angles)
    else:
        texture_affinity = 0.0

    # Text affinity (deferred — paragraph overlap not computed)
    text_affinity = 0.0

    # Linear run score
    line_share = sum(1 for p in prims if p.get("primitive_type") == "line") / n
    linear_run_score = max(0.0, line_share - 0.5) * 2.0  # 0 at <=50% lines, 1.0 at 100%

    gcs = (
        compactness + symmetry + enclosure + anchor_presence + topology_stability
        - texture_affinity - text_affinity - linear_run_score
    ) / 5.0  # normalize to roughly 0..1
    gcs = max(0.0, min(1.0, gcs))

    components = {
        "compactness": round(compactness, 3),
        "symmetry": round(symmetry, 3),
        "enclosure": round(enclosure, 3),
        "anchor_presence": anchor_presence,
        "topology_stability": round(topology_stability, 3),
        "texture_affinity": round(texture_affinity, 3),
        "text_affinity": text_affinity,
        "linear_run_score": round(linear_run_score, 3),
    }

    semantic_types = {"curve", "rect", "quad"}
    semantic_n = sum(1 for p in prims if p.get("primitive_type") in semantic_types)
    purity = semantic_n / n if n > 0 else 0

    return (round(gcs, 4), components, round(purity, 4))


async def main() -> None:
    print("=== Wave 20.14.6 SAGG Growth — Gulfstream ===")
    sheets = await _fetch_seed_sheets()
    print(f"SYMBOL_DOMINANT sheets: {len(sheets)}")

    seeds_by_sheet: dict[str, list[dict]] = {}
    total_seeds = 0
    for sid in sheets:
        s = await _fetch_seeds_for_sheet(sid)
        if s:
            seeds_by_sheet[sid] = s
            total_seeds += len(s)
    print(f"total seeds: {total_seeds}")
    seeds = [s for v in seeds_by_sheet.values() for s in v]

    admitted_inserts: list[dict] = []
    rejected_count = 0
    used_prims: set[str] = set()  # don't reuse a primitive across multiple seeds
    rejection_summary: dict[str, int] = defaultdict(int)
    gcs_pass = 0; gcs_fail = 0; purity_fail = 0

    for sheet_id, sheet_seeds in seeds_by_sheet.items():
        print(f"\nsheet={sheet_id[:8]}: {len(sheet_seeds)} seeds")
        sheet_admitted = 0
        for seed in sheet_seeds:
            if seed["id"] in used_prims:
                continue
            cx, cy = _centroid(seed)
            local = await _fetch_local_neighborhood(sheet_id, cx, cy, radius=60.0)
            neighbors = [n for n in local if n["id"] != seed["id"] and n["id"] not in used_prims]

            graph_prims, rej = _grow(seed, neighbors)
            for k, v in rej.items():
                rejection_summary[k] += v

            if len(graph_prims) < _MIN_GRAPH_SIZE:
                rejected_count += 1; rejection_summary["min_size_floor"] += 1; continue

            # Structural commitment gate — kills lone-primitive and sub-pixel admissions
            x0c, y0c, x1c, y1c = _bbox_union(graph_prims)
            wc, hc = x1c - x0c, y1c - y0c
            if wc * hc < _MIN_BBOX_AREA:
                rejected_count += 1; rejection_summary["bbox_area_floor"] += 1; continue
            if max(wc, hc) < _MIN_TOTAL_DIM:
                rejected_count += 1; rejection_summary["total_dim_floor"] += 1; continue

            gcs, components, purity = _compute_gcs(graph_prims)
            if gcs < _GCS_THRESHOLD:
                gcs_fail += 1; rejected_count += 1; continue
            if purity < _PURITY_THRESHOLD:
                purity_fail += 1; rejected_count += 1; continue
            gcs_pass += 1

            # Persist as SAGG-admitted
            x0, y0, x1, y1 = _bbox_union(graph_prims)
            bw, bh = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
            types = sorted(p.get("primitive_type") or "p" for p in graph_prims)
            composed_sig = hashlib.sha1("|".join(types).encode()).hexdigest()

            admitted_inserts.append({
                "id": str(uuid_mod.uuid4()),
                "suite_id": SUITE,
                "project_id": PROJ,
                "sheet_id": sheet_id,
                "bbox": {"x": x0, "y": y0, "w": bw, "h": bh},
                "primitive_count": len(graph_prims),
                "constituent_motif_ids": [p["id"] for p in graph_prims],
                "composed_signature": composed_sig,
                "plausibility_score": round(gcs, 4),
                "total_instance_count": len(graph_prims),
                "sagg_admitted": True,
                "gcs_score": gcs,
                "gcs_components": components,
                "purity_score": purity,
            })
            for p in graph_prims:
                used_prims.add(p["id"])
            sheet_admitted += 1

        print(f"  admitted: {sheet_admitted}, "
              f"gcs_pass={gcs_pass}, gcs_fail={gcs_fail}, purity_fail={purity_fail}")

    # Flush inserts in batches
    print(f"\n=== INSERTING {len(admitted_inserts)} SAGG-admitted graphs ===")
    BATCH = 50
    for i in range(0, len(admitted_inserts), BATCH):
        chunk = admitted_inserts[i:i + BATCH]
        try:
            await supabase_insert_batch_ignore_conflicts(
                "blueprint_composed_symbols", chunk, conflict_target="id"
            )
            print(f"  inserted batch {i//BATCH + 1} ({len(chunk)} rows)")
        except Exception as exc:
            print(f"  insert FAIL batch {i//BATCH + 1}: {type(exc).__name__}: {exc}")

    print(f"\n=== SAGG SUMMARY ===")
    print(f"seeds: {len(seeds)}")
    print(f"admitted graphs: {len(admitted_inserts)}")
    print(f"rejected: {rejected_count}")
    print(f"GCS pass: {gcs_pass}, GCS fail: {gcs_fail}, purity fail: {purity_fail}")
    print(f"growth rejections: {dict(rejection_summary)}")


if __name__ == "__main__":
    asyncio.run(main())
