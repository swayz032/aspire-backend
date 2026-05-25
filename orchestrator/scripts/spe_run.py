"""Wave 20.14.5E — Semantic Persistence Engine (SPE) per roadmap v13.

For each SAGG-admitted candidate graph:
  1. Fetch constituent primitives ONCE
  2. Run 8 semantically-targeted perturbations in-memory
  3. Re-compute WL signature + role histogram + field consistency + adjacency per variant
  4. Aggregate stability metrics
  5. Assign persistence_band + final_score
  6. Single bulk UPDATE

IO-disciplined: no temp tables, no per-perturbation DB queries, batched writes.
Cap at top-50 candidates per project (we have 8, no risk).
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
_MAX_CANDIDATES = 50          # cap for IO discipline
_EARLY_COLLAPSE_BREAK_COUNT = 3  # if first 3 perturbations all break signature, halt as UNSTABLE


# ============================================================
# In-memory WL signature (mirrors SQL function but no DB calls)
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


def _build_adjacency(prims: list[dict]) -> tuple[list[int], list[list[int]]]:
    n = len(prims)
    if n == 0:
        return ([], [])
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
    return (list(range(n)), adj)


def _wl_signature(prims: list[dict]) -> tuple[str, int, int]:
    n = len(prims)
    if n == 0:
        return ("empty", 0, 0)

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

    _, adj = _build_adjacency(prims)
    edges = sum(len(adj[i]) for i in range(n)) // 2

    for _ in range(_WL_ITERATIONS):
        new_labels: list[str] = []
        for i in range(n):
            neigh = sorted(labels[k] for k in adj[i])
            blob = labels[i] + "|" + ",".join(neigh)
            new_labels.append(hashlib.sha1(blob.encode()).hexdigest()[:12])
        labels = new_labels

    final = sorted(labels)
    sig = hashlib.sha1(("|".join(final) + f"::n={n},e={edges}").encode()).hexdigest()
    return (sig, n, edges)


def _role_histogram(prims: list[dict]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for p in prims:
        c[p.get("dominant_role") or "none"] += 1
    return dict(c)


def _field_consistency(prims: list[dict]) -> float:
    if not prims:
        return 1.0
    c: Counter[str] = Counter()
    for p in prims:
        c[p.get("field_type") or "UNKNOWN_FIELD"] += 1
    return max(c.values()) / len(prims)


def _adjacency_signature(prims: list[dict]) -> frozenset:
    """Set of (lo_idx, hi_idx) adjacency edges — comparable across perturbations."""
    _, adj = _build_adjacency(prims)
    edges = set()
    for i in range(len(prims)):
        for j in adj[i]:
            if i < j:
                edges.add((prims[i]["id"], prims[j]["id"]))
    return frozenset(edges)


# ============================================================
# 8 semantically-targeted perturbations
# ============================================================

def perturb_drop_lowest_density(prims: list[dict]) -> list[dict]:
    if len(prims) <= 1:
        return prims
    by_density = sorted(prims, key=lambda p: float(p.get("raster_density") or 0))
    return by_density[1:]  # drop lowest


def perturb_suppress_feed_lines(prims: list[dict]) -> list[dict]:
    return [p for p in prims if p.get("dominant_role") != "feed_line"] or prims


def perturb_suppress_supports(prims: list[dict]) -> list[dict]:
    return [p for p in prims if p.get("dominant_role") != "support"] or prims


def perturb_remove_leaf_nodes(prims: list[dict]) -> list[dict]:
    """Remove primitives with only 1 adjacency edge (peripheral)."""
    if len(prims) <= 2:
        return prims
    _, adj = _build_adjacency(prims)
    keep_idxs = [i for i in range(len(prims)) if len(adj[i]) > 1]
    if not keep_idxs:
        return prims
    return [prims[i] for i in keep_idxs]


def perturb_anchor_swap(prims: list[dict]) -> list[dict]:
    """Move first curve to end (perturbs seed-priority assumption)."""
    if len(prims) <= 1:
        return prims
    first_curve = next((p for p in prims if p.get("primitive_type") == "curve"), None)
    if first_curve is None:
        return prims
    rest = [p for p in prims if p["id"] != first_curve["id"]]
    return rest + [first_curve]


def perturb_jitter_endpoints_positive(prims: list[dict]) -> list[dict]:
    """Apply small positive coordinate jitter to test adjacency robustness."""
    jittered = []
    for p in prims:
        pp = dict(p)
        for key in ("x0", "y0", "x1", "y1", "cx", "cy"):
            v = pp.get(key)
            if v is not None:
                try:
                    pp[key] = float(v) + 1.5
                except (TypeError, ValueError):
                    pass
        jittered.append(pp)
    return jittered


def perturb_jitter_endpoints_negative(prims: list[dict]) -> list[dict]:
    jittered = []
    for p in prims:
        pp = dict(p)
        for key in ("x0", "y0", "x1", "y1", "cx", "cy"):
            v = pp.get(key)
            if v is not None:
                try:
                    pp[key] = float(v) - 1.5
                except (TypeError, ValueError):
                    pass
        jittered.append(pp)
    return jittered


def perturb_drop_first_non_anchor(prims: list[dict]) -> list[dict]:
    """Remove the first non-curve primitive (tests connector dependency)."""
    if len(prims) <= 1:
        return prims
    for i, p in enumerate(prims):
        if p.get("primitive_type") != "curve":
            return prims[:i] + prims[i + 1:]
    return prims  # all curves, nothing to drop


def perturb_swap_role_label(prims: list[dict]) -> list[dict]:
    """Swap roles of first 2 primitives — tests role-bound topology stability."""
    if len(prims) < 2:
        return prims
    out = [dict(p) for p in prims]
    out[0]["dominant_role"], out[1]["dominant_role"] = (
        out[1].get("dominant_role"), out[0].get("dominant_role"),
    )
    return out


def perturb_permute_endpoints(prims: list[dict]) -> list[dict]:
    """Swap x0/x1 and y0/y1 on first primitive — tests endpoint-order semantic invariance."""
    if not prims:
        return prims
    out = [dict(p) for p in prims]
    p0 = out[0]
    p0["x0"], p0["x1"] = p0.get("x1"), p0.get("x0")
    p0["y0"], p0["y1"] = p0.get("y1"), p0.get("y0")
    return out


def perturb_neighbor_injection(prims: list[dict]) -> list[dict]:
    """Inject a synthetic line near the graph centroid.
    Real symbols stay structurally distinct (new prim is foreign).
    Placeholders absorb anything = signature changes radically."""
    if not prims:
        return prims
    cx_g = sum(float(p.get("cx") or 0) for p in prims) / len(prims)
    cy_g = sum(float(p.get("cy") or 0) for p in prims) / len(prims)
    fake = {
        "id": "synthetic_injection",
        "primitive_type": "line",
        "dominant_role": "connector",
        "role_confidence": 0.5,
        "raster_density": 0.5,
        "x0": cx_g + 1, "y0": cy_g + 1,
        "x1": cx_g + 6, "y1": cy_g + 6,
        "cx": cx_g + 3.5, "cy": cy_g + 3.5,
        "bbox_w": 5, "bbox_h": 5,
        "field_type": prims[0].get("field_type"),
    }
    return prims + [fake]


def perturb_force_isolation(prims: list[dict]) -> list[dict]:
    """Push primitives apart by 50pt so adjacency breaks. Real symbols depend on
    adjacency for identity (signature collapses). Independent placeholders don't change."""
    if len(prims) <= 1:
        return prims
    out = []
    for i, p in enumerate(prims):
        pp = dict(p)
        shift = i * 50.0  # progressive displacement
        for k in ("x0", "x1", "cx"):
            v = pp.get(k)
            if v is not None:
                try:
                    pp[k] = float(v) + shift
                except (TypeError, ValueError):
                    pass
        out.append(pp)
    return out


