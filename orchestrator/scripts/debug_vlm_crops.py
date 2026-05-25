"""Visual debug — save the actual crops VLM is being shown.

Takes the top 3 UNKNOWN families by instance count, fetches a representative
sheet PNG, applies the SAME crop transformation as vlm_symbol_verifier, and
writes the output to C:\\Users\\tonio\\Pictures\\wave-20-debug\\ for visual
inspection.

Usage:
    railway run python orchestrator/scripts/debug_vlm_crops.py
"""
import asyncio
import os
import sys
from collections import defaultdict

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.sheet_image_storage import (  # noqa: E402
    download_sheet_image,
)
from aspire_orchestrator.services.blueprint.terminal_resolver import STATE_UNKNOWN_REVIEW  # noqa: E402
from aspire_orchestrator.services.blueprint.vlm_symbol_verifier import _crop_png  # noqa: E402
from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
OUT_DIR = r"C:\Users\tonio\Pictures\wave-20-debug"


async def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1. Get UNKNOWN families
    rows: list[dict] = []
    last = ""
    while True:
        cur = f"&id=gt.{last}" if last else ""
        page = await supabase_select(
            "blueprint_resolved_symbols",
            filters=(
                f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}"
                f"&terminal_state=eq.{STATE_UNKNOWN_REVIEW}{cur}&order=id"
            ),
            limit=1000,
        )
        if not page:
            break
        rows.extend(page); last = page[-1]["id"]
        if len(page) < 1000:
            break

    families: dict[str, list[dict]] = defaultdict(list)
    family_inst: dict[str, int] = defaultdict(int)
    for r in rows:
        mh = r.get("motif_hash") or "_"
        families[mh].append(r)
        b = r.get("bbox") or {}
        try:
            family_inst[mh] += int(b.get("instance_count", 1))
        except Exception:
            family_inst[mh] += 1

    top_families = sorted(family_inst.items(), key=lambda kv: kv[1], reverse=True)[:3]
    print(f"Top 3 UNKNOWN families by instances:")
    for mh, n in top_families:
        print(f"  {mh[:12]} inst={n}")

    # 2. For each, crop a representative motif
    for mh, total_inst in top_families:
        rep = families[mh][0]
        sheet_id = rep.get("sheet_id")
        bbox = rep.get("bbox")
        if not sheet_id or not bbox:
            continue
        print(f"\nFamily {mh[:12]}:")
        print(f"  sheet_id={sheet_id[:8]}")
        print(f"  bbox={bbox}")
        try:
            png = await download_sheet_image(
                suite_id=SUITE, project_id=PROJ, sheet_id=sheet_id, correlation_id="debug",
            )
            if not png:
                print(f"  SHEET PNG MISSING for {sheet_id[:8]}")
                continue
            print(f"  sheet_png_size_bytes={len(png)}")
            crop = _crop_png(png, bbox)
            if not crop:
                print(f"  CROP RETURNED None (bbox shape invalid)")
                continue
            out_path = os.path.join(OUT_DIR, f"motif_{mh[:12]}.png")
            with open(out_path, "wb") as f:
                f.write(crop)
            print(f"  saved motif crop: {out_path} ({len(crop)} bytes)")
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")

    # 3. Also crop one legend candidate
    legend_rows = await supabase_select(
        "blueprint_project_legends",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&label=neq.null",
        limit=5,
    )
    print(f"\nLegend rows found: {len(legend_rows or [])}")
    for lr in (legend_rows or [])[:3]:
        lid = lr.get("id"); label = lr.get("label"); sheet_id = lr.get("source_sheet_id"); rep_bbox = lr.get("representation_bbox")
        print(f"\nLegend {lid[:8]} label='{label}'")
        print(f"  source_sheet_id={sheet_id[:8] if sheet_id else 'None'}")
        print(f"  representation_bbox={rep_bbox}")
        if not sheet_id or not rep_bbox:
            continue
        png = await download_sheet_image(
            suite_id=SUITE, project_id=PROJ, sheet_id=sheet_id, correlation_id="debug",
        )
        if not png:
            print(f"  SHEET PNG MISSING")
            continue
        # Use the SAME extend_right as vlm_symbol_verifier
        crop = _crop_png(png, rep_bbox, extend_right_px=600)
        if not crop:
            print(f"  legend CROP None")
            continue
        out_path = os.path.join(OUT_DIR, f"legend_{lid[:8]}_{label[:20].replace('/','_').replace(' ','_')}.png")
        with open(out_path, "wb") as f:
            f.write(crop)
        print(f"  saved legend crop: {out_path} ({len(crop)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
