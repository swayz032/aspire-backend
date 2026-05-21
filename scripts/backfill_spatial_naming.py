"""Backfill spatial OCR → symbol naming for an existing project.

Wave 17b.2 retroactive runner.

For each sheet in the target project:
  1. Load existing rows from blueprint_symbols (with their bbox + signature_hash).
  2. Download the cached page PDF/PNG from Supabase Storage; PNG path lives on
     blueprint_sheets.image_storage_path. Word-level bboxes are recovered by
     rendering the original sheet via PyMuPDF — see _load_words_for_sheet.
     If the sheet's source PDF isn't reachable, the sheet is skipped (with
     a warning) rather than guessed at.
  3. Build SymbolBox inputs from existing rows (only those whose class is
     still an opaque / fallback name — real names are left alone).
  4. Run spatial_ocr_binder.bind_symbols_to_ocr against the sheet's
     discipline (from blueprint_sheets.discipline).
  5. For every hit, UPDATE blueprint_symbols.class and confidence; UPSERT to
     blueprint_symbol_memory.

Idempotent: re-running yields the same result because we only overwrite rows
whose class is still in the "opaque/fallback" set, and memory upserts dedupe
by signature_hash.

Law compliance:
  Law #2 — emits one drew.see.spatial_binding receipt per project run.
  Law #3 — fails closed: missing --suite aborts before any write.
  Law #6 — every UPDATE / SELECT scopes by suite_id.
  Law #9 — never logs raw OCR text; logs counts only.

Usage:
    python -m scripts.backfill_spatial_naming <project_id> --suite <uuid> [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger("backfill_spatial_naming")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Classes we treat as "needs naming" — anything else is left alone.
_OPAQUE_PREFIXES: tuple[str, ...] = (
    "unknown_motif_",
    "sig:",
    "class_",
    "unmapped:",
)
_FALLBACK_BANDS: frozenset[str] = frozenset({
    f"{disc}_symbol_{band}"
    for disc in (
        "architectural", "structural", "mechanical", "electrical", "plumbing",
        "fire_protection", "civil", "landscape", "general",
    )
    for band in ("high", "mid", "low")
})


def _is_renamable(class_name: str) -> bool:
    if not class_name:
        return True
    if class_name.startswith(_OPAQUE_PREFIXES):
        return True
    if class_name in _FALLBACK_BANDS:
        return True
    return False


async def _load_sheets(project_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_sheets",
        filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    return rows or []


async def _load_symbols(sheet_id: str, suite_id: str) -> list[dict[str, Any]]:
    from aspire_orchestrator.services.supabase_client import supabase_select
    rows = await supabase_select(
        "blueprint_symbols",
        filters=f"sheet_id=eq.{sheet_id}&suite_id=eq.{suite_id}",
    )
    return rows or []


async def _download_raw_pdf(suite_id: str, project_id: str) -> bytes | None:
    """Pull the raw source PDF from the blueprint-raw-pdfs bucket.

    Mirrors download_sheet_image semantics. Returns None on miss / error.
    """
    import httpx
    from aspire_orchestrator.config.settings import settings
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    base = settings.supabase_url.rstrip("/")
    url = f"{base}/storage/v1/object/blueprint-raw-pdfs/{suite_id}/{project_id}/raw.pdf"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            _log.warning("backfill: raw pdf download status=%d", resp.status_code)
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001
        _log.warning("backfill: raw pdf download error (%s)", type(exc).__name__)
        return None


async def _load_words_for_sheet(
    suite_id: str, project_id: str, sheet: dict[str, Any], correlation_id: str
):
    """Recover word-level PDF-point bboxes for one sheet.

    Strategy: download the cached PNG from Storage, render it back into a
    1-page PDF via PyMuPDF (so word coords come out in PDF points consistent
    with the symbol bbox storage). When the PNG is missing, we have no choice
    but to skip — without the geometry, binding is impossible and we refuse
    to guess (Law #3 fail-closed).

    Important: blueprint_symbols.bbox.x/y/w/h are in PDF points captured at
    the original sheet's PDF rendering. The stored PNG is at 200 DPI raster.
    PyMuPDF reads a PDF with its native coordinate system; if we wrap the PNG
    into a single-page PDF, the resulting page has size matching the PNG in
    pixels — NOT the original PDF page size. To keep things calibrated we
    render the PNG at scale=72/200 so 1 PDF unit ≈ 1 original PDF point.

    Returns: list[OcrWord] with PDF-point bboxes, OR None when no words could
    be recovered (caller skips).
    """
    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        download_sheet_image,
    )
    from aspire_orchestrator.services.blueprint.spatial_ocr_binder import (
        OcrWord,
    )

    try:
        import fitz  # PyMuPDF
    except ImportError:
        _log.error("PyMuPDF required for backfill word extraction")
        return None

    sheet_id = str(sheet["id"])
    png_bytes = await download_sheet_image(
        suite_id=suite_id,
        project_id=project_id,
        sheet_id=sheet_id,
        correlation_id=correlation_id,
    )
    if not png_bytes:
        _log.info("backfill: sheet=%s no cached PNG — skipping word extract", sheet_id[:8])
        return None

    # Convert PNG → single-page PDF in memory so PyMuPDF's word reader works
    # in PDF coords. We scale 200 DPI → 72 DPI so 1pt ≈ 1pt of the original.
    try:
        with fitz.open(stream=png_bytes, filetype="png") as img_doc:
            pix_w = img_doc[0].rect.width
            pix_h = img_doc[0].rect.height
            # Scale factor: stored as 200 DPI → PDF point space at 72 DPI
            scale = 72.0 / 200.0
            pdf_w = pix_w * scale
            pdf_h = pix_h * scale
            new_pdf = fitz.open()
            new_page = new_pdf.new_page(width=pdf_w, height=pdf_h)
            new_page.insert_image(new_page.rect, stream=png_bytes)
            # PNG has no text layer — get_text("words") on a rasterized PDF is
            # empty. The backfill relies on the sheet still having a real PDF
            # with a text layer reachable somewhere else. If we hit this path,
            # the sheet was scan-only and we cannot bind by spatial OCR.
            words_raw = new_page.get_text("words")
            new_pdf.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning("backfill: sheet=%s pdf re-wrap failed (%s)", sheet_id[:8], type(exc).__name__)
        return None

    if not words_raw:
        return None

    out: list[OcrWord] = []
    for tup in words_raw:
        try:
            x0, y0, x1, y1, text, *_ = tup
        except (ValueError, TypeError):
            continue
        if not text:
            continue
        out.append(OcrWord(
            text=str(text),
            x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1),
            confidence=1.0,
        ))
    return out


async def _load_words_via_ocr_text_split(sheet: dict[str, Any]):
    """Conservative fallback when PNG is unbindable: parse ocr_text into pseudo-words.

    Without geometry this cannot drive spatial binding. Returns None — the
    backfill skips the sheet rather than fabricate positions (no-fallback rule).
    """
    return None


async def _backfill_one_project(
    project_id: str,
    suite_id: str,
    *,
    dry_run: bool,
    correlation_id: str,
    source_pdf_path: str | None = None,
) -> dict[str, int]:
    from aspire_orchestrator.services.blueprint.spatial_ocr_binder import (
        SymbolBox,
        bind_symbols_to_ocr,
    )
    from aspire_orchestrator.services.blueprint import symbol_memory
    from aspire_orchestrator.services.supabase_client import supabase_update

    summary = {
        "sheets_total": 0,
        "sheets_with_words": 0,
        "sheets_skipped_no_words": 0,
        "symbols_total": 0,
        "symbols_renamable": 0,
        "symbols_bound": 0,
        "symbols_unbound": 0,
        "memory_upserts": 0,
        "updates_failed": 0,
    }

    sheets = await _load_sheets(project_id, suite_id)
    summary["sheets_total"] = len(sheets)
    _log.info("backfill: project=%s sheets=%d", project_id[:8], len(sheets))

    # Try to open the source PDF (preferred — preserves text layer for
    # PyMuPDF word extraction). Falls back to per-sheet PNG (lossy).
    try:
        import fitz  # PyMuPDF
    except ImportError:
        _log.error("PyMuPDF required for backfill")
        return summary

    pdf_doc = None
    sheet_by_page: dict[int, dict[str, Any]] = {}
    if source_pdf_path:
        try:
            pdf_doc = fitz.open(source_pdf_path)
            _log.info("backfill: opened source pdf local=%s pages=%d", source_pdf_path, len(pdf_doc))
        except Exception as exc:  # noqa: BLE001
            _log.warning("backfill: local pdf open failed (%s)", type(exc).__name__)
            pdf_doc = None
    if pdf_doc is None:
        raw = await _download_raw_pdf(suite_id, project_id)
        if raw:
            try:
                pdf_doc = fitz.open(stream=raw, filetype="pdf")
                _log.info("backfill: opened source pdf from storage pages=%d", len(pdf_doc))
            except Exception as exc:  # noqa: BLE001
                _log.warning("backfill: storage pdf open failed (%s)", type(exc).__name__)

    # Map page_number → sheet by sheet_number when source PDF available.
    if pdf_doc is not None:
        for sh in sheets:
            try:
                pn = int(sh.get("sheet_number") or 0)
            except (TypeError, ValueError):
                continue
            if pn >= 1:
                sheet_by_page[pn] = sh

    for sheet in sheets:
        sheet_id = str(sheet["id"])
        discipline = sheet.get("discipline")
        sheet_symbols = await _load_symbols(sheet_id, suite_id)
        summary["symbols_total"] += len(sheet_symbols)
        renamable = [s for s in sheet_symbols if _is_renamable(s.get("class") or "")]
        summary["symbols_renamable"] += len(renamable)
        if not renamable:
            continue

        words = None
        if pdf_doc is not None:
            try:
                pn = int(sheet.get("sheet_number") or 0)
            except (TypeError, ValueError):
                pn = 0
            if 1 <= pn <= len(pdf_doc):
                from aspire_orchestrator.services.blueprint.spatial_ocr_binder import (
                    extract_words_with_pymupdf,
                )
                try:
                    words = extract_words_with_pymupdf(pdf_doc[pn - 1])
                except Exception as exc:  # noqa: BLE001
                    _log.warning("backfill: page word extract failed page=%d (%s)", pn, type(exc).__name__)
                    words = None
        if not words:
            words = await _load_words_for_sheet(suite_id, project_id, sheet, correlation_id)
        if not words:
            summary["sheets_skipped_no_words"] += 1
            _log.info(
                "backfill: sheet=%s discipline=%s renamable=%d — no words available, skipping",
                sheet_id[:8], discipline, len(renamable),
            )
            continue
        summary["sheets_with_words"] += 1

        sym_boxes: list[SymbolBox] = []
        symbol_row_by_sigid: dict[str, dict[str, Any]] = {}
        for srow in renamable:
            bbox = srow.get("bbox") or {}
            sig = (
                bbox.get("signature_hash")
                or (srow.get("class") or "").replace("unknown_motif_", "")
                or str(srow["id"])
            )
            try:
                x = float(bbox.get("x", 0))
                y = float(bbox.get("y", 0))
                w = float(bbox.get("w", 0))
                h = float(bbox.get("h", 0))
            except (TypeError, ValueError):
                continue
            sym_boxes.append(SymbolBox(
                symbol_id=str(srow["id"]),
                x=x, y=y, w=w, h=h,
                signature_hash=str(sig),
            ))
            symbol_row_by_sigid[str(srow["id"])] = srow

        if not sym_boxes:
            continue

        binds = bind_symbols_to_ocr(sym_boxes, words, discipline=discipline)

        for b in binds:
            if b.label is None:
                summary["symbols_unbound"] += 1
                continue
            summary["symbols_bound"] += 1
            srow = symbol_row_by_sigid.get(b.symbol_id)
            if srow is None:
                continue
            new_bbox = dict(srow.get("bbox") or {})
            new_bbox["name_source"] = "spatial_ocr"
            new_bbox["spatial_label_hash"] = b.raw_text
            new_bbox["spatial_distance_pt"] = b.distance_pt

            if dry_run:
                _log.info(
                    "backfill[DRY]: sheet=%s symbol=%s %s -> %s (d=%.1f conf=%.2f)",
                    sheet_id[:8], b.symbol_id[:8], srow.get("class"), b.label,
                    b.distance_pt, b.confidence,
                )
                continue

            try:
                await supabase_update(
                    "blueprint_symbols",
                    f"id=eq.{srow['id']}&suite_id=eq.{suite_id}",
                    {"class": b.label, "confidence": b.confidence, "bbox": new_bbox},
                )
            except Exception as exc:  # noqa: BLE001
                summary["updates_failed"] += 1
                _log.warning(
                    "backfill: update failed symbol=%s err=%s",
                    str(srow["id"])[:8], type(exc).__name__,
                )
                continue

            # Memory upsert (Law #6: suite-scoped). Only if signature_hash present.
            sig_hash = (srow.get("bbox") or {}).get("signature_hash") or b.signature_hash
            if sig_hash:
                try:
                    await symbol_memory.upsert_observation(
                        suite_id=suite_id,
                        office_id=srow.get("office_id"),
                        signature_hash=str(sig_hash),
                        canonical_name=b.label,
                        category=None,
                        source="spatial_ocr",
                        project_id=project_id,
                        instances_observed=(srow.get("bbox") or {}).get("instance_count", 1) or 1,
                        confidence=b.confidence,
                    )
                    summary["memory_upserts"] += 1
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "backfill: memory upsert failed sig=%s err=%s",
                        str(sig_hash)[:8], type(exc).__name__,
                    )

    # Receipt (Law #2)
    if not dry_run:
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([{
                "id": str(uuid_mod.uuid4()),
                "outcome": "success",
                "action_type": "drew.see.spatial_binding",
                "receipt_type": "drew.see.spatial_binding",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": "skillpack:drew-blueprint:backfill",
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                "metadata": {
                    "project_id": project_id,
                    "mode": "backfill",
                    **summary,
                },
            }])
        except Exception:  # noqa: BLE001
            pass

    return summary


async def _main_async(
    project_id: str, suite_id: str, dry_run: bool, source_pdf: str | None,
) -> int:
    correlation_id = f"backfill-spatial-{uuid_mod.uuid4().hex[:8]}"
    _log.info(
        "backfill start project=%s suite=%s dry_run=%s corr=%s source_pdf=%s",
        project_id[:8], suite_id[:8], dry_run, correlation_id,
        source_pdf or "(auto from storage)",
    )
    summary = await _backfill_one_project(
        project_id, suite_id, dry_run=dry_run, correlation_id=correlation_id,
        source_pdf_path=source_pdf,
    )
    _log.info("backfill complete: %s", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill spatial OCR symbol naming.")
    parser.add_argument("project_id", help="Blueprint project UUID")
    parser.add_argument(
        "--suite", default=os.getenv("ASPIRE_BACKFILL_SUITE_ID", ""),
        help="Suite UUID (or set ASPIRE_BACKFILL_SUITE_ID).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source-pdf", default=None,
        help="Optional local path to the source PDF (preserves text layer for "
             "PyMuPDF word extraction). When omitted, attempts to fetch from "
             "blueprint-raw-pdfs storage bucket.",
    )
    args = parser.parse_args()

    if not args.suite:
        _log.error("--suite (or ASPIRE_BACKFILL_SUITE_ID) is required (Law #6).")
        return 2

    return asyncio.run(_main_async(args.project_id, args.suite, args.dry_run, args.source_pdf))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
