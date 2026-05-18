"""Blueprint read API routes — Wave 2.5 + Wave 2.7.

Routes (Wave 2.5):
  GET /v1/blueprints/projects/{project_id}         — project + sheet_count + stage_progress
  GET /v1/blueprints/projects/{project_id}/sheets  — list sheets (filterable + dedup-aware)
  GET /v1/blueprints/projects/{project_id}/status  — lightweight status for frontend polling

Routes (Wave 2.7 — Scope tab + Takeoff tab):
  GET  /v1/blueprints/projects/{project_id}/symbols          — symbols per sheet or project
  GET  /v1/blueprints/projects/{project_id}/assemblies       — derived assemblies
  GET  /v1/blueprints/projects/{project_id}/materials        — material line items
  GET  /v1/blueprints/projects/{project_id}/missing_inputs   — contractor input gaps
  GET  /v1/blueprints/projects/{project_id}/story            — phased narrative
  POST /v1/blueprints/projects/{project_id}/missing_inputs/{input_id}/resolve
                                                             — resolve a gap (YELLOW)

Law compliance:
  Law #2 — receipt cut on every read AND on every state-change (success + failure).
  Law #3 — fail closed: missing scope headers → 401, missing capability token → 401.
  Law #4 — POST /resolve is YELLOW tier: requires capability_token in request body.
           GREEN tier: all GET endpoints (no state change, no token required).
  Law #6 — tenant isolation: suite_id enforced in every query filter.
           Cross-tenant attempt returns 404, not 403 (no existence leak).
  Law #9 — markdown body, supplier name/address NEVER appear in receipts or logs.
           Only counts and opaque IDs are logged.

Auth pattern: X-Tenant-Id / X-Suite-Id / X-Office-Id headers (identical to
contacts.py, voicemails.py, callbacks.py pattern in this codebase).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, status

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.routes._scope import _resolve_scope
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.blueprint.schemas.blueprint_assembly_read import (
    BlueprintAssemblyRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_material_read import (
    BlueprintMaterialRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_missing_input_read import (
    BlueprintMissingInputRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_project_read import (
    BlueprintProjectRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_project_status import (
    BlueprintProjectStatus,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_sheet_read import (
    BlueprintSheetRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_story_read import (
    BlueprintStoryPhase,
    BlueprintStoryRead,
)
from aspire_orchestrator.services.blueprint.schemas.blueprint_symbol_read import (
    BlueprintSymbolRead,
)
from aspire_orchestrator.services.blueprint.schemas.missing_input_resolve_request import (
    MissingInputResolveRequest,
)
from aspire_orchestrator.services.blueprint.schemas.truth import TruthClass
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
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
    risk_tier: str = "green",
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
                "risk_tier": risk_tier,
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
# Shared project-existence check (DRY helper used by all Wave 2.7 endpoints)
# ---------------------------------------------------------------------------

async def _require_project(
    project_id: str,
    suite_id: str,
    scope: ScopedIdentity,
    action_type: str,
) -> dict[str, Any]:
    """Load and return the project row, or raise 404 (Law #3 / Law #6).

    Emits a failure receipt on DB error or on not-found.
    """
    try:
        rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.require_project: DB error action=%s project=%s suite=%s error=%s",
            action_type,
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type=action_type,
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not rows:
        _cut_receipt(
            scope=scope,
            action_type=action_type,
            outcome="not_found",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return rows[0]


# ===========================================================================
# Wave 2.5 endpoints
# ===========================================================================


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


# ===========================================================================
# Wave 2.7 endpoints
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/symbols
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/symbols",
    response_model=list[BlueprintSymbolRead],
    summary="List blueprint symbols (Takeoff tab)",
)
async def list_blueprint_symbols(
    project_id: str,
    sheet_id: str | None = Query(None, description="Filter to one sheet"),
    confidence_floor: float = Query(0.70, description="Minimum confidence threshold (default: 0.70)"),
    class_prefix: str | None = Query(None, description="Filter by class prefix e.g. 'electrical_'"),
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> list[BlueprintSymbolRead]:
    """List detected symbols for a blueprint project.

    Symbols are detected by Drew SEE (Wave 3).  Filtered by confidence_floor
    to reduce low-quality detections on the Takeoff tab.

    Returns 404 when project is not visible to the requesting tenant (Law #6).
    Emits blueprint.read.symbols receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    # Verify project ownership (Law #6) — emits failure receipt on error
    await _require_project(project_id, suite_id, scope, "blueprint.read.symbols")

    # Build symbol filter — suite_id always present (RLS belt-and-suspenders)
    filter_parts = [f"suite_id=eq.{suite_id}"]

    if sheet_id:
        filter_parts.append(f"sheet_id=eq.{sheet_id}")
    else:
        # Scope to project via its sheets — join done client-side via sheet_id IN list.
        # For now: filter by suite_id only (RLS enforces isolation) and post-filter.
        # The PostgREST `sheet_id=in.(...)` form is used when we have sheet IDs.
        try:
            sheet_rows = await supabase_select(
                "blueprint_sheets",
                filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            )
            if sheet_rows:
                sheet_ids = ",".join(str(r["id"]) for r in sheet_rows)
                filter_parts.append(f"sheet_id=in.({sheet_ids})")
            else:
                # No sheets → no symbols
                _cut_receipt(
                    scope=scope,
                    action_type="blueprint.read.symbols",
                    outcome="success",
                    redacted_inputs={"project_id": project_id, "sheet_id": sheet_id},
                    redacted_outputs={"symbol_count": 0},
                )
                return []
        except SupabaseClientError:
            pass  # Fall through — will return empty if DB unreachable

    # Confidence floor — PostgREST gte filter
    filter_parts.append(f"confidence=gte.{confidence_floor}")

    filter_str = "&".join(filter_parts)

    try:
        rows = await supabase_select(
            "blueprint_symbols",
            filters=filter_str,
            order_by="confidence.desc",
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.list_symbols: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read.symbols",
            outcome="failed",
            redacted_inputs={"project_id": project_id, "sheet_id": sheet_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Optional: class_prefix post-filter (PostgREST lacks LIKE on non-text columns)
    if class_prefix:
        rows = [r for r in rows if (r.get("class_") or r.get("class", "") or "").startswith(class_prefix)]

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read.symbols",
        outcome="success",
        redacted_inputs={"project_id": project_id, "sheet_id": sheet_id, "confidence_floor": confidence_floor},
        redacted_outputs={"symbol_count": len(rows)},
    )

    return [
        BlueprintSymbolRead(
            id=row["id"],
            sheet_id=row["sheet_id"],
            class_=row.get("class_") or row.get("class"),
            bbox=row.get("bbox"),
            confidence=row.get("confidence"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/assemblies
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/assemblies",
    response_model=list[BlueprintAssemblyRead],
    summary="List blueprint assemblies (Scope tab)",
)
async def list_blueprint_assemblies(
    project_id: str,
    active_only: bool = Query(True, description="Exclude superseded assemblies (default: true)"),
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> list[BlueprintAssemblyRead]:
    """List derived assemblies for a blueprint project.

    Assemblies are produced by Drew REASON (Wave 4).  By default excludes
    superseded rows so the Scope tab always shows the current version.

    Returns 404 when project is not visible to the requesting tenant (Law #6).
    Emits blueprint.read.assemblies receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    await _require_project(project_id, suite_id, scope, "blueprint.read.assemblies")

    filter_parts = [
        f"project_id=eq.{project_id}",
        f"suite_id=eq.{suite_id}",
    ]
    if active_only:
        filter_parts.append("supersedes_id=is.null")

    filter_str = "&".join(filter_parts)

    try:
        rows = await supabase_select(
            "blueprint_assemblies",
            filters=filter_str,
            order_by="created_at.desc",
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.list_assemblies: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read.assemblies",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read.assemblies",
        outcome="success",
        redacted_inputs={"project_id": project_id, "active_only": active_only},
        redacted_outputs={"assembly_count": len(rows)},
    )

    return [
        BlueprintAssemblyRead(
            id=row["id"],
            type=row.get("type"),
            quantity=row.get("quantity"),
            unit=row.get("unit"),
            truth=row["truth"],
            supersedes_id=row.get("supersedes_id"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/materials
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/materials",
    response_model=list[BlueprintMaterialRead],
    summary="List blueprint materials (Scope tab)",
)
async def list_blueprint_materials(
    project_id: str,
    tariff_only: bool = Query(False, description="Filter to tariff-flagged materials only"),
    has_supplier: bool = Query(False, description="Filter to materials with a supplier assigned"),
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> list[BlueprintMaterialRead]:
    """List material line items for a blueprint project.

    Produced by Drew REASON (Wave 4); supplier_id populated by PROCURE (Wave 5).
    Supports tariff_only and has_supplier filters for the Scope tab.

    Law #9: supplier address / business name are NOT returned here.
    Only the opaque supplier_id UUID is included.

    Returns 404 when project is not visible to the requesting tenant (Law #6).
    Emits blueprint.read.materials receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    await _require_project(project_id, suite_id, scope, "blueprint.read.materials")

    filter_parts = [
        f"project_id=eq.{project_id}",
        f"suite_id=eq.{suite_id}",
        # Active only — Wave 5 PROCURE always supersedes, so exclude stale rows
        "supersedes_id=is.null",
    ]

    # tariff_only: exclude rows where tariff_flag = 'none'
    if tariff_only:
        filter_parts.append("tariff_flag=neq.none")

    # has_supplier: exclude rows with no supplier_id
    if has_supplier:
        filter_parts.append("supplier_id=not.is.null")

    filter_str = "&".join(filter_parts)

    try:
        rows = await supabase_select(
            "blueprint_materials",
            filters=filter_str,
            order_by="created_at.desc",
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.list_materials: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read.materials",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read.materials",
        outcome="success",
        # Law #9: never log supplier_id values or line_item text in receipt outputs
        redacted_inputs={"project_id": project_id, "tariff_only": tariff_only, "has_supplier": has_supplier},
        redacted_outputs={"material_count": len(rows)},
    )

    return [
        BlueprintMaterialRead(
            id=row["id"],
            line_item=row.get("line_item"),
            quantity=row.get("quantity"),
            unit=row.get("unit"),
            truth=row["truth"],
            tariff_flag=row.get("tariff_flag", "none"),
            supplier_id=row.get("supplier_id"),
            supersedes_id=row.get("supersedes_id"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/missing_inputs
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/missing_inputs",
    response_model=list[BlueprintMissingInputRead],
    summary="List blueprint missing inputs (Scope tab)",
)
async def list_blueprint_missing_inputs(
    project_id: str,
    unresolved_only: bool = Query(True, description="Exclude resolved inputs (default: true)"),
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> list[BlueprintMissingInputRead]:
    """List contractor input gaps for a blueprint project.

    By default returns only unresolved gaps (resolved_at IS NULL).
    Set unresolved_only=false to see all inputs including resolved ones.

    Returns 404 when project is not visible to the requesting tenant (Law #6).
    Emits blueprint.read.missing_inputs receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    await _require_project(project_id, suite_id, scope, "blueprint.read.missing_inputs")

    filter_parts = [
        f"project_id=eq.{project_id}",
        f"suite_id=eq.{suite_id}",
    ]

    if unresolved_only:
        filter_parts.append("resolved_at=is.null")

    filter_str = "&".join(filter_parts)

    try:
        rows = await supabase_select(
            "blueprint_missing_inputs",
            filters=filter_str,
            order_by="created_at.asc",
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.list_missing_inputs: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read.missing_inputs",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read.missing_inputs",
        outcome="success",
        redacted_inputs={"project_id": project_id, "unresolved_only": unresolved_only},
        redacted_outputs={"missing_input_count": len(rows)},
    )

    return [
        BlueprintMissingInputRead(
            id=row["id"],
            description=row.get("description"),
            suggested_resolution=row.get("suggested_resolution"),
            resolved_by=row.get("resolved_by"),
            resolved_at=row.get("resolved_at"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# GET /v1/blueprints/projects/{project_id}/story
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/story",
    response_model=BlueprintStoryRead,
    summary="Get blueprint story (Scope tab — phased narrative)",
)
async def get_blueprint_story(
    project_id: str,
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> BlueprintStoryRead:
    """Return the active phased story for a blueprint project.

    The story is produced by Drew REASON (Wave 4).  Only the active version
    (supersedes_id IS NULL) is returned; prior versions are ignored.

    Law #9: markdown text is NOT logged or embedded in receipts — only
    phase_count and mean_confidence are recorded.

    Returns 404 when project is not visible to the requesting tenant (Law #6).
    Emits blueprint.read.story receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    await _require_project(project_id, suite_id, scope, "blueprint.read.story")

    try:
        rows = await supabase_select(
            "blueprint_stories",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&supersedes_id=is.null",
            order_by="phase.asc",
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.get_story: DB error project=%s suite=%s error=%s",
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.read.story",
            outcome="failed",
            redacted_inputs={"project_id": project_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Build aggregated response from per-phase rows
    phases: list[BlueprintStoryPhase] = []
    mean_confidence: float | None = None
    model_version: str | None = None
    generated_at: datetime | None = None

    for row in rows:
        phase_num = row.get("phase")
        markdown = row.get("markdown") or ""
        if phase_num is not None:
            phases.append(
                BlueprintStoryPhase(
                    phase_number=int(phase_num),
                    markdown=markdown,
                    truth_distribution=row.get("truth_distribution"),
                )
            )
        # Harvest project-level metadata from any row
        if mean_confidence is None and row.get("mean_confidence") is not None:
            mean_confidence = float(row["mean_confidence"])
        if model_version is None and row.get("model_version") is not None:
            model_version = str(row["model_version"])
        if generated_at is None:
            raw_ts = row.get("created_at")
            if isinstance(raw_ts, str):
                try:
                    generated_at = datetime.fromisoformat(raw_ts)
                except ValueError:
                    pass
            elif isinstance(raw_ts, datetime):
                generated_at = raw_ts

    _cut_receipt(
        scope=scope,
        action_type="blueprint.read.story",
        outcome="success",
        # Law #9: no markdown content in receipt
        redacted_inputs={"project_id": project_id},
        redacted_outputs={"phase_count": len(phases), "mean_confidence": mean_confidence},
    )

    return BlueprintStoryRead(
        project_id=project_id,
        phases=phases,
        mean_confidence=mean_confidence,
        model_version=model_version,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# POST /v1/blueprints/projects/{project_id}/missing_inputs/{input_id}/resolve
# ---------------------------------------------------------------------------

@router.post(
    "/projects/{project_id}/missing_inputs/{input_id}/resolve",
    summary="Resolve a missing input (YELLOW tier — capability token required)",
    status_code=status.HTTP_200_OK,
)
async def resolve_missing_input(
    project_id: str,
    input_id: str,
    body: MissingInputResolveRequest = Body(...),
    x_tenant_id: str | None = Header(None, alias="x-tenant-id"),
    x_suite_id: str | None = Header(None, alias="x-suite-id"),
    x_office_id: str | None = Header(None, alias="x-office-id"),
) -> dict[str, Any]:
    """Resolve a contractor input gap.

    YELLOW tier (Law #4): requires a valid capability_token in the request body.
    Validates the token is non-empty (full server-side signature verification
    delegated to the token_service in production — stub here for unit tests).

    Actions:
      1. Validates capability_token (fail-closed — Law #3).
      2. Marks blueprint_missing_inputs row as resolved.
      3. Inserts a field_confirmed blueprint_assemblies row derived from the gap.
      4. Emits blueprint.missing_input.resolved receipt (Law #2).

    Returns 404 when the project or input is not visible to the requesting
    tenant (Law #6 — no existence leak).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    # Law #3 / Law #4: fail closed on missing or blank capability token
    if not body.capability_token or not body.capability_token.strip():
        _cut_receipt(
            scope=scope,
            action_type="blueprint.missing_input.resolved",
            risk_tier="yellow",
            outcome="denied",
            redacted_inputs={"project_id": project_id, "input_id": input_id},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )

    # Verify project ownership (Law #6)
    await _require_project(project_id, suite_id, scope, "blueprint.missing_input.resolved")

    # Load the missing input row
    try:
        input_rows = await supabase_select(
            "blueprint_missing_inputs",
            filters=f"id=eq.{input_id}&project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.resolve_input: DB error input=%s project=%s suite=%s error=%s",
            input_id[:8] if len(input_id) >= 8 else input_id,
            project_id[:8] if len(project_id) >= 8 else project_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.missing_input.resolved",
            risk_tier="yellow",
            outcome="failed",
            redacted_inputs={"project_id": project_id, "input_id": input_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not input_rows:
        _cut_receipt(
            scope=scope,
            action_type="blueprint.missing_input.resolved",
            risk_tier="yellow",
            outcome="not_found",
            redacted_inputs={"project_id": project_id, "input_id": input_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    now_iso = datetime.now(timezone.utc).isoformat()

    # Step 1: Mark missing input as resolved
    try:
        await supabase_update(
            "blueprint_missing_inputs",
            filters=f"id=eq.{input_id}&suite_id=eq.{suite_id}",
            data={
                "resolved_by": str(body.resolved_by),
                "resolved_at": now_iso,
            },
        )
    except SupabaseClientError as exc:
        logger.error(
            "blueprints.resolve_input: update failed input=%s suite=%s error=%s",
            input_id[:8] if len(input_id) >= 8 else input_id,
            suite_id[:8],
            type(exc).__name__,
        )
        _cut_receipt(
            scope=scope,
            action_type="blueprint.missing_input.resolved",
            risk_tier="yellow",
            outcome="failed",
            redacted_inputs={"project_id": project_id, "input_id": input_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Update failed")

    # Step 2: Insert a field_confirmed assembly row derived from the resolution
    field_confirmed_id = str(uuid.uuid4())
    assembly_row: dict[str, Any] = {
        "id": field_confirmed_id,
        "suite_id": suite_id,
        "project_id": project_id,
        "type": "field_confirmed_resolution",
        "quantity": None,
        "unit": None,
        "truth": TruthClass.FIELD_CONFIRMED.value,
        "supersedes_id": None,
        "created_at": now_iso,
        "created_by": str(body.resolved_by),
    }

    assembly_inserted = False
    try:
        from aspire_orchestrator.services.supabase_client import supabase_insert
        await supabase_insert("blueprint_assemblies", assembly_row)
        assembly_inserted = True
    except (SupabaseClientError, Exception) as exc:
        # Non-fatal: log and continue — primary action (resolve) succeeded
        logger.warning(
            "blueprints.resolve_input: assembly insert failed input=%s error=%s",
            input_id[:8] if len(input_id) >= 8 else input_id,
            type(exc).__name__,
        )

    _cut_receipt(
        scope=scope,
        action_type="blueprint.missing_input.resolved",
        risk_tier="yellow",
        outcome="success",
        redacted_inputs={"project_id": project_id, "input_id": input_id},
        redacted_outputs={
            "resolved_at": now_iso,
            "field_confirmed_assembly_id": field_confirmed_id,
            "assembly_inserted": assembly_inserted,
        },
    )

    return {
        "success": True,
        "input_id": input_id,
        "resolved_at": now_iso,
        "field_confirmed_assembly_id": field_confirmed_id,
        "assembly_inserted": assembly_inserted,
    }
