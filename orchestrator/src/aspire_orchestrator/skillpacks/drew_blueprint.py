# [STATUS: v1-active, BYPASS] — Reachable via /v1/agents/invoke-sync.
# Bypasses LangGraph: no policy gate, no token mint, no central receipt audit.
# Migration debt — route through /v1/intents in a later wave.
"""Drew Blueprint Story Engine — Wave 2A: INGEST + CLASSIFY implemented.

Reads architectural blueprints, builds a multi-discipline understanding, and produces
a phase-by-phase build narrative with line-item materials.

Pipeline tasks (orchestrator-driven, Drew never decides):
    INGEST   → parse PDFs (LlamaParse primary, Azure Doc Intel fallback)
    CLASSIFY → assign discipline + sheet metadata
    SEE      → vision pass for symbols / bbox / confidence  [stub]
    REASON   → derive assemblies, materials, story-by-phase  [stub]
    PROCURE  → push material requests to supplier playbooks  [stub]

Law compliance:
  - Law #1: Skill pack runs bounded tasks; orchestrator decides invocation.
  - Law #2: Every method emits a receipt via _emit_receipt.
  - Law #3: Fails closed on unknown task or missing prompt/model env in production.
  - Law #6: All DB writes include suite_id; queries filter via RLS.
  - Law #7: No autonomous decisions; tasks are dispatched in by name.
  - Law #9: PDF bytes never logged; only len(), hash prefixes, and page counts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

PROMPT_PATH = (
    Path(__file__).parent.parent
    / "services"
    / "blueprint"
    / "prompts"
    / "drew_system_prompt.md"
)

ACTOR_DREW = "skillpack:drew-blueprint"
RECEIPT_VERSION = "1.0"


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """SHA256 of canonicalized inputs for receipt linkage (mirrors Adam)."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _build_receipt(
    *,
    correlation_id: str,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Drew receipt aligned with Adam's shape (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor": ACTOR_DREW,
        "correlation_id": correlation_id,
        "status": status,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": "allow" if status != "denied" else "deny",
            "policy_id": "drew-blueprint-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    return receipt