PERTURBATIONS = [
    ("drop_lowest_density", perturb_drop_lowest_density),
    ("suppress_feed_lines", perturb_suppress_feed_lines),
    ("suppress_supports", perturb_suppress_supports),
    ("remove_leaf_nodes", perturb_remove_leaf_nodes),
    ("anchor_swap", perturb_anchor_swap),
    ("jitter_positive", perturb_jitter_endpoints_positive),
    ("jitter_negative", perturb_jitter_endpoints_negative),
    ("drop_first_connector", perturb_drop_first_non_anchor),
    ("swap_role_label", perturb_swap_role_label),
    ("permute_endpoints", perturb_permute_endpoints),
    ("neighbor_injection", perturb_neighbor_injection),
    ("force_isolation", perturb_force_isolation),
]


# ============================================================
# Persistence band classification + FinalScore
# ============================================================

def _classify_band(matching_sigs: int, total: int,
                   role_stability: float, field_stability: float,
                   adjacency_stability: float,
                   n_primitives: int, role_diversity: float) -> str:
    """Calibrated for 12-perturbation suite. PERSISTENT_SYMBOL requires:
      - survival ≥50%
      - structural integrity (adjacency)
      - field consistency
      - STRUCTURAL COMPLEXITY (n>=5 AND role diversity > 0.30) — guards against
        tiny graphs that score "persistent" only because they're invariant to ops."""
    survival_ratio = matching_sigs / total if total else 0
    role_flat = role_stability > 0.95

    if survival_ratio < 0.20:
        return "UNSTABLE_NOISE"

    # Structural complexity gate — tiny graphs with no role diversity cannot be PERSISTENT_SYMBOL
    has_complexity = (n_primitives >= 5) and (role_diversity >= 0.30)

    if (survival_ratio >= 0.50 and adjacency_stability >= 0.60
        and field_stability >= 0.70 and has_complexity):
        return "PERSISTENT_SYMBOL"
    if survival_ratio >= 0.30 and field_stability >= 0.70 and n_primitives >= 5:
        return "FIELD_STABLE_FRAGMENT"
    if role_flat and adjacency_stability < 0.30:
        return "TEXTURAL_STRUCTURE"
    return "DEGENERATE_ANNOTATION"


