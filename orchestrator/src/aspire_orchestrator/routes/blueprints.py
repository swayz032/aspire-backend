"""Blueprint read API routes — Wave 2.5.

Routes:
  GET /v1/blueprints/projects/{project_id}         — project + sheet_count + stage_progress
  GET /v1/blueprints/projects/{project_id}/sheets  — list sheets (filterable + dedup-aware)
  GET /v1/blueprints/projects/{project_id}/status  — lightweight status for frontend polling

Law compliance:
  Law #2 — receipt cut on every read (even GREEN tier reads get receipts in Aspire).
  Law #3 — fail closed: missing scope headers → 401. RLS hides row → 404 (not 403).
  Law #4 — GREEN tier: read-only, no state changes.
  Law #6 — tenant isolation: suite_id set via SET LOCAL before every query; RLS
            enforces it at DB layer. Cross-tenant attempt returns 404, not 403,
            to avoid existence leaks.
  Law #9 — no PII in logs or receipts.

Auth pattern: X-Tenant-Id / X-Suite-Id / X-Office-Id headers (set by API Gateway,
identical to contacts.py, voicemails.py, callbacks.py pattern in this codebase).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, status

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.routes._scope import _resolve_scope
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.blueprint.schemas.blueprint_project_read import (
    BlueprintProjectRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_project_status import (
    BlueprintProjectStatus,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_sheet_read import (
    BlueprintSheetRead,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/blueprints", tags=["blueprints"])


# ---------------------------------------------------------------------------
# Receipt helper (Law #2)
# ---------------------------------------------------------------------------

def _cut_receipt(
    *,
    scope: ScopedIdentity,
    action_type: str,
    outcome: str = "success",
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
                "suite_id": str(scope.suite_id),
                "office_id": str(scope.office_id),
                "tenant_id": str(scope.tenant_id),
                "outcome": outcome,
                "tool_used": "blueprints_route",
                "risk_tier": "green",
                "redacted_inputs": redacted_inputs or {},
                "redacted_outputs": redacted_outputs or {},
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "created_at": now,
            }
        ]
    )
    return rid


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}",
    response_model=BlueprintProjectRead,
    summary="Get blueprint project",
)
async def get_blueprint_project(
    project_id: str,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> BlueprintProjectRead:
    """Return a blueprint project visible to the requesting tenant.

    Returns 404 when the project does not exist OR when RLS hides it from the
    requesting tenant (existence is never leaked — Law #3 / Law #6).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    try:
        rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.get_project: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not rows:
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="not_found",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    project_row = rows[0]

    # Count sheets for this project (scoped to suite_id)
    sheet_count = 0
    try:
        sheet_rows = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
        sheet_count = len(sheet_rows)
    except SupabaseClientError:
        pass  # sheet_count defaults to 0 — non-critical

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read",
        outcome="success",
        redacted_inputs={"project_id": project_id},
        redacted_outputs={"sheet_count": sheet_count},
    )

    stage_progress: dict[str, str] = project_row.get("stage_progress") or {
        "ingest": "not_started",
        "classify": "not_started",
        "see": "not_started",
        "reason": "not_started",
        "procure": "not_started",
    }

    return BlueprintProjectRead(
        id=project_row["id"],
        address=project_row.get("address"),
        created_at=project_row["created_at"],
        created_by=project_row.get("created_by"),
        stage_progress=stage_progress,
        sheet_count=sheet_count,
    )


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/sheets
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/sheets",
    response_model=list[BlueprintSheetRead],
    summary="List blueprint sheets",
)
async def list_blueprint_sheets(
    project_id: str,
    discipline: str | None = Query(None, description="Filter by discipline code (A, S, M, E, P, ...)"),
    active_only: bool = Query(True, description="Exclude superseded sheets (default: true)"),
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> list[BlueprintSheetRead]:
    """List sheets for a blueprint project.

    By default excludes superseded sheets (active_only=true).
    Optionally filter by discipline code.

    Returns 404 when the project is not found or RLS hides it.
    Returns an empty list when the project exists but has no matching sheets.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    # Verify project exists and belongs to this tenant (existence check)
    try:
        proj_rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.list_sheets: project check failed project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not proj_rows:
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="not_found",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Build filter string for sheets query
    filter_parts = [f"project_id=eq.{project_id}", f"suite_id=eq.{suite_id}"]

    if active_only:
        # PostgREST: supersedes_id=is.null → only sheets that are not superseded
        filter_parts.append("supersedes_id=is.null")

    if discipline:
        from urllib.parse import quote as _quote
        filter_parts.append(f"discipline=eq.{_quote(discipline, safe='')}")

    filter_str = "&".join(filter_parts)

    try:
        sheet_rows = await supabase_select(
            "blueprint_sheets",
            filters=filter_str,
            order_by="sheet_number.asc",
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.list_sheets: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read",
        outcome="success",
        redacted_inputs={
            "project_id": project_id,
            "discipline": discipline,
            "active_only": active_only,
        },
        redacted_outputs={"sheet_count": len(sheet_rows)},
    )

    return [
        BlueprintSheetRead(
            id=row["id"],
            sheet_number=row.get("sheet_number"),
            discipline=row.get("discipline"),
            scale=row.get("scale"),
            revision=row.get("revision"),
            supersedes_id=row.get("supersedes_id"),
            thumbnail_url=row.get("thumbnail_url"),
            seal_detected=bool(row.get("seal_detected", False)),
            created_at=row["created_at"],
        )
        for row in sheet_rows
    ]


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/status
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/status",
    response_model=BlueprintProjectStatus,
    summary="Get blueprint project pipeline status",
)
async def get_blueprint_status(
    project_id: str,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> BlueprintProjectStatus:
    """Return lightweight pipeline status for the frontend polling loop.

    Polled every 2s while any stage is `in_progress`.
    Returns symbol_count and missing_input_count from their respective tables.
    Returns 404 when project is not found or RLS hides it.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    # Load project (single query)
    try:
        proj_rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.get_status: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not proj_rows:
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read",
            outcome="not_found",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    project_row = proj_rows[0]

    # Count related records (best-effort; failures return 0)
    sheet_count = 0
    symbol_count = 0
    missing_input_count = 0

    try:
        sheet_rows = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
        sheet_count = len(sheet_rows)
    except SupabaseClientError:
        pass

    try:
        symbol_rows = await supabase_select(
            "blueprint_symbols",
            filters=f"suite_id=eq.{suite_id}",
        )
        # Filter client-side because sheet_ids may be many and no project FK on symbols
        symbol_count = len(symbol_rows)
    except SupabaseClientError:
        pass

    try:
        missing_rows = await supabase_select(
            "blueprint_missing_inputs",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
        missing_input_count = len(missing_rows)
    except SupabaseClientError:
        pass

    stage_progress: dict[str, str] = project_row.get("stage_progress") or {
        "ingest": "not_started",
        "classify": "not_started",
        "see": "not_started",
        "reason": "not_started",
        "procure": "not_started",
    }

    updated_at_raw = project_row.get("created_at")
    updated_at: datetime
    if isinstance(updated_at_raw, str):
        try:
            updated_at = datetime.fromisoformat(updated_at_raw)
        except ValueError:
            updated_at = datetime.now(timezone.utc)
    elif isinstance(updated_at_raw, datetime):
        updated_at = updated_at_raw
    else:
        updated_at = datetime.now(timezone.utc)

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read",
        outcome="success",
        redacted_inputs={"project_id": project_id},
        redacted_outputs={
            "sheet_count": sheet_count,
            "symbol_count": symbol_count,
            "missing_input_count": missing_input_count,
        },
    )

    return BlueprintProjectStatus(
        project_id=project_row["id"],
        stage_progress=stage_progress,
        updated_at=updated_at,
        sheet_count=sheet_count,
        symbol_count=symbol_count,
        missing_input_count=missing_input_count,
    )
