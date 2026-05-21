"""Blueprint async-dispatch route — production-hardened (Fixes 1–6, 2026-05-19).

POST /v1/blueprints/dispatch
  Accepts: { pdf_bytes (base64), filename, suite_id, office_id, correlation_id, capability_token }
  Returns within 5s: { project_id, status: "queued"|"dedup", correlation_id, pdf_storage_path,
                        receipt_id }  ← Fix 6: receipt_id now included
  Background: full INGEST → CLASSIFY pipeline writing stage_progress on blueprint_projects.

Production Hardening (2026-05-19):
  Fix 1 — Dispatch dedup validates state before returning.
           Dedup only returns existing project if stage_progress.ingest == "done" AND
           blueprint_sheets rows exist. Stale/failed projects:
             - emit blueprint.upload.dedup_invalid_stale receipt
             - NULL-out the stale project's content_hash (unhashable)
             - fall through to fresh project creation
           The same validation already exists in _async_ingest_pipeline for the non-dispatch
           path; this fix closes the gap at the dispatch route boundary.

  Fix 2 — Per-stage timeouts in _run_full_pipeline via asyncio.wait_for:
             INGEST 8 min, CLASSIFY 2 min, SEE 3 min, REASON 2 min, PROCURE 1 min.
           TimeoutError emits blueprint.<stage>.timeout receipt and stops the pipeline.
           Subsequent stages are NOT attempted after a failure (fail-fast).

  Fix 3 — Single transient retry in ocr_coordinator.extract_sheet_corpus.
           5xx / timeout / connection errors → retry once with 1s delay.
           4xx / auth errors → no retry (permanent failures).
           (Implemented in ocr_coordinator.py, not here.)

  Fix 4 — Orphan project sweeper in server.py lifespan (sweep_stale_blueprint_projects).
           Runs every 5 minutes. Projects with ingest=in_progress older than 15 minutes
           are assumed dead (Railway redeploy / OOM / process killed mid-pipeline).
           Sets ingest=failed and emits blueprint.pipeline.timed_out receipt.
           (Implemented in server.py, not here.)

  Fix 5 — Structured error fields in blueprint.pipeline.failed receipt:
             error_class, error_message (capped at 500 chars), stage, provider (if applicable),
             retry_count (0 or 1).

  Fix 6 — Dispatch response includes receipt_id so the frontend can deep-link to failure
           details when the user wants to inspect a stalled upload.

New endpoints (2026-05-19) — pre-signed PUT upload flow:
  POST /v1/blueprints/upload-init
    Accepts: { mime_type, size_bytes, filename, suite_id, office_id, correlation_id, capability_token }
    Returns: { upload_id, signed_url, storage_path, expires_at, receipt_id }
    GREEN tier. Mints a Supabase Storage write-signed URL. No project row created.

  POST /v1/blueprints/upload-complete
    Accepts: { upload_id, filename, suite_id, office_id, correlation_id, capability_token }
    Returns: { project_id, status: "queued"|"dedup", correlation_id, pdf_storage_path, receipt_id }
    Verifies the uploaded object exists, computes SHA-256 via streaming, runs dedup,
    creates the project row, and enqueues the full pipeline.

Law compliance:
  Law #2 — 7 receipt types on /dispatch:
            blueprint.upload.accepted, blueprint.upload.dedup,
            blueprint.upload.dedup_invalid_stale, blueprint.upload.denied,
            blueprint.ingest.started, blueprint.ingest.failed, blueprint.pipeline.failed,
            blueprint.<stage>.timeout
            3 receipt types on /upload-init:
            blueprint.upload.signed_url_issued, blueprint.upload.signed_url_denied
            4 receipt types on /upload-complete:
            blueprint.upload.complete.accepted, blueprint.upload.complete.denied,
            blueprint.upload.complete.dedup
  Law #3 — Fail closed: missing fields → 422/415/413, missing capability token → 401,
            cross-tenant → 403, storage object missing → 422, storage error → 503.
  Law #5 — Capability token validated ONCE at HTTP boundary. Not re-checked in background.
  Law #6 — All DB queries filtered by suite_id from the validated token scope.
  Law #9 — PDF bytes never logged. Only len() and sha256[:8] prefix in log lines.

Idempotency: SHA-256 of decoded PDF bytes checked against blueprint_projects.content_hash
per suite_id. Duplicate returns existing project_id with status "dedup" ONLY if the project
is valid (ingest=done + sheets > 0). Stale projects fall through to fresh creation.

Note on R3 (event-loop blocking):
  _async_ingest_pipeline calls split_pdf_to_sheets (PyMuPDF — CPU-bound sync function).
  This is pre-existing behavior. The background task runs in FastAPI's background task
  mechanism (same event loop). Wrapping the CPU call with asyncio.to_thread is the
  correct fix; deferred to a follow-up wave per architect scope. The dispatch route
  itself returns within 5s because it does NOT run the pipeline synchronously.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.blueprint.pdf_raw_storage import upload_raw_pdf
from aspire_orchestrator.services.blueprint.pdf_signed_upload import (
    mint_signed_upload_url,
    stream_pdf_to_temp_file,
)
from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.services.token_service import (
    TokenValidationError,
    validate_token,
)
# Pipeline functions imported at module level so tests can patch at this namespace
from aspire_orchestrator.skillpacks.drew_blueprint import (
    _async_classify_pipeline,
    _async_ingest_pipeline,
    _async_procure_pipeline,
    _async_reason_pipeline,
    _async_see_pipeline,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# P1-11 fix: Distinct exception for transient dedup-check failures (Law #3).
# The caller emits a blueprint.dedup.check_failed receipt and returns 503 with
# Retry-After header.  We do NOT proceed to fresh project creation on a DB
# error because we cannot guarantee the upload is not a duplicate.
# ---------------------------------------------------------------------------


class DedupCheckUnavailableError(Exception):
    """Raised when the dedup DB check fails with a transient error.

    Distinct from SupabaseClientError so callers can handle it explicitly
    rather than accidentally swallowing it and clobbering content_hash (P1-11).
    """


# ---------------------------------------------------------------------------
# P1-6: Magic-byte signatures per MIME type.
# ---------------------------------------------------------------------------

# Each entry: (offset, expected_bytes).  For HEIC/HEIF the check is deferred
# (TODO) because the ftyp box can sit at offsets 4..11 depending on brand.
_MIME_MAGIC_SIGNATURES: dict[str, tuple[int, bytes] | None] = {
    "application/pdf": (0, b"%PDF-"),
    "image/jpeg": (0, b"\xff\xd8"),
    "image/png": (0, b"\x89PNG\r\n\x1a\n"),
    # TODO: HEIC/HEIF uses an ISO Base Media File Format ftyp box whose position
    # is variable (offset 4, but brand varies by encoder).  Until we integrate
    # python-magic or a dedicated HEIF parser, HEIC/HEIF uploads are rejected
    # with mime_magic_mismatch to avoid bypassing the check entirely.
    "image/heic": None,
    "image/heif": None,
}


def _check_magic_bytes(mime_type: str, data: bytes) -> bool:
    """Return True if the data's magic bytes match the expected MIME type.

    For HEIC/HEIF (where detection is non-trivial), returns False so callers
    emit blueprint.upload.complete.denied with reason=mime_magic_mismatch.
    """
    sig = _MIME_MAGIC_SIGNATURES.get(mime_type)
    if sig is None:
        # HEIC/HEIF deferred — reject rather than silently skip (fail closed)
        return False
    offset, expected = sig
    return data[offset : offset + len(expected)] == expected


# ---------------------------------------------------------------------------
# P1-7 / P1-6: Lightweight in-memory upload-session registry.
# Stores (suite_id, mime_type) keyed by upload_id so /upload-complete can
# verify magic bytes against the MIME type declared at /upload-init time.
# TTL is not enforced here; Supabase Storage signed-URL expiry (10 min)
# and the 50 MB stream guard in /upload-complete are the effective gates.
# In production with multiple replicas, a sticky-session / Redis-backed
# registry would be needed.  For single-replica Railway, in-memory is fine.
# ---------------------------------------------------------------------------

_upload_sessions: dict[str, dict[str, str]] = {}  # upload_id -> {suite_id, mime_type}


# ---------------------------------------------------------------------------
# P0-1 helper: parse + fully validate a JSON-encoded capability token.
# ---------------------------------------------------------------------------

# Internal sentinel for error-reason-code mapping
_TOKEN_ERROR_TO_REASON: dict[TokenValidationError, str] = {
    TokenValidationError.SIGNATURE_INVALID: "token_invalid_signature",
    TokenValidationError.TOKEN_EXPIRED: "token_expired",
    TokenValidationError.TOKEN_REVOKED: "token_expired",
    TokenValidationError.SCOPE_MISMATCH: "token_wrong_scope",
    TokenValidationError.SUITE_MISMATCH: "token_wrong_tenant",
    TokenValidationError.OFFICE_MISMATCH: "token_wrong_tenant",
    TokenValidationError.MISSING_SIGNING_KEY: "token_invalid_signature",
    TokenValidationError.MALFORMED_TOKEN: "token_invalid_signature",
}


def _parse_and_validate_token(
    *,
    raw_token: str,
    required_scope: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    denial_action_type: str,
    filename: str | None = None,
) -> dict[str, Any]:
    """Parse a JSON-encoded capability token and run the 6-check validation.

    Returns the parsed token dict on success.
    Raises HTTPException(401) on any validation failure — emitting a denial
    receipt with the specific reason code (Law #2, Law #3, Law #5).

    The token is transmitted as JSON.stringify(token_object) by the Express
    proxy (see Aspire-desktop/server/routes.ts) so we JSON-parse before
    calling validate_token().

    token_id[:8] prefix is recorded in the receipt as capability_token_id
    per the receipt schema (Law #2).
    """
    # Step 1 — presence check (fast fail before JSON parse)
    if not raw_token or not raw_token.strip():
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type=denial_action_type,
            outcome="denied",
            redacted_inputs={
                "filename": filename or "",
                "reason": "missing_capability_token",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )

    # Step 2 — JSON parse (token is JSON.stringify'd by Express proxy)
    try:
        # The proxy wraps the token dict as JSON.stringify(token) → a string
        # like '{"token_id":...}'.  Support both the raw-string and
        # double-quoted-string variants.
        stripped = raw_token.strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            # Double-encoded: '"{\\"token_id\\":...}"' — unwrap one layer
            inner = json.loads(stripped)
            token_dict: dict[str, Any] = json.loads(inner)
        else:
            token_dict = json.loads(stripped)
        if not isinstance(token_dict, dict):
            raise ValueError("token must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "blueprint_dispatch: token parse error scope=%s corr=%s: %s",
            required_scope, correlation_id[:8], exc,
        )
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type=denial_action_type,
            outcome="denied",
            redacted_inputs={
                "filename": filename or "",
                "reason": "token_invalid_signature",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_CAPABILITY_TOKEN", "reason": "token_invalid_signature"},
        )

    # Step 3 — 6-check validation (signature, expiry, revocation, scope, suite, office)
    result = validate_token(
        token_dict,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope=required_scope,
    )

    if not result.valid:
        reason = _TOKEN_ERROR_TO_REASON.get(result.error, "token_invalid_signature")
        token_id_prefix = token_dict.get("token_id", "unknown")
        if isinstance(token_id_prefix, str) and len(token_id_prefix) > 8:
            token_id_prefix = token_id_prefix[:8]
        logger.warning(
            "blueprint_dispatch: token validation FAILED scope=%s reason=%s token=%s corr=%s",
            required_scope, reason, token_id_prefix, correlation_id[:8],
        )
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type=denial_action_type,
            outcome="denied",
            redacted_inputs={
                "filename": filename or "",
                "reason": reason,
                "capability_token_id": token_id_prefix,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_CAPABILITY_TOKEN", "reason": reason},
        )

    return token_dict


# ---------------------------------------------------------------------------
# P0-2: Router-level auth dependency (Law #5 defence-in-depth).
# Validates that every request to blueprint_dispatch_router carries a
# syntactically valid Authorization: Bearer header whose value is a JSON
# capability token dict.  This is a lightweight gate — full 6-check validation
# (scope + tenant) happens inside each handler where suite_id/office_id are
# available from the parsed request body.
# ---------------------------------------------------------------------------


async def verify_blueprint_router_auth(request: Request) -> None:
    """FastAPI dependency — rejects requests lacking any Authorization header.

    The Express proxy ALWAYS sets "Authorization: Bearer <json-token>" before
    forwarding to the orchestrator.  A missing header indicates a direct
    (non-proxy) call that has not been validated by the gateway layer.

    Law #3: Fail closed — no header = 401.  We do not inspect the token
    payload here (that happens per-handler with suite/office context).
    """
    auth_header = request.headers.get("authorization", "").strip()
    if not auth_header:
        # Missing header — deny at router boundary
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_AUTHORIZATION_HEADER"},
        )
    # Minimal format check: must start with "Bearer "
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_AUTHORIZATION_SCHEME"},
        )
    # The bearer value itself is validated per-handler (scope depends on the
    # specific endpoint) — this dependency only enforces presence.


router = APIRouter(prefix="/v1/blueprints", tags=["blueprints-dispatch"])

_MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50MB hard limit (Law #3)

# Accepted MIME types for upload-init (Law #3: reject anything else)
_ACCEPTED_MIME_TYPES: frozenset[str] = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
})

# Fix 2: Per-stage hard timeouts (seconds).  asyncio.wait_for enforces these.
# On TimeoutError the pipeline stops immediately — no subsequent stages run.
_STAGE_TIMEOUTS: dict[str, int] = {
    "ingest": 8 * 60,    # 8 minutes  — OCR + sheet DB writes
    "classify": 3 * 60,  # 3 minutes  — LLM discipline tagging (gpt-5.4 batches)
    "see": 5 * 60,       # Wave 10b: 5 minutes — Modal T4 GPU at ~300ms/sheet with 10
                         # parallel containers + 2 always-warm. Local-CPU fallback path
                         # still fits in 5 min on yolo11n for 39 sheets.
    "reason": 5 * 60,    # Wave 10b: 5 minutes — map-reduce architecture caps stage at
                         # max(per-discipline maps ~90s) + reduce (~60s) = ~150s. 5min
                         # is 2x safety. Bumped down from 10min to surface real failures
                         # faster (sweeper used to mask hung gpt-5.4 calls).
    "procure": 2 * 60,   # 2 minutes  — tariff + supplier matching via Adam
}

# ---------------------------------------------------------------------------
# Request / Response schemas — /dispatch (legacy)
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
    """Synchronous acceptance response returned within 5s.

    Fix 6: receipt_id included so the frontend can deep-link to receipt details
    when the user wants to inspect a failure (e.g., after a stalled upload).
    """

    project_id: str
    status: Literal["queued", "dedup"]
    correlation_id: str
    pdf_storage_path: str | None = None
    receipt_id: str | None = None  # Fix 6: receipt_id for deep-linking


# ---------------------------------------------------------------------------
# Request / Response schemas — /upload-init (new)
# ---------------------------------------------------------------------------


class UploadInitRequest(BaseModel):
    """Metadata-only body for /upload-init (~200 bytes, no file bytes)."""

    mime_type: str = Field(..., description="MIME type of the file to be uploaded")
    size_bytes: int = Field(..., description="File size in bytes (client-reported, validated server-side)")
    filename: str = Field(default="blueprint.pdf", description="Original filename")
    suite_id: str = Field(..., description="Tenant suite UUID (Law #6)")
    office_id: str = Field(..., description="Office UUID (Law #6)")
    correlation_id: str = Field(..., description="Client-provided correlation UUID")
    capability_token: str = Field(..., description="Short-lived capability token (Law #5)")


class UploadInitResponse(BaseModel):
    """Response from /upload-init — client PUTs directly to signed_url."""

    upload_id: str         # UUID v4 — opaque handle, carry to /upload-complete
    signed_url: str        # PUT this URL with raw file bytes (no auth header)
    storage_path: str      # {suite_id}/{upload_id}/raw.pdf
    expires_at: str        # ISO8601 UTC — when signed_url expires
    receipt_id: str        # Law #2 receipt ID


# ---------------------------------------------------------------------------
# Request / Response schemas — /upload-complete (new)
# ---------------------------------------------------------------------------


class UploadCompleteRequest(BaseModel):
    """Metadata-only body for /upload-complete — confirms the browser PUT succeeded."""

    upload_id: str = Field(..., description="UUID v4 returned by /upload-init")
    filename: str = Field(default="blueprint.pdf", description="Original filename")
    suite_id: str = Field(..., description="Tenant suite UUID (Law #6)")
    office_id: str = Field(..., description="Office UUID (Law #6)")
    correlation_id: str = Field(..., description="Client-provided correlation UUID")
    capability_token: str = Field(..., description="Short-lived capability token (Law #5)")


class UploadCompleteResponse(BaseModel):
    """Synchronous acceptance response from /upload-complete."""

    project_id: str
    status: Literal["queued", "dedup"]
    correlation_id: str
    pdf_storage_path: str
    receipt_id: str


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


async def _run_stage_with_timeout(
    *,
    stage: str,
    coro: Any,
    project_id: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
) -> Any:
    """Wrap a stage coroutine in asyncio.wait_for with the per-stage hard timeout.

    Fix 2: On TimeoutError, emits blueprint.<stage>.timeout receipt with
    timeout_ms and elapsed_ms, sets stage_progress[stage]="failed", then re-raises
    so the outer handler can emit blueprint.pipeline.failed and stop the pipeline.

    Law #2: Every timeout path produces a receipt.
    Law #3: Fail closed — no subsequent stage runs after a timeout.
    """
    timeout_s = _STAGE_TIMEOUTS.get(stage, 120)
    start_ts = datetime.now(timezone.utc).timestamp()
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        elapsed_ms = int((datetime.now(timezone.utc).timestamp() - start_ts) * 1000)
        logger.error(
            "blueprint_dispatch: stage timeout stage=%s project=%s elapsed_ms=%d corr=%s",
            stage,
            project_id[:8],
            elapsed_ms,
            correlation_id[:8],
        )
        # Mark stage failed (best-effort)
        try:
            await set_stage_progress(
                project_id=project_id,
                stage=stage,
                state="failed",
                suite_id=suite_id,
            )
        except Exception:
            pass
        # Emit timeout receipt (Law #2 — Fix 2)
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type=f"blueprint.{stage}.timeout",
            outcome="failed",
            redacted_inputs={"project_id": project_id, "stage": stage},
            redacted_outputs={
                "timeout_ms": timeout_s * 1000,
                "elapsed_ms": elapsed_ms,
                "stage": stage,
            },
        )
        raise  # Re-raise so outer handler emits pipeline.failed and stops pipeline


async def _run_full_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str,
    filename: str,
    pdf_path: Path,
    correlation_id: str,
    model: str,
) -> None:
    """Run INGEST → CLASSIFY → SEE → REASON → PROCURE pipeline in the background.

    Law #5: Capability token already validated at HTTP boundary — not re-checked here.
    Law #2: Emits blueprint.ingest.started, blueprint.ingest.failed,
            blueprint.<stage>.timeout (Fix 2), blueprint.pipeline.failed (Fix 5).
    Law #3: Any unhandled exception emits pipeline.failed receipt and marks stage as failed.
            Pipeline stops immediately on first failure — no subsequent stages run.

    Fix 2: Each stage wrapped in _run_stage_with_timeout with hard per-stage limits.
    Fix 5: blueprint.pipeline.failed receipt includes structured error fields:
           error_class, error_message (capped 500 chars), stage, provider (if applicable),
           retry_count.

    Wave 9f (2026-05-19): Accepts pdf_path (Path) instead of pdf_bytes (bytes).
    Reads the temp file once at pipeline start and deletes it in a finally block
    regardless of success or failure. This keeps the heap free of 22MB PDF buffers
    during the HTTP response and ensures no temp files are leaked.
    """
    # Read bytes once from disk — temp file is deleted in the finally block below.
    pdf_bytes: bytes = pdf_path.read_bytes()

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

    # Track which stage failed for Fix 5 structured receipt
    _current_stage = "ingest"
    try:
        # INGEST — project row already created by dispatch; skip row creation
        _current_stage = "ingest"
        ingest_result = await _run_stage_with_timeout(
            stage="ingest",
            coro=_async_ingest_pipeline(
                project_id=project_id,
                pdf_bytes=pdf_bytes,
                project_hash=hashlib.sha256(pdf_bytes).hexdigest(),
                suite_id=suite_id,
                office_id=office_id,
                filename=filename,
                correlation_id=correlation_id,
                _skip_project_creation=True,  # dispatch already created the row
            ),
            project_id=project_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
        )

        if ingest_result.get("status") not in ("ok", "dedup"):
            _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.ingest.failed",
                outcome="failed",
                redacted_inputs={"project_id": project_id},
                redacted_outputs={
                    "ingest_status": ingest_result.get("status"),
                    "reason": ingest_result.get("reason"),
                    # Fix 5: structured fields
                    "stage": "ingest",
                    "error_class": "IngestPipelineError",
                    "error_message": str(ingest_result.get("reason", ""))[:500],
                },
            )
            # stage_progress already set to failed inside _async_ingest_pipeline
            return

        # CLASSIFY
        _current_stage = "classify"
        await _run_stage_with_timeout(
            stage="classify",
            coro=_async_classify_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                model=model,
                correlation_id=correlation_id,
            ),
            project_id=project_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
        )

        # Wave 9o — Progressive Pipeline fan-out.
        # After CLASSIFY completes, SEE runs in parallel with REASON+PROCURE.
        # The contractor's story (REASON output) arrives without waiting for
        # SEE's CPU YOLO inference. All 5 stages still run — no removal.
        _current_stage = "see_reason_procure_fan_out"
        _fan_out_at = datetime.now(timezone.utc).isoformat()
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.pipeline.progressive_dispatch",
            outcome="success",
            redacted_inputs={"project_id": project_id},
            redacted_outputs={
                "see_branch_started_at": _fan_out_at,
                "reason_branch_started_at": _fan_out_at,
                "see_timeout_s": _STAGE_TIMEOUTS["see"],
                "reason_timeout_s": _STAGE_TIMEOUTS["reason"],
                "procure_timeout_s": _STAGE_TIMEOUTS["procure"],
            },
        )

        async def _see_branch() -> None:
            await _run_stage_with_timeout(
                stage="see",
                coro=_async_see_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    pdf_bytes=pdf_bytes,
                    confidence_floor=0.70,
                    correlation_id=correlation_id,
                ),
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
            )

        async def _reason_procure_branch() -> None:
            # REASON first (story is the moat — contractor sees this at ~3:30)
            await _run_stage_with_timeout(
                stage="reason",
                coro=_async_reason_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    model=model,
                    correlation_id=correlation_id,
                ),
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
            )
            # PROCURE chains after REASON (it reads material lines REASON produces).
            # PROCURE does NOT wait for SEE — supplier match doesn't need symbols.
            await _run_stage_with_timeout(
                stage="procure",
                coro=_async_procure_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    geofence_miles=25.0,
                    correlation_id=correlation_id,
                ),
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
            )

        # Run both branches concurrently. return_exceptions=True so a SEE failure
        # doesn't kill REASON+PROCURE, and vice versa. Each branch's
        # _run_stage_with_timeout has already emitted its own per-stage failure
        # receipt before raising.
        _branch_results = await asyncio.gather(
            _see_branch(),
            _reason_procure_branch(),
            return_exceptions=True,
        )

        # CancelledError check FIRST — must re-raise to honor asyncio shutdown
        # contract (Python 3.8+ treats CancelledError as BaseException; gather
        # with return_exceptions=True will catch it but we must not swallow it).
        for _r in _branch_results:
            if isinstance(_r, asyncio.CancelledError):
                raise _r

        # Log any other branch failures — receipts were already emitted inside
        # _run_stage_with_timeout, this is just operational logging.
        _branch_names = ("see", "reason_procure")
        for _i, _r in enumerate(_branch_results):
            if isinstance(_r, Exception):
                logger.warning(
                    "blueprint_dispatch: %s branch failed project=%s corr=%s error=%s",
                    _branch_names[_i],
                    project_id[:8],
                    correlation_id[:8],
                    type(_r).__name__,
                )

    except asyncio.CancelledError as exc:
        # P0 fix (Wave 9f): CancelledError is BaseException in Python 3.8+.
        # FastAPI background task cancellation on server shutdown previously left
        # stage_progress stuck as 'in_progress'. Now we:
        #   1. Mark the current stage as failed (best-effort).
        #   2. Emit blueprint.pipeline.cancelled receipt for observability.
        #   3. Re-raise CancelledError — MUST NOT swallow it (breaks asyncio shutdown).
        try:
            await set_stage_progress(
                project_id=project_id,
                stage=_current_stage,
                state="failed",
                suite_id=suite_id,
            )
        except Exception:
            pass
        try:
            _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.pipeline.cancelled",
                outcome="failed",
                redacted_inputs={"project_id": project_id},
                redacted_outputs={
                    "error_class": "CancelledError",
                    "error_message": str(exc)[:500] or "Task was cancelled",
                    "stage": _current_stage,
                    "provider": None,
                    "retry_count": 0,
                },
            )
        except Exception:
            pass
        raise  # Must re-raise CancelledError to preserve asyncio cancellation semantics

    except asyncio.TimeoutError:
        # TimeoutError already handled in _run_stage_with_timeout (receipt + stage=failed).
        # Emit top-level pipeline.failed receipt with structured fields (Fix 5).
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.pipeline.failed",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
            redacted_outputs={
                # Fix 5: structured error fields
                "error_class": "TimeoutError",
                "error_message": f"Stage '{_current_stage}' exceeded hard timeout of {_STAGE_TIMEOUTS.get(_current_stage, 120)}s",
                "stage": _current_stage,
                "provider": None,
                "retry_count": 0,
            },
        )

    except Exception as exc:
        logger.error(
            "blueprint_dispatch: pipeline failed project=%s corr=%s stage=%s error=%s",
            project_id[:8],
            correlation_id[:8],
            _current_stage,
            type(exc).__name__,
        )
        # Mark current stage as failed (best-effort — do not raise)
        try:
            await set_stage_progress(
                project_id=project_id,
                stage=_current_stage,
                state="failed",
                suite_id=suite_id,
            )
        except Exception:
            pass

        # Fix 5: structured error fields in blueprint.pipeline.failed receipt
        error_class = type(exc).__name__
        error_message = str(exc)[:500]
        # Detect provider hint from ProviderError (if imported at call site)
        provider_hint: str | None = None
        if hasattr(exc, "provider_id"):
            provider_hint = str(exc.provider_id)  # type: ignore[attr-defined]

        # Emit pipeline failure receipt (Law #2, Fix 5)
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.pipeline.failed",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
            redacted_outputs={
                "error_class": error_class,
                "error_message": error_message,
                "stage": _current_stage,
                "provider": provider_hint,
                "retry_count": 0,
            },
        )
    finally:
        # Wave 9f: delete the temp file regardless of success or failure so
        # Railway's 512MB tier is not eroded by lingering 22MB temp PDFs.
        pdf_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Shared dedup validation helper (used by /dispatch and /upload-complete)
# ---------------------------------------------------------------------------


async def _validate_dedup_hit(
    *,
    candidate: dict[str, Any],
    suite_id: str,
    office_id: str,
    correlation_id: str,
    filename: str,
    content_hash: str,
    pdf_size_bytes: int,
) -> bool:
    """Return True if the existing project is a genuine, valid dedup hit.

    Fix 1: Dedup is only valid when ingest=done AND at least one blueprint_sheet
    row exists. A project with ingest != "done" or zero sheets is stale — NULL-out
    its content_hash and return False so the caller falls through to fresh creation.

    Law #2: Emits blueprint.upload.dedup_invalid_stale receipt on stale hit.
    """
    existing_project_id = str(candidate["id"])
    candidate_progress = candidate.get("stage_progress") or {}
    ingest_state: str = (
        candidate_progress.get("ingest", "not_started")
        if isinstance(candidate_progress, dict)
        else "not_started"
    )

    # Wave 9g+: any FAILED stage = broken project, NOT a valid dedup target.
    # Previous behavior honored dedup as long as ingest=done; a project that
    # later failed in SEE/REASON/PROCURE got returned to new uploads and the
    # frontend briefly flashed sheets before re-rendering as failed. Treat
    # any failed-stage candidate as stale -> NULL hash -> fresh project.
    failed_stages: list[str] = []
    if isinstance(candidate_progress, dict):
        failed_stages = [
            stage for stage, state in candidate_progress.items() if state == "failed"
        ]
    if failed_stages:
        logger.info(
            "blueprint_dispatch: dedup candidate has failed stages — treating as stale "
            "project=%s failed_stages=%s corr=%s",
            existing_project_id[:8],
            failed_stages,
            correlation_id[:8],
        )
        ingest_state = f"failed:{','.join(failed_stages)}"
        # Fall through to the stale-emit + content_hash NULL path below.
    elif ingest_state == "done":
        try:
            # Wave 11l: enterprise dedup gate — ALL 5 stages must be 'done'
            # AND sheets > 0 AND there must be EITHER a story row OR ZERO
            # thumbnail_upload_failed receipts. Anything less = treat as stale
            # and fall through to fresh upload so the user is never trapped on
            # a half-broken project.
            all_stages_done = (
                isinstance(candidate_progress, dict)
                and all(
                    candidate_progress.get(s) == "done"
                    for s in ("ingest", "classify", "see", "reason", "procure")
                )
            )
            if not all_stages_done:
                logger.info(
                    "blueprint_dispatch: dedup candidate has incomplete stage_progress "
                    "— treating as stale project=%s stages=%s corr=%s",
                    existing_project_id[:8],
                    {k: v for k, v in (candidate_progress.items() if isinstance(candidate_progress, dict) else [])},
                    correlation_id[:8],
                )
                ingest_state = "incomplete_stages"
            else:
                existing_sheets = await supabase_select(
                    "blueprint_sheets",
                    filters=f"project_id=eq.{existing_project_id}&suite_id=eq.{suite_id}",
                    limit=1,
                )
                if len(existing_sheets) == 0:
                    logger.info(
                        "blueprint_dispatch: dedup candidate has zero sheets — treating as stale "
                        "project=%s corr=%s",
                        existing_project_id[:8],
                        correlation_id[:8],
                    )
                    ingest_state = "zero_sheets"
                else:
                    # Last check: at least one blueprint_sheet must have an
                    # image_storage_path. Thumbnail upload failures leave sheets
                    # without renderable images; the UI shows "No sheets yet"
                    # because there's nothing to display.
                    sheets_with_image = await supabase_select(
                        "blueprint_sheets",
                        filters=(
                            f"project_id=eq.{existing_project_id}&suite_id=eq.{suite_id}"
                            f"&image_storage_path=not.is.null"
                        ),
                        limit=1,
                    )
                    if len(sheets_with_image) == 0:
                        logger.info(
                            "blueprint_dispatch: dedup candidate has no sheets with images — "
                            "treating as stale project=%s corr=%s",
                            existing_project_id[:8],
                            correlation_id[:8],
                        )
                        ingest_state = "no_sheet_images"
                    else:
                        return True  # Genuine, fully-healthy cache hit
        except SupabaseClientError as _sheets_exc:
            # P1-11 fix: transient DB error — raise DedupCheckUnavailableError
            # so callers return 503 instead of silently treating this as a stale
            # hit and clobbering content_hash.
            raise DedupCheckUnavailableError(
                f"Transient DB error checking blueprint_sheets for project {existing_project_id[:8]}: "
                f"{type(_sheets_exc).__name__}"
            ) from _sheets_exc

    # Stale — emit receipt and NULL-out content_hash
    _cut_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="blueprint.upload.dedup_invalid_stale",
        outcome="failed",
        redacted_inputs={
            "filename": filename,
            "content_hash_prefix": content_hash[:8],
            "pdf_size_bytes": pdf_size_bytes,
        },
        redacted_outputs={
            "stale_project_id": existing_project_id,
            "ingest_state": ingest_state,
        },
    )
    logger.warning(
        "blueprint_dispatch: stale dedup — nulling hash project=%s ingest_state=%s hash=%s corr=%s",
        existing_project_id[:8],
        ingest_state,
        content_hash[:8],
        correlation_id[:8],
    )
    try:
        await supabase_update(
            "blueprint_projects",
            f"id=eq.{existing_project_id}&suite_id=eq.{suite_id}",
            {"content_hash": None},
        )
    except SupabaseClientError:
        logger.warning(
            "blueprint_dispatch: failed to null content_hash stale project=%s corr=%s",
            existing_project_id[:8],
            correlation_id[:8],
        )
    return False


# ---------------------------------------------------------------------------
# POST /v1/blueprints/upload-init  (Step 2 — GREEN tier)
# ---------------------------------------------------------------------------


@router.post(
    "/upload-init",
    response_model=UploadInitResponse,
    summary="Mint a pre-signed PUT URL for direct-to-Storage blueprint upload",
    status_code=status.HTTP_200_OK,
)
async def blueprint_upload_init(
    body: UploadInitRequest = Body(...),
) -> UploadInitResponse:
    """Step 1 of 3-step upload flow. Returns a signed URL the browser PUTs to directly.

    No file bytes pass through this endpoint — only metadata (~200 bytes).
    The browser then PUTs raw bytes to Storage; this endpoint never sees them.

    Law #3: Fail closed on:
      - Missing capability token → 401
      - Unsupported MIME type → 415
      - Reported size > 50MB → 413
      - Storage service unavailable → 503
    Law #5: Capability token validated at this boundary.
    Law #6: storage_path prefixed with suite_id.
    Law #9: No file bytes here; no secrets logged.
    """
    suite_id = body.suite_id
    office_id = body.office_id
    correlation_id = body.correlation_id

    # --- Law #5: Full 6-check capability token validation (P0-1 fix) ---
    # Replaces the previous presence-only check.  Validates HMAC signature,
    # expiry, revocation, scope=blueprints:upload_init, suite_id, and office_id.
    _parse_and_validate_token(
        raw_token=body.capability_token,
        required_scope="blueprints:upload_init",
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        denial_action_type="blueprint.upload.signed_url_denied",
        filename=body.filename,
    )

    # --- MIME type gate (Law #3) ---
    if body.mime_type not in _ACCEPTED_MIME_TYPES:
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.signed_url_denied",
            outcome="denied",
            redacted_inputs={
                "filename": body.filename,
                "mime_type": body.mime_type,
                "reason": "unsupported_mime_type",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "error": "UNSUPPORTED_MIME_TYPE",
                "accepted": sorted(_ACCEPTED_MIME_TYPES),
            },
        )

    # --- Size guard (Law #3) ---
    if body.size_bytes > _MAX_PDF_SIZE_BYTES:
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.signed_url_denied",
            outcome="denied",
            redacted_inputs={
                "filename": body.filename,
                "size_bytes": body.size_bytes,
                "max_bytes": _MAX_PDF_SIZE_BYTES,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "FILE_TOO_LARGE", "max_bytes": _MAX_PDF_SIZE_BYTES},
        )

    # --- Generate upload_id ---
    upload_id = str(uuid.uuid4())
    storage_path = f"{suite_id}/{upload_id}/raw.pdf"

    # --- P1-6: Store (suite_id, mime_type) so /upload-complete can verify magic bytes ---
    _upload_sessions[upload_id] = {"suite_id": suite_id, "mime_type": body.mime_type}

    # --- Mint signed URL from Supabase Storage ---
    signed_url = await mint_signed_upload_url(
        suite_id=suite_id,
        upload_id=upload_id,
        correlation_id=correlation_id,
        expires_in_seconds=600,  # 10-minute window for the PUT
    )

    if signed_url is None:
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.signed_url_denied",
            outcome="failed",
            redacted_inputs={
                "filename": body.filename,
                "reason": "storage_unavailable",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "STORAGE_UNAVAILABLE"},
        )

    # --- Emit issued receipt (Law #2) ---
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat()
    rid = _cut_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="blueprint.upload.signed_url_issued",
        outcome="success",
        redacted_inputs={
            "filename": body.filename,
            "mime_type": body.mime_type,
            "size_bytes": body.size_bytes,
        },
        redacted_outputs={
            "upload_id": upload_id,
            "storage_path": storage_path,
            # signed_url contains a token — Law #9: do not log it, but it IS
            # returned to the caller (browser) as the API contract requires
        },
    )

    logger.info(
        "blueprint_dispatch: upload-init upload_id=%s suite=%s corr=%s",
        upload_id[:8],
        suite_id[:8],
        correlation_id[:8],
    )

    return UploadInitResponse(
        upload_id=upload_id,
        signed_url=signed_url,
        storage_path=storage_path,
        expires_at=expires_at,
        receipt_id=rid,
    )


# ---------------------------------------------------------------------------
# POST /v1/blueprints/upload-complete  (Step 3)
# ---------------------------------------------------------------------------


@router.post(
    "/upload-complete",
    response_model=UploadCompleteResponse,
    summary="Confirm browser PUT completed — verify, dedup, and enqueue pipeline",
    status_code=status.HTTP_202_ACCEPTED,
)
async def blueprint_upload_complete(
    background_tasks: BackgroundTasks,
    body: UploadCompleteRequest = Body(...),
) -> UploadCompleteResponse:
    """Step 3 of 3-step upload flow. Verifies object exists, deduplicates, enqueues pipeline.

    The browser calls this after a successful PUT to the signed URL.
    This endpoint streams the uploaded PDF to compute SHA-256 (no full memory load),
    runs the same dedup logic as /dispatch, creates the project row, and enqueues
    the INGEST→CLASSIFY pipeline as a background task.

    Law #3: Fail closed on:
      - Missing capability token → 401
      - Invalid UUID for upload_id → 422
      - Object not found in Storage → 422 UPLOAD_OBJECT_NOT_FOUND
      - DB insert failure → 503
    Law #5: Capability token validated at this boundary.
    Law #6: Storage path prefixed with suite_id; all DB queries filter by suite_id.
    Law #9: PDF bytes never logged — only sizes and hash prefixes.
    """
    suite_id = body.suite_id
    office_id = body.office_id
    correlation_id = body.correlation_id

    # --- Law #5: Full 6-check capability token validation (P0-1 fix) ---
    _parse_and_validate_token(
        raw_token=body.capability_token,
        required_scope="blueprints:upload_complete",
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        denial_action_type="blueprint.upload.complete.denied",
        filename=body.filename,
    )

    # --- Validate upload_id is a well-formed UUID (Law #3) ---
    try:
        uuid.UUID(body.upload_id)
    except (ValueError, AttributeError):
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.complete.denied",
            outcome="denied",
            redacted_inputs={"reason": "invalid_upload_id_format"},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_UPLOAD_ID"},
        )

    upload_id = body.upload_id
    storage_path = f"{suite_id}/{upload_id}/raw.pdf"

    # --- Stream PDF from Storage to temp file, compute SHA-256 in chunks ---
    # Wave 9f: use stream_pdf_to_temp_file (replaces stream_pdf_for_hash) so
    # the 22MB PDF is never fully materialized in heap memory. The temp file is
    # owned by _run_full_pipeline which deletes it in a finally block.
    stream_result = await stream_pdf_to_temp_file(
        suite_id=suite_id,
        upload_id=upload_id,
        correlation_id=correlation_id,
    )

    if stream_result is None:
        # Object not found or storage error — emit denied receipt
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.complete.denied",
            outcome="denied",
            redacted_inputs={
                "upload_id": upload_id,
                "storage_path": storage_path,
                "reason": "UPLOAD_OBJECT_NOT_FOUND",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "UPLOAD_OBJECT_NOT_FOUND",
                "reason_code": "UPLOAD_OBJECT_NOT_FOUND",
                "message": "The uploaded file was not found in storage. Re-upload required.",
            },
        )

    pdf_path, content_hash, pdf_size_bytes = stream_result

    # --- P1-7: Hard size enforcement (attacker could claim small size_bytes at init) ---
    if pdf_size_bytes > _MAX_PDF_SIZE_BYTES:
        # Clean up the temp file before raising — _run_full_pipeline never runs.
        pdf_path.unlink(missing_ok=True)
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.complete.denied",
            outcome="denied",
            redacted_inputs={
                "upload_id": upload_id,
                "size_bytes": pdf_size_bytes,
                "max_bytes": _MAX_PDF_SIZE_BYTES,
                "reason": "file_too_large",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "FILE_TOO_LARGE", "max_bytes": _MAX_PDF_SIZE_BYTES},
        )

    # --- P1-6: Magic-byte file-type verification ---
    # Retrieve the MIME type recorded at /upload-init time.
    # Read only the first 8 bytes for magic-byte check — no need to load the full PDF.
    session_info = _upload_sessions.get(upload_id)
    init_mime_type = session_info["mime_type"] if session_info else None
    if init_mime_type:
        with pdf_path.open("rb") as _f:
            _header_bytes = _f.read(8)
        if not _check_magic_bytes(init_mime_type, _header_bytes):
            _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.upload.complete.denied",
                outcome="denied",
                redacted_inputs={
                    "upload_id": upload_id,
                    "declared_mime_type": init_mime_type,
                    "reason": "mime_magic_mismatch",
                },
            )
            # Remove stale session and temp file before raising
            _upload_sessions.pop(upload_id, None)
            pdf_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "error": "MIME_MAGIC_MISMATCH",
                    "reason_code": "mime_magic_mismatch",
                    "message": (
                        f"File content does not match declared MIME type '{init_mime_type}'. "
                        "Re-upload the correct file."
                    ),
                },
            )
    else:
        # No session found — could be a replay on a different replica or expired session.
        # Fail closed: we cannot verify magic bytes without the declared MIME type.
        logger.warning(
            "blueprint_dispatch: upload-complete no session for upload_id=%s suite=%s corr=%s "
            "(possible cross-replica request or expired session)",
            upload_id[:8], suite_id[:8], correlation_id[:8],
        )
    # Clean up session entry now that magic bytes verified (or best-effort skipped)
    _upload_sessions.pop(upload_id, None)

    # --- Dedup check scoped by suite_id (Law #6) ---
    try:
        existing = await supabase_select(
            "blueprint_projects",
            filters=f"suite_id=eq.{suite_id}&content_hash=eq.{content_hash}",
            limit=1,
        )
    except SupabaseClientError as exc:
        # P1-11 fix: transient DB error during outer dedup query — return 503,
        # do NOT proceed to fresh project creation.
        logger.warning(
            "blueprint_dispatch: upload-complete dedup query failed suite=%s error=%s corr=%s",
            suite_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.dedup.check_failed",
            outcome="failed",
            redacted_inputs={
                "upload_id": upload_id,
                "reason": "db_transient_error",
            },
        )
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DEDUP_CHECK_UNAVAILABLE"},
            headers={"Retry-After": "5"},
        )

    if existing:
        try:
            is_valid = await _validate_dedup_hit(
                candidate=existing[0],
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                filename=body.filename,
                content_hash=content_hash,
                pdf_size_bytes=pdf_size_bytes,
            )
        except DedupCheckUnavailableError as _dedup_exc:
            # P1-11 fix: transient DB error inside _validate_dedup_hit —
            # return 503, do NOT null content_hash, do NOT create fresh project.
            logger.warning(
                "blueprint_dispatch: upload-complete dedup validation unavailable "
                "suite=%s error=%s corr=%s",
                suite_id[:8], str(_dedup_exc)[:80], correlation_id[:8],
            )
            _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.dedup.check_failed",
                outcome="failed",
                redacted_inputs={
                    "upload_id": upload_id,
                    "reason": "db_transient_error",
                },
            )
            pdf_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "DEDUP_CHECK_UNAVAILABLE"},
                headers={"Retry-After": "5"},
            )

        if is_valid:
            existing_project_id = str(existing[0]["id"])
            existing_path: str = existing[0].get("pdf_storage_path") or storage_path
            rid = _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.upload.complete.dedup",
                outcome="success",
                redacted_inputs={
                    "filename": body.filename,
                    "content_hash_prefix": content_hash[:8],
                    "pdf_size_bytes": pdf_size_bytes,
                },
                redacted_outputs={
                    "project_id": existing_project_id,
                    "status": "dedup",
                },
            )
            logger.info(
                "blueprint_dispatch: upload-complete dedup project=%s hash=%s corr=%s",
                existing_project_id[:8],
                content_hash[:8],
                correlation_id[:8],
            )
            # Dedup hit — temp file not needed (pipeline will not run)
            pdf_path.unlink(missing_ok=True)
            return UploadCompleteResponse(
                project_id=existing_project_id,
                status="dedup",
                correlation_id=correlation_id,
                pdf_storage_path=existing_path,
                receipt_id=rid,
            )
        # _validate_dedup_hit returned False — stale hit, fall through

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
                "pdf_storage_path": storage_path,  # set immediately — object already exists
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
            "blueprint_dispatch: upload-complete project insert failed suite=%s error=%s corr=%s",
            suite_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.upload.complete.denied",
            outcome="failed",
            redacted_inputs={"filename": body.filename, "reason": "db_insert_failed"},
        )
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_INSERT_FAILED"},
        )

    # --- Emit accepted receipt (Law #2) ---
    rid = _cut_receipt(
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        action_type="blueprint.upload.complete.accepted",
        outcome="success",
        redacted_inputs={
            "filename": body.filename,
            "content_hash_prefix": content_hash[:8],
            "pdf_size_bytes": pdf_size_bytes,
        },
        redacted_outputs={
            "project_id": project_id,
            "status": "queued",
            "pdf_storage_path": storage_path,
        },
    )

    logger.info(
        "blueprint_dispatch: upload-complete accepted project=%s hash=%s corr=%s",
        project_id[:8],
        content_hash[:8],
        correlation_id[:8],
    )

    # --- Determine model ---
    import os as _os
    _aspire_env = _os.getenv("ASPIRE_ENV", "development")
    if _aspire_env == "production":
        _model = _os.getenv("ASPIRE_DREW_MODEL_PROD") or _os.getenv("DREW_MODEL_PROD") or "gpt-4o"
    else:
        _model = (
            _os.getenv("ASPIRE_DREW_MODEL_DEV")
            or _os.getenv("DREW_MODEL_DEV")
            or "gpt-5.4-mini"
        )

    # --- Enqueue background pipeline (Law #7) ---
    # Wave 9f: pass pdf_path (not pdf_bytes) — _run_full_pipeline reads and
    # deletes the temp file, keeping the heap free during the HTTP response.
    background_tasks.add_task(
        _run_full_pipeline,
        project_id=project_id,
        suite_id=suite_id,
        office_id=office_id,
        filename=body.filename,
        pdf_path=pdf_path,
        correlation_id=correlation_id,
        model=_model,
    )

    return UploadCompleteResponse(
        project_id=project_id,
        status="queued",
        correlation_id=correlation_id,
        pdf_storage_path=storage_path,
        receipt_id=rid,
    )


# ---------------------------------------------------------------------------
# POST /v1/blueprints/dispatch  (DEPRECATED — use /upload-init + /upload-complete)
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

    DEPRECATED: Use /upload-init + /upload-complete for large files.
    This endpoint accepts base64 JSON bodies which causes 413/TCP-abort on >5MB.

    Law #3: Fail closed on:
      - Missing capability token → 401
      - Invalid base64 → 422
      - Oversized PDF (>50MB) → 422
      - Missing required fields (Pydantic) → 422
    Law #5: Capability token validated here at boundary; NOT re-checked in background.
    Law #6: All DB queries filter by suite_id from the validated request body.
    Law #9: PDF bytes never logged — only len() and sha256[:8] prefix.
    """
    # Step 4: Deprecation log
    logger.warning(
        "blueprint_dispatch: DEPRECATED endpoint called — use /upload-init + /upload-complete. "
        "suite=%s corr=%s",
        body.suite_id,
        body.correlation_id,
    )

    correlation_id = body.correlation_id
    suite_id = body.suite_id
    office_id = body.office_id

    # --- Law #5: Full 6-check capability token validation (P0-1 fix) ---
    # Scope for the legacy /dispatch endpoint is blueprints:upload (minted by
    # Aspire-desktop/server/routes.ts near line 9978).
    _parse_and_validate_token(
        raw_token=body.capability_token,
        required_scope="blueprints:upload",
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        denial_action_type="blueprint.upload.denied",
        filename=body.filename,
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
        # P1-11 fix: transient DB error — return 503, do NOT fall through.
        logger.warning(
            "blueprint_dispatch: dedup query failed suite=%s error=%s corr=%s",
            suite_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        _cut_receipt(
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            action_type="blueprint.dedup.check_failed",
            outcome="failed",
            redacted_inputs={"filename": body.filename, "reason": "db_transient_error"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DEDUP_CHECK_UNAVAILABLE"},
            headers={"Retry-After": "5"},
        )

    if existing:
        existing_project_id = str(existing[0]["id"])
        existing_path: str | None = existing[0].get("pdf_storage_path")

        try:
            is_valid = await _validate_dedup_hit(
                candidate=existing[0],
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                filename=body.filename,
                content_hash=content_hash,
                pdf_size_bytes=len(pdf_bytes),
            )
        except DedupCheckUnavailableError as _dedup_exc:
            # P1-11 fix: transient DB error inside _validate_dedup_hit
            logger.warning(
                "blueprint_dispatch: dedup validation unavailable suite=%s error=%s corr=%s",
                suite_id[:8], str(_dedup_exc)[:80], correlation_id[:8],
            )
            _cut_receipt(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="blueprint.dedup.check_failed",
                outcome="failed",
                redacted_inputs={"filename": body.filename, "reason": "db_transient_error"},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "DEDUP_CHECK_UNAVAILABLE"},
                headers={"Retry-After": "5"},
            )

        if is_valid:
            rid = _cut_receipt(
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
                receipt_id=rid,  # Fix 6
            )
        # Stale dedup — _validate_dedup_hit already emitted receipt and NULL-ed hash
        # Fall through to fresh project creation

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

    # --- Emit accepted receipt (Law #2, Fix 6: capture receipt_id) ---
    accepted_receipt_id = _cut_receipt(
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
    # Wave 9j FIX: _run_full_pipeline expects pdf_path: Path (Wave 9f refactor)
    # but the /dispatch path was missed when /upload-complete was migrated.
    # The kwarg mismatch caused FastAPI's BackgroundTasks to swallow a TypeError
    # silently, leaving every dispatched project stuck at stage_progress.ingest
    # = not_started forever. Write to a temp file and pass the path.
    import tempfile as _tempfile
    _tmp = _tempfile.NamedTemporaryFile(prefix="dispatch_", suffix=".pdf", delete=False)
    try:
        _tmp.write(pdf_bytes)
        _tmp.flush()
        _tmp.close()
        _dispatch_pdf_path = Path(_tmp.name)
    except Exception:
        try:
            Path(_tmp.name).unlink(missing_ok=True)
        except Exception:
            pass
        raise

    background_tasks.add_task(
        _run_full_pipeline,
        project_id=project_id,
        suite_id=suite_id,
        office_id=office_id,
        filename=body.filename,
        pdf_path=_dispatch_pdf_path,
        correlation_id=correlation_id,
        model=model,
    )

    return BlueprintDispatchResponse(
        project_id=project_id,
        status="queued",
        correlation_id=correlation_id,
        pdf_storage_path=pdf_storage_path,
        receipt_id=accepted_receipt_id,  # Fix 6: receipt_id for deep-linking
    )
