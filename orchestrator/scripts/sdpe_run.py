"""Wave 20.14.5F — Semantic Dependency Perturbation Engine (SDPE) per roadmap v14.

Replaces SPE's uniform survival_ratio with damage-weighted scoring.

Pipeline per candidate graph:
  1. Ablation matrix: remove each primitive ONE-AT-A-TIME, measure semantic damage
  2. Per-primitive semantic_weight = aggregate of dependency/topology/role/field deltas
  3. Semantic Core Ratio = count(load-bearing prims) / total
  4. Weighted Perturbation Survival = Σ(survived * weight_of_attacked) / Σ(weight_of_attacked)
  5. Counterfactual assimilation = does graph absorb foreign primitive?
  6. Multiplicative graph_score = weighted_survival
                                * semantic_core_ratio
                                * role_diversity
                                * field_consistency

Bands derived from adaptive bimodal split, not absolute thresholds.

IO-disciplined: in-memory ablations + perturbations, single bulk UPDATE.
"""
import asyncio
import hashlib
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.supabase_client import (  # noqa: E402
    supabase_select,
    supabase_update,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"

_WL_ITERATIONS = 3
_ADJ_PT = 4.0
_MAX_CANDIDATES = 50

# Per-primitive weight components (aggregate to semantic_weight)
_W_DEPENDENCY = 0.35       # graph fragments if removed
_W_ROLE_CENTRALITY = 0.25  # role bridging
_W_ADJ_ENTROPY = 0.20      # adjacency entropy delta
_W_FIELD_TENSION = 0.10    # field-boundary tension
_W_TOPOLOGY = 0.10         # WL signature delta

# Core ratio threshold — calibrated against actual weight ranges (most prims fall 0.10-0.35)
_LOAD_BEARING_FLOOR = 0.15

# Counterfactual injection radius — must be close enough that real symbols could absorb
_INJECT_RADIUS_PT = 3.0


# ============================================================
# Topology primitives (same as SPE — kept identical for sig comparability)
# ============================================================

def _endpoints(p: dict) -> list[tuple[float, float]]:
    try:
        x0 = float(p.get("x0") or 0); y0 = float(p.get("y0") or 0)
        x1 = float(p.get("x1") or 0); y1 = float(p.get("y1") or 0)
    except (TypeError, ValueError):
        return []
    if p.get("primitive_type") == "line":
        return [(x0, y0), (x1, y1)]
    try:
        cx = float(p.get("cx") or (x0 + x1) / 2)
        cy = float(p.get("cy") or (y0 + y1) / 2)
    except (TypeError, ValueError):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [(x0, y0), (x1, y1), (cx, cy)]


def _build_adjacency(prims: list[dict]) -> list[list[int]]:
    n = len(prims)
    if n == 0:
        return []
    eps = [_endpoints(p) for p in prims]
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            touched = False
            for a in eps[i]:
                for b in eps[j]:
                    if math.hypot(a[0] - b[0], a[1] - b[1]) <= _ADJ_PT:
                        touched = True; break
                if touched:
                    break
            if touched:
                adj[i].append(j); adj[j].append(i)
    return adj


def _wl_signature(prims: list[dict]) -> str:
    n = len(prims)
    if n == 0:
        return "empty"
    labels: list[str] = []
    for p in prims:
        role = p.get("dominant_role") or "none"
        try:
            conf = float(p.get("role_confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        bucket = min(3, max(0, int(conf * 4)))
        ptype = (p.get("primitive_type") or "p")[:1]
        labels.append(f"{role}:{bucket}:{ptype}")
    adj = _build_adjacency(prims)
    edges = sum(len(a) for a in adj) // 2
    for _ in range(_WL_ITERATIONS):
        new_labels: list[str] = []
        for i in range(n):
            neigh = sorted(labels[k] for k in adj[i])
            blob = labels[i] + "|" + ",".join(neigh)
            new_labels.append(hashlib.sha1(blob.encode()).hexdigest()[:12])
        labels = new_labels
    final = sorted(labels)
    return hashlib.sha1(("|".join(final) + f"::n={n},e={edges}").encode()).hexdigest()


def _connected_components(adj: list[list[int]]) -> int:
    n = len(adj)
    visited = [False] * n
    count = 0
    for i in range(n):
        if visited[i]:
            continue
        count += 1
        stack = [i]
        while stack:
            u = stack.pop()
            if visited[u]:
                continue
            visited[u] = True
            for v in adj[u]:
                if not visited[v]:
                    stack.append(v)
    return count


def _role_set(prims: list[dict]) -> frozenset:
    return frozenset(p.get("dominant_role") or "none" for p in prims)


def _adjacency_entropy(adj: list[list[int]]) -> float:
    if not adj:
        return 0.0
    degrees = [len(a) for a in adj]
    total = sum(degrees) or 1
    ent = 0.0
    for d in degrees:
        if d > 0:
            pr = d / total
            ent -= pr * math.log2(pr)
    return ent


def _field_consistency(prims: list[dict]) -> float:
    if not prims:
        return 1.0
    c: Counter[str] = Counter()
    for p in prims:
        c[p.get("field_type") or "UNKNOWN_FIELD"] += 1
    return max(c.values()) / len(prims)


def _role_diversity(prims: list[dict]) -> float:
    counts: Counter[str] = Counter()
    for p in prims:
        counts[p.get("dominant_role") or "none"] += 1
    total = sum(counts.values()) or 1
    ent = 0.0
    for c in counts.values():
        if c > 0:
            pr = c / total
            ent -= pr * math.log2(pr)
    return min(1.0, ent / 2.0)


# ============================================================
# Per-primitive ablation matrix → semantic_weight
# ============================================================

def compute_ablation_matrix(prims: list[dict]) -> list[dict]:
    """For each primitive, measure semantic damage caused by its removal."""
    if len(prims) < 2:
        return [{"id": prims[0]["id"], "semantic_weight": 0.0,
                 "dependency_degree": 0, "role_centrality": 0.0,
                 "adj_entropy_delta": 0.0, "field_tension": 0.0,
                 "topology_delta": 0.0}] if prims else []

    base_adj = _build_adjacency(prims)
    base_components = _connected_components(base_adj)
    base_roles = _role_set(prims)
    base_adj_entropy = _adjacency_entropy(base_adj)
    base_field = _field_consistency(prims)
    base_sig = _wl_signature(prims)

    matrix: list[dict] = []
    for i, p in enumerate(prims):
        ablated = [prims[j] for j in range(len(prims)) if j != i]
        if not ablated:
            continue
        abl_adj = _build_adjacency(ablated)
        abl_components = _connected_components(abl_adj)
        abl_roles = _role_set(ablated)
        abl_adj_entropy = _adjacency_entropy(abl_adj)
        abl_field = _field_consistency(ablated)
        abl_sig = _wl_signature(ablated)

        # Dependency: did graph fragment?
        dependency_degree = max(0, abl_components - base_components)

        # Role centrality: were any roles lost?
        lost_roles = base_roles - abl_roles
        role_centrality = len(lost_roles) / max(len(base_roles), 1)

        # Adjacency entropy delta (absolute change)
        adj_entropy_delta = abs(abl_adj_entropy - base_adj_entropy) / max(base_adj_entropy, 1.0)

        # Field tension (field consistency drop)
        field_tension = max(0.0, base_field - abl_field)

        # Topology delta (WL sig changes binary, but normalized)
        topology_delta = 1.0 if abl_sig != base_sig else 0.0

        # Normalize dependency_degree to 0..1 (max sensible: prim count / 2)
        dep_norm = min(1.0, dependency_degree / max(len(prims) / 2, 1.0))

        semantic_weight = (
            _W_DEPENDENCY * dep_norm
            + _W_ROLE_CENTRALITY * role_centrality
            + _W_ADJ_ENTROPY * min(1.0, adj_entropy_delta)
            + _W_FIELD_TENSION * field_tension
            + _W_TOPOLOGY * topology_delta
        )

        matrix.append({
            "id": p["id"],
            "primitive_type": p.get("primitive_type"),
            "dominant_role": p.get("dominant_role"),
            "semantic_weight": round(semantic_weight, 4),
            "dependency_degree": dependency_degree,
            "role_centrality": round(role_centrality, 4),
            "adj_entropy_delta": round(adj_entropy_delta, 4),
            "field_tension": round(field_tension, 4),
            "topology_delta": topology_delta,
        })

    return matrix


def semantic_core_ratio(ablation: list[dict]) -> float:
    if not ablation:
        return 0.0
    core = sum(1 for a in ablation if a.get("semantic_weight", 0) >= _LOAD_BEARING_FLOOR)
    return round(core / len(ablation), 4)


# ============================================================
# Damage-weighted perturbations
# ============================================================
# Each perturbation declares which primitives it attacks (by index or filter).
# Survival weight = sum of semantic_weights of attacked primitives.

def perturb_drop_lowest_density(prims: list[dict]) -> tuple[list[dict], list[str]]:
    if len(prims) <= 1:
        return prims, []
    by_density = sorted(range(len(prims)), key=lambda i: float(prims[i].get("raster_density") or 0))
    drop_idx = by_density[0]
    return [p for i, p in enumerate(prims) if i != drop_idx], [prims[drop_idx]["id"]]


def perturb_suppress_feed_lines(prims: list[dict]) -> tuple[list[dict], list[str]]:
    attacked = [p["id"] for p in prims if p.get("dominant_role") == "feed_line"]
    keep = [p for p in prims if p.get("dominant_role") != "feed_line"]
    return (keep or prims), attacked


def perturb_suppress_supports(prims: list[dict]) -> tuple[list[dict], list[str]]:
    attacked = [p["id"] for p in prims if p.get("dominant_role") == "support"]
    keep = [p for p in prims if p.get("dominant_role") != "support"]
    return (keep or prims), attacked


def perturb_remove_leaf_nodes(prims: list[dict]) -> tuple[list[dict], list[str]]:
    if len(prims) <= 2:
        return prims, []
    adj = _build_adjacency(prims)
    leaf_idxs = [i for i in range(len(prims)) if len(adj[i]) <= 1]
    if not leaf_idxs or len(leaf_idxs) == len(prims):
        return prims, []
    attacked = [prims[i]["id"] for i in leaf_idxs]
    kept = [p for i, p in enumerate(prims) if i not in set(leaf_idxs)]
    return kept, attacked


def perturb_jitter_endpoints(prims: list[dict], delta: float) -> tuple[list[dict], list[str]]:
    out = []
    for p in prims:
        pp = dict(p)
        for k in ("x0", "y0", "x1", "y1", "cx", "cy"):
            v = pp.get(k)
            if v is not None:
                try:
                    pp[k] = float(v) + delta
                except (TypeError, ValueError):
                    pass
        out.append(pp)
    return out, [p["id"] for p in prims]  # affects all


def perturb_force_isolation(prims: list[dict]) -> tuple[list[dict], list[str]]:
    if len(prims) <= 1:
        return prims, []
    out = []
    for i, p in enumerate(prims):
        pp = dict(p)
        shift = i * 50.0
        for k in ("x0", "x1", "cx"):
            v = pp.get(k)
            if v is not None:
                try:
                    pp[k] = float(v) + shift
                except (TypeError, ValueError):
                    pass
        out.append(pp)
    return out, [p["id"] for p in prims]


def perturb_swap_role_label(prims: list[dict]) -> tuple[list[dict], list[str]]:
    if len(prims) < 2:
        return prims, []
    out = [dict(p) for p in prims]
    out[0]["dominant_role"], out[1]["dominant_role"] = (
        out[1].get("dominant_role"), out[0].get("dominant_role"),
    )
    return out, [out[0]["id"], out[1]["id"]]


def perturb_drop_first_connector(prims: list[dict]) -> tuple[list[dict], list[str]]:
    for i, p in enumerate(prims):
        if p.get("primitive_type") != "curve":
            return (prims[:i] + prims[i + 1:]), [p["id"]]
    return prims, []


PERTURBATIONS = [
    ("drop_lowest_density", lambda p: perturb_drop_lowest_density(p)),
    ("suppress_feed_lines", lambda p: perturb_suppress_feed_lines(p)),
    ("suppress_supports", lambda p: perturb_suppress_supports(p)),
    ("remove_leaf_nodes", lambda p: perturb_remove_leaf_nodes(p)),
    ("jitter_positive", lambda p: perturb_jitter_endpoints(p, 1.5)),
    ("jitter_negative", lambda p: perturb_jitter_endpoints(p, -1.5)),
    ("force_isolation", lambda p: perturb_force_isolation(p)),
    ("swap_role_label", lambda p: perturb_swap_role_label(p)),
    ("drop_first_connector", lambda p: perturb_drop_first_connector(p)),
]


# ============================================================
# Counterfactual Neighbor Assimilation
# ============================================================

def counterfactual_assimilation(prims: list[dict]) -> float:
    """Inject foreign primitive far enough that real symbols won't adjacency-touch
    (boundary preserved) but close enough that noise graphs will absorb.

    Soft test: compare ROLE DISTRIBUTION instead of exact WL hash.
    Real symbols: role distribution shifts proportionally to graph size (small).
    Noise fragments: role distribution dominated by injected primitive."""
    if not prims:
        return 0.0
    base_roles: Counter[str] = Counter()
    for p in prims:
        base_roles[p.get("dominant_role") or "none"] += 1
    base_total = sum(base_roles.values()) or 1

    # Place foreign primitive far from any existing primitive (boundary test)
    cx_g = sum(float(p.get("cx") or 0) for p in prims) / len(prims)
    cy_g = sum(float(p.get("cy") or 0) for p in prims) / len(prims)
    foreign = {
        "id": "counterfactual_neighbor",
        "primitive_type": "line",
        "dominant_role": "connector",
        "role_confidence": 0.5,
        "raster_density": 0.5,
        "x0": cx_g + 30, "y0": cy_g + 30,
        "x1": cx_g + 36, "y1": cy_g + 36,
        "cx": cx_g + 33, "cy": cy_g + 33,
        "bbox_w": 6, "bbox_h": 6,
        "field_type": prims[0].get("field_type"),
    }
    perturbed = prims + [foreign]
    pert_roles: Counter[str] = Counter()
    for p in perturbed:
        pert_roles[p.get("dominant_role") or "none"] += 1
    pert_total = sum(pert_roles.values()) or 1

    # Role distribution L1 distance, normalized
    keys = set(base_roles) | set(pert_roles)
    l1 = sum(abs(base_roles.get(k, 0) / base_total - pert_roles.get(k, 0) / pert_total)
             for k in keys)
    # Resistance = 1.0 means distribution barely changed (graph is big enough to absorb 1 prim trivially)
    # = 0.0 means distribution shifted radically (graph was tiny so 1 prim dominates)
    return round(max(0.0, 1.0 - l1 / 2.0), 4)


# ============================================================
# SDPE evaluation per graph
# ============================================================

def evaluate_sdpe(prims: list[dict]) -> dict:
    if not prims:
        return {}

    # 1. Ablation matrix
    ablation = compute_ablation_matrix(prims)
    weight_by_id = {a["id"]: a["semantic_weight"] for a in ablation}
    core_ratio = semantic_core_ratio(ablation)

    base_sig = _wl_signature(prims)

    # 2. Weighted perturbation survival
    total_weight = 0.0
    survived_weight = 0.0
    variant_results = []

    for name, op in PERTURBATIONS:
        try:
            variant, attacked_ids = op(prims)
        except Exception as exc:
            variant_results.append({"name": name, "error": type(exc).__name__})
            continue

        # Sum semantic weight of attacked primitives
        # Jitter / isolation affect ALL — use mean weight; ablation ops use specific weights
        if not attacked_ids:
            attack_weight = 0.0  # no-op perturbation contributes nothing
        elif len(attacked_ids) == len(prims):
            attack_weight = sum(weight_by_id.get(pid, 0) for pid in attacked_ids) / len(prims)
        else:
            attack_weight = sum(weight_by_id.get(pid, 0) for pid in attacked_ids)

        survived = _wl_signature(variant) == base_sig

        total_weight += attack_weight
        if survived:
            survived_weight += attack_weight

        variant_results.append({
            "name": name,
            "survived": survived,
            "attack_weight": round(attack_weight, 4),
            "attacked_count": len(attacked_ids),
        })

    weighted_survival = (survived_weight / total_weight) if total_weight > 0 else 0.5

    # 3. Counterfactual neighbor assimilation
    assim = counterfactual_assimilation(prims)

    # 4. Other components
    role_div = _role_diversity(prims)
    field_cons = _field_consistency(prims)

    # 5. Multiplicative graph_score with DAMPED weighted_survival.
    # ^0.3 damping prevents over-penalizing semantic fragility (real symbols are fragile).
    # Role-uniformity penalty: graphs with role_diversity ~0 (all same role) collapse.
    wsurv_damped = max(0.05, weighted_survival) ** 0.3
    role_factor = role_div + 0.05  # never zero
    graph_score = round(
        wsurv_damped * core_ratio * role_factor * field_cons * (0.3 + 0.7 * assim),
        4
    )

    return {
        "ablation_matrix": ablation,
        "semantic_core_ratio": core_ratio,
        "weighted_survival": round(weighted_survival, 4),
        "assimilation_resistance": round(assim, 4),
        "sdpe_graph_score": graph_score,
        "perturbation_results": variant_results,
        "role_diversity": round(role_div, 4),
        "field_consistency": round(field_cons, 4),
    }


# ============================================================
# Adaptive bimodal banding
# ============================================================

def adaptive_band(score: float, all_scores: list[float],
                  n_prims: int, core_ratio: float) -> str:
    """Adaptive banding with structural complexity gate.
    Tiny graphs (n<5) cannot reach PERSISTENT_SYMBOL no matter their score,
    because per-primitive importance is inflated in small graphs."""
    if score < 0.01:
        return "DEGENERATE_ANNOTATION"

    # Structural complexity gate — guards against tiny-graph score inflation
    if n_prims < 5:
        # Tiny graphs max out at FIELD_STABLE_FRAGMENT
        if score >= 0.05 and core_ratio >= 0.30:
            return "FIELD_STABLE_FRAGMENT"
        return "DEGENERATE_ANNOTATION"

    # Tiny "complex" graphs (n=5-6): still can't be PERSISTENT
    # unless core_ratio < 0.80 (meaning NOT every primitive is load-bearing — real diversity)
    if n_prims < 7 and core_ratio >= 0.80:
        return "FIELD_STABLE_FRAGMENT"

    if len(all_scores) < 4:
        if score < 0.05:
            return "DEGENERATE_ANNOTATION"
        if score < 0.10:
            return "FIELD_STABLE_FRAGMENT"
        return "PERSISTENT_SYMBOL"

    non_zero = sorted([s for s in all_scores if s >= 0.01], reverse=True)
    if not non_zero:
        return "DEGENERATE_ANNOTATION"
    if len(non_zero) >= 4:
        p25 = non_zero[len(non_zero) // 4]
        p75 = non_zero[min(len(non_zero) - 1, 3 * len(non_zero) // 4)]
    else:
        p25 = non_zero[0]
        p75 = non_zero[-1]
    if score >= p25:
        return "PERSISTENT_SYMBOL"
    if score >= p75:
        return "FIELD_STABLE_FRAGMENT"
    return "DEGENERATE_ANNOTATION"


# ============================================================
# Main
# ============================================================

async def main() -> None:
    print("=== Wave 20.14.5F SDPE ===")
    candidates = await supabase_select(
        "blueprint_composed_symbols",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&sagg_admitted=eq.true&order=primitive_count.desc",
        limit=_MAX_CANDIDATES,
    ) or []
    print(f"candidates: {len(candidates)}")
    if not candidates:
        return

    all_pids: set[str] = set()
    for c in candidates:
        for pid in (c.get("constituent_motif_ids") or []):
            all_pids.add(pid)
    print(f"distinct primitives: {len(all_pids)}")

    prim_by_id: dict[str, dict] = {}
    id_list = list(all_pids)
    for i in range(0, len(id_list), 50):
        chunk = id_list[i:i + 50]
        rows = await supabase_select(
            "blueprint_primitives",
            f"id=in.({','.join(chunk)})&suite_id=eq.{SUITE}",
            limit=55,
        ) or []
        for r in rows:
            prim_by_id[r["id"]] = r

    # Evaluate each candidate
    results: list[tuple[str, dict]] = []
    for c in candidates:
        prims = [prim_by_id[pid] for pid in (c.get("constituent_motif_ids") or [])
                 if pid in prim_by_id]
        if not prims:
            continue
        r = evaluate_sdpe(prims)
        results.append((c["id"], r))

    # Adaptive banding from full distribution
    all_scores = [r["sdpe_graph_score"] for _, r in results]
    print(f"\n=== SDPE SCORES (sorted) ===")
    for gid, r in sorted(results, key=lambda kv: -kv[1]["sdpe_graph_score"]):
        band = adaptive_band(r["sdpe_graph_score"], all_scores, len(r["ablation_matrix"]), r["semantic_core_ratio"])
        print(f"  {gid[:8]}  score={r['sdpe_graph_score']:.4f}  "
              f"core={r['semantic_core_ratio']:.2f}  "
              f"wsurv={r['weighted_survival']:.2f}  "
              f"assim={r['assimilation_resistance']:.2f}  "
              f"roleDiv={r['role_diversity']:.2f}  "
              f"band={band}")

    # Single-pass bulk UPDATE
    print(f"\nbulk UPDATE {len(results)} rows...")
    for gid, r in results:
        band = adaptive_band(r["sdpe_graph_score"], all_scores, len(r["ablation_matrix"]), r["semantic_core_ratio"])
        payload = {
            "ablation_matrix": r["ablation_matrix"],
            "semantic_core_ratio": r["semantic_core_ratio"],
            "weighted_survival": r["weighted_survival"],
            "assimilation_resistance": r["assimilation_resistance"],
            "sdpe_graph_score": r["sdpe_graph_score"],
            "persistence_band": band,
        }
        try:
            await supabase_update(
                "blueprint_composed_symbols",
                f"id=eq.{gid}&suite_id=eq.{SUITE}",
                payload,
            )
        except Exception as exc:
            print(f"  update fail {gid[:8]}: {type(exc).__name__}: {exc}")

    band_counts: Counter[str] = Counter()
    for _, r in results:
        band_counts[adaptive_band(r["sdpe_graph_score"], all_scores, len(r["ablation_matrix"]), r["semantic_core_ratio"])] += 1
    print(f"\n=== FINAL BAND DISTRIBUTION ===")
    for b, n in band_counts.most_common():
        print(f"  {b}: {n}")


if __name__ == "__main__":
    asyncio.run(main())
