"""Wave 20.14.6 — WL Graph Signatures over composed symbols.

Computes 3-iteration Weisfeiler-Lehman graph hash per composed symbol on
Gulfstream. Node features = dominant_role + bucketed confidence.
Edges = endpoint adjacency (<= _ADJ_PT) between constituent primitives.

Output: blueprint_composed_symbols.wl_signature (sha1 hex, 40 chars).

The WL signature is invariant under rotation/stroke/fill since it only
encodes topology + role distribution — not raw geometry. This is the
project-local symbol fingerprint that Wave 20.14.8 will match against
legend symbols via approximate graph edit distance.

Scope: constituent primitives only (matches Wave 20.14.5 PRI pattern).
"""
import asyncio
import hashlib
import math
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.supabase_client import (  # noqa: E402
    supabase_select,
    supabase_update,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"

_ADJ_PT = 4.0          # endpoint adjacency threshold (points)
_WL_ITERATIONS = 3     # standard WL depth — captures 3-hop neighborhood
_CONF_BUCKETS = 4      # quantize confidence into 4 buckets for stable hashing


def _bucket_conf(c: float | None) -> int:
    if c is None:
        return 0
    return min(_CONF_BUCKETS - 1, max(0, int(c * _CONF_BUCKETS)))


def _initial_label(prim: dict) -> str:
    """Initial WL label = role + bucketed confidence + primitive type."""
    role = prim.get("dominant_role") or "none"
    conf = _bucket_conf(prim.get("role_confidence"))
    ptype = prim.get("primitive_type") or "path"
    return f"{role}:{conf}:{ptype[0]}"


def _endpoints(prim: dict) -> list[tuple[float, float]]:
    """Return endpoint coordinates. Lines have 2 endpoints; curves/rects use bbox corners."""
    try:
        x0 = float(prim.get("x0") or 0); y0 = float(prim.get("y0") or 0)
        x1 = float(prim.get("x1") or 0); y1 = float(prim.get("y1") or 0)
    except (TypeError, ValueError):
        return []
    ptype = prim.get("primitive_type")
    if ptype == "line":
        return [(x0, y0), (x1, y1)]
    # For curves/rects/etc, use bbox corners + centroid as "endpoints"
    try:
        cx = float(prim.get("cx") or (x0 + x1) / 2)
        cy = float(prim.get("cy") or (y0 + y1) / 2)
    except (TypeError, ValueError):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [(x0, y0), (x1, y1), (cx, cy)]


def _build_edges(prims: list[dict]) -> list[tuple[int, int]]:
    """Endpoint-adjacency edges (i, j) for prims that share endpoints within _ADJ_PT."""
    eps: list[list[tuple[float, float]]] = [_endpoints(p) for p in prims]
    edges: list[tuple[int, int]] = []
    for i in range(len(prims)):
        for j in range(i + 1, len(prims)):
            touched = False
            for a in eps[i]:
                for b in eps[j]:
                    if math.hypot(a[0] - b[0], a[1] - b[1]) <= _ADJ_PT:
                        touched = True; break
                if touched:
                    break
            if touched:
                edges.append((i, j))
    return edges


def _wl_hash(prims: list[dict]) -> tuple[str, int, int]:
    """Run k-iteration WL on constituent graph. Returns (signature_hex, n_nodes, n_edges)."""
    n = len(prims)
    if n == 0:
        return ("empty", 0, 0)

    labels: list[str] = [_initial_label(p) for p in prims]
    edges = _build_edges(prims)
    adj: dict[int, list[int]] = defaultdict(list)
    for i, j in edges:
        adj[i].append(j); adj[j].append(i)

    for _ in range(_WL_ITERATIONS):
        new_labels: list[str] = []
        for i in range(n):
            neigh_multiset = sorted(labels[k] for k in adj[i])
            blob = labels[i] + "|" + ",".join(neigh_multiset)
            new_labels.append(hashlib.sha1(blob.encode()).hexdigest()[:12])
        labels = new_labels

    # Final signature = sorted hash multiset → stable across node ordering
    final_multiset = sorted(labels)
    sig_input = "|".join(final_multiset) + f"::n={n},e={len(edges)}"
    return (hashlib.sha1(sig_input.encode()).hexdigest(), n, len(edges))


async def main() -> None:
    print("=== Wave 20.14.6 WL Graph Signatures ===")

    # Fetch all composed symbols for project
    composed = await supabase_select(
        "blueprint_composed_symbols",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&order=id",
    ) or []
    print(f"composed symbols to sign: {len(composed)}")
    if not composed:
        return

    # Collect ALL constituent primitive IDs across all composed symbols
    all_ids: set[str] = set()
    for cs in composed:
        for pid in (cs.get("constituent_motif_ids") or []):
            all_ids.add(pid)
    print(f"distinct constituent primitives: {len(all_ids)}")

    # Single bulk fetch via IN-clause chunking (Supabase URL limit ~2000 chars)
    prim_by_id: dict[str, dict] = {}
    id_list = list(all_ids)
    CHUNK = 50
    for i in range(0, len(id_list), CHUNK):
        chunk = id_list[i:i + CHUNK]
        in_filter = ",".join(chunk)
        rows = await supabase_select(
            "blueprint_primitives",
            f"id=in.({in_filter})&suite_id=eq.{SUITE}",
            limit=CHUNK + 5,
        ) or []
        for r in rows:
            prim_by_id[r["id"]] = r

    print(f"fetched primitive rows: {len(prim_by_id)}")

    # Sign each composed symbol
    sig_counter: Counter[str] = Counter()
    updated = 0
    for cs in composed:
        cids = cs.get("constituent_motif_ids") or []
        prims = [prim_by_id[c] for c in cids if c in prim_by_id]
        sig, n_nodes, n_edges = _wl_hash(prims)
        sig_counter[sig] += 1

        try:
            await supabase_update(
                "blueprint_composed_symbols",
                f"id=eq.{cs['id']}&suite_id=eq.{SUITE}",
                {
                    "wl_signature": sig,
                    "wl_node_count": n_nodes,
                    "wl_edge_count": n_edges,
                    "wl_iterations": _WL_ITERATIONS,
                },
            )
            updated += 1
        except Exception as exc:
            print(f"  update fail cs={cs['id'][:8]}: {type(exc).__name__}: {exc}")

    print()
    print(f"=== DONE — {updated}/{len(composed)} composed symbols signed ===")
    print(f"distinct WL signatures: {len(sig_counter)}")
    print()
    print("=== TOP SIGNATURE FAMILIES (instances ≥ 2) ===")
    for sig, n in sig_counter.most_common(20):
        if n >= 2:
            print(f"  {sig[:16]}…  {n} instances")
    singletons = sum(1 for v in sig_counter.values() if v == 1)
    print(f"singletons: {singletons}")


if __name__ == "__main__":
    asyncio.run(main())