class Drew:
    """Drew skill pack — blueprint ingestion + story generation."""

    actor: str = "drew"

    def __init__(self) -> None:
        if not PROMPT_PATH.exists():
            raise RuntimeError(f"Drew system prompt missing: {PROMPT_PATH}")
        self.system_prompt: str = PROMPT_PATH.read_text(encoding="utf-8")

        env = os.getenv("ASPIRE_ENV", "development")
        if env == "production":
            model = os.getenv("ASPIRE_DREW_MODEL_PROD") or os.getenv("DREW_MODEL_PROD")
            if not model:
                raise RuntimeError(
                    "ASPIRE_DREW_MODEL_PROD env var required in production (Law #3)",
                )
            self.model: str = model
        else:
            self.model = (
                os.getenv("ASPIRE_DREW_MODEL_DEV")
                or os.getenv("DREW_MODEL_DEV")
                or "gpt-5.4-mini"
            )

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------
    def run_agentic_loop(
        self,
        task: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        """Route an inbound task to its handler. Fails closed on unknown task."""
        dispatch: dict[str, Any] = {
            "INGEST": self.ingest,
            "CLASSIFY": self.classify,
            "SEE": self.see,
            "REASON": self.reason,
            "PROCURE": self.procure,
        }
        handler = dispatch.get(task)
        if handler is None:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="drew.unknown_task",
                status="denied",
                inputs={"task": task},
            )
            return {"status": "deny", "reason": f"unknown task: {task}"}
        return handler(payload, correlation_id)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Stage 1: INGEST (Wave 2A — real implementation)
    # ------------------------------------------------------------------
    def ingest(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Ingest a blueprint PDF: split, OCR, store sheets, upload thumbnails.

        Payload keys:
          - pdf_bytes: base64-encoded PDF binary
          - filename: str (for receipt metadata)
          - suite_id: UUID str
          - office_id: UUID str

        Returns:
          {"status": "ok", "stage": "ingest", "project_id": str,
           "sheet_count": int, "sheet_ids": [str]}

        Idempotency: SHA-256 of the PDF bytes is checked against blueprint_projects.
        If a matching project already exists, returns it without re-ingesting.

        Law #6: All inserts include suite_id; RLS enforces isolation.
        Law #9: PDF bytes never logged; only len(), hash prefix, and page count.
        """
        # Validate required payload keys
        required_keys = ("pdf_bytes", "suite_id", "office_id")
        for key in required_keys:
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.ingest",
                    status="failed",
                    inputs={"task": "INGEST", "missing_key": key},
                )
                return {"status": "error", "stage": "ingest", "reason": f"missing payload key: {key}"}

        suite_id: str = str(payload["suite_id"])
        office_id: str = str(payload["office_id"])
        filename: str = str(payload.get("filename", "blueprint.pdf"))

        # Decode base64 PDF bytes
        try:
            pdf_bytes: bytes = base64.b64decode(payload["pdf_bytes"])
        except Exception as exc:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.ingest",
                status="failed",
                inputs={"task": "INGEST", "suite_id": suite_id},
            )
            return {"status": "error", "stage": "ingest", "reason": f"invalid base64 pdf_bytes: {type(exc).__name__}"}

        # Compute project hash for idempotency check (Law #3: fail-closed on dup)
        project_hash: str = hashlib.sha256(pdf_bytes).hexdigest()

        # Mark stage as in_progress before starting pipeline (best-effort)
        # We need a project_id first, so we optimistically set it after creation.
        # The _async_ingest_pipeline handles in_progress + done/failed internally.

        # Run the async pipeline in a sync context (Drew.ingest is sync; called
        # from run_agentic_loop which is sync). Use asyncio.run or get_event_loop.
        try:
            result = _run_async_ingest(
                pdf_bytes=pdf_bytes,
                project_hash=project_hash,
                suite_id=suite_id,
                office_id=office_id,
                filename=filename,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.ingest",
                status="failed",
                inputs={"task": "INGEST", "suite_id": suite_id, "project_hash": project_hash[:8]},
                metadata={"error": type(exc).__name__},
            )
            return {"status": "error", "stage": "ingest", "reason": f"ingest pipeline failed: {type(exc).__name__}"}

        # Emit receipt on success or dedup
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.ingest",
            status=result["status"],
            inputs={"task": "INGEST", "suite_id": suite_id, "project_hash": project_hash[:8]},
            metadata={
                "project_id": result.get("project_id"),
                "sheet_count": result.get("sheet_count", 0),
                "provider_mix": result.get("provider_mix", {}),
                "filename": filename,
                "pdf_size_bytes": len(pdf_bytes),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Stage 2: CLASSIFY (Wave 2A — real implementation)
    # ------------------------------------------------------------------
    def classify(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Classify blueprint sheets: discipline tagging + revision linking.

        Payload keys:
          - project_id: UUID str
          - suite_id: UUID str

        Returns:
          {"status": "ok", "stage": "classify", "project_id": str,
           "discipline_counts": {...}, "revisions": int}

        Law #6: DB queries filter by suite_id via RLS.
        """
        required_keys = ("project_id", "suite_id")
        for key in required_keys:
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.classify",
                    status="failed",
                    inputs={"task": "CLASSIFY", "missing_key": key},
                )
                return {"status": "error", "stage": "classify", "reason": f"missing payload key: {key}"}

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])

        # Mark in_progress before classifying
        _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="classify", state="in_progress")

        try:
            result = _run_async_classify(
                project_id=project_id,
                suite_id=suite_id,
                model=self.model,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="classify", state="failed")
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.classify",
                status="failed",
                inputs={"task": "CLASSIFY", "project_id": project_id},
                metadata={"error": type(exc).__name__},
            )
            return {"status": "error", "stage": "classify", "reason": f"classify pipeline failed: {type(exc).__name__}"}

        _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="classify", state="done")
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.classify",
            status=result["status"],
            inputs={"task": "CLASSIFY", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "discipline_counts": result.get("discipline_counts", {}),
                "revisions": result.get("revisions", 0),
                "needs_review_count": result.get("needs_review_count", 0),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Remaining stages — stubs (Wave 3-5)
    # Progress wiring is real; stage body is replaced by Wave 3/4/5.
    # ------------------------------------------------------------------
    def see(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        project_id = str(payload.get("project_id", ""))
        suite_id = str(payload.get("suite_id", ""))
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="in_progress")
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.see",
            status="stub",
            inputs={"task": "SEE"},
        )
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="done")
        return {"status": "stub", "stage": "see"}

    def reason(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        project_id = str(payload.get("project_id", ""))
        suite_id = str(payload.get("suite_id", ""))
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="reason", state="in_progress")
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.reason",
            status="stub",
            inputs={"task": "REASON"},
        )
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="reason", state="done")
        return {"status": "stub", "stage": "reason"}

    def procure(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        project_id = str(payload.get("project_id", ""))
        suite_id = str(payload.get("suite_id", ""))
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="procure", state="in_progress")
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.procure",
            status="stub",
            inputs={"task": "PROCURE"},
        )
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="procure", state="done")
        return {"status": "stub", "stage": "procure"}

    # ------------------------------------------------------------------
    # Receipt emission (Law #2)
    # ------------------------------------------------------------------
    def _emit_receipt(
        self,
        *,
        correlation_id: str,
        event_type: str,
        status: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        receipt = _build_receipt(
            correlation_id=correlation_id,
            event_type=event_type,
            status=status,
            inputs=inputs,
            metadata=metadata,
        )
        store_receipts([receipt])
        return receipt


# ---------------------------------------------------------------------------
# Async pipeline helpers (called via asyncio bridge from sync .ingest/.classify)
# ---------------------------------------------------------------------------

def _run_async_set_stage(
    *,
    project_id: str,
    suite_id: str,
    stage: str,
    state: str,
) -> None:
    """Bridge: run async set_stage_progress from sync context.

    Swallows all errors — progress tracking is best-effort.
    """
    if not project_id or not suite_id:
        return

    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress

    async def _run() -> None:
        await set_stage_progress(
            project_id=project_id,
            stage=stage,
            state=state,
            suite_id=suite_id,
        )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                future.result(timeout=10)
        else:
            loop.run_until_complete(_run())
    except RuntimeError:
        try:
            asyncio.run(_run())
        except Exception:
            pass
    except Exception:
        pass  # Never propagate — progress is best-effort


def _run_async_ingest(
    *,
    pdf_bytes: bytes,
    project_hash: str,
    suite_id: str,
    office_id: str,
    filename: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async ingest pipeline from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Running under uvicorn/asyncio — use a new event loop in thread pool
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_ingest_pipeline(
                        pdf_bytes=pdf_bytes,
                        project_hash=project_hash,
                        suite_id=suite_id,
                        office_id=office_id,
                        filename=filename,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=120)
        else:
            return loop.run_until_complete(
                _async_ingest_pipeline(
                    pdf_bytes=pdf_bytes,
                    project_hash=project_hash,
                    suite_id=suite_id,
                    office_id=office_id,
                    filename=filename,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_ingest_pipeline(
                pdf_bytes=pdf_bytes,
                project_hash=project_hash,
                suite_id=suite_id,
                office_id=office_id,
                filename=filename,
                correlation_id=correlation_id,
            )
        )


async def _async_ingest_pipeline(
    *,
    pdf_bytes: bytes,
    project_hash: str,
    suite_id: str,
    office_id: str,
    filename: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Async ingest: idempotency check → OCR → store sheets → upload thumbnails."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_insert,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
    from aspire_orchestrator.services.blueprint.thumbnail_storage import upload_sheet_thumbnail
    from aspire_orchestrator.services.receipt_store import store_receipts

    # Idempotency check: look for existing project with same content hash
    try:
        existing = await supabase_select(
            "blueprint_projects",
            filters=f"suite_id=eq.{suite_id}&address=eq.{project_hash}",
            limit=1,
        )
        if existing:
            project_id = str(existing[0]["id"])
            _log.info(
                "drew.ingest: dedup hit for hash=%s suite=%s, returning existing project=%s",
                project_hash[:8],
                suite_id[:8],
                project_id[:8],
            )
            # Count existing sheets for dedup receipt
            sheets = await supabase_select(
                "blueprint_sheets",
                filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            )
            sheet_ids = [str(s["id"]) for s in sheets]
            return {
                "status": "dedup",
                "stage": "ingest",
                "project_id": project_id,
                "sheet_count": len(sheet_ids),
                "sheet_ids": sheet_ids,
                "provider_mix": {},
            }
    except SupabaseClientError as exc:
        _log.warning(
            "drew.ingest: idempotency check failed (%s), proceeding with fresh ingest",
            type(exc).__name__,
        )

    # Create project row (using address field to store hash for idempotency key)
    project_id = str(uuid.uuid4())
    try:
        await supabase_insert(
            "blueprint_projects",
            {
                "id": project_id,
                "suite_id": suite_id,
                "office_id": office_id,
                "address": project_hash,  # Repurposed for content hash idempotency
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except SupabaseClientError as exc:
        raise RuntimeError(f"Failed to create blueprint_project: {exc}") from exc

    # Mark ingest stage as in_progress
    await set_stage_progress(
        project_id=project_id,
        stage="ingest",
        state="in_progress",
        suite_id=suite_id,
    )

    _log.info(
        "drew.ingest: created project=%s, hash=%s, corr=%s",
        project_id[:8],
        project_hash[:8],
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # OCR: extract sheet corpus
    corpus = await extract_sheet_corpus(
        pdf_bytes,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    # Store each sheet + upload thumbnail
    sheet_ids: list[str] = []
    thumbnail_failures: int = 0
    for ocr_sheet in corpus.sheets:
        sheet_id = str(uuid.uuid4())
        try:
            await supabase_insert(
                "blueprint_sheets",
                {
                    "id": sheet_id,
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "project_id": project_id,
                    "sheet_number": str(ocr_sheet.page_number),
                    "ocr_text": ocr_sheet.text,
                    "hash": ocr_sheet.page_hash,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            sheet_ids.append(sheet_id)
        except SupabaseClientError as exc:
            _log.error(
                "drew.ingest: failed to insert sheet page=%d error=%s",
                ocr_sheet.page_number,
                type(exc).__name__,
            )
            # Continue — partial ingest with error is better than total failure
            continue

        # Upload thumbnail for this sheet (soft failure — sheet is still useful)
        if ocr_sheet.image_bytes:
            signed_url = await upload_sheet_thumbnail(
                suite_id=suite_id,
                project_id=project_id,
                sheet_id=sheet_id,
                png_bytes=ocr_sheet.image_bytes,
                correlation_id=correlation_id,
            )
            if signed_url:
                try:
                    await supabase_update(
                        "blueprint_sheets",
                        f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                        {"thumbnail_url": signed_url},
                    )
                except SupabaseClientError:
                    _log.warning(
                        "drew.ingest: failed to write thumbnail_url for sheet=%s",
                        sheet_id[:8],
                    )
            else:
                # Thumbnail upload failed — emit receipt for observability (Law #2)
                thumbnail_failures += 1
                store_receipts([
                    {
                        "receipt_version": RECEIPT_VERSION,
                        "receipt_id": str(uuid.uuid4()),
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event_type": "blueprint.ingest.thumbnail_upload_failed",
                        "actor": ACTOR_DREW,
                        "correlation_id": correlation_id,
                        "status": "failed",
                        "inputs_hash": _compute_inputs_hash({"sheet_id": sheet_id}),
                        "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                        "redactions": [],
                        "metadata": {
                            "sheet_id": sheet_id,
                            "project_id": project_id,
                            "suite_id": suite_id,
                        },
                    }
                ])

    # Mark ingest stage done (or failed if no sheets persisted)
    final_state = "done" if sheet_ids else "failed"
    await set_stage_progress(
        project_id=project_id,
        stage="ingest",
        state=final_state,
        suite_id=suite_id,
    )

    _log.info(
        "drew.ingest: stored %d sheets for project=%s thumbnail_failures=%d corr=%s",
        len(sheet_ids),
        project_id[:8],
        thumbnail_failures,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    return {
        "status": "ok",
        "stage": "ingest",
        "project_id": project_id,
        "sheet_count": len(sheet_ids),
        "sheet_ids": sheet_ids,
        "provider_mix": corpus.provider_mix,
    }


def _run_async_classify(
    *,
    project_id: str,
    suite_id: str,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async classify pipeline from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_classify_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        model=model,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=120)
        else:
            return loop.run_until_complete(
                _async_classify_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    model=model,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_classify_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                model=model,
                correlation_id=correlation_id,
            )
        )


async def _async_classify_pipeline(
    *,
    project_id: str,
    suite_id: str,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Async classify: discipline tagging + revision detection + DB updates."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.discipline_tagger import tag_disciplines
    from aspire_orchestrator.services.blueprint.revision_detector import detect_revisions

    # Load sheets for this project (RLS enforces suite_id at DB level)
    try:
        sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except SupabaseClientError as exc:
        raise RuntimeError(f"Failed to load sheets for project {project_id[:8]}: {exc}") from exc

    if not sheets:
        _log.warning(
            "drew.classify: no sheets found for project=%s, corr=%s",
            project_id[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        return {
            "status": "ok",
            "stage": "classify",
            "project_id": project_id,
            "discipline_counts": {},
            "revisions": 0,
            "needs_review_count": 0,
        }

    # Discipline tagging
    tags = await tag_disciplines(
        sheets,
        model=model,
        correlation_id=correlation_id,
    )

    # Build sheet_id → tag index and persist discipline/confidence
    tag_index = {t.sheet_id: t for t in tags}
    discipline_counts: dict[str, int] = {}
    needs_review_count = 0

    for sheet in sheets:
        sheet_id = str(sheet["id"])
        tag = tag_index.get(sheet_id)
        if not tag:
            continue

        discipline_value = tag.discipline
        if discipline_value:
            discipline_counts[discipline_value] = discipline_counts.get(discipline_value, 0) + 1
        if tag.needs_review:
            needs_review_count += 1

        # Update sheet row with discipline
        update_data: dict[str, Any] = {}
        if discipline_value:
            update_data["discipline"] = discipline_value
        if tag.needs_review:
            # Insert a missing_inputs row for contractor review
            try:
                from aspire_orchestrator.services.supabase_client import supabase_insert
                await supabase_insert(
                    "blueprint_missing_inputs",
                    {
                        "id": str(uuid.uuid4()),
                        "suite_id": suite_id,
                        "project_id": project_id,
                        "description": (
                            f"Sheet {sheet.get('sheet_number', sheet_id[:8])}: "
                            f"discipline confidence {tag.confidence:.0%} below threshold. "
                            f"Tagger reasoning: {tag.reasoning}"
                        ),
                        "suggested_resolution": "Please verify the discipline for this sheet.",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.classify: failed to insert missing_inputs for sheet %s: %s",
                    sheet_id[:8],
                    type(exc).__name__,
                )

        if update_data:
            try:
                await supabase_update(
                    "blueprint_sheets",
                    f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                    update_data,
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.classify: failed to update sheet %s: %s",
                    sheet_id[:8],
                    type(exc).__name__,
                )

    # Revision detection
    revision_links = detect_revisions(sheets)
    revision_count = len(revision_links)

    for link in revision_links:
        try:
            await supabase_update(
                "blueprint_sheets",
                f"id=eq.{link.superseding_sheet_id}&suite_id=eq.{suite_id}",
                {"supersedes_id": link.superseded_sheet_id},
            )
        except SupabaseClientError as exc:
            _log.warning(
                "drew.classify: failed to update supersedes_id for sheet %s: %s",
                link.superseding_sheet_id[:8],
                type(exc).__name__,
            )

    _log.info(
        "drew.classify: project=%s, disciplines=%s, revisions=%d, needs_review=%d, corr=%s",
        project_id[:8],
        discipline_counts,
        revision_count,
        needs_review_count,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    return {
        "status": "ok",
        "stage": "classify",
        "project_id": project_id,
        "discipline_counts": discipline_counts,
        "revisions": revision_count,
        "needs_review_count": needs_review_count,
    }
