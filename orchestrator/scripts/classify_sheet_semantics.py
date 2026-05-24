"""Wave 20.16 — Sheet Semantic Typing.

Classifies each blueprint sheet into an archetype based on its VTPL primitive
distribution. Output drives per-type composition rules in PCE-v2.

Archetypes (per GPT v9 strategic guidance):
  HATCHING_DOMINANT — massive parallel-line density (texture fields, fills)
  SYMBOL_DOMINANT   — high curve/arc count (fixtures, sprinklers, electrical)
  TEXT_DOMINANT     — paragraph regions cover majority of visible area
  LAYOUT_DOMINANT   — sparse curves, structured rectangles (title blocks, drafting)
  MIXED             — none of the above dominant
"""
import asyncio
import sys
import uuid as uuid_mod
from collections import defaultdict
from statistics import median

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.supabase_client import (  # noqa: E402
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"

# Thresholds tuned from Gulfstream observation
_HATCHING_LINE_COUNT = 50_000           # >this lines = likely hatching-dominant
_HATCHING_LINE_RATIO = 0.85             # line/total ratio above this = texture-dominated
_SYMBOL_CURVE_COUNT = 500               # >this curves = symbol-rich sheet
_SYMBOL_CURVE_RATIO = 0.05              # curve/visible ratio above this = symbol candidate
_TINY_PRIMITIVE_MAX_DIM = 3.0           # <3pt bbox = noise/anchor
_LAYOUT_VISIBLE_RATIO = 0.40            # <this fraction visible = mostly cruft/layout


def _parallel_line_score(lines_with_angles: list[float]) -> float:
    """0..1 — high score = many lines clustered around same angle (hatching signature).

    Bins angles into 10° buckets, returns share of top bucket.
    """
    if len(lines_with_angles) < 10:
        return 0.0
    bins = defaultdict(int)
    for a in lines_with_angles:
        if a is None:
            continue
        b = int(abs(a) // 10) * 10
        bins[b % 180] += 1
    if not bins:
        return 0.0
    top = max(bins.values())
    return top / len(lines_with_angles)


def _classify(features: dict) -> str:
    line_n = features["line_count"]
    curve_n = features["curve_count"]
    visible = features["visible_primitives"]
    line_ratio = line_n / max(visible, 1)
    curve_ratio = curve_n / max(visible, 1)
    visibility = features["visibility_ratio"]
    parallel = features["parallel_line_score"]

    # HATCHING — many lines, dominant angle bin, low curve ratio
    if line_n >= _HATCHING_LINE_COUNT and parallel >= 0.35 and curve_ratio < 0.02:
        return "HATCHING_DOMINANT"
    if line_ratio >= _HATCHING_LINE_RATIO and line_n >= 20_000 and parallel >= 0.40:
        return "HATCHING_DOMINANT"

    # SYMBOL — high curve count or curve ratio
    if curve_n >= _SYMBOL_CURVE_COUNT and curve_ratio >= _SYMBOL_CURVE_RATIO:
        return "SYMBOL_DOMINANT"

    # LAYOUT — low visibility (mostly cruft) AND not symbol/hatching
    if visibility < _LAYOUT_VISIBLE_RATIO and curve_n < 100:
        return "LAYOUT_DOMINANT"

    # TEXT — fallback for sheets with moderate line density + low curves
    if curve_n < 100 and line_n < 20_000:
        return "TEXT_DOMINANT"

    return "MIXED"


async def main() -> None:
    print("=== Wave 20.16 Sheet Semantic Typing — Gulfstream ===")

    sheets = await supabase_select(
        "blueprint_sheets",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}",
    ) or []
    print(f"sheets: {len(sheets)}")

    classifications: list[dict] = []
    type_counts = defaultdict(int)

    for s in sheets:
        sheet_id = s["id"]
        # Fetch primitives for this sheet (cursor pagination)
        prims: list[dict] = []
        last = ""
        while True:
            cur = f"&id=gt.{last}" if last else ""
            page = await supabase_select(
                "blueprint_primitives",
                f"sheet_id=eq.{sheet_id}&suite_id=eq.{SUITE}{cur}&order=id",
                limit=1000,
            )
            if not page:
                break
            prims.extend(page)
            last = page[-1]["id"]
            if len(page) < 1000:
                break

        if not prims:
            continue

        # Compute features
        total = len(prims)
        visible = sum(1 for p in prims if float(p.get("raster_density", 0) or 0) >= 0.05)
        ptype_counts = defaultdict(int)
        tiny = 0
        line_angles = []
        for p in prims:
            ptype = p.get("primitive_type") or "path"
            ptype_counts[ptype] += 1
            try:
                bw = float(p.get("bbox_w") or 0); bh = float(p.get("bbox_h") or 0)
                if max(bw, bh) < _TINY_PRIMITIVE_MAX_DIM:
                    tiny += 1
            except (TypeError, ValueError):
                pass
            if ptype == "line":
                try:
                    a = p.get("angle_deg")
                    if a is not None:
                        line_angles.append(float(a))
                except (TypeError, ValueError):
                    pass

        parallel = _parallel_line_score(line_angles)
        features = {
            "total_primitives": total,
            "visible_primitives": visible,
            "curve_count": ptype_counts.get("curve", 0),
            "line_count": ptype_counts.get("line", 0),
            "rect_count": ptype_counts.get("rect", 0),
            "polyline_count": ptype_counts.get("polyline", 0),
            "tiny_primitive_count": tiny,
            "curve_density": ptype_counts.get("curve", 0) / max(visible, 1),
            "line_density": ptype_counts.get("line", 0) / max(visible, 1),
            "visibility_ratio": visible / max(total, 1),
            "parallel_line_score": parallel,
        }
        semantic_type = _classify(features)
        type_counts[semantic_type] += 1

        classifications.append({
            "id": str(uuid_mod.uuid4()),
            "suite_id": SUITE,
            "project_id": PROJ,
            "sheet_id": sheet_id,
            "semantic_type": semantic_type,
            "total_primitives": total,
            "visible_primitives": visible,
            "curve_count": features["curve_count"],
            "line_count": features["line_count"],
            "rect_count": features["rect_count"],
            "polyline_count": features["polyline_count"],
            "tiny_primitive_count": tiny,
            "curve_density": round(features["curve_density"], 4),
            "line_density": round(features["line_density"], 4),
            "visibility_ratio": round(features["visibility_ratio"], 4),
            "parallel_line_score": round(parallel, 4),
            "classification_basis": features,
        })

    # Persist
    if classifications:
        # Clear prior runs
        from aspire_orchestrator.services.supabase_client import SupabaseClientError
        try:
            await supabase_insert_batch_ignore_conflicts(
                "blueprint_sheet_semantics", classifications, conflict_target="project_id,sheet_id",
            )
        except SupabaseClientError as exc:
            print(f"insert failed: {exc}")

    print()
    print("=== ARCHETYPE DISTRIBUTION ===")
    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<22s} {n} sheets")

    print()
    print("=== TOP SYMBOL-DOMINANT SHEETS (priority for PCE) ===")
    symbol_sheets = [c for c in classifications if c["semantic_type"] == "SYMBOL_DOMINANT"]
    symbol_sheets.sort(key=lambda c: c["curve_count"], reverse=True)
    for c in symbol_sheets[:10]:
        print(f"  sheet={c['sheet_id'][:8]}  curves={c['curve_count']}  visible={c['visible_primitives']}  curve_density={c['curve_density']}")


if __name__ == "__main__":
    asyncio.run(main())
