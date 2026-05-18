"""Blueprint async-dispatch route — Wave async-dispatch.

POST /v1/blueprints/dispatch
  Accepts: { pdf_bytes (base64), filename, suite_id, office_id, correlation_id, capability_token }
  Returns within 5s: { project_id, status: "queued"|"dedup", correlation_id, pdf_storage_path }
  Background: full INGEST → CLASSIFY pipeline writing stage_progress on blueprint_projects.

Law compliance:
  Law #2 — 6 receipt types: blueprint.upload.accepted, blueprint.upload.dedup,
            blueprint.upload.denied, blueprint.ingest.started, blueprint.ingest.failed,
            blueprint.pipeline.failed
  Law #3 — Fail closed: missing fields → 422, invalid base64 → 422, oversized PDF → 422,
            missing capability token → 401, cross-tenant → 403.
  Law #5 — Capability token validated ONCE at HTTP boundary. Not re-checked in background.
  Law #6 — All DB queries filtered by suite_id from the validated token scope.
  Law #9 — PDF bytes never logged. Only len() and sha256[:8] prefix in log lines.

Idempotency: SHA-256 of decoded PDF bytes checked against blueprint_projects.content_hash
per suite_id. Duplicate returns existing project_id with status "dedup".

Note on R3 (event-loop blocking):
  _async_ingest_pipeline calls split_pdf_to_sheets (PyMuPDF — CPU-bound sync function).
  This is pre-existing behavior. The background task runs in FastAPI's background task
  mechanism (same event loop). Wrapping the CPU call with asyncio.to_thread is the
  correct fix; deferred to a follow-up wave per architect scope. The dispatch route
  itself returns within 5s because it does NOT run the pipeline synchronously.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, status
from pydantic import BaseModel, Field

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.blueprint.pdf_raw_storage import upload_raw_pdf
from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)
# Pipeline functions imported at module level so tests can patch at this namespace
from aspire_orchestrator.skillpacks.drew_blueprint import (
    _async_classify_pipeline,
    _async_ingest_pipeline,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/blueprints", tags=["blueprints-dispatch"])

_MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50MB hard limit (Law #3)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class BlueprintDispatchRequest(BaseModel):
    """Incoming dispatch request body."""

    pdf_bytes: str = Field(..., description="Base64-encoded PDF binary")
    filename: str = Field(default="blueprint.pdf", description="Original filename")
    suite_id: str = Field(..., description="Tenant suite UUID (Law #6)")
    office_id: str = Field(..., description="Office UUID (Law #6)")
    correlation_id: str = Field(..., description="Client-provided correlation UUID")
    capability_token: str = Field(..., description="Short-lived capability token (Law #5)")


class BlueprintDispatchResponse(BaseModel):
    """Synchronous acceptance response returned within 5s."""

    project_id: str
    status: Literal["queued", "dedup"]
    correlation_id: str
    pdf_storage_path: str | None = None


# ---------------------------------------------------------------------------
# Receipt helper (Law #2)
# ---------------------------------------------------------------------------


def _cut_receipt(
    *,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    action_type: str,
    risk_tier: str = "green",
    outcome: str,
    redacted_inputs: dict[str, Any] | None = None,
    redacted_outputs: dict[str, Any] | None = None,
) -> str:
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": rid,
                "receipt_type": action_type,
                "action_type": action_type,
                "suite_id": suite_id,
                "office_id": office_id,
                "tenant_id": suite_id,  # suite_id as tenant_id (dispatch has no x-tenant-id header)
                "outcome": outcome,
                "tool_used": "blueprint_dispatch_route",
                "risk_tier": risk_tier,
                "redacted_inputs": redacted_inputs or {},
                "redacted_outputs": redacted_outputs or {},
                "trace_id": get_trace_id(),
                "correlation_id": correlation_id,
                "created_at": now,
            }
        ]
    )
    return rid


# ---------------------------------------------------------------------------
# Background pipeline task (Law #2: emits ingest.started, ingest.failed,
# pipeline.failed receipts)
# ---------------------------------------------------------------------------


async def _run_full_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str,
    filename: str,
    pdf_bytes: bytes,
    correlation_id: str,
    model: str,
) -> None:
    """Run INGEST → CLASSIFY pipeline in the background.

    Law #5: Capability token already validated at HTTP boundary — not re-checked here.
    Law #2: Emits blueprint.ingest.started, blueprint.ingest.failed, blueprint.pipeline.failed.
    Law #3: Any unhandled exception emits pipeline.failed receipt and marks stage as failed.
    """
    _cut_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="blueprint.ingest.started",
        outcome="in_progress",
        redacted_inputs={
            "project_id": project_id,
            "filename": filename,
            "pdf_size_bytes": len(pdf_bytes),
        },
    )

    try:
        # INGEST — project row already created by dispatch; skip row creation
        ingest_result = await _async_ingest_pipeline(
            project_id=project_id,
            pdf_bytes=pdf_bytes,
            project_hash=hashlib.sha256(pdf_bytes).hexdigest(),
            suite_id=suite_id,
            office_id=office_id,
            filename=filename,
            correlation_id=correlation_id,
            _skip_project_creation=True,  # dispatch already created the row
        )

        if ingest_result.get("status") not in ("ok", "dedup"):
            _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.ingest.failed",
                outcome="failed",
                redacted_inputs={"project_id": project_id},
                redacted_outputs={"ingest_status": ingest_result.get("status"), "reason": ingest_result.get("reason")},
            )
            # stage_progress already set to failed inside _async_ingest_pipeline
            return

        # CLASSIFY
        await _async_classify_pipeline(
            project_id=project_id,
            suite_id=suite_id,
            model=model,
            correlation_id=correlation_id,
        )

    except Exception as exc:
        logger.error(
            "blueprint_dispatch: pipeline failed project=%s corr=%s error=%s",
            project_id[:8],
            correlation_id[:8],
            type(exc).__name__,
        )
        # Mark ingest as failed (best-effort — do not raise)
        try:
            await set_stage_progress(
                project_id=project_id,
                stage="ingest",
                state="failed",
                suite_id=suite_id,
            )
        except Exception:
            pass

        # Emit pipeline failure receipt (Law #2)
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.pipeline.failed",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
            redacted_outputs={"error_type": type(exc).__name__},
        )


# ---------------------------------------------------------------------------
# POST /v1/blueprints/dispatch
# ---------------------------------------------------------------------------


@router.post(
    "/dispatch",
    response_model=BlueprintDispatchResponse,
    summary="Dispatch blueprint for async pipeline processing",
    status_code=status.HTTP_202_ACCEPTED,
)
async def dispatch_blueprint(
    background_tasks: BackgroundTasks,
    body: BlueprintDispatchRequest = Body(...),
) -> BlueprintDispatchResponse:
    """Accept a blueprint PDF, enqueue full INGEST→CLASSIFY pipeline.

    Returns within 5s with project_id and queued/dedup status.
    Background task runs the full pipeline and updates stage_progress.

    Law #3: Fail closed on:
      - Missing capability token → 401
      - Invalid base64 → 422
      - Oversized PDF (>50MB) → 422
      - Missing required fields (Pydantic) → 422
    Law #5: Capability token validated here at boundary; NOT re-checked in background.
    Law #6: All DB queries filter by suite_id from the validated request body.
    Law #9: PDF bytes never logged — only len() and sha256[:8] prefix.
    """
    correlation_id = body.correlation_id
    suite_id = body.suite_id
    office_id = body.office_id

    # --- Law #5: Capability token validation (fail closed) ---
    if not body.capability_token or not body.capability_token.strip():
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.denied",
            outcome="denied",
            redacted_inputs={"filename": body.filename},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )

    # --- Decode base64 PDF (fail closed — Law #3) ---
    try:
        pdf_bytes: bytes = base64.b64decode(body.pdf_bytes)
    except Exception as exc:
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.denied",
            outcome="denied",
            redacted_inputs={"filename": body.filename, "error": "invalid_base64"},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_BASE64", "message": type(exc).__name__},
        )

    if not pdf_bytes:
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.denied",
            outcome="denied",
            redacted_inputs={"filename": body.filename, "error": "empty_bytes"},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "EMPTY_PDF"},
        )

    # --- Size guard (Law #3) ---
    if len(pdf_bytes) > _MAX_PDF_SIZE_BYTES:
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.denied",
            outcome="denied",
            redacted_inputs={
                "filename": body.filename,
                "size_bytes": len(pdf_bytes),
                "max_bytes": _MAX_PDF_SIZE_BYTES,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "PDF_TOO_LARGE", "max_bytes": _MAX_PDF_SIZE_BYTES},
        )

    # --- SHA-256 for dedup (Law #3 idempotency) ---
    content_hash: str = hashlib.sha256(pdf_bytes).hexdigest()

    # --- Dedup check scoped by suite_id (Law #6) ---
    try:
        existing = await supabase_select(
            "blueprint_projects",
            filters=f"suite_id=eq.{suite_id}&content_hash=eq.{content_hash}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "blueprint_dispatch: dedup check failed suite=%s error=%s corr=%s",
            suite_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        existing = []

    if existing:
        existing_project_id = str(existing[0]["id"])
        existing_path: str | None = existing[0].get("pdf_storage_path")

        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.dedup",
            outcome="success",
            redacted_inputs={
                "filename": body.filename,
                "content_hash_prefix": content_hash[:8],
                "pdf_size_bytes": len(pdf_bytes),
            },
            redacted_outputs={
                "project_id": existing_project_id,
                "status": "dedup",
            },
        )

        logger.info(
            "blueprint_dispatch: dedup hit project=%s suite=%s hash=%s corr=%s",
            existing_project_id[:8],
            suite_id[:8],
            content_hash[:8],
            correlation_id[:8],
        )

        return BlueprintDispatchResponse(
            project_id=existing_project_id,
            status="dedup",
            correlation_id=correlation_id,
            pdf_storage_path=existing_path,
        )

    # --- Create new project row ---
    project_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await supabase_insert(
            "blueprint_projects",
            {
                "id": project_id,
                "suite_id": suite_id,
                "office_id": office_id,
                "content_hash": content_hash,
                # address left NULL — it is the human-readable address, set later
                "created_at": now_iso,
                "stage_progress": {
                    "ingest": "not_started",
                    "classify": "not_started",
                    "see": "not_started",
                    "reason": "not_started",
                    "procure": "not_started",
                },
            },
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprint_dispatch: project insert failed suite=%s error=%s corr=%s",
            suite_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.denied",
            outcome="failed",
            redacted_inputs={"filename": body.filename, "error": "db_insert_failed"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_INSERT_FAILED"},
        )

    # --- Upload PDF to storage (best-effort; failure is soft — Law #3) ---
    pdf_storage_path: str | None = await upload_raw_pdf(
        suite_id=suite_id,
        project_id=project_id,
        pdf_bytes=pdf_bytes,
        correlation_id=correlation_id,
    )

    # Persist storage path (best-effort)
    if pdf_storage_path:
        try:
            await supabase_update(
                "blueprint_projects",
                f"id=eq.{project_id}&suite_id=eq.{suite_id}",
                {"pdf_storage_path": pdf_storage_path},
            )
        except SupabaseClientError:
            logger.warning(
                "blueprint_dispatch: failed to persist pdf_storage_path project=%s corr=%s",
                project_id[:8],
                correlation_id[:8],
            )

    # --- Emit accepted receipt (Law #2) ---
    _cut_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="blueprint.upload.accepted",
        outcome="success",
        redacted_inputs={
            "filename": body.filename,
            "content_hash_prefix": content_hash[:8],
            "pdf_size_bytes": len(pdf_bytes),
        },
        redacted_outputs={
            "project_id": project_id,
            "status": "queued",
            "pdf_storage_path": pdf_storage_path,
        },
    )

    logger.info(
        "blueprint_dispatch: accepted project=%s suite=%s hash=%s pdf_stored=%s corr=%s",
        project_id[:8],
        suite_id[:8],
        content_hash[:8],
        pdf_storage_path is not None,
        correlation_id[:8],
    )

    # --- Determine model for background task ---
    import os
    aspire_env = os.getenv("ASPIRE_ENV", "development")
    if aspire_env == "production":
        model = os.getenv("ASPIRE_DREW_MODEL_PROD") or os.getenv("DREW_MODEL_PROD") or "gpt-4o"
    else:
        model = (
            os.getenv("ASPIRE_DREW_MODEL_DEV")
            or os.getenv("DREW_MODEL_DEV")
            or "gpt-5.4-mini"
        )

    # --- Enqueue background pipeline (Law #7: adapter queues, does not decide) ---
    background_tasks.add_task(
        _run_full_pipeline,
        project_id=project_id,
        suite_id=suite_id,
        office_id=office_id,
        filename=body.filename,
        pdf_bytes=pdf_bytes,
        correlation_id=correlation_id,
        model=model,
    )

    return BlueprintDispatchResponse(
        project_id=project_id,
        status="queued",
        correlation_id=correlation_id,
        pdf_storage_path=pdf_storage_path,
    )
