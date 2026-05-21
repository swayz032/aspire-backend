"""Wave 11j — VGSE production pipeline orchestrator.

Single entry-point that the SEE stage calls. Given a PDF + project context,
runs the full VGSE flow (classify → motifs → legend → tenant memory →
unanchored fallback) and writes detections to blueprint_symbols + bumps
blueprint_symbol_memory.

Provenance receipts emitted at key boundaries (Law #2):
  blueprint.see.vgse.invoked
  blueprint.see.vgse.page_classified
  blueprint.see.vgse.motifs_resolved
  blueprint.see.vgse.completed

Falls back to existing YOLO/CPU path when page is RASTER_SCAN.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import fitz

from aspire_orchestrator.services.blueprint import symbol_memory
from aspire_orchestrator.services.blueprint.spatial_ocr_binder import (
    BindResult,
    OcrWord,
    SymbolBox,
    bind_symbols_to_ocr,
    extract_words_with_pymupdf,
    summarize_binds,
)
from aspire_orchestrator.services.blueprint.vgse import (
    MotifDetection,
    VGSEPageResult,
    extract_legend_signatures,
    extract_motifs,
    process_page,
)

logger = logging.getLogger(__name__)


@dataclass
class VGSEDetection:
    """One emitted detection row, ready for blueprint_symbols insert."""
    sheet_id: str
    suite_id: str
    office_id: str | None
    signature_hash: str
    canonical_name: str
    name_source: str
    count: int
    bbox_pt: list[float]   # representative bbox (first instance)
    spatial_entropy: float
    item_count: int
    page_number: int


async def run_vgse_for_project(
    *,
    pdf_path: str,
    suite_id: str,
    office_id: str | None,
    project_id: str,
    sheet_ids_by_page: dict[int, str],
    correlation_id: str,
    discipline_by_page: dict[int, str | None] | None = None,
) -> dict[str, Any]:
    """Run VGSE for every page in the PDF.

    Args:
        pdf_path: local filesystem path to the project PDF (already streamed
            from Supabase Storage by the caller).
        sheet_ids_by_page: maps 1-based page_number → blueprint_sheets.id so
            we emit one symbol row per (sheet, signature, instance).

    Returns:
        {
          "vector_pages": int,
          "raster_pages": int,
          "total_motifs_found": int,
          "total_instances": int,
          "memory_hits": int,
          "legend_hits": int,
          "unanchored": int,
          "detections": list[VGSEDetection-as-dict],
          "page_categories": {page_number: category}
        }
    """
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "vector_pages": 0,
        "raster_pages": 0,
        "total_motifs_found": 0,
        "total_instances": 0,
        "memory_hits": 0,
        "legend_hits": 0,
        "spatial_ocr_hits": 0,
        "unanchored": 0,
        "detections": [],
        "page_categories": {},
        "spatial_binding_by_discipline": {},
    }
    discipline_by_page = discipline_by_page or {}

    with fitz.open(pdf_path) as doc:
        for page_idx, page in enumerate(doc):
            page_number = page_idx + 1
            page_result: VGSEPageResult = process_page(page)
            summary["page_categories"][page_number] = page_result.category

            if page_result.category in ("RASTER_SCAN", "UNKNOWN_LOW_CONTENT"):
                summary["raster_pages"] += 1
                continue
            summary["vector_pages"] += 1

            if not page_result.motifs:
                continue

            # Legend map from the page (already in page_result.motifs.name_source='legend')
            legend_map = {e["signature_hash"]: e["label"] for e in page_result.legend_entries}

            # Tenant memory takes priority over page legend
            resolved = await symbol_memory.resolve_motif_names(
                suite_id=suite_id,
                motifs=page_result.motifs,
                legend_map=legend_map,
            )

            sheet_id = sheet_ids_by_page.get(page_number)
            if not sheet_id:
                logger.warning(
                    "vgse: no sheet_id for page %d (project=%s) — skipping motif emit",
                    page_number, project_id[:8],
                )
                continue

            # Wave 17b.2 — spatial OCR binding. For motifs that fell through to
            # 'unanchored', try to bind a real label from the nearest OCR word
            # before emitting. Words come from PyMuPDF (free, exact PDF-pt
            # bboxes) since Azure's flat 'content' string discards geometry.
            page_discipline = discipline_by_page.get(page_number)
            unanchored_motifs = [m for m in resolved if m.name_source == "unanchored"]
            page_bind_results: dict[str, BindResult] = {}
            if unanchored_motifs:
                try:
                    words = extract_words_with_pymupdf(page)
                except Exception as _word_exc:  # noqa: BLE001
                    logger.warning(
                        "vgse: word extract failed page=%d (%s)",
                        page_number, type(_word_exc).__name__,
                    )
                    words = []
                if words:
                    sym_boxes: list[SymbolBox] = []
                    for m in unanchored_motifs:
                        if not m.bboxes:
                            continue
                        bx0, by0, bx1, by1 = m.bboxes[0]
                        sym_boxes.append(SymbolBox(
                            symbol_id=m.signature_hash,
                            x=float(bx0),
                            y=float(by0),
                            w=float(bx1 - bx0),
                            h=float(by1 - by0),
                            signature_hash=m.signature_hash,
                        ))
                    binds = bind_symbols_to_ocr(
                        sym_boxes,
                        words,
                        discipline=page_discipline,
                    )
                    for b in binds:
                        if b.label is not None:
                            page_bind_results[b.symbol_id] = b
                if page_bind_results:
                    disc_key = page_discipline or "unknown"
                    summary["spatial_binding_by_discipline"][disc_key] = (
                        summary["spatial_binding_by_discipline"].get(disc_key, 0)
                        + len(page_bind_results)
                    )

            for motif in resolved:
                # Apply spatial bind if available (only for previously-unanchored motifs)
                if motif.name_source == "unanchored" and motif.signature_hash in page_bind_results:
                    bind = page_bind_results[motif.signature_hash]
                    motif = MotifDetection(
                        signature_hash=motif.signature_hash,
                        count=motif.count,
                        sample_item_count=motif.sample_item_count,
                        sample_size_pt=motif.sample_size_pt,
                        spatial_entropy=motif.spatial_entropy,
                        bboxes=motif.bboxes,
                        canonical_name=bind.label or motif.canonical_name,
                        name_source="spatial_ocr",
                    )
                summary["total_motifs_found"] += 1
                summary["total_instances"] += motif.count
                if motif.name_source == "tenant_memory":
                    summary["memory_hits"] += 1
                elif motif.name_source == "legend":
                    summary["legend_hits"] += 1
                elif motif.name_source == "spatial_ocr":
                    summary["spatial_ocr_hits"] += 1
                else:
                    summary["unanchored"] += 1

                # Persist to symbol memory for future projects
                try:
                    await symbol_memory.upsert_observation(
                        suite_id=suite_id,
                        office_id=office_id,
                        signature_hash=motif.signature_hash,
                        canonical_name=motif.canonical_name,
                        category=None,  # VGSE doesn't assign categories yet — Wave 11h ext
                        source=motif.name_source,
                        project_id=project_id,
                        instances_observed=motif.count,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "vgse: memory upsert failed (%s) sig=%s",
                        type(exc).__name__, motif.signature_hash[:8],
                    )

                # Build a representative detection
                first_bbox = motif.bboxes[0] if motif.bboxes else (0, 0, 0, 0)
                detection = VGSEDetection(
                    sheet_id=sheet_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    signature_hash=motif.signature_hash,
                    canonical_name=motif.canonical_name,
                    name_source=motif.name_source,
                    count=motif.count,
                    bbox_pt=[round(c, 2) for c in first_bbox],
                    spatial_entropy=motif.spatial_entropy,
                    item_count=motif.sample_item_count,
                    page_number=page_number,
                )
                summary["detections"].append(asdict(detection))

    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    summary["elapsed_ms"] = elapsed_ms
    summary["correlation_id"] = correlation_id

    # Wave 17b.2 — emit spatial binding receipt (Law #2). Best-effort: never block
    # the pipeline on receipt persistence.
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts as _sr
        _sr([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "drew.see.spatial_binding",
            "receipt_type": "drew.see.spatial_binding",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint",
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {
                "project_id": project_id,
                "symbols_total": summary["total_motifs_found"],
                "spatial_ocr_bound": summary["spatial_ocr_hits"],
                "unbound": summary["unanchored"],
                "memory_hits": summary["memory_hits"],
                "legend_hits": summary["legend_hits"],
                "by_discipline": summary["spatial_binding_by_discipline"],
            },
        }])
    except Exception:  # noqa: BLE001
        pass  # receipt emission is best-effort

    logger.info(
        "vgse: project=%s pages=%d vector=%d raster=%d motifs=%d instances=%d "
        "memory=%d legend=%d spatial_ocr=%d unanchored=%d (%dms)",
        project_id[:8],
        summary["vector_pages"] + summary["raster_pages"],
        summary["vector_pages"],
        summary["raster_pages"],
        summary["total_motifs_found"],
        summary["total_instances"],
        summary["memory_hits"],
        summary["legend_hits"],
        summary["spatial_ocr_hits"],
        summary["unanchored"],
        elapsed_ms,
    )

    return summary


def detections_to_blueprint_symbol_rows(
    detections: list[dict],
    actor_id: str = "drew",
    discipline_by_sheet_id: dict[str, str | None] | None = None,
) -> list[dict]:
    """Convert VGSEDetection list to blueprint_symbols rows.

    blueprint_symbols schema: id, suite_id, office_id, sheet_id, class,
    bbox (jsonb {x, y, w, h}), confidence, created_at, created_by.

    Each motif inserts ONE representative row per sheet — the contractor's
    Takeoff tab UI can drill into bboxes for the full count.

    Wave 17b.3: when a motif name is opaque ('unknown_motif_*'), apply the
    interpretable fallback so the contractor sees e.g. 'electrical_symbol_high'
    instead of a raw hash. The original VGSE name + signature_hash are kept
    in bbox.original_class / bbox.signature_hash for Wave 18 spatial OCR
    binding to dedupe against.
    """
    from aspire_orchestrator.services.blueprint.see_naming import (
        interpretable_fallback,
        is_opaque_name,
    )

    discipline_by_sheet_id = discipline_by_sheet_id or {}
    rows: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for d in detections:
        bbox = d.get("bbox_pt", [0, 0, 0, 0])
        x0, y0, x1, y1 = (bbox + [0, 0, 0, 0])[:4]
        # class string: prefer canonical_name (set by tenant memory or legend),
        # fall back to a 'sig:<8>' placeholder which the naming pass below
        # will rewrite into an interpretable string.
        original_class = d.get("canonical_name") or f"sig:{d['signature_hash'][:8]}"
        sheet_id = d["sheet_id"]
        # Apply interpretable fallback only for opaque names — real names
        # (tenant_memory / legend hits) pass through unchanged.
        # Wave 17b.2 — spatial_ocr names are anchored to architect text → 0.85.
        _source = d.get("name_source")
        if _source in ("tenant_memory", "legend"):
            confidence = 0.95
        elif _source == "spatial_ocr":
            confidence = 0.85
        else:
            confidence = 0.60
        if is_opaque_name(original_class):
            class_name = interpretable_fallback(
                original_class,
                discipline_by_sheet_id.get(sheet_id),
                confidence,
            )
        else:
            class_name = original_class
        rows.append({
            "id": str(uuid.uuid4()),
            "suite_id": d["suite_id"],
            "office_id": d.get("office_id"),
            "sheet_id": sheet_id,
            "class": class_name,
            "bbox": {
                "x": x0,
                "y": y0,
                "w": max(0.0, x1 - x0),
                "h": max(0.0, y1 - y0),
                "signature_hash": d["signature_hash"],
                "original_class": original_class,
                "instance_count": d["count"],
                "name_source": d.get("name_source"),
                "entropy": d.get("spatial_entropy"),
            },
            "confidence": confidence,
            "created_at": now,
            "created_by": None,
        })
    return rows
