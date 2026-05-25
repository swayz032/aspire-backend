"""Wave 20.14.5D — SAGG v2 with Growth Trajectory + Persistence Score.

Path-dependent semantic graph parsing per roadmap v12.

Per-step metrics captured:
  step_n, added_prim_id, gcs, purity, role_histogram, field_consistency,
  topology_entropy, anchor_strength, adjacency_confidence

Trajectory classification:
  CONVERGENT      — gcs/purity stabilize or improve; field stays consistent
  DIFFUSIVE       — entropy rises, purity falls, field transitions spike
  DEGENERATE_STATIC — single-step, no real growth; geometric placeholder

Persistence Score = topology_similarity_across_steps
                  + anchor_consistency_across_steps
                  + field_stability_across_steps

Ranked by PS, not GCS. Real symbols score higher even if geometrically irregular.
"""
import asyncio
import hashlib
import math
import sys
import uuid as uuid_mod
from collections import Counter, defaultdict

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.supabase_client import (  # noqa: E402
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"

_PROXIMITY_PT = 25.0
_MIN_RASTER = 0.05
_MAX_GRAPH_SIZE = 20
_MIN_GRAPH_SIZE = 3
_MIN_BBOX_AREA = 80.0
_MIN_TOTAL_DIM = 10.0
_MAX_ASPECT = 4.0
_PURITY_THRESHOLD = 0.50
_PS_THRESHOLD = 0.45  # persistence score floor


# ============================================================
# Per-primitive helpers
# ============================================================

def _centroid(p: dict) -> tuple[float, float]:
    try:
        return (float(p.get("cx") or 0), float(p.get("cy") or 0))
    except (TypeError, ValueError):
        return (0.0, 0.0)


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


def _role_histogram(prims: list[dict]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for p in prims:
        counts[p.get("dominant_role") or "none"] += 1
    return dict(counts)


def _field_consistency(prims: list[dict]) -> float:
    if not prims:
        return 1.0
    counts: Counter[str] = Counter()
    for p in prims:
        counts[p.get("field_type") or "UNKNOWN_FIELD"] += 1
    top = max(counts.values())
    return top / len(prims)


def _topology_entropy(prims: list[dict]) -> float:
    """Shannon entropy over primitive_type distribution; lower = more uniform structure."""
    if not prims:
        return 0.0
    counts: Counter[str] = Counter()
    for p in prims:
        counts[p.get("primitive_type") or "p"] += 1
    n = len(prims)
    ent = 0.0
    for c in counts.values():
        if c > 0:
            pr = c / n
            ent -= pr * math.log2(pr)
    return ent


def _anchor_strength(prims: list[dict]) -> float:
    """Sum of curve sizes / total bbox-diagonal; higher = stronger anchors."""
    diag = 0.0
    x0, y0, x1, y1 = _bbox_union(prims)
    diag = max(math.hypot(x1 - x0, y1 - y0), 1.0)
    curve_size = 0.0
    for p in prims:
        if p.get("primitive_type") == "curve":
            try:
                curve_size += max(float(p.get("bbox_w") or 0), float(p.get("bbox_h") or 0))
            except (TypeError, ValueError):
                pass
    return min(1.0, curve_size / diag)


def _adjacency_confidence(prims: list[dict], radius: float = 4.0) -> float:
    """Share of primitive pairs whose endpoints touch within radius pt."""
    if len(prims) < 2:
        return 1.0
    pairs = 0
    touching = 0
    for i in range(len(prims)):
        for j in range(i + 1, len(prims)):
            pairs += 1
            try:
                p1 = prims[i]; p2 = prims[j]
                x0a = float(p1.get("x0") or 0); y0a = float(p1.get("y0") or 0)
                x1a = float(p1.get("x1") or 0); y1a = float(p1.get("y1") or 0)
                x0b = float(p2.get("x0") or 0); y0b = float(p2.get("y0") or 0)
                x1b = float(p2.get("x1") or 0); y1b = float(p2.get("y1") or 0)
                eps_a = [(x0a, y0a), (x1a, y1a)]
                eps_b = [(x0b, y0b), (x1b, y1b)]
                if any(math.hypot(a[0] - b[0], a[1] - b[1]) <= radius
                       for a in eps_a for b in eps_b):
                    touching += 1
            except (TypeError, ValueError):
                continue
    return touching / max(pairs, 1)


def _gcs_components(prims: list[dict]) -> tuple[float, dict[str, float]]:
    """Standard GCS for one step."""
    if not prims:
        return (0.0, {})
    n = len(prims)
    x0, y0, x1, y1 = _bbox_union(prims)
    w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
    aspect = max(w, h) / max(min(w, h), 1e-6)
    compactness = max(0.0, 1.0 - (aspect - 1.0) / 4.0)
    closed_types = {"curve", "rect", "quad"}
    closed_n = sum(1 for p in prims if p.get("primitive_type") in closed_types)
    enclosure = closed_n / n
    has_anchor = any(
        p.get("primitive_type") == "curve"
        and max(float(p.get("bbox_w") or 0), float(p.get("bbox_h") or 0)) >= 5
        for p in prims
    )
    anchor_presence = 1.0 if has_anchor else 0.0
    line_share = sum(1 for p in prims if p.get("primitive_type") == "line") / n
    linear_run_score = max(0.0, line_share - 0.5) * 2.0
    gcs = (compactness + enclosure + anchor_presence - linear_run_score) / 3.0
    gcs = max(0.0, min(1.0, gcs))
    return (round(gcs, 4), {
        "compactness": round(compactness, 3),
        "enclosure": round(enclosure, 3),
        "anchor_presence": anchor_presence,
        "linear_run": round(linear_run_score, 3),
    })


def _purity(prims: list[dict]) -> float:
    if not prims:
        return 0.0
    semantic_types = {"curve", "rect", "quad"}
    sem = sum(1 for p in prims if p.get("primitive_type") in semantic_types)
    return sem / len(prims)


# ============================================================
# Trajectory classification + Persistence Score
# ============================================================

def _classify_trajectory(steps: list[dict]) -> str:
    """Classify trajectory as CONVERGENT / DIFFUSIVE / DEGENERATE_STATIC."""
    if len(steps) <= 1:
        return "DEGENERATE_STATIC"

    # Build deltas over the trajectory
    gcs_curve = [s["gcs"] for s in steps]
    pur_curve = [s["purity"] for s in steps]
    fc_curve = [s["field_consistency"] for s in steps]
    ent_curve = [s["topology_entropy"] for s in steps]

    gcs_end_minus_mid = gcs_curve[-1] - gcs_curve[len(gcs_curve) // 2]
    pur_end_minus_start = pur_curve[-1] - pur_curve[0]
    fc_min = min(fc_curve)
    ent_growth = ent_curve[-1] - ent_curve[0]

    # Diffusive: ONLY if BOTH purity drops heavily AND field consistency collapses.
    # Purity drop alone is structural enrichment (curve anchor + connectors).
    # Field crossing is contamination.
    if (pur_end_minus_start < -0.30 and fc_min < 0.70) or ent_growth > 1.2:
        return "DIFFUSIVE"

    # Convergent: field stays consistent and GCS doesn't catastrophically decay
    if fc_min >= 0.70 and gcs_end_minus_mid >= -0.20:
        # Bonus: role diversity (got connector/support added to curve seed)
        type_distros_last = steps[-1].get("type_distro") or {}
        if len(type_distros_last) >= 2:  # multiple primitive types = real symbol structure
            return "CONVERGENT"
        # Single-type graphs (all curves OR all rects) = annotation-like
        return "DEGENERATE_STATIC"

    return "DEGENERATE_STATIC"


def _persistence_score(steps: list[dict]) -> float:
    """PS = topology_similarity_across_steps + anchor_consistency + field_stability."""
    if len(steps) <= 1:
        # Single-step graphs have no trajectory — assign baseline
        return 0.30

    n_steps = len(steps)

    # Topology similarity: how much does primitive_type distribution shift step-to-step?
    type_distros = [s.get("type_distro") or {} for s in steps]
    sims: list[float] = []
    for i in range(1, len(type_distros)):
        prev = type_distros[i - 1]; cur = type_distros[i]
        keys = set(prev) | set(cur)
        total_prev = sum(prev.values()) or 1
        total_cur = sum(cur.values()) or 1
        diff = 0.0
        for k in keys:
            diff += abs(prev.get(k, 0) / total_prev - cur.get(k, 0) / total_cur)
        sims.append(max(0.0, 1.0 - diff / 2.0))
    topology_similarity = sum(sims) / len(sims) if sims else 0.5

    # Anchor consistency: did anchor strength stay stable or improve?
    anchor_curve = [s["anchor_strength"] for s in steps]
    anchor_drops = sum(1 for i in range(1, len(anchor_curve))
                       if anchor_curve[i] < anchor_curve[i - 1] - 0.10)
    anchor_consistency = max(0.0, 1.0 - anchor_drops / max(n_steps - 1, 1))

    # Field stability: minimum field_consistency across steps
    fc_curve = [s["field_consistency"] for s in steps]
    field_stability = min(fc_curve)

    ps = (topology_similarity + anchor_consistency + field_stability) / 3.0
    return round(ps, 4)


# ============================================================
# SAGG growth with trajectory recording
# ============================================================

async def _fetch_seeds_for_sheet(sheet_id: str) -> list[dict]:
    return await supabase_select(
        "blueprint_primitives",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}&sagg_seed_eligible=eq.true",
        limit=1000,
    ) or []


async def _fetch_symbol_sheets() -> list[str]:
    rows = await supabase_select(
        "blueprint_sheet_semantics",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&semantic_type=eq.SYMBOL_DOMINANT",
    ) or []
    return [r["sheet_id"] for r in rows]


async def _fetch_local_neighborhood(sheet_id: str, cx: float, cy: float, radius: float = 60.0) -> list[dict]:
    return await supabase_select(
        "blueprint_primitives",
        f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}"
        f"&raster_density=gte.{_MIN_RASTER}"
        f"&cx=gte.{cx - radius}&cx=lte.{cx + radius}"
        f"&cy=gte.{cy - radius}&cy=lte.{cy + radius}",
        limit=500,
    ) or []


def _step_metrics(prims: list[dict]) -> dict:
    gcs, comp = _gcs_components(prims)
    type_distro: dict[str, int] = {}
    for p in prims:
        t = p.get("primitive_type") or "p"
        type_distro[t] = type_distro.get(t, 0) + 1
    return {
        "gcs": gcs,
        "purity": round(_purity(prims), 4),
        "field_consistency": round(_field_consistency(prims), 4),
        "topology_entropy": round(_topology_entropy(prims), 4),
        "anchor_strength": round(_anchor_strength(prims), 4),
        "adjacency_confidence": round(_adjacency_confidence(prims), 4),
        "n": len(prims),
        "type_distro": type_distro,
        "components": comp,
    }


def _grow_with_trajectory(seed: dict, neighbors: list[dict]) -> tuple[list[dict], list[dict], dict[str, int], list[dict]]:
    """Returns (admitted_prims, trajectory_steps, rejection_counter, rejection_candidates)."""
    admitted = [seed]
    admitted_ids = {seed["id"]}
    cx_s, cy_s = _centroid(seed)
    rejections: dict[str, int] = defaultdict(int)
    rejection_candidates: list[dict] = []

    trajectory: list[dict] = []
    step0 = _step_metrics(admitted)
    step0["step"] = 0
    step0["added_prim_id"] = seed["id"]
    trajectory.append(step0)

    scored = []
    for n in neighbors:
        if n["id"] in admitted_ids:
            continue
        ncx, ncy = _centroid(n)
        d = math.hypot(ncx - cx_s, ncy - cy_s)
        if d > _PROXIMITY_PT * 2:
            continue
        scored.append((d, n))
    scored.sort(key=lambda kv: kv[0])

    seed_field = seed.get("field_type")
    step_n = 0
    for _, cand in scored:
        if len(admitted) >= _MAX_GRAPH_SIZE:
            rejections["max_size"] += 1; break

        cand_field = cand.get("field_type")
        if cand_field in ("HATCH_FIELD", "TEXT_FIELD", "BORDER_FIELD"):
            rejections[f"field_block_{cand_field}"] += 1
            rejection_candidates.append({"id": cand["id"], "reason": f"field_block_{cand_field}"})
            continue
        if seed_field == "SYMBOL_FIELD" and cand_field == "STRUCTURAL_FIELD":
            rejections["field_block_structural"] += 1
            rejection_candidates.append({"id": cand["id"], "reason": "field_block_structural"})
            continue

        trial = admitted + [cand]
        x0, y0, x1, y1 = _bbox_union(trial)
        w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
        aspect = max(w, h) / max(min(w, h), 1e-6)
        if aspect > _MAX_ASPECT:
            rejections["aspect_collapse"] += 1
            rejection_candidates.append({"id": cand["id"], "reason": "aspect_collapse"})
            continue

        line_count = sum(1 for p in trial if p.get("primitive_type") == "line")
        if len(trial) >= 4 and line_count / len(trial) > 0.85:
            rejections["linear_dominance"] += 1
            rejection_candidates.append({"id": cand["id"], "reason": "linear_dominance"})
            continue

        ax = sum(_centroid(p)[0] for p in admitted) / len(admitted)
        ay = sum(_centroid(p)[1] for p in admitted) / len(admitted)
        cand_cx, cand_cy = _centroid(cand)
        if math.hypot(cand_cx - ax, cand_cy - ay) > _PROXIMITY_PT:
            rejections["too_far_from_centroid"] += 1
            rejection_candidates.append({"id": cand["id"], "reason": "too_far"})
            continue

        admitted.append(cand)
        admitted_ids.add(cand["id"])
        step_n += 1

        step_metric = _step_metrics(admitted)
        step_metric["step"] = step_n
        step_metric["added_prim_id"] = cand["id"]
        trajectory.append(step_metric)

    return admitted, trajectory, dict(rejections), rejection_candidates[:30]  # cap


async def main() -> None:
    print("=== Wave 20.14.5D SAGG v2 with Trajectory + PS ===")
    sheets = await _fetch_symbol_sheets()
    print(f"SYMBOL_DOMINANT sheets: {len(sheets)}")

    seeds_by_sheet: dict[str, list[dict]] = {}
    total_seeds = 0
    for sid in sheets:
        s = await _fetch_seeds_for_sheet(sid)
        if s:
            seeds_by_sheet[sid] = s
            total_seeds += len(s)
    print(f"field-gated seeds: {total_seeds}")

    admitted_inserts: list[dict] = []
    used_prims: set[str] = set()
    cls_counts: Counter[str] = Counter()
    rejection_summary: dict[str, int] = defaultdict(int)

    for sheet_id, sheet_seeds in seeds_by_sheet.items():
        print(f"\nsheet={sheet_id[:8]}: {len(sheet_seeds)} seeds")
        for seed in sheet_seeds:
            if seed["id"] in used_prims:
                continue
            cx, cy = _centroid(seed)
            local = await _fetch_local_neighborhood(sheet_id, cx, cy, radius=60.0)
            neighbors = [n for n in local if n["id"] != seed["id"] and n["id"] not in used_prims]

            graph_prims, trajectory, rej, rej_cands = _grow_with_trajectory(seed, neighbors)
            for k, v in rej.items():
                rejection_summary[k] += v

            if len(graph_prims) < _MIN_GRAPH_SIZE:
                continue
            x0c, y0c, x1c, y1c = _bbox_union(graph_prims)
            wc, hc = x1c - x0c, y1c - y0c
            if wc * hc < _MIN_BBOX_AREA or max(wc, hc) < _MIN_TOTAL_DIM:
                continue

            pur = _purity(graph_prims)
            if pur < _PURITY_THRESHOLD:
                continue

            traj_class = _classify_trajectory(trajectory)
            ps = _persistence_score(trajectory)
            cls_counts[traj_class] += 1
            # NO class-based rejection — SPE perturbation bands candidates instead

            # Persist
            types = sorted(p.get("primitive_type") or "p" for p in graph_prims)
            sig = hashlib.sha1("|".join(types).encode()).hexdigest()
            final_gcs, _ = _gcs_components(graph_prims)

            # Strip type_distro from trajectory (large) before persisting — keep curves
            trajectory_compact = [
                {k: v for k, v in s.items() if k != "type_distro"}
                for s in trajectory
            ]
            field_history = [s["field_consistency"] for s in trajectory]
            role_hist = _role_histogram(graph_prims)

            admitted_inserts.append({
                "id": str(uuid_mod.uuid4()),
                "suite_id": SUITE,
                "project_id": PROJ,
                "sheet_id": sheet_id,
                "bbox": {"x": x0c, "y": y0c, "w": wc, "h": hc},
                "primitive_count": len(graph_prims),
                "constituent_motif_ids": [p["id"] for p in graph_prims],
                "composed_signature": sig,
                "plausibility_score": ps,
                "total_instance_count": len(graph_prims),
                "sagg_admitted": True,
                "gcs_score": final_gcs,
                "purity_score": round(pur, 4),
                "growth_trajectory": trajectory_compact,
                "growth_steps": len(trajectory),
                "trajectory_class": traj_class,
                "persistence_score": ps,
                "role_histogram": role_hist,
                "field_transition_history": field_history,
            })
            for p in graph_prims:
                used_prims.add(p["id"])

    # Batch insert
    print(f"\n=== INSERTING {len(admitted_inserts)} ===")
    BATCH = 50
    for i in range(0, len(admitted_inserts), BATCH):
        chunk = admitted_inserts[i:i + BATCH]
        try:
            await supabase_insert_batch_ignore_conflicts(
                "blueprint_composed_symbols", chunk, conflict_target="id"
            )
            print(f"  batch {i//BATCH + 1} ({len(chunk)})")
        except Exception as exc:
            print(f"  batch FAIL: {type(exc).__name__}: {exc}")

    print(f"\n=== TRAJECTORY CLASSIFICATION ===")
    for cls, n in cls_counts.most_common():
        print(f"  {cls}: {n}")
    print(f"\nadmitted (post-class-gate): {len(admitted_inserts)}")
    print(f"rejections: {dict(rejection_summary)}")


if __name__ == "__main__":
    asyncio.run(main())
