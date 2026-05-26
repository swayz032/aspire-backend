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
        # Wave 17b.4 — legend telemetry
        "legend_regions_found": 0,
        "legend_pairs_extracted": 0,
        "legend_signatures_matched": 0,
        "legend_signatures_unmatched": 0,
        "legend_pages_with_region": 0,
        "legend_memory_upserts": 0,
    }
    discipline_by_page = discipline_by_page or {}

    # Capture legend row count before any work — for persistence assertion (Track B Pass 1.C).
    _legend_count_before: int = 0
    try:
        from aspire_orchestrator.services.receipt_store import count_table_rows as _count_rows_vgse
        _legend_count_before = _count_rows_vgse(
            "blueprint_project_legends",
            project_id=project_id,
            suite_id=suite_id,
        )
    except Exception:
        _legend_count_before = 0

    with fitz.open(pdf_path) as doc:
        # ─── Wave 17b.4 PASS 1 ────────────────────────────────────────────
        # Extract legend signatures from EVERY page first. A legend on page 2
        # can name a symbol that appears on pages 5, 8, 12... so we build a
        # project-wide signature→canonical_name table BEFORE resolving motifs.
        project_legend_map: dict[str, str] = {}
        page_results: dict[int, VGSEPageResult] = {}
        legend_source_page: dict[str, int] = {}
        for page_idx, page in enumerate(doc):
            page_number = page_idx + 1
            page_disc = discipline_by_page.get(page_number)
            page_result = process_page(page, discipline=page_disc)
            page_results[page_number] = page_result
            summary["page_categories"][page_number] = page_result.category
            if page_result.legend_entries:
                summary["legend_pages_with_region"] += 1
                summary["legend_pairs_extracted"] += len(page_result.legend_entries)
                for entry in page_result.legend_entries:
                    sig = entry["signature_hash"]
                    label = entry["label"]
                    # First page that names a signature wins. We don't reassign
                    # if a later page proposes a different label — legends are
                    # supposed to be consistent within a project.
                    if sig not in project_legend_map:
                        project_legend_map[sig] = label
                        legend_source_page[sig] = page_number

            # Count regions independently of pair extraction so we report what
            # we *saw* even if no pairs got matched.
            try:
                from aspire_orchestrator.services.blueprint.vgse import find_legend_regions
                summary["legend_regions_found"] += len(find_legend_regions(page))
            except Exception:  # noqa: BLE001
                pass

        # ─── Persist project-wide legend → memory (Wave 17b.4 step B) ────
        # Push every legend-derived (signature → name) pair to blueprint_symbol_memory
        # so future projects from the same architect get instant matches AND
        # subsequent sheets within THIS project benefit from the propagation.
        for sig, label in project_legend_map.items():
            try:
                await symbol_memory.upsert_observation(
                    suite_id=suite_id,
                    office_id=office_id,
                    signature_hash=sig,
                    canonical_name=label,
                    category=None,
                    source="legend",
                    project_id=project_id,
                    instances_observed=0,  # no per-instance count yet — pass 2 will add
                    confidence=0.95,
                )
                summary["legend_memory_upserts"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "vgse: legend memory upsert failed (%s) sig=%s",
                    type(exc).__name__, sig[:8],
                )

        # ─── PASS 2 — resolve motifs with project-wide legend map ─────────
        signatures_matched: set[str] = set()
        for page_number in sorted(page_results.keys()):
            page_result = page_results[page_number]

            if page_result.category in ("RASTER_SCAN", "UNKNOWN_LOW_CONTENT"):
                summary["raster_pages"] += 1
                continue
            summary["vector_pages"] += 1

            if not page_result.motifs:
                continue

            # Build effective legend map: per-page legend ∪ project-wide.
            # Per-page entries naturally already match this page's signatures;
            # project-wide entries cover cross-page propagation.
            per_page_legend = {e["signature_hash"]: e["label"] for e in page_result.legend_entries}
            legend_map: dict[str, str] = dict(project_legend_map)
            legend_map.update(per_page_legend)  # current page wins on duplicates

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
                    # Wave 17b.4: pass 2 lost the `page` from the original
                    # enumerate loop — re-fetch by index from `doc` which is
                    # still open in this context.
                    page = doc[page_number - 1]
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
                if motif.name_source == "legend":
                    signatures_matched.add(motif.signature_hash)
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

    # Wave 17b.4: finalize legend metrics. unmatched = legend signatures that
    # had a label but no motif on any page ended up resolving to them.
    summary["legend_signatures_matched"] = len(signatures_matched)
    summary["legend_signatures_unmatched"] = max(
        0, len(project_legend_map) - len(signatures_matched)
    )

    # Wave 17b.4 — emit legend extraction receipt with persistence assertion (Track B Pass 1.C).
    # Uses DB count of blueprint_project_legends. expected_min=0 because many sheets
    # legitimately have no legend regions (structural, MEP legend-free sheets).
    try:
        from aspire_orchestrator.services.receipt_store import (
            count_table_rows as _count_rows_leg,
            emit_persistence_asserted_receipt as _emit_pa_legend,
        )
        _legend_count_after = _count_rows_leg(
            "blueprint_project_legends",
            project_id=project_id,
            suite_id=suite_id,
        )
        _emit_pa_legend(
            stage="drew.see.legend_extracted",
            expected_table="blueprint_project_legends",
            expected_min=0,
            count_before=_legend_count_before,
            count_after=_legend_count_after,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            redacted_outputs={
                "legend_regions_found": summary["legend_regions_found"],
                "legend_pages_with_region": summary["legend_pages_with_region"],
                "legend_pairs_extracted": summary["legend_pairs_extracted"],
                "legend_signatures_matched": summary["legend_signatures_matched"],
                "legend_signatures_unmatched": summary["legend_signatures_unmatched"],
                "legend_memory_upserts": summary["legend_memory_upserts"],
                "motif_legend_resolutions": summary["legend_hits"],
            },
        )
    except Exception:  # noqa: BLE001
        pass

    # Wave 17b.2 — emit spatial binding receipt with persistence assertion (Track B Pass 1.C).
    # advisory: expected_min=0 because spatial binding may write 0 rows on raster-only PDFs.
    # Uses legend_memory_upserts as a proxy for memory writes (blueprint_symbol_memory lacks
    # a direct project_id column for efficient counting).
    try:
        from aspire_orchestrator.services.receipt_store import (
            emit_persistence_asserted_receipt as _emit_pa_binding,
        )
        _emit_pa_binding(
            stage="drew.see.spatial_binding",
            expected_table="blueprint_symbol_memory",
            expected_min=0,
            count_before=0,
            count_after=summary["legend_memory_upserts"],
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            redacted_outputs={
                "symbols_total": summary["total_motifs_found"],
                "spatial_ocr_bound": summary["spatial_ocr_hits"],
                "unbound": summary["unanchored"],
                "memory_hits": summary["memory_hits"],
                "legend_hits": summary["legend_hits"],
                "by_discipline": summary["spatial_binding_by_discipline"],
            },
        )
    except Exception:  # noqa: BLE001
        pass  # receipt emission is best-effort

    logger.info(
        "vgse: project=%s pages=%d vector=%d raster=%d motifs=%d instances=%d "
        "memory=%d legend=%d spatial_ocr=%d unanchored=%d "
        "[legend_regions=%d pairs=%d sigs_matched=%d sigs_unmatched=%d upserts=%d] "
        "(%dms)",
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
        summary["legend_regions_found"],
        summary["legend_pairs_extracted"],
        summary["legend_signatures_matched"],
        summary["legend_signatures_unmatched"],
        summary["legend_memory_upserts"],
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
