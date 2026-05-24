"""Visual Evidence Harness — see the pipeline with your own eyes.

For each stage of the Wave 20.6 + 20.13 pipeline, emit visual artifacts
to disk so a human can spot-check every binding decision.

Per GPT v9 mandate: every material bind should emit:
  - composed symbol crop (visual verification)
  - primitive overlay (topology proof)
  - matched legend crop (comparison)
  - constraint scores + rejection reasons (explainability)

This becomes:
  - regression infrastructure
  - contractor trust layer
  - anti-hallucination proof
  - human review accelerator

Output structure (under C:/Users/tonio/Pictures/wave-20-evidence/):
  composed/{sheet_short}/{composed_id_short}.png     -- composed symbol crop
  primitives/{sheet_short}/{composed_id_short}_overlay.png -- primitive bbox overlay
  legends/{legend_short}_{label}.png                  -- legend crops
  manifest.csv                                        -- one row per artifact

Usage:
    railway run python orchestrator/scripts/visual_evidence_harness.py
"""
import asyncio
import csv
import io
import os
import sys
from collections import defaultdict
from typing import Any

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.sheet_image_storage import (  # noqa: E402
    download_sheet_image,
)
from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: E402

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
OUT_DIR = r"C:\Users\tonio\Pictures\wave-20-evidence"

PAD_PX = 80
PRIMITIVE_OVERLAY_COLOR = (255, 0, 0)  # red boxes
PRIMITIVE_OVERLAY_WIDTH = 2
LEGEND_LABEL_EXTEND_PX = 600

# Cap artifact volume for tonight's spot-check
MAX_COMPOSED_PER_PROJECT = 30
MAX_LEGENDS = 15


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _safe_name(s: str, max_len: int = 32) -> str:
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    out = "".join(c if c in keep else "_" for c in (s or "")[:max_len])
    return out or "x"


def _crop_png(png_bytes: bytes, xywh: tuple[float, float, float, float], pad: int = PAD_PX, extend_right: int = 0) -> bytes | None:
    try:
        from PIL import Image
        x0 = int(xywh[0])
        y0 = int(xywh[1])
        x1 = int(xywh[0] + xywh[2])
        y1 = int(xywh[1] + xywh[3])
        img = Image.open(io.BytesIO(png_bytes))
        w, h = img.size
        left = max(0, x0 - pad)
        top = max(0, y0 - pad)
        right = min(w, x1 + pad + extend_right)
        bottom = min(h, y1 + pad)
        if right <= left or bottom <= top:
            return None
        cropped = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _crop_with_primitive_overlay(
    png_bytes: bytes,
    composed_xywh: tuple[float, float, float, float],
    primitive_bboxes: list[tuple[float, float, float, float]],
    pad: int = PAD_PX,
) -> bytes | None:
    """Crop composed bbox, draw red rectangles over each constituent primitive."""
    try:
        from PIL import Image, ImageDraw
        cx0 = int(composed_xywh[0])
        cy0 = int(composed_xywh[1])
        cx1 = int(composed_xywh[0] + composed_xywh[2])
        cy1 = int(composed_xywh[1] + composed_xywh[3])
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        w, h = img.size
        left = max(0, cx0 - pad)
        top = max(0, cy0 - pad)
        right = min(w, cx1 + pad)
        bottom = min(h, cy1 + pad)
        if right <= left or bottom <= top:
            return None
        cropped = img.crop((left, top, right, bottom))
        draw = ImageDraw.Draw(cropped)
        for pb in primitive_bboxes:
            px0 = int(pb[0]) - left
            py0 = int(pb[1]) - top
            px1 = int(pb[0] + pb[2]) - left
            py1 = int(pb[1] + pb[3]) - top
            # Clamp
            px0 = max(0, px0); py0 = max(0, py0)
            px1 = min(right - left, px1); py1 = min(bottom - top, py1)
            if px1 > px0 and py1 > py0:
                draw.rectangle([px0, py0, px1, py1], outline=PRIMITIVE_OVERLAY_COLOR, width=PRIMITIVE_OVERLAY_WIDTH)
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


