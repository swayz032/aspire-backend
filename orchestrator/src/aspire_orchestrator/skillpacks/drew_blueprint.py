# [STATUS: v1-active, BYPASS] — Reachable via /v1/agents/invoke-sync.
# Bypasses LangGraph: no policy gate, no token mint, no central receipt audit.
# Migration debt — route through /v1/intents in a later wave.
"""Drew Blueprint Story Engine — Wave 3: SEE implemented (INGEST + CLASSIFY + SEE live).

Reads architectural blueprints, builds a multi-discipline understanding, and produces
a phase-by-phase build narrative with line-item materials.

Pipeline tasks (orchestrator-driven, Drew never decides):
    INGEST   → parse PDFs (LlamaParse primary, Azure Doc Intel fallback)
    CLASSIFY → assign discipline + sheet metadata
    SEE      → YOLOv11 symbol detection + scale calibration + engineer-seal flag  [Wave 3]
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
    # Stage 3: SEE (Wave 3 — real implementation, stage_progress wired in Wave 2.5)
    # ------------------------------------------------------------------
    def see(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Run YOLOv11 symbol detection + scale calibration + seal flagging.

        Payload keys (required):
          - project_id: UUID str — the project to scan.
          - suite_id:   UUID str — tenant scope.
          - pdf_bytes:  base64-encoded PDF binary — re-rendered to recover
                        the per-sheet 200 DPI images (we never persist images).

        Optional:
          - office_id:  UUID str
          - confidence_floor: float (default 0.70)

        Returns:
          {"status": "ok"|"error"|"failed", "stage": "see",
           "project_id": str, "sheet_count": int, "symbol_count": int,
           "seal_sheets": int, "missing_inputs": int,
           "mean_confidence": float, "model_version": str}

        Image source: Wave 1 pdf_splitter renders pages at 200 DPI but the
        raster bytes are not stored in DB. The caller resupplies pdf_bytes
        in the payload; we re-split and match by SHA-256 hash to the
        blueprint_sheets rows persisted by INGEST.

        Receipts:
          - blueprint.see — emitted once per call. metadata includes the
            aggregate symbol_count, mean_confidence, seal_sheets,
            missing_inputs, sheet_count, model_version.

        Stage progress (Wave 2.5):
          - On entry with valid project_id+suite_id: stage_progress["see"]="in_progress"
          - On success: stage_progress["see"]="done"
          - On failure: stage_progress["see"]="failed"
        """
        # Validate required payload keys
        for key in ("project_id", "suite_id", "pdf_bytes"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.see",
                    status="failed",
                    inputs={"task": "SEE", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "see",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None
        confidence_floor: float = float(payload.get("confidence_floor", 0.70))

        # Wave 2.5: mark stage in_progress (no-op if project_id/suite_id falsy)
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="in_progress")

        try:
            pdf_bytes: bytes = base64.b64decode(payload["pdf_bytes"])
        except Exception as exc:
            if project_id and suite_id:
                _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="failed")
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.see",
                status="failed",
                inputs={"task": "SEE", "project_id": project_id},
                metadata={"error": f"invalid base64 pdf_bytes: {type(exc).__name__}"},
            )
            return {
                "status": "error",
                "stage": "see",
                "reason": f"invalid base64 pdf_bytes: {type(exc).__name__}",
            }

        try:
            result = _run_async_see(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                pdf_bytes=pdf_bytes,
                confidence_floor=confidence_floor,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            if project_id and suite_id:
                _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="failed")
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.see",
                status="failed",
                inputs={"task": "SEE", "project_id": project_id, "suite_id": suite_id},
                metadata={"error": type(exc).__name__},
            )
            return {
                "status": "error",
                "stage": "see",
                "reason": f"see pipeline failed: {type(exc).__name__}",
            }

        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.see",
            status=result["status"],
            inputs={"task": "SEE", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "sheet_count": result.get("sheet_count", 0),
                "symbol_count": result.get("symbol_count", 0),
                "mean_confidence": result.get("mean_confidence", 0.0),
                "seal_sheets": result.get("seal_sheets", 0),
                "missing_inputs": result.get("missing_inputs", 0),
                "model_version": result.get("model_version", "yolo11m.pt"),
            },
        )
        # Wave 2.5: mark stage done/failed based on result
        if project_id and suite_id:
            final_state = "done" if result.get("status") == "ok" else "failed"
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state=final_state)
        return result

    # ------------------------------------------------------------------
    # Stage 4: REASON (Wave 4 — real implementation)
    # ------------------------------------------------------------------
    def reason(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Derive assemblies, materials, phased story from sheets + symbols.

        Payload keys (required):
          - project_id: UUID str
          - suite_id:   UUID str

        Optional:
          - office_id: UUID str

        Returns:
          {"status": "ok"|"error", "stage": "reason",
           "project_id": str, "phase_count": int, "assembly_count": int,
           "material_count": int, "missing_input_count": int,
           "mean_confidence": float, "truth_distribution": dict,
           "model_version": str}

        Receipts:
          - blueprint.reason — emitted once per call. metadata includes counts
            and truth_distribution (never the story markdown — Law #9).

        Stage progress (Wave 2.5):
          - in_progress on entry with valid project_id+suite_id
          - done on success
          - failed on exception
        """
        # Validate required payload keys
        for key in ("project_id", "suite_id"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.reason",
                    status="failed",
                    inputs={"task": "REASON", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "reason",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None

        # Wave 2.5: mark stage in_progress
        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="reason", state="in_progress"
        )

        try:
            result = _run_async_reason(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                model=self.model,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(
                project_id=project_id, suite_id=suite_id, stage="reason", state="failed"
            )
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.reason",
                status="failed",
                inputs={"task": "REASON", "project_id": project_id, "suite_id": suite_id},
                metadata={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            return {
                "status": "error",
                "stage": "reason",
                "reason": f"reason pipeline failed: {type(exc).__name__}",
            }

        # Emit receipt (counts only — no markdown, Law #9)
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.reason",
            status=result.get("status", "ok"),
            inputs={"task": "REASON", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "phase_count": result.get("phase_count", 0),
                "assembly_count": result.get("assembly_count", 0),
                "material_count": result.get("material_count", 0),
                "missing_input_count": result.get("missing_input_count", 0),
                "mean_confidence": result.get("mean_confidence", 0.0),
                "truth_distribution": result.get("truth_distribution", {}),
                "model_version": result.get("model_version", self.model),
            },
        )

        final_state = "done" if result.get("status") == "ok" else "failed"
        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="reason", state=final_state
        )
        return result

    def procure(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Procure: tariff classification + supplier matching for all blueprint_materials.

        Risk tier: GREEN (read + classify). Push-to-materials is YELLOW (Law #4) and
        requires a capability token for materials.bundle.add — enforced at gateway.
        Law #6: All DB reads/writes scoped by suite_id.
        Law #9: line_item text truncated to 100 chars in logs and receipts.
        """
        for key in ("project_id", "suite_id"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.procure",
                    status="failed",
                    inputs={"task": "PROCURE", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "procure",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None
        tenant_id: str = str(payload.get("tenant_id") or suite_id)
        office_zip: str | None = str(payload["office_zip"]) if payload.get("office_zip") else None
        office_lat: float | None = float(payload["office_lat"]) if payload.get("office_lat") is not None else None
        office_lng: float | None = float(payload["office_lng"]) if payload.get("office_lng") is not None else None
        geofence_miles: float = float(payload.get("geofence_miles", 25.0))

        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="procure", state="in_progress"
        )

        try:
            result = _run_async_procure(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                office_zip=office_zip,
                office_lat=office_lat,
                office_lng=office_lng,
                tenant_id=tenant_id,
                geofence_miles=geofence_miles,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(
                project_id=project_id, suite_id=suite_id, stage="procure", state="failed"
            )
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.procure",
                status="failed",
                inputs={"task": "PROCURE", "project_id": project_id, "suite_id": suite_id},
                metadata={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            return {
                "status": "error",
                "stage": "procure",
                "reason": f"procure pipeline failed: {type(exc).__name__}",
            }

        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.procure",
            status=result.get("status", "ok"),
            inputs={"task": "PROCURE", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "materials_processed": result.get("materials_processed", 0),
                "tariff_flagged": result.get("tariff_flagged", 0),
                "tariff_breakdown": result.get("tariff_breakdown", {}),
                "suppliers_matched": result.get("suppliers_matched", 0),
                "supplier_match_rate": result.get("supplier_match_rate", 0.0),
                "memory_writes": result.get("memory_writes", 0),
            },
        )

        final_state = "done" if result.get("status") == "ok" else "failed"
        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="procure", state=final_state
        )
        return result

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
# Async pipeline helpers (called via asyncio bridge from sync .ingest/.classify/.see)
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


# ---------------------------------------------------------------------------
# Stage 3 SEE — async helpers
# ---------------------------------------------------------------------------

def _run_async_see(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    pdf_bytes: bytes,
    confidence_floor: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async SEE pipeline from sync context (mirrors ingest/classify)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_see_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        pdf_bytes=pdf_bytes,
                        confidence_floor=confidence_floor,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=600)  # SEE is slower; 10min ceiling
        else:
            return loop.run_until_complete(
                _async_see_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    pdf_bytes=pdf_bytes,
                    confidence_floor=confidence_floor,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_see_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                pdf_bytes=pdf_bytes,
                confidence_floor=confidence_floor,
                correlation_id=correlation_id,
            )
        )


async def _async_see_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    pdf_bytes: bytes,
    confidence_floor: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Per-sheet SEE: YOLO symbols + scale + seal. Persists symbols/scale/flag.

    Sheet matching strategy: pdf_splitter re-renders the PDF and produces the
    same SHA-256 hashes the INGEST stage stored. We index sheet rows by hash
    so we know which DB row each rendered image belongs to.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_insert,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.pdf_splitter import split_pdf_to_sheets
    from aspire_orchestrator.services.blueprint.symbol_detector import (
        SymbolDetectorError,
        detect_symbols,
    )
    from aspire_orchestrator.services.blueprint.scale_calibrator import calibrate_scale
    from aspire_orchestrator.services.blueprint.seal_detector import detect_engineer_seal

    # Load all sheet rows for this project (RLS-scoped).
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
            "drew.see: no sheets found for project=%s, corr=%s",
            project_id[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        return {
            "status": "ok",
            "stage": "see",
            "project_id": project_id,
            "sheet_count": 0,
            "symbol_count": 0,
            "mean_confidence": 0.0,
            "seal_sheets": 0,
            "missing_inputs": 0,
            "model_version": "yolo11m.pt",
        }

    # Re-render PDF to recover per-sheet 200 DPI PNG bytes.
    try:
        extracts = split_pdf_to_sheets(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"pdf_splitter failed in SEE: {type(exc).__name__}") from exc

    # Index DB rows by hash (the canonical join key).
    sheet_by_hash: dict[str, dict[str, Any]] = {
        str(row.get("hash")): row for row in sheets if row.get("hash")
    }

    total_symbols = 0
    seal_sheets = 0
    missing_inputs = 0
    confidence_sum = 0.0
    confidence_count = 0
    model_version = "yolo11m.pt"

    for extract in extracts:
        sheet_row = sheet_by_hash.get(extract.page_hash)
        if not sheet_row:
            _log.info(
                "drew.see: no DB row matches hash=%s page=%d (likely page added post-INGEST)",
                extract.page_hash[:8],
                extract.page_number,
            )
            continue
        sheet_id = str(sheet_row["id"])

        # ───── 1. YOLO symbol detection ─────
        try:
            detections = await detect_symbols(
                extract.image_bytes,
                sheet_id=sheet_id,
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                confidence_floor=confidence_floor,
            )
        except SymbolDetectorError as exc:
            # Law #3: fail-closed — weights missing is not a recoverable per-sheet failure;
            # bubble up so the orchestrator can surface a clear operational message.
            raise RuntimeError(
                f"YOLO weights unavailable: {exc}. "
                "Run `python -c \"from ultralytics import YOLO; YOLO('yolo11m.pt')\"` to fetch."
            ) from exc

        for det in detections:
            try:
                await supabase_insert(
                    "blueprint_symbols",
                    {
                        "id": str(uuid.uuid4()),
                        "suite_id": suite_id,
                        "office_id": office_id,
                        "sheet_id": sheet_id,
                        "class": det.class_name,
                        "bbox": det.bbox,
                        "confidence": det.confidence,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                total_symbols += 1
                confidence_sum += det.confidence
                confidence_count += 1
                model_version = det.model_version
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.see: failed to insert symbol sheet=%s (%s)",
                    sheet_id[:8],
                    type(exc).__name__,
                )

        # ───── 2. Scale calibration ─────
        sheet_text: str = str(sheet_row.get("ocr_text") or extract.text or "")
        calibration = calibrate_scale(extract.image_bytes, sheet_text)
        if calibration.scale_factor > 0 and calibration.confidence >= 0.70:
            scale_str = (
                calibration.text_match
                or f"{calibration.scale_factor:.6f}{calibration.units}/px"
            )
            try:
                await supabase_update(
                    "blueprint_sheets",
                    f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                    {"scale": scale_str[:120]},
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.see: failed to update scale sheet=%s (%s)",
                    sheet_id[:8],
                    type(exc).__name__,
                )
        elif calibration.method != "none" and calibration.confidence < 0.50:
            # Low-confidence or disagreement → contractor must confirm.
            try:
                await supabase_insert(
                    "blueprint_missing_inputs",
                    {
                        "id": str(uuid.uuid4()),
                        "suite_id": suite_id,
                        "project_id": project_id,
                        "description": (
                            f"Sheet {sheet_row.get('sheet_number', sheet_id[:8])}: "
                            f"scale calibration confidence {calibration.confidence:.0%} "
                            f"(method={calibration.method}). Please confirm drawing scale."
                        ),
                        "suggested_resolution": (
                            "Enter the explicit drawing scale (e.g. 1/4\" = 1'-0\" or 1:50)."
                        ),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                missing_inputs += 1
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.see: failed to insert scale missing_input sheet=%s (%s)",
                    sheet_id[:8],
                    type(exc).__name__,
                )

        # ───── 3. Engineer-seal detection ─────
        seal = detect_engineer_seal(extract.image_bytes)
        if seal.seal_detected:
            seal_sheets += 1
            try:
                await supabase_update(
                    "blueprint_sheets",
                    f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                    {"seal_detected": True},
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.see: failed to set seal_detected sheet=%s (%s)",
                    sheet_id[:8],
                    type(exc).__name__,
                )

    mean_conf = (confidence_sum / confidence_count) if confidence_count else 0.0

    _log.info(
        "drew.see: project=%s sheets=%d symbols=%d mean_conf=%.2f seals=%d "
        "missing=%d corr=%s",
        project_id[:8],
        len(sheets),
        total_symbols,
        mean_conf,
        seal_sheets,
        missing_inputs,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    return {
        "status": "ok",
        "stage": "see",
        "project_id": project_id,
        "sheet_count": len(sheets),
        "symbol_count": total_symbols,
        "mean_confidence": round(mean_conf, 4),
        "seal_sheets": seal_sheets,
        "missing_inputs": missing_inputs,
        "model_version": model_version,
    }


# ---------------------------------------------------------------------------
# Stage 4 REASON — async helpers
# ---------------------------------------------------------------------------

def _run_async_reason(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async REASON pipeline from sync context (mirrors ingest/classify/see)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_reason_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        model=model,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=300)  # REASON: 5-min ceiling
        else:
            return loop.run_until_complete(
                _async_reason_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    model=model,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_reason_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                model=model,
                correlation_id=correlation_id,
            )
        )


async def _async_reason_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Async REASON: call story_writer.write_story and map result to stage dict."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.blueprint.story_writer import write_story

    output = await write_story(
        project_id,
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        model=model,
    )

    _log.info(
        "drew.reason: project=%s phases=%d assemblies=%d materials=%d "
        "missing=%d mean_conf=%.3f corr=%s",
        project_id[:8],
        output.phase_count,
        output.assembly_count,
        output.material_count,
        output.missing_input_count,
        output.mean_confidence,
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    return {
        "status": "ok",
        "stage": "reason",
        "project_id": project_id,
        "phase_count": output.phase_count,
        "assembly_count": output.assembly_count,
        "material_count": output.material_count,
        "missing_input_count": output.missing_input_count,
        "mean_confidence": output.mean_confidence,
        "truth_distribution": output.truth_distribution,
        "model_version": output.model_version,
    }


# ---------------------------------------------------------------------------
# Stage 5 PROCURE — async helpers (Wave 5)
# ---------------------------------------------------------------------------

def _run_async_procure(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    office_zip: str | None,
    office_lat: float | None,
    office_lng: float | None,
    tenant_id: str,
    geofence_miles: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async PROCURE pipeline from sync context (mirrors ingest/classify)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_procure_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        office_zip=office_zip,
                        office_lat=office_lat,
                        office_lng=office_lng,
                        tenant_id=tenant_id,
                        geofence_miles=geofence_miles,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=300)  # PROCURE: 5-min ceiling
        else:
            return loop.run_until_complete(
                _async_procure_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    office_zip=office_zip,
                    office_lat=office_lat,
                    office_lng=office_lng,
                    tenant_id=tenant_id,
                    geofence_miles=geofence_miles,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_procure_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                office_zip=office_zip,
                office_lat=office_lat,
                office_lng=office_lng,
                tenant_id=tenant_id,
                geofence_miles=geofence_miles,
                correlation_id=correlation_id,
            )
        )



def classify_material_category(line_item: str) -> str:
    """Keyword-based classifier that maps a line_item to a SupplyCategory.

    Categories (in match-priority order):
      commercial_plumbing  -- commercial-grade plumbing fixtures / fittings
      appliance_finish     -- appliances, finish surfaces, cabinets, tile
      local_trade          -- bulk commodities ordered locally (call-for-quote)
      specialty_hardware   -- custom / specialty / antique items
      commodity            -- default for everything else (HD / big-box)

    Law #9: Only line_item[:80] used; no raw text logged.
    """
    text = line_item.lower()

    _COMMERCIAL_PLUMBING = (
        "copper pipe", "brass fitting", "commercial faucet",
        "water heater commercial", "fixture commercial", "gpm",
        "lavatory", "urinal", "floor drain", "backflow preventer",
    )
    _APPLIANCE_FINISH = (
        "dishwasher", "refrigerator", "range", "oven", "washer", "dryer",
        "microwave", "countertop", "cabinet", "tile", "paint", "finish",
        "faucet",
    )
    _LOCAL_TRADE = (
        "bulk rebar", "2x4 bulk", "conduit bulk", "gravel", "concrete bulk",
        "insulation bulk", "r-13 bulk", "drywall bulk",
    )
    _SPECIALTY = ("custom", "specialty", "vintage", "antique")

    if any(kw in text for kw in _COMMERCIAL_PLUMBING):
        return "commercial_plumbing"
    if any(kw in text for kw in _APPLIANCE_FINISH):
        return "appliance_finish"
    if any(kw in text for kw in _LOCAL_TRADE):
        return "local_trade"
    if any(kw in text for kw in _SPECIALTY):
        return "specialty_hardware"
    return "commodity"


async def _async_procure_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    office_zip: str | None,
    office_lat: float | None,
    office_lng: float | None,
    tenant_id: str,
    geofence_miles: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Async PROCURE: Wave 5.1a-4 -- tariff-flag + Adam supplier search.

    Drew delegates all supplier discovery to Adam via
    get_or_fetch_supplier_candidates (24-hr TTL cache). Drew classifies,
    picks, and persists; Adam queries only (Law #1).

    Law #2: blueprint.material_pick.memory_write receipt per row.
    Law #6: All DB reads/writes scoped by suite_id in filter strings.
    Law #9: line_item truncated to <=80 chars in logs/receipts.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.tariff_engine import (
        TariffFlag,
        detect_tariff_flag,
        estimate_tariff_impact_usd,
    )
    from aspire_orchestrator.services.blueprint.supplier_cache import (
        get_or_fetch_supplier_candidates,
    )
    from aspire_orchestrator.services.blueprint.adam_supplier_router import (
        route_supplier_search,
    )
    from aspire_orchestrator.services.memory_service import MemoryService
    from aspire_orchestrator.schemas.memory_v1 import (
        MemoryObjectIn,
        Provenance,
        ScopedIdentity,
    )
    from uuid import UUID

    # Build ScopedIdentity (tenant_id defaults to suite_id on BYPASS route)
    try:
        _tenant_uuid = UUID(tenant_id)
        _suite_uuid = UUID(suite_id)
        _office_uuid = UUID(office_id) if office_id else _suite_uuid
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"Invalid tenant/suite/office UUID: {exc}") from exc

    import hashlib as _hashlib
    try:
        _trace_uuid = UUID(correlation_id)
        _corr_uuid = UUID(correlation_id)
    except (ValueError, AttributeError):
        _hash_bytes = _hashlib.sha256(correlation_id.encode()).digest()[:16]
        _trace_uuid = UUID(bytes=_hash_bytes)
        _corr_uuid = _trace_uuid

    scoped_identity = ScopedIdentity(
        tenant_id=_tenant_uuid,
        suite_id=_suite_uuid,
        office_id=_office_uuid,
    )

    # Load all material rows for this project (RLS-scoped by suite_id)
    try:
        materials = await supabase_select(
            "blueprint_materials",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except SupabaseClientError as exc:
        raise RuntimeError(
            f"Failed to load blueprint_materials for project {project_id[:8]}: {exc}"
        ) from exc

    if not materials:
        _log.warning(
            "drew.procure: no materials found for project=%s suite=%s corr=%s",
            project_id[:8],
            suite_id[:8],
            correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
        )
        return {
            "status": "ok",
            "stage": "procure",
            "project_id": project_id,
            "materials_processed": 0,
            "tariff_flagged": 0,
            "tariff_breakdown": {},
            "suppliers_matched": 0,
            "supplier_match_rate": 0.0,
            "memory_writes": 0,
        }

    memory_svc = MemoryService()
    materials_processed = 0
    tariff_flagged = 0
    tariff_breakdown: dict[str, int] = {}
    suppliers_matched = 0
    memory_writes = 0

    for mat in materials:
        material_id = str(mat["id"])
        line_item: str = str(mat.get("line_item") or "")
        quantity: float | None = mat.get("quantity")

        materials_processed += 1

        # 1. Category + tariff classification
        category = classify_material_category(line_item)
        tariff_flag = detect_tariff_flag(line_item)
        brand_familiarity_map: dict[str, float] = {}  # TODO Wave 5.2: read from memory

        # 2. Adam delegation via supplier_cache (24-hr TTL, per-project credit cap)
        async def _fetch_fn(force_serpapi_only: bool = False) -> dict[str, Any]:
            return await route_supplier_search(
                line_item=line_item,
                category=category,  # type: ignore[arg-type]
                brand_familiarity_map=brand_familiarity_map,
                geofence_miles=geofence_miles,
                office_zip=office_zip,
                office_lat=office_lat,
                office_lng=office_lng,
                suite_id=suite_id,
                office_id=office_id or suite_id,
                correlation_id=correlation_id,
            )

        candidates_result: dict[str, Any]
        was_cached: bool
        try:
            candidates_result, was_cached = await get_or_fetch_supplier_candidates(
                suite_id=suite_id,
                project_id=project_id,
                category=category,
                line_item=line_item,
                office_zip=office_zip,
                correlation_id=correlation_id,
                fetch_fn=_fetch_fn,
            )
        except Exception as exc:
            _log.warning(
                "drew.procure: Adam search failed material=%s error=%s",
                material_id[:8],
                type(exc).__name__,
            )
            candidates_result = {"candidates": [], "credits_used": 0, "status": "error"}
            was_cached = False

        candidates: list[dict[str, Any]] = list(candidates_result.get("candidates") or [])
        credits_used: int = int(candidates_result.get("credits_used") or 0)

        # 3. Drew picks: top + 2 alternates
        top: dict[str, Any] | None = candidates[0] if candidates else None
        alternates: list[dict[str, Any]] = candidates[1:3] if len(candidates) > 1 else []

        # 4. Tariff exposure (unit_cost from top candidate price)
        unit_cost: float | None = None
        if top and (top.get("price") or {}).get("value") is not None:
            unit_cost = float(top["price"]["value"])

        tariff_exposure: float | None = estimate_tariff_impact_usd(
            flag=tariff_flag,
            quantity=quantity,
            unit_cost_usd=unit_cost,
        )

        # 5. UPDATE blueprint_materials row
        update_payload: dict[str, Any] = {"tariff_flag": tariff_flag.value}
        if tariff_exposure is not None:
            update_payload["tariff_exposure_usd"] = tariff_exposure
        if top:
            top_supplier: dict[str, Any] = top.get("supplier") or {}
            update_payload["supplier_id"] = (
                top_supplier.get("id") or top_supplier.get("name", "")[:60]
            )
            update_payload["supplier_candidates_json"] = [dict(c) for c in alternates]
        else:
            update_payload["supplier_id"] = None
            update_payload["supplier_candidates_json"] = [dict(c) for c in candidates]

        try:
            await supabase_update(
                "blueprint_materials",
                f"id=eq.{material_id}&suite_id=eq.{suite_id}",
                update_payload,
            )
            if top:
                suppliers_matched += 1
        except SupabaseClientError as exc:
            _log.warning(
                "drew.procure: failed to update material=%s error=%s",
                material_id[:8],
                type(exc).__name__,
            )

        if tariff_flag != TariffFlag.NONE:
            tariff_flagged += 1
            tariff_breakdown[tariff_flag.value] = tariff_breakdown.get(tariff_flag.value, 0) + 1

        # 6. Write material_pick to memory (Law #2)
        top_supplier_name: str = (
            ((top.get("supplier") or {}).get("name") or "NO MATCH") if top else "NO MATCH"
        )
        top_supplier_id: str | None = (
            ((top.get("supplier") or {}).get("id") or None) if top else None
        )
        top_match_class: str | None = (top.get("match_class") or None) if top else None
        top_match_score: float = float((top.get("match_score") or 0.0)) if top else 0.0

        memory_obj = MemoryObjectIn(
            scope=scoped_identity,
            provenance=Provenance(
                source_surface="system",
                source_agent=None,
                trace_id=_trace_uuid,
                correlation_id=_corr_uuid,
            ),
            memory_type="decision_fact",
            entity_type="material_pick",
            entity_id=None,
            title=f"Material pick: {line_item[:60]}",
            summary=(
                f"Drew picked {top_supplier_name} for {line_item[:80]}; "
                f"tariff_flag={tariff_flag.value}"
            ),
            detail={
                "material_id": material_id,
                "project_id": project_id,
                "supplier_id": top_supplier_id,
                "candidates_count": len(candidates),
                "match_class": top_match_class,
                "match_score": top_match_score,
                "tariff_flag": tariff_flag.value,
                "tariff_exposure_usd": tariff_exposure,
                "credits_used": credits_used,
                "was_cached": was_cached,
                "category": category,
            },
            visibility_scope="service",
            status="drafted",
            idempotency_key=f"drew:material:{project_id}:{material_id}",
        )

        try:
            await memory_svc.write(memory_obj, scope=scoped_identity, embed=False)
            memory_writes += 1
            store_receipts([
                _build_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.material_pick.memory_write",
                    status="ok",
                    inputs={
                        "material_id": material_id,
                        "project_id": project_id,
                        "suite_id": suite_id,
                    },
                    metadata={
                        "supplier_name": top_supplier_name,
                        "tariff_flag": tariff_flag.value,
                        "match_class": top_match_class,
                        "was_cached": was_cached,
                        "category": category,
                        "visibility_scope": "service",
                    },
                )
            ])
        except Exception as exc:
            _log.warning(
                "drew.procure: memory write failed material=%s error=%s",
                material_id[:8],
                type(exc).__name__,
            )
            store_receipts([
                _build_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.material_pick.memory_write",
                    status="failed",
                    inputs={
                        "material_id": material_id,
                        "project_id": project_id,
                        "suite_id": suite_id,
                    },
                    metadata={"error": type(exc).__name__},
                )
            ])

    supplier_match_rate = (
        round(suppliers_matched / materials_processed, 4) if materials_processed else 0.0
    )

    _log.info(
        "drew.procure: project=%s processed=%d tariff_flagged=%d suppliers_matched=%d "
        "memory_writes=%d corr=%s",
        project_id[:8],
        materials_processed,
        tariff_flagged,
        suppliers_matched,
        memory_writes,
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    return {
        "status": "ok",
        "stage": "procure",
        "project_id": project_id,
        "materials_processed": materials_processed,
        "tariff_flagged": tariff_flagged,
        "tariff_breakdown": tariff_breakdown,
        "suppliers_matched": suppliers_matched,
        "supplier_match_rate": supplier_match_rate,
        "memory_writes": memory_writes,
    }
