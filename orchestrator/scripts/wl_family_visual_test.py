"""Wave 20.14.9 — VISUAL EVIDENCE TEST for WL family breakthrough.

For each of the top WL families (filtered to min_dim >= 8pt = real symbols),
save crops of every instance side-by-side so we can SEE whether the family
represents a real construction symbol that repeats consistently.

If breakthrough → all crops of family X look the same (sprinkler heads, etc.)
If false positive → crops look like random fragments.

Output: C:/Users/tonio/Pictures/wave-20-evidence/families/{sig_prefix}/{idx}.png
        manifest.csv with sig + sheet_code + bbox + primitive_count
"""
import asyncio
import io
import os
import sys
from collections import defaultdict

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.sheet_image_storage import download_sheet_image  # noqa: E402
from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
OUT_DIR = r"C:\Users\tonio\Pictures\wave-20-evidence\families"

PDF_TO_PNG_SCALE = 200 / 72  # sheets rendered at 200 DPI
PAD_PX = 40
MIN_DIM_PT = 8.0   # real-size filter — excludes 3pt micro-noise
TOP_FAMILIES = 6    # render top 6 multi-instance families
MAX_INSTANCES_PER_FAMILY = 8


def _crop_png(png_bytes: bytes, x_pt: float, y_pt: float, w_pt: float, h_pt: float, pad_px: int = PAD_PX) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(png_bytes))
        W, H = img.size
        x0 = int(x_pt * PDF_TO_PNG_SCALE) - pad_px
        y0 = int(y_pt * PDF_TO_PNG_SCALE) - pad_px
        x1 = int((x_pt + w_pt) * PDF_TO_PNG_SCALE) + pad_px
        y1 = int((y_pt + h_pt) * PDF_TO_PNG_SCALE) + pad_px
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(W, x1); y1 = min(H, y1)
        if x1 <= x0 or y1 <= y0:
            return None
        crop = img.crop((x0, y0, x1, y1))
        # Draw rectangle showing actual bbox (without padding)
        d = ImageDraw.Draw(crop)
        rx0 = pad_px; ry0 = pad_px
        rx1 = (x1 - x0) - pad_px; ry1 = (y1 - y0) - pad_px
        d.rectangle([rx0, ry0, rx1, ry1], outline=(255, 0, 0, 255), width=2)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        print(f"  crop fail: {type(exc).__name__}: {exc}")
        return None


async def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1. Top families (real-size, min_dim >= MIN_DIM_PT, primitives >= 3)
    families_raw = await supabase_select(
        "rpc/exec_sql",
        "",
    ) if False else None

    # Use direct query instead of RPC
    all_composed = await supabase_select(
        "blueprint_composed_symbols",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}"
        f"&primitive_count=gte.3&wl_signature=not.is.null&order=wl_signature",
    ) or []

    # Filter to real-size in-Python
    real = []
    for cs in all_composed:
        b = cs.get("bbox") or {}
        try:
            w = float(b.get("w") or 0); h = float(b.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if min(w, h) >= MIN_DIM_PT:
            real.append(cs)

    families: dict[str, list[dict]] = defaultdict(list)
    for cs in real:
        families[cs["wl_signature"]].append(cs)

    # Top by instance count
    top = sorted(families.items(), key=lambda kv: -len(kv[1]))[:TOP_FAMILIES]
    print(f"Real-size composed symbols (min_dim >= {MIN_DIM_PT}pt): {len(real)}")
    print(f"Real families with >=2 instances: {sum(1 for _, v in families.items() if len(v) >= 2)}")
    print(f"Rendering top {len(top)} families")

    # 2. Build sheet_id → png_bytes cache
    sheet_ids_needed: set[str] = set()
    for sig, members in top:
        for m in members[:MAX_INSTANCES_PER_FAMILY]:
            sheet_ids_needed.add(m["sheet_id"])
    print(f"Distinct sheets to download: {len(sheet_ids_needed)}")

    # Fetch sheet rows for storage paths
    if sheet_ids_needed:
        in_filter = ",".join(sheet_ids_needed)
        sheet_rows = await supabase_select(
            "blueprint_sheets",
            f"id=in.({in_filter})&suite_id=eq.{SUITE}",
            limit=len(sheet_ids_needed) + 5,
        ) or []
    else:
        sheet_rows = []

    sheet_by_id: dict[str, dict] = {s["id"]: s for s in sheet_rows}
    png_cache: dict[str, bytes] = {}

    for sid in sheet_ids_needed:
        s = sheet_by_id.get(sid)
        if not s:
            continue
        path = s.get("image_storage_path")
        if not path:
            print(f"  no storage path for sheet {sid[:8]}"); continue
        try:
            png = await download_sheet_image(SUITE, PROJ, sid, "wl-family-test")
            if png:
                png_cache[sid] = png
                print(f"  OK {sid[:8]} ({s.get('sheet_code') or 'no-code'}) = {len(png)//1024} KB")
        except Exception as exc:
            print(f"  FAIL {sid[:8]}: {type(exc).__name__}: {exc}")

    # 3. Render family crops
    manifest_lines = ["family_sig,instance_idx,sheet_id,sheet_code,bbox_w_pt,bbox_h_pt,primitive_count,filename"]
    for sig, members in top:
        sig_short = sig[:12]
        family_dir = os.path.join(OUT_DIR, sig_short)
        os.makedirs(family_dir, exist_ok=True)
        instances = members[:MAX_INSTANCES_PER_FAMILY]
        print(f"\n=== Family {sig_short} — {len(members)} total, rendering {len(instances)} ===")

        for idx, cs in enumerate(instances):
            sid = cs["sheet_id"]
            png = png_cache.get(sid)
            if not png:
                print(f"  inst {idx}: no PNG for sheet {sid[:8]}"); continue
            b = cs.get("bbox") or {}
            try:
                x = float(b["x"]); y = float(b["y"]); w = float(b["w"]); h = float(b["h"])
            except (KeyError, TypeError, ValueError):
                continue
            crop = _crop_png(png, x, y, w, h)
            if not crop:
                continue
            sheet_code = (sheet_by_id.get(sid) or {}).get("sheet_code") or "no-code"
            fname = f"{idx:02d}_{sheet_code}_n{cs.get('primitive_count', 0)}.png"
            full = os.path.join(family_dir, fname)
            with open(full, "wb") as f:
                f.write(crop)
            manifest_lines.append(
                f"{sig_short},{idx},{sid[:8]},{sheet_code},{w:.1f},{h:.1f},{cs.get('primitive_count', 0)},{fname}"
            )
            print(f"  OK inst {idx}: {sheet_code} bbox={w:.1f}x{h:.1f}pt prims={cs.get('primitive_count')}")

    manifest_path = os.path.join(OUT_DIR, "manifest.csv")
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(manifest_lines))
    print(f"\nManifest: {manifest_path}")
    print(f"Output dir: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
