"""Wave 20.14 — Extract real vector primitives from PDF + persist to blueprint_primitives.

Uses PyMuPDF (already a dep) to walk every vector drawing op on every sheet,
extracts full geometry (endpoints, centroids, lengths, angles, colors, layer),
and computes per-primitive visible-pixel signal by rastering each primitive
in isolation against a tiny canvas at the source DPI.

Output: blueprint_primitives rows ready for composition + topology embedding
in Wave 20.14.1 (PCE-v2) and Wave 20.14.2 (real graph signatures).

Usage:
    railway run python orchestrator/scripts/extract_primitives_vtpl.py --project-id <uuid>
"""
import asyncio
import io
import math
import sys
import uuid as uuid_mod
from typing import Any

sys.path.insert(0, "orchestrator/src")

from aspire_orchestrator.services.blueprint.sheet_image_storage import download_sheet_image  # noqa: E402
from aspire_orchestrator.services.supabase_client import (  # noqa: E402
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

PROJ = "7a93d144-5d53-47d3-8b97-067ebe249d88"
SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
RASTER_DPI = 144  # primitives rendered at this DPI for visibility scoring


def _hex_color(rgb_tuple: Any) -> str | None:
    if not rgb_tuple or not isinstance(rgb_tuple, (tuple, list)):
        return None
    try:
        r, g, b = int(rgb_tuple[0] * 255), int(rgb_tuple[1] * 255), int(rgb_tuple[2] * 255)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return None


def _classify_op(items: list[Any]) -> str:
    """Classify a drawing path by its dominant op."""
    if not items:
        return "path"
    ops = {it[0] for it in items if it}
    if "l" in ops and "c" not in ops:
        return "line" if len(items) == 1 else "polyline"
    if "c" in ops:
        return "curve"
    if "re" in ops:
        return "rect"
    if "qu" in ops:
        return "quad"
    return "path"


_PAGE_RASTER_CACHE: dict[int, Any] = {}  # page_idx → grayscale PIL Image


def _get_page_raster(pdf_page, page_idx: int):
    """Render a full page ONCE at RASTER_DPI, cache as grayscale PIL Image."""
    if page_idx in _PAGE_RASTER_CACHE:
        return _PAGE_RASTER_CACHE[page_idx]
    import fitz
    from PIL import Image
    zoom = RASTER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    _PAGE_RASTER_CACHE[page_idx] = img
    return img


def _primitive_visibility(
    pdf_page,
    bbox: tuple[float, float, float, float],
    page_idx: int = 0,
    pad: int = 4,
) -> tuple[int, float]:
    """Sample the pre-rendered page raster at the primitive's bbox region.

    Was: full PDF rasterization per primitive (34K renders × 50ms = ~28 min).
    Now: one page render cached, per-primitive pixel sampling (microseconds).
    """
    try:
        img = _get_page_raster(pdf_page, page_idx)
        zoom = RASTER_DPI / 72.0
        x0, y0, x1, y1 = bbox
        # PDF coords → page raster coords
        px0 = max(0, int((x0 - pad) * zoom))
        py0 = max(0, int((y0 - pad) * zoom))
        px1 = min(img.width, int((x1 + pad) * zoom))
        py1 = min(img.height, int((y1 + pad) * zoom))
        if px1 <= px0 or py1 <= py0:
            return (0, 0.0)
        cropped = img.crop((px0, py0, px1, py1))
        pixels = list(cropped.getdata())
        if not pixels:
            return (0, 0.0)
        non_white = sum(1 for p in pixels if p < 220)
        return (non_white, non_white / len(pixels))
    except Exception:
        return (0, 0.0)


async def _fetch_sheets(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    return await supabase_select(
        "blueprint_sheets",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    ) or []


async def main() -> None:
    import fitz  # PyMuPDF

    # 1. Download project PDF from Storage
    proj_rows = await supabase_select(
        "blueprint_projects",
        f"id=eq.{PROJ}&suite_id=eq.{SUITE}",
        limit=1,
    )
    if not proj_rows:
        print(f"ERROR: project {PROJ} not found")
        return
    storage_path = proj_rows[0].get("pdf_storage_path")
    print(f"project storage path: {storage_path}")

    # Use sheet PNG fetcher infra to get the PDF bytes — Storage download via the same client
    import os
    import urllib.request

    SUPABASE_URL = "https://qtuehjqlcmfcascqjjhc.supabase.co"
    KEY = os.environ.get("ASPIRE_SUPABASE_SERVICE_ROLE_KEY") or os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY", ""
    )
    if not KEY:
        print("ERROR: ASPIRE_SUPABASE_SERVICE_ROLE_KEY missing from env")
        return
    pdf_url = f"{SUPABASE_URL}/storage/v1/object/blueprint-raw-pdfs/{storage_path}"
    req = urllib.request.Request(pdf_url, headers={"Authorization": f"Bearer {KEY}"})
    print(f"downloading PDF from Storage...")
    with urllib.request.urlopen(req, timeout=120) as r:
        pdf_bytes = r.read()
    print(f"got PDF: {len(pdf_bytes)} bytes")

    # 2. Open PDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    print(f"PDF pages: {doc.page_count}")

    # 3. Match sheets to pages via page_number
    sheets = await _fetch_sheets(PROJ, SUITE)
    sheet_by_pageno: dict[int, str] = {}
    for s in sheets:
        # sheet_number is a STRING ("1", "2", ...) that maps to PDF page index
        try:
            pn = int(str(s.get("sheet_number", "")).strip())
            if pn > 0:
                sheet_by_pageno[pn] = s["id"]
        except (TypeError, ValueError):
            continue
    print(f"sheets matched by page_number: {len(sheet_by_pageno)}")

    inserts: list[dict[str, Any]] = []
    total_paths = 0
    total_persisted = 0
    visible_count = 0

    # 4. Walk every page → every drawing → every primitive
    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        page_number = page_idx + 1
        sheet_id = sheet_by_pageno.get(page_number)
        if not sheet_id:
            continue

        drawings = page.get_drawings()
        page_persisted = 0
        for d in drawings:
            items = d.get("items", [])
            if not items:
                continue
            total_paths += 1
            ptype = _classify_op(items)
            rect = d.get("rect")
            if rect is None:
                continue
            x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
            bw, bh = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

            # Line geometry
            length = None
            angle_deg = None
            if ptype == "line" and len(items) == 1:
                it = items[0]
                try:
                    p0, p1 = it[1], it[2]
                    length = math.hypot(p1.x - p0.x, p1.y - p0.y)
                    angle_deg = math.degrees(math.atan2(p1.y - p0.y, p1.x - p0.x))
                except Exception:
                    pass

            # Render-consistency signal (uses page-level raster cache — ~1000x faster than per-primitive)
            visible_px, density = _primitive_visibility(page, (x0, y0, x1, y1), page_idx=page_idx)
            if density >= 0.05:
                visible_count += 1

            row = {
                "id": str(uuid_mod.uuid4()),
                "suite_id": SUITE,
                "project_id": PROJ,
                "sheet_id": sheet_id,
                "primitive_type": ptype,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "cx": cx, "cy": cy,
                "length": length,
                "angle_deg": angle_deg,
                "stroke_width": float(d.get("width", 0) or 0),
                "color_hex": _hex_color(d.get("color")),
                "fill_hex": _hex_color(d.get("fill")),
                "layer": d.get("layer"),
                "page_number": page_number,
                "bbox_w": bw, "bbox_h": bh,
                "visible_pixels": visible_px,
                "raster_density": round(density, 5),
            }
            inserts.append(row)
            page_persisted += 1

            # Flush in batches of 500
            if len(inserts) >= 500:
                try:
                    await supabase_insert_batch_ignore_conflicts(
                        "blueprint_primitives", inserts, conflict_target="id",
                    )
                    total_persisted += len(inserts)
                except Exception as exc:
                    print(f"batch insert fail: {type(exc).__name__}: {exc}")
                inserts = []

        if page_persisted:
            print(f"  page {page_number} sheet={sheet_id[:8]}: {page_persisted} primitives extracted")

    # Final flush
    if inserts:
        try:
            await supabase_insert_batch_ignore_conflicts(
                "blueprint_primitives", inserts, conflict_target="id",
            )
            total_persisted += len(inserts)
        except Exception as exc:
            print(f"final batch insert fail: {type(exc).__name__}: {exc}")

    print()
    print(f"=== DONE ===")
    print(f"total drawing paths walked: {total_paths}")
    print(f"persisted to blueprint_primitives: {total_persisted}")
    print(f"visible (raster_density >= 0.05): {visible_count}")
    print(f"invisible/cruft ratio: {(total_persisted - visible_count) / max(total_persisted, 1):.1%}")


if __name__ == "__main__":
    asyncio.run(main())
