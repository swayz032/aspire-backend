"""Wave 20.14.2 — PCE-v2: assemble composed symbols from REAL VTPL primitives.

Replaces Wave 20.13 PCE (which consumed Wave 11g-v2 motif bboxes — synthesized
rectangles). PCE-v2 reads blueprint_primitives directly (visible only),
filtered to SYMBOL_DOMINANT sheets, and composes via spatial proximity.

This is the actual breakthrough enabler — composed symbols built from real
visible geometry, not bbox synthesis. Per GPT v9: 'composition rules
should differ per sheet type.'

Algorithm:
  1. Read SYMBOL_DOMINANT sheet IDs from blueprint_sheet_semantics
  2. For each, fetch visible primitives (raster_density >= 0.05, bbox >= 3pt)
  3. Build spatial proximity graph using primitive centroids
  4. Connected components → composed symbol candidates
  5. Plausibility filter (reject linear runs, oversized chains)
  6. Compute composed_signature = sorted hash of constituent primitive type sequence
  7. Persist to blueprint_composed_symbols
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

_PROXIMITY_PT = 20.0          # tighter than Wave 20.13 — real primitives are smaller
_MIN_PRIMITIVE_DIM = 3.0      # filter sub-pixel anchors
_MAX_COMPONENT_PRIMITIVES = 25
_MIN_COMPONENT_PRIMITIVES = 2  # singletons not interesting at primitive level
_MAX_BBOX_ASPECT = 12.0       # > this = linear run / wall / dimension
_VISIBILITY_FLOOR = 0.05


class _UF:
    __slots__ = ("p",)
    def __init__(self, n: int) -> None:
        self.p = list(range(n))
    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


async def _fetch_primitives_for_sheet(sheet_id: str) -> list[dict]:
    """Fetch visible CURVES for one sheet — they're the discriminative anchors.

    Strategy: curves anchor symbols (sprinkler circles, fixture rounds). Use them
    as cluster seeds, then composed_symbol bbox unions naturally include adjacent
    detail lines via PCE proximity logic.
    """
    rows: list[dict] = []
    last_id = ""
    while True:
        cursor = f"&id=gt.{last_id}" if last_id else ""
        page = await supabase_select(
            "blueprint_primitives",
            f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}"
            f"&primitive_type=eq.curve&raster_density=gte.{_VISIBILITY_FLOOR}"
            f"{cursor}&order=id",
            limit=1000,
        )
        if not page:
            break
        rows.extend([p for p in page if max(
            float(p.get("bbox_w") or 0), float(p.get("bbox_h") or 0)
        ) >= _MIN_PRIMITIVE_DIM])
        last_id = page[-1]["id"]
        if len(page) < 1000:
            break
    return rows


async def main() -> None:
    print("=== Wave 20.14.2 PCE-v2 — compose from real VTPL primitives ===")

    # 1. Get SYMBOL_DOMINANT sheet IDs
    sym_rows = await supabase_select(
        "blueprint_sheet_semantics",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&semantic_type=eq.SYMBOL_DOMINANT",
    ) or []
    symbol_sheet_ids = [r["sheet_id"] for r in sym_rows]
    print(f"SYMBOL_DOMINANT sheets: {len(symbol_sheet_ids)}")

    total_primitives = 0
    total_components = 0
    total_composed = 0
    total_rejected = 0
    inserts: list[dict] = []
    rejection_counts: dict[str, int] = defaultdict(int)

    for sheet_id in symbol_sheet_ids:
        prims = await _fetch_primitives_for_sheet(sheet_id)
        if len(prims) < _MIN_COMPONENT_PRIMITIVES:
            continue
        total_primitives += len(prims)

        # Centroids
        centroids: list[tuple[float, float]] = []
        for p in prims:
            try:
                centroids.append((float(p.get("cx") or 0), float(p.get("cy") or 0)))
            except (TypeError, ValueError):
                centroids.append((0.0, 0.0))

        # Sort by x for sweep
        order = sorted(range(len(prims)), key=lambda i: centroids[i][0])
        uf = _UF(len(prims))
        for i_pos, i in enumerate(order):
            cx_i = centroids[i][0]
            for j_pos in range(i_pos + 1, len(prims)):
                j = order[j_pos]
                if centroids[j][0] - cx_i > _PROXIMITY_PT:
                    break
                if math.hypot(
                    centroids[j][0] - cx_i,
                    centroids[j][1] - centroids[i][1],
                ) <= _PROXIMITY_PT:
                    uf.union(i, j)

        # Collect components
        comps: dict[int, list[int]] = defaultdict(list)
        for i in range(len(prims)):
            comps[uf.find(i)].append(i)
        total_components += len(comps)

        for root, member_idxs in comps.items():
            n = len(member_idxs)
            if n < _MIN_COMPONENT_PRIMITIVES:
                total_rejected += 1; rejection_counts["singleton"] += 1; continue
            if n > _MAX_COMPONENT_PRIMITIVES:
                total_rejected += 1; rejection_counts["too_large"] += 1; continue

            # Union bbox
            xs = []; ys = []; types: list[str] = []
            for i in member_idxs:
                p = prims[i]
                try:
                    x0 = float(p.get("x0") or 0); y0 = float(p.get("y0") or 0)
                    x1 = float(p.get("x1") or 0); y1 = float(p.get("y1") or 0)
                    xs.extend([x0, x1]); ys.extend([y0, y1])
                    types.append(p.get("primitive_type") or "path")
                except (TypeError, ValueError):
                    continue
            if not xs:
                continue
            bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
            bw, bh = max(bx1 - bx0, 0.01), max(by1 - by0, 0.01)
            aspect = max(bw, bh) / max(min(bw, bh), 1e-6)
            if aspect > _MAX_BBOX_ASPECT:
                total_rejected += 1; rejection_counts["linear_run"] += 1; continue

            # Composed signature: sorted primitive type sequence hashed
            sig_input = "|".join(sorted(types))
            composed_sig = hashlib.sha1(sig_input.encode()).hexdigest()
            # Plausibility: type diversity reward
            unique_types = len(set(types))
            plausibility = min(1.0, (unique_types / 3.0) * 0.5 + (n / 10.0) * 0.5)

            inserts.append({
                "id": str(uuid_mod.uuid4()),
                "suite_id": SUITE,
                "project_id": PROJ,
                "sheet_id": sheet_id,
                "bbox": {"x": bx0, "y": by0, "w": bw, "h": bh},
                "primitive_count": n,
                "constituent_motif_ids": [prims[i]["id"] for i in member_idxs],
                "composed_signature": composed_sig,
                "plausibility_score": round(plausibility, 4),
                "total_instance_count": n,
            })
            total_composed += 1

        # Flush per sheet
        if inserts:
            try:
                await supabase_insert_batch_ignore_conflicts(
                    "blueprint_composed_symbols", inserts, conflict_target="id",
                )
            except Exception as exc:
                print(f"  insert fail sheet={sheet_id[:8]}: {type(exc).__name__}")
            print(f"  sheet={sheet_id[:8]} prims={len(prims)} composed={len([i for i in inserts])}")
            inserts = []

    print()
    print(f"=== DONE ===")
    print(f"primitives processed: {total_primitives}")
    print(f"components found:     {total_components}")
    print(f"composed persisted:   {total_composed}")
    print(f"rejected:             {total_rejected}")
    print(f"rejection breakdown:  {dict(rejection_counts)}")


if __name__ == "__main__":
    asyncio.run(main())
