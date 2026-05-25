"""Wave 20.14.6 — SAGG VISUAL EVIDENCE TEST.

Renders SAGG-admitted graphs (sagg_admitted=true) sorted by GCS score.
Shows top 10 + bottom 5 so we can SEE if admissibility produced real symbols.

If breakthrough → top GCS graphs look like sprinkler heads / fixtures / outlets
If still failing → diagnose what survived admissibility but shouldn't
"""
import asyncio
import io
import os
import sys

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.sheet_image_storage import download_sheet_image  # noqa: E402
from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
OUT_DIR = r"C:\Users\tonio\Pictures\wave-20-evidence\sagg"

PDF_TO_PNG_SCALE = 200 / 72
PAD_PX = 40
TOP_N = 15
BOTTOM_N = 5


def _crop_png(png_bytes: bytes, x_pt: float, y_pt: float, w_pt: float, h_pt: float, label: str) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(png_bytes))
        W, H = img.size
        x0 = int(x_pt * PDF_TO_PNG_SCALE) - PAD_PX
        y0 = int(y_pt * PDF_TO_PNG_SCALE) - PAD_PX
        x1 = int((x_pt + w_pt) * PDF_TO_PNG_SCALE) + PAD_PX
        y1 = int((y_pt + h_pt) * PDF_TO_PNG_SCALE) + PAD_PX
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(W, x1); y1 = min(H, y1)
        if x1 <= x0 or y1 <= y0:
            return None
        crop = img.crop((x0, y0, x1, y1))
        d = ImageDraw.Draw(crop)
        d.rectangle([PAD_PX, PAD_PX, (x1 - x0) - PAD_PX, (y1 - y0) - PAD_PX],
                    outline=(255, 0, 0, 255), width=2)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        print(f"  crop fail: {type(exc).__name__}: {exc}")
        return None


async def main() -> None:
    os.makedirs(os.path.join(OUT_DIR, "top"), exist_ok=True)
    os.makedirs(os.path.join(OUT_DIR, "bottom"), exist_ok=True)

    # Fetch all SAGG-admitted, sort by trajectory class + persistence_score
    admitted = await supabase_select(
        "blueprint_composed_symbols",
        f"project_id=eq.{PROJ}&suite_id=eq.{SUITE}&sagg_admitted=eq.true"
        f"&order=final_score.desc,persistence_band.asc",
        limit=1000,
    ) or []
    print(f"SAGG-admitted graphs: {len(admitted)}")

    top = admitted[:TOP_N]
    bottom = admitted[-BOTTOM_N:] if len(admitted) > BOTTOM_N else []

    # Pre-fetch sheets
    sheet_ids = {cs["sheet_id"] for cs in (top + bottom)}
    sheet_rows = await supabase_select(
        "blueprint_sheets",
        f"id=in.({','.join(sheet_ids)})&suite_id=eq.{SUITE}",
        limit=len(sheet_ids) + 5,
    ) or []
    sheet_by_id = {s["id"]: s for s in sheet_rows}

    png_cache: dict[str, bytes] = {}
    for sid in sheet_ids:
        try:
            png = await download_sheet_image(SUITE, PROJ, sid, "sagg-vis")
            if png:
                png_cache[sid] = png
                code = (sheet_by_id.get(sid) or {}).get("sheet_code") or "no-code"
                print(f"  PNG {sid[:8]} ({code}) = {len(png)//1024} KB")
        except Exception as exc:
            print(f"  PNG FAIL {sid[:8]}: {exc}")

    manifest = ["bucket,idx,gcs,purity,prim_count,sheet_code,bbox_w_pt,bbox_h_pt,components,filename"]

    def _render(bucket: str, rows: list[dict]) -> None:
        print(f"\n=== {bucket.upper()} ({len(rows)} graphs) ===")
        for idx, cs in enumerate(rows):
            sid = cs["sheet_id"]
            png = png_cache.get(sid)
            if not png:
                continue
            b = cs.get("bbox") or {}
            try:
                x = float(b["x"]); y = float(b["y"]); w = float(b["w"]); h = float(b["h"])
            except (KeyError, TypeError, ValueError):
                continue
            sheet_code = (sheet_by_id.get(sid) or {}).get("sheet_code") or "no-code"
            gcs = cs.get("gcs_score") or 0
            purity = cs.get("purity_score") or 0
            n = cs.get("primitive_count") or 0
            crop = _crop_png(png, x, y, w, h, sheet_code)
            if not crop:
                continue
            band = (cs.get("persistence_band") or "?")[:4]
            fs = cs.get("final_score") or 0
            ss = cs.get("signature_stability") or 0
            fname = f"{idx:02d}_{band}_fs{int(fs*100):03d}_ss{int(ss*100):03d}_{sheet_code}_n{n}.png"
            full = os.path.join(OUT_DIR, bucket, fname)
            with open(full, "wb") as f:
                f.write(crop)
            comp = cs.get("gcs_components") or {}
            comp_str = "/".join(f"{k[:3]}={v}" for k, v in comp.items())
            manifest.append(f"{bucket},{idx},{gcs},{purity},{n},{sheet_code},{w:.1f},{h:.1f},{comp_str},{fname}")
            print(f"  OK [{bucket}/{idx:02d}] gcs={gcs:.3f} pur={purity:.2f} prims={n} bbox={w:.1f}x{h:.1f} {sheet_code}")

    _render("top", top)
    _render("bottom", bottom)

    mpath = os.path.join(OUT_DIR, "manifest.csv")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write("\n".join(manifest))
    print(f"\nManifest: {mpath}\nOutput: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