async def _fetch_composed_symbols(project_id: str, suite_id: str, cap: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    last_id = ""
    while len(rows) < cap:
        cur = f"&id=gt.{last_id}" if last_id else ""
        page = await supabase_select(
            "blueprint_composed_symbols",
            f"project_id=eq.{project_id}&suite_id=eq.{suite_id}{cur}&order=plausibility_score.desc",
            limit=min(500, cap - len(rows)),
        )
        if not page:
            break
        rows.extend(page)
        last_id = page[-1]["id"]
        if len(page) < 500:
            break
    return rows[:cap]


async def _fetch_primitive_bboxes(motif_ids: list[str], suite_id: str) -> dict[str, tuple[float, float, float, float]]:
    if not motif_ids:
        return {}
    out: dict[str, tuple[float, float, float, float]] = {}
    # Page motif fetches in batches of 50
    for i in range(0, len(motif_ids), 50):
        chunk = motif_ids[i : i + 50]
        chunk_csv = ",".join(chunk)
        rows = await supabase_select(
            "blueprint_symbols",
            f"id=in.({chunk_csv})&suite_id=eq.{suite_id}",
        )
        for r in rows or []:
            b = r.get("bbox") or {}
            if isinstance(b, dict) and {"x", "y", "w", "h"}.issubset(b.keys()):
                try:
                    out[r["id"]] = (float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"]))
                except (TypeError, ValueError):
                    continue
    return out


async def _fetch_legends(project_id: str, suite_id: str, cap: int) -> list[dict[str, Any]]:
    rows = await supabase_select(
        "blueprint_project_legends",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        limit=cap,
    )
    return rows or []


async def main() -> None:
    _ensure_dir(OUT_DIR)
    composed_dir = os.path.join(OUT_DIR, "composed")
    primitives_dir = os.path.join(OUT_DIR, "primitives")
    legends_dir = os.path.join(OUT_DIR, "legends")
    _ensure_dir(composed_dir)
    _ensure_dir(primitives_dir)
    _ensure_dir(legends_dir)

    manifest_path = os.path.join(OUT_DIR, "manifest.csv")
    manifest_rows: list[dict[str, str]] = []

    print(f"=== Visual Evidence Harness — output to {OUT_DIR} ===")

    # 1. Composed symbols
    print(f"\n[1/3] Fetching top {MAX_COMPOSED_PER_PROJECT} composed symbols by plausibility...")
    composed = await _fetch_composed_symbols(PROJ, SUITE, MAX_COMPOSED_PER_PROJECT)
    print(f"      got {len(composed)} composed symbols")

    sheet_pngs: dict[str, bytes | None] = {}
    for cs in composed:
        sheet_id = cs.get("sheet_id")
        if not sheet_id:
            continue
        if sheet_id not in sheet_pngs:
            try:
                sheet_pngs[sheet_id] = await download_sheet_image(
                    suite_id=SUITE, project_id=PROJ, sheet_id=sheet_id, correlation_id="evidence-harness",
                )
            except Exception as e:
                print(f"      sheet PNG fetch failed sheet={sheet_id[:8]} err={type(e).__name__}")
                sheet_pngs[sheet_id] = None

    for cs in composed:
        cid = cs["id"]
        sheet_id = cs.get("sheet_id")
        bbox = cs.get("bbox") or {}
        try:
            xywh = (float(bbox.get("x", 0)), float(bbox.get("y", 0)), float(bbox.get("w", 0)), float(bbox.get("h", 0)))
        except (TypeError, ValueError):
            continue
        png = sheet_pngs.get(sheet_id)
        if not png:
            continue
        sheet_short = (sheet_id or "x")[:8]
        cid_short = cid[:12]
        sheet_subdir = os.path.join(composed_dir, sheet_short)
        prim_subdir = os.path.join(primitives_dir, sheet_short)
        _ensure_dir(sheet_subdir)
        _ensure_dir(prim_subdir)

        crop = _crop_png(png, xywh)
        composed_path = os.path.join(sheet_subdir, f"{cid_short}.png")
        if crop:
            with open(composed_path, "wb") as f:
                f.write(crop)

        # Primitive overlay
        motif_ids = cs.get("constituent_motif_ids") or []
        prim_bboxes_by_id = await _fetch_primitive_bboxes(motif_ids, SUITE)
        prim_bboxes = list(prim_bboxes_by_id.values())
        overlay = _crop_with_primitive_overlay(png, xywh, prim_bboxes)
        overlay_path = os.path.join(prim_subdir, f"{cid_short}_overlay.png")
        if overlay:
            with open(overlay_path, "wb") as f:
                f.write(overlay)

        manifest_rows.append({
            "kind": "composed",
            "id": cid,
            "sheet_id": sheet_id or "",
            "primitive_count": str(cs.get("primitive_count") or 0),
            "plausibility": str(cs.get("plausibility_score") or 0.0),
            "total_instance_count": str(cs.get("total_instance_count") or 0),
            "bbox_w": f"{xywh[2]:.1f}",
            "bbox_h": f"{xywh[3]:.1f}",
            "composed_signature": (cs.get("composed_signature") or "")[:16],
            "path": composed_path,
            "overlay_path": overlay_path,
        })

    # 2. Legends
    print(f"\n[2/3] Fetching top {MAX_LEGENDS} legends with non-null labels...")
    legends = await _fetch_legends(PROJ, SUITE, MAX_LEGENDS)
    print(f"      got {len(legends)} legends")
    for lr in legends:
        lid = lr.get("id")
        label = lr.get("label") or ""
        if not label.strip():
            continue
        sheet_id = lr.get("source_sheet_id")
        rep_bbox_8 = lr.get("representation_bbox")
        if not sheet_id or not rep_bbox_8 or len(rep_bbox_8) < 8:
            continue
        # Convert Azure 8-coord polygon → xywh
        xs = [rep_bbox_8[i] for i in (0, 2, 4, 6)]
        ys = [rep_bbox_8[i] for i in (1, 3, 5, 7)]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        xywh = (x0, y0, x1 - x0, y1 - y0)
        if sheet_id not in sheet_pngs:
            try:
                sheet_pngs[sheet_id] = await download_sheet_image(
                    suite_id=SUITE, project_id=PROJ, sheet_id=sheet_id, correlation_id="evidence-harness",
                )
            except Exception:
                sheet_pngs[sheet_id] = None
        png = sheet_pngs.get(sheet_id)
        if not png:
            continue
        crop = _crop_png(png, xywh, extend_right=LEGEND_LABEL_EXTEND_PX)
        safe_label = _safe_name(label)
        out_path = os.path.join(legends_dir, f"{lid[:8]}_{safe_label}.png")
        if crop:
            with open(out_path, "wb") as f:
                f.write(crop)
            manifest_rows.append({
                "kind": "legend",
                "id": lid,
                "sheet_id": sheet_id,
                "label": label,
                "bbox_w": f"{xywh[2]:.1f}",
                "bbox_h": f"{xywh[3]:.1f}",
                "path": out_path,
                "composed_signature": "",
                "primitive_count": "",
                "plausibility": "",
                "total_instance_count": "",
                "overlay_path": "",
            })

    # 3. Manifest
    print(f"\n[3/3] Writing manifest to {manifest_path}")
    fieldnames = [
        "kind", "id", "sheet_id", "label", "primitive_count", "plausibility",
        "total_instance_count", "bbox_w", "bbox_h", "composed_signature",
        "path", "overlay_path",
    ]
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in manifest_rows:
            full = {k: row.get(k, "") for k in fieldnames}
            w.writerow(full)

    print(f"\n=== DONE ===")
    print(f"composed crops: {sum(1 for r in manifest_rows if r['kind']=='composed')}")
    print(f"legend crops:   {sum(1 for r in manifest_rows if r['kind']=='legend')}")
    print(f"manifest:       {manifest_path}")
    print(f"\nOpen {OUT_DIR} to see what the pipeline actually detects.")


if __name__ == "__main__":
    asyncio.run(main())