def _final_score(persistence: float, field_stability: float, role_diversity: float,
                 graph_recurrence: float, visibility_density: float) -> float:
    return round(
        0.30 * persistence
        + 0.25 * field_stability
        + 0.20 * role_diversity
        + 0.15 * graph_recurrence
        + 0.10 * visibility_density,
        4
    )


def _role_diversity(role_hist: dict[str, int]) -> float:
    """Shannon-normalized over role distribution. More diverse = higher."""
    total = sum(role_hist.values()) or 1
    ent = 0.0
    for c in role_hist.values():
        if c > 0:
            pr = c / total
            ent -= pr * math.log2(pr)
    # Max possible entropy with 4 roles: log2(4) = 2
    return min(1.0, ent / 2.0)


# ============================================================
# Per-graph evaluation
# ============================================================

def evaluate_persistence(graph_row: dict, primitives: list[dict],
                         graph_recurrence_table: dict[str, int]) -> dict:
    """Run all 8 perturbations on this graph, return result dict."""
    if not primitives:
        return {}

    base_sig, base_n, base_e = _wl_signature(primitives)
    base_roles = _role_histogram(primitives)
    base_field = _field_consistency(primitives)
    base_adj = _adjacency_signature(primitives)

    per_variant_results = []
    matching_sigs = 0
    role_l1_distances: list[float] = []
    field_consistencies: list[float] = [base_field]
    adj_jaccards: list[float] = []
    consecutive_breaks = 0
    early_exit = False

    for name, op in PERTURBATIONS:
        if early_exit:
            per_variant_results.append({"name": name, "skipped": "early_exit"})
            continue
        try:
            variant = op(primitives)
            v_sig, v_n, v_e = _wl_signature(variant)
            v_roles = _role_histogram(variant)
            v_field = _field_consistency(variant)
            v_adj = _adjacency_signature(variant)
        except Exception as exc:
            per_variant_results.append({"name": name, "error": type(exc).__name__})
            consecutive_breaks += 1
            if consecutive_breaks >= _EARLY_COLLAPSE_BREAK_COUNT:
                early_exit = True
            continue

        sig_matches = (v_sig == base_sig)
        if sig_matches:
            matching_sigs += 1
            consecutive_breaks = 0
        else:
            consecutive_breaks += 1

        # Role L1 distance (normalized)
        all_roles = set(base_roles) | set(v_roles)
        base_total = sum(base_roles.values()) or 1
        v_total = sum(v_roles.values()) or 1
        l1 = sum(abs(base_roles.get(r, 0) / base_total - v_roles.get(r, 0) / v_total)
                 for r in all_roles)
        role_l1_distances.append(l1 / 2.0)  # normalize to 0..1
        field_consistencies.append(v_field)

        # Adjacency Jaccard
        union = base_adj | v_adj
        inter = base_adj & v_adj
        jacc = len(inter) / max(len(union), 1)
        adj_jaccards.append(jacc)

        per_variant_results.append({
            "name": name,
            "wl_sig_match": sig_matches,
            "wl_sig": v_sig[:12],
            "n_nodes": v_n, "n_edges": v_e,
            "role_l1": round(l1 / 2.0, 4),
            "field_consistency": round(v_field, 4),
            "adj_jaccard": round(jacc, 4),
        })

        if early_exit:
            break

    total_ran = sum(1 for r in per_variant_results if "skipped" not in r and "error" not in r)
    signature_stability = matching_sigs / max(total_ran, 1)
    role_stability = max(0.0, 1.0 - sum(role_l1_distances) / max(len(role_l1_distances), 1))
    field_stability = min(field_consistencies) if field_consistencies else 0
    adjacency_stability = sum(adj_jaccards) / max(len(adj_jaccards), 1) if adj_jaccards else 0

    persistence = (signature_stability + role_stability + adjacency_stability) / 3.0

    # FinalScore inputs
    role_div = _role_diversity(base_roles)

    band = _classify_band(matching_sigs, len(PERTURBATIONS),
                          role_stability, field_stability, adjacency_stability,
                          len(primitives), role_div)
    graph_recurrence = min(1.0, (graph_recurrence_table.get(base_sig, 1) - 1) / 5.0)
    avg_density = sum(float(p.get("raster_density") or 0) for p in primitives) / len(primitives)
    visibility_density = min(1.0, avg_density / 0.5)

    final = _final_score(persistence, field_stability, role_div, graph_recurrence, visibility_density)

    return {
        "persistence_band": band,
        "perturbation_results": per_variant_results,
        "signature_stability": round(signature_stability, 4),
        "role_stability": round(role_stability, 4),
        "adjacency_stability": round(adjacency_stability, 4),
        "final_score": final,
        "base_wl_signature": base_sig,
    }


# ============================================================
# Main driver
# ============================================================

async def main() -> None:
    print("=== Wave 20.14.5E Semantic Persistence Engine ===")

    # 1. Fetch SAGG-admitted candidates
    candidates = await supabase_select(
        "blueprint_composed_symbols",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&sagg_admitted=eq.true&order=primitive_count.desc",
        limit=_MAX_CANDIDATES,
    ) or []
    print(f"candidates to perturb: {len(candidates)}")
    if not candidates:
        return

    # 2. Single bulk fetch of all distinct constituent primitive IDs
    all_pids: set[str] = set()
    for c in candidates:
        for pid in (c.get("constituent_motif_ids") or []):
            all_pids.add(pid)
    print(f"distinct primitives: {len(all_pids)}")

    prim_by_id: dict[str, dict] = {}
    id_list = list(all_pids)
    CHUNK = 50
    for i in range(0, len(id_list), CHUNK):
        chunk = id_list[i:i + CHUNK]
        rows = await supabase_select(
            "blueprint_primitives",
            f"id=in.({','.join(chunk)})&suite_id=eq.{SUITE}",
            limit=CHUNK + 5,
        ) or []
        for r in rows:
            prim_by_id[r["id"]] = r
    print(f"fetched primitives: {len(prim_by_id)}")

    # 3. Pre-compute graph_recurrence table via WL signatures of all candidates
    sig_counter: Counter[str] = Counter()
    for c in candidates:
        prims = [prim_by_id[pid] for pid in (c.get("constituent_motif_ids") or [])
                 if pid in prim_by_id]
        sig, _, _ = _wl_signature(prims)
        sig_counter[sig] += 1

    # 4. Evaluate each candidate
    band_counts: Counter[str] = Counter()
    update_payloads = []
    now = datetime.now(timezone.utc).isoformat()

    for c in candidates:
        prims = [prim_by_id[pid] for pid in (c.get("constituent_motif_ids") or [])
                 if pid in prim_by_id]
        if not prims:
            continue
        result = evaluate_persistence(c, prims, sig_counter)
        if not result:
            continue
        band_counts[result["persistence_band"]] += 1

        payload = {
            "persistence_band": result["persistence_band"],
            "perturbation_results": result["perturbation_results"],
            "signature_stability": result["signature_stability"],
            "role_stability": result["role_stability"],
            "adjacency_stability": result["adjacency_stability"],
            "final_score": result["final_score"],
            "perturbation_run_at": now,
        }
        update_payloads.append((c["id"], payload))

    # 5. Single-pass bulk UPDATE
    print(f"\nbulk UPDATE {len(update_payloads)} rows...")
    for gid, payload in update_payloads:
        try:
            await supabase_update(
                "blueprint_composed_symbols",
                f"id=eq.{gid}&suite_id=eq.{SUITE}",
                payload,
            )
        except Exception as exc:
            print(f"  update fail {gid[:8]}: {type(exc).__name__}: {exc}")

    print(f"\n=== PERSISTENCE BAND DISTRIBUTION ===")
    for band, n in band_counts.most_common():
        print(f"  {band}: {n}")

    print(f"\n=== TOP BY FINAL SCORE ===")
    sorted_updates = sorted(update_payloads, key=lambda kv: -kv[1]["final_score"])
    for gid, payload in sorted_updates[:10]:
        print(f"  {gid[:8]} band={payload['persistence_band']:<22s} "
              f"score={payload['final_score']:.3f} "
              f"sig_stab={payload['signature_stability']:.2f} "
              f"role_stab={payload['role_stability']:.2f} "
              f"adj_stab={payload['adjacency_stability']:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
