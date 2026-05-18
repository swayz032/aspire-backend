"""Memory Engine page routes — 12 routes for Office Memory + Finance Memory pages.

Implements plan §5 (memory_pages). Mounted in server.py under tag "memory-pages".

Office Memory routes (visibility_scope='office'):
  POST /v1/office-memory/get-memory-brief       — Wraps build_office_brief
  POST /v1/office-memory/search-memory          — Wraps memory search (Pass 5 stub)
  POST /v1/office-memory/get-thread-memory      — list_by_thread + build_thread_brief
  POST /v1/office-memory/create-handoff-note    — MemoryService.write memory_type=handoff_note
  POST /v1/office-memory/save-session-summary   — MemoryService.write memory_type=session_summary
  POST /v1/office-memory/promote-artifact       — MemoryService.write memory_type=artifact_reference

Finance Memory routes (visibility_scope='finance'):
  POST /v1/finance-memory/get-memory-brief
  POST /v1/finance-memory/search-memory
  POST /v1/finance-memory/get-thread-memory
  POST /v1/finance-memory/create-handoff-note
  POST /v1/finance-memory/save-session-summary
  POST /v1/finance-memory/promote-artifact

Auth: same X-Tenant-Id / X-Suite-Id / X-Office-Id / X-Actor-Id header dependency
as memory.py. Reuses get_scope() from that module.

Law compliance:
  Law #2: Every state-changing write emits a receipt via MemoryService (which calls receipt_store).
  Law #3: Missing headers → 401. Tenant mismatch → 403. Service failure → 422/503.
  Law #6: visibility_scope='finance' routes enforce finance tenant isolation at service layer.
  Law #9: No PII in log lines. Summary content never echoed in error messages.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aspire_orchestrator.schemas.memory_v1 import (
    FinanceBriefOut,
    MemoryObjectIn,
    MemoryObjectOut,
    MemorySearchRequest as SpineMemorySearchRequest,
    MemoryStatus,
    OfficeBriefOut,
    Provenance,
    ScopedIdentity,
    ServiceBriefOut,
    ThreadBriefOut,
)
from aspire_orchestrator.services.brief_materializer import BriefMaterializer
from aspire_orchestrator.services.memory_search import MemorySearchService
from aspire_orchestrator.services.memory_service import MemoryService, MemoryServiceError

# Reuse the shared scope dependency from memory.py
from aspire_orchestrator.routes.memory import (
    ScopedIdentityFromHeaders,
    MemorySearchRequest,
    MemorySearchResponse,
    get_scope,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Shared request / response models
# ---------------------------------------------------------------------------


class GetMemoryBriefRequest(BaseModel):
    """Input for get-memory-brief endpoints."""

    scope: ScopedIdentity
    force_refresh: bool = False


class SearchMemoryRequest(BaseModel):
    """Input for search-memory endpoints."""

    scope: ScopedIdentity
    q: str = Field(min_length=1)
    memory_type: list[str] | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=200)


class SearchMemoryResponse(BaseModel):
    results: list[MemoryObjectOut]
    total: int
    note: str | None = None


class GetThreadMemoryRequest(BaseModel):
    """Input for get-thread-memory endpoints."""

    scope: ScopedIdentity
    thread_id: UUID
    limit: int = Field(default=50, ge=1, le=500)


class GetThreadMemoryResponse(BaseModel):
    objects: list[MemoryObjectOut]
    brief: ThreadBriefOut | None
    total: int


class CreateHandoffNoteRequest(BaseModel):
    """Input for create-handoff-note endpoints."""

    scope: ScopedIdentity
    summary: str = Field(min_length=1)
    title: str | None = None
    thread_id: UUID | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    correlation_id: UUID
    trace_id: UUID
    source_agent: str | None = None
    linked_memory_ids: list[UUID] = Field(default_factory=list)
    idempotency_key: str | None = None


class CreateHandoffNoteResponse(BaseModel):
    memory_id: UUID
    status: str


class SaveSessionSummaryRequest(BaseModel):
    """Input for save-session-summary endpoints."""

    scope: ScopedIdentity
    summary: str = Field(min_length=1)
    title: str | None = None
    thread_id: UUID | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    correlation_id: UUID
    trace_id: UUID
    source_agent: str | None = None
    session_duration_seconds: int | None = None
    idempotency_key: str | None = None


class SaveSessionSummaryResponse(BaseModel):
    memory_id: UUID
    status: str


class PromoteArtifactRequest(BaseModel):
    """Input for promote-artifact endpoints."""

    scope: ScopedIdentity
    summary: str = Field(min_length=1)
    title: str | None = None
    thread_id: UUID | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    correlation_id: UUID
    trace_id: UUID
    source_agent: str | None = None
    artifact_origin: str | None = None
    linked_artifact_ids: list[UUID] = Field(default_factory=list)
    idempotency_key: str | None = None


class PromoteArtifactResponse(BaseModel):
    memory_id: UUID
    status: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_tenant_match(scope_header: ScopedIdentityFromHeaders, body_scope: ScopedIdentity) -> None:
    """Raise 403 if header tenant_id != body tenant_id (Law #6)."""
    if str(scope_header.tenant_id) != str(body_scope.tenant_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "Scope header tenant_id does not match request body tenant_id",
                "tenant_id": str(scope_header.tenant_id),
            },
        )


def _build_provenance(
    *,
    correlation_id: UUID,
    trace_id: UUID,
    source_agent: str | None = None,
    artifact_origin: str | None = None,
    summary_origin: str | None = None,
    channel: str = "ui",
) -> Provenance:
    from aspire_orchestrator.schemas.memory_v1 import Channel, SourceAgent

    return Provenance(
        source_surface="canvas_desk",
        source_agent=source_agent if source_agent in ("ava", "sarah", "eli", "nora", "finn", "tim", "system") else "system",
        runtime_family="ui",
        channel=channel if channel in ("voice", "video", "email", "sms", "workflow", "finance", "ui", "webhook") else "ui",
        trace_id=trace_id,
        correlation_id=correlation_id,
        artifact_origin=artifact_origin,
        summary_origin=summary_origin,
    )


async def _write_memory(
    *,
    scope: ScopedIdentity,
    memory_type: str,
    summary: str,
    title: str | None,
    thread_id: UUID | None,
    entity_type: str | None,
    entity_id: UUID | None,
    correlation_id: UUID,
    trace_id: UUID,
    source_agent: str | None,
    visibility_scope: str,
    status: MemoryStatus | None = None,
    promoted_at: datetime | None = None,
    detail: dict[str, Any] | None = None,
    linked_artifact_ids: list[UUID] | None = None,
    idempotency_key: str | None = None,
) -> MemoryObjectOut:
    """Single shared write path for all 12 page-route writes.

    Wraps MemoryService.write which owns Law #2 receipt emission.
    Not currently used by page routes (each builds MemoryObjectIn inline for clarity),
    but available for future consolidation.
    """
    provenance = _build_provenance(
        correlation_id=correlation_id,
        trace_id=trace_id,
        source_agent=source_agent,
    )

    obj_in = MemoryObjectIn(
        scope=scope,
        provenance=provenance,
        memory_type=memory_type,  # type: ignore[arg-type]
        entity_type=entity_type,
        entity_id=entity_id,
        thread_id=thread_id,
        title=title,
        summary=summary,
        detail=detail or {},
        visibility_scope=visibility_scope,  # type: ignore[arg-type]
        status=status,
        linked_artifact_ids=linked_artifact_ids or [],
        promoted_at=promoted_at,
        idempotency_key=idempotency_key,
    )

    svc = MemoryService()
    return await svc.write(obj_in, scope=scope)


# ---------------------------------------------------------------------------
# ============================  OFFICE MEMORY  =============================
# ---------------------------------------------------------------------------


@router.post("/v1/office-memory/get-memory-brief", response_model=OfficeBriefOut)
async def office_get_memory_brief(
    body: GetMemoryBriefRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> OfficeBriefOut:
    """Return the office brief for the caller's office."""
    _assert_tenant_match(scope, body.scope)

    try:
        materializer = BriefMaterializer()
        return await materializer.build_office_brief(
            office_id=body.scope.office_id,
            scope=body.scope,
        )
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("office_memory.get_memory_brief error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "BRIEF_BUILD_FAILED", "message": "Brief build failed"})


@router.post("/v1/office-memory/search-memory", response_model=SearchMemoryResponse)
async def office_search_memory(
    body: SearchMemoryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SearchMemoryResponse:
    """Search office memory — wraps MemorySearchService with visibility_scope='office'.

    The page route forces visibility_scope='office' regardless of caller intent.
    Cross-scope searches must use /v1/memory/search directly.
    """
    _assert_tenant_match(scope, body.scope)

    canonical_req = SpineMemorySearchRequest(
        tenant_id=body.scope.tenant_id,
        suite_id=body.scope.suite_id,
        office_id=body.scope.office_id,
        query_text=body.q,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        memory_types=body.memory_type,  # type: ignore[arg-type]
        visibility_scope="office",  # forced for office page
        limit=body.limit,
    )

    try:
        result = await MemorySearchService().search(canonical_req, scope=body.scope)
    except MemoryServiceError as exc:
        if exc.code == "TENANT_ISOLATION_VIOLATION":
            raise HTTPException(status_code=403, detail={"code": exc.code, "message": str(exc)})
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("office_memory.search_memory error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "MEMORY_SEARCH_FAILED", "message": "Office memory search failed"},
        )

    logger.info(
        "office_memory.search_memory: q=%r tenant=%s items=%d",
        body.q[:40] if body.q else "",
        str(scope.tenant_id)[:8],
        len(result.items),
    )

    return SearchMemoryResponse(
        results=result.items,
        total=result.total or len(result.items),
        note=None,
    )


@router.post("/v1/office-memory/get-thread-memory", response_model=GetThreadMemoryResponse)
async def office_get_thread_memory(
    body: GetThreadMemoryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> GetThreadMemoryResponse:
    """List memory objects for a thread and return the thread brief."""
    _assert_tenant_match(scope, body.scope)

    try:
        svc = MemoryService()
        objects, _ = await svc.list_by_thread(
            body.thread_id,
            scope=body.scope,
            limit=body.limit,
        )

        brief: ThreadBriefOut | None = None
        try:
            materializer = BriefMaterializer()
            brief = await materializer.build_thread_brief(
                thread_id=body.thread_id,
                scope=body.scope,
            )
        except Exception as brief_exc:
            logger.warning(
                "office_memory.get_thread_memory: brief fetch failed (non-fatal): %s",
                type(brief_exc).__name__,
            )

    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("office_memory.get_thread_memory error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "THREAD_MEMORY_FAILED", "message": "Thread memory fetch failed"})

    return GetThreadMemoryResponse(
        objects=objects,
        brief=brief,
        total=len(objects),
    )


@router.post("/v1/office-memory/create-handoff-note", response_model=CreateHandoffNoteResponse)
async def office_create_handoff_note(
    body: CreateHandoffNoteRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> CreateHandoffNoteResponse:
    """Write a handoff_note memory object with visibility_scope='office'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
    )

    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="handoff_note",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail={},
        visibility_scope="office",
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("office_memory.create_handoff_note error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Handoff note write failed"})

    logger.info(
        "office_memory.create_handoff_note: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return CreateHandoffNoteResponse(memory_id=result.memory_id, status="success")


@router.post("/v1/office-memory/save-session-summary", response_model=SaveSessionSummaryResponse)
async def office_save_session_summary(
    body: SaveSessionSummaryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SaveSessionSummaryResponse:
    """Write a session_summary memory object with visibility_scope='office'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
    )

    detail: dict[str, Any] = {}
    if body.session_duration_seconds is not None:
        detail["session_duration_seconds"] = body.session_duration_seconds

    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="session_summary",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail=detail,
        visibility_scope="office",
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("office_memory.save_session_summary error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Session summary write failed"})

    logger.info(
        "office_memory.save_session_summary: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return SaveSessionSummaryResponse(memory_id=result.memory_id, status="success")


@router.post("/v1/office-memory/promote-artifact", response_model=PromoteArtifactResponse)
async def office_promote_artifact(
    body: PromoteArtifactRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> PromoteArtifactResponse:
    """Promote an artifact: write artifact_reference with status='promoted', visibility_scope='office'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
        artifact_origin=body.artifact_origin,
    )

    now = datetime.now(tz=timezone.utc)
    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="artifact_reference",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail={"artifact_origin": body.artifact_origin},
        visibility_scope="office",
        status="promoted",
        promoted_at=now,
        linked_artifact_ids=body.linked_artifact_ids,
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("office_memory.promote_artifact error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Artifact promotion failed"})

    logger.info(
        "office_memory.promote_artifact: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return PromoteArtifactResponse(memory_id=result.memory_id, status="promoted")


# ---------------------------------------------------------------------------
# ============================  FINANCE MEMORY  ============================
# visibility_scope='finance' on all writes
# ---------------------------------------------------------------------------


@router.post("/v1/finance-memory/get-memory-brief", response_model=FinanceBriefOut)
async def finance_get_memory_brief(
    body: GetMemoryBriefRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> FinanceBriefOut:
    """Return the finance brief for the caller's office."""
    _assert_tenant_match(scope, body.scope)

    try:
        materializer = BriefMaterializer()
        return await materializer.build_finance_brief(
            office_id=body.scope.office_id,
            scope=body.scope,
        )
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("finance_memory.get_memory_brief error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "BRIEF_BUILD_FAILED", "message": "Finance brief build failed"})


@router.post("/v1/finance-memory/search-memory", response_model=SearchMemoryResponse)
async def finance_search_memory(
    body: SearchMemoryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SearchMemoryResponse:
    """Search finance memory — wraps MemorySearchService with visibility_scope='finance'.

    The page route forces visibility_scope='finance' so finance memory is
    isolated from the office memory page (Law #6 visibility_scope guarantee).
    """
    _assert_tenant_match(scope, body.scope)

    canonical_req = SpineMemorySearchRequest(
        tenant_id=body.scope.tenant_id,
        suite_id=body.scope.suite_id,
        office_id=body.scope.office_id,
        query_text=body.q,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        memory_types=body.memory_type,  # type: ignore[arg-type]
        visibility_scope="finance",  # forced for finance page
        limit=body.limit,
    )

    try:
        result = await MemorySearchService().search(canonical_req, scope=body.scope)
    except MemoryServiceError as exc:
        if exc.code == "TENANT_ISOLATION_VIOLATION":
            raise HTTPException(status_code=403, detail={"code": exc.code, "message": str(exc)})
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("finance_memory.search_memory error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "MEMORY_SEARCH_FAILED", "message": "Finance memory search failed"},
        )

    logger.info(
        "finance_memory.search_memory: q=%r tenant=%s items=%d",
        body.q[:40] if body.q else "",
        str(scope.tenant_id)[:8],
        len(result.items),
    )

    return SearchMemoryResponse(
        results=result.items,
        total=result.total or len(result.items),
        note=None,
    )


@router.post("/v1/finance-memory/get-thread-memory", response_model=GetThreadMemoryResponse)
async def finance_get_thread_memory(
    body: GetThreadMemoryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> GetThreadMemoryResponse:
    """List memory objects for a thread and return the thread brief (finance scope)."""
    _assert_tenant_match(scope, body.scope)

    try:
        svc = MemoryService()
        objects, _ = await svc.list_by_thread(
            body.thread_id,
            scope=body.scope,
            limit=body.limit,
        )

        brief: ThreadBriefOut | None = None
        try:
            materializer = BriefMaterializer()
            brief = await materializer.build_thread_brief(
                thread_id=body.thread_id,
                scope=body.scope,
            )
        except Exception as brief_exc:
            logger.warning(
                "finance_memory.get_thread_memory: brief fetch failed (non-fatal): %s",
                type(brief_exc).__name__,
            )

    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("finance_memory.get_thread_memory error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "THREAD_MEMORY_FAILED", "message": "Thread memory fetch failed"})

    return GetThreadMemoryResponse(
        objects=objects,
        brief=brief,
        total=len(objects),
    )


@router.post("/v1/finance-memory/create-handoff-note", response_model=CreateHandoffNoteResponse)
async def finance_create_handoff_note(
    body: CreateHandoffNoteRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> CreateHandoffNoteResponse:
    """Write a handoff_note memory object with visibility_scope='finance'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
        channel="finance",
    )

    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="handoff_note",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail={},
        visibility_scope="finance",
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("finance_memory.create_handoff_note error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Finance handoff note write failed"})

    logger.info(
        "finance_memory.create_handoff_note: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return CreateHandoffNoteResponse(memory_id=result.memory_id, status="success")


@router.post("/v1/finance-memory/save-session-summary", response_model=SaveSessionSummaryResponse)
async def finance_save_session_summary(
    body: SaveSessionSummaryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SaveSessionSummaryResponse:
    """Write a session_summary memory object with visibility_scope='finance'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
        channel="finance",
    )

    detail: dict[str, Any] = {}
    if body.session_duration_seconds is not None:
        detail["session_duration_seconds"] = body.session_duration_seconds

    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="session_summary",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail=detail,
        visibility_scope="finance",
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("finance_memory.save_session_summary error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Finance session summary write failed"})

    logger.info(
        "finance_memory.save_session_summary: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return SaveSessionSummaryResponse(memory_id=result.memory_id, status="success")


@router.post("/v1/finance-memory/promote-artifact", response_model=PromoteArtifactResponse)
async def finance_promote_artifact(
    body: PromoteArtifactRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> PromoteArtifactResponse:
    """Promote an artifact with visibility_scope='finance', status='promoted'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
        artifact_origin=body.artifact_origin,
        channel="finance",
    )

    now = datetime.now(tz=timezone.utc)
    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="artifact_reference",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail={"artifact_origin": body.artifact_origin},
        visibility_scope="finance",
        status="promoted",
        promoted_at=now,
        linked_artifact_ids=body.linked_artifact_ids,
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("finance_memory.promote_artifact error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Finance artifact promotion failed"})

    logger.info(
        "finance_memory.promote_artifact: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return PromoteArtifactResponse(memory_id=result.memory_id, status="promoted")


# ---------------------------------------------------------------------------
# ============================  SERVICE MEMORY  ============================
# visibility_scope='service' on all writes — Wave 5.1b-3
# ---------------------------------------------------------------------------


@router.post("/v1/service-memory/get-memory-brief", response_model=ServiceBriefOut)
async def service_get_memory_brief(
    body: GetMemoryBriefRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> ServiceBriefOut:
    """Return the service brief for the caller's office.

    TODO: Wire build_service_brief() once the parallel Wave 5.1b-3 agent ships
    that method on BriefMaterializer. Until then the route returns a stub so
    downstream callers can exercise the auth/scope path.
    """
    _assert_tenant_match(scope, body.scope)

    try:
        materializer = BriefMaterializer()
        return await materializer.build_service_brief(
            office_id=body.scope.office_id,
            scope=body.scope,
        )
    except AttributeError:
        # Stub path: build_service_brief not yet on BriefMaterializer.
        logger.warning(
            "service_memory.get_memory_brief: build_service_brief not yet wired — returning stub"
        )
        return ServiceBriefOut(
            tenant_id=body.scope.tenant_id,
            suite_id=body.scope.suite_id,
            office_id=body.scope.office_id,
            brief_text=None,
            brief_json={"placeholder": "build_service_brief not yet wired"},
            last_built_at=datetime.now(tz=timezone.utc),
        )
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("service_memory.get_memory_brief error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "BRIEF_BUILD_FAILED", "message": "Service brief build failed"})


@router.post("/v1/service-memory/search-memory", response_model=SearchMemoryResponse)
async def service_search_memory(
    body: SearchMemoryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SearchMemoryResponse:
    """Search service memory — wraps MemorySearchService with visibility_scope='service'.

    The page route forces visibility_scope='service' regardless of caller intent.
    Cross-scope searches must use /v1/memory/search directly.
    """
    _assert_tenant_match(scope, body.scope)

    canonical_req = SpineMemorySearchRequest(
        tenant_id=body.scope.tenant_id,
        suite_id=body.scope.suite_id,
        office_id=body.scope.office_id,
        query_text=body.q,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        memory_types=body.memory_type,  # type: ignore[arg-type]
        visibility_scope="service",  # forced for service page
        limit=body.limit,
    )

    try:
        result = await MemorySearchService().search(canonical_req, scope=body.scope)
    except MemoryServiceError as exc:
        if exc.code == "TENANT_ISOLATION_VIOLATION":
            raise HTTPException(status_code=403, detail={"code": exc.code, "message": str(exc)})
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("service_memory.search_memory error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "MEMORY_SEARCH_FAILED", "message": "Service memory search failed"},
        )

    logger.info(
        "service_memory.search_memory: q=%r tenant=%s items=%d",
        body.q[:40] if body.q else "",
        str(scope.tenant_id)[:8],
        len(result.items),
    )

    return SearchMemoryResponse(
        results=result.items,
        total=result.total or len(result.items),
        note=None,
    )


@router.post("/v1/service-memory/get-thread-memory", response_model=GetThreadMemoryResponse)
async def service_get_thread_memory(
    body: GetThreadMemoryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> GetThreadMemoryResponse:
    """List memory objects for a thread and return the thread brief (service scope)."""
    _assert_tenant_match(scope, body.scope)

    try:
        svc = MemoryService()
        objects, _ = await svc.list_by_thread(
            body.thread_id,
            scope=body.scope,
            limit=body.limit,
        )

        brief: ThreadBriefOut | None = None
        try:
            materializer = BriefMaterializer()
            brief = await materializer.build_thread_brief(
                thread_id=body.thread_id,
                scope=body.scope,
            )
        except Exception as brief_exc:
            logger.warning(
                "service_memory.get_thread_memory: brief fetch failed (non-fatal): %s",
                type(brief_exc).__name__,
            )

    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("service_memory.get_thread_memory error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "THREAD_MEMORY_FAILED", "message": "Thread memory fetch failed"})

    return GetThreadMemoryResponse(
        objects=objects,
        brief=brief,
        total=len(objects),
    )


@router.post("/v1/service-memory/create-handoff-note", response_model=CreateHandoffNoteResponse)
async def service_create_handoff_note(
    body: CreateHandoffNoteRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> CreateHandoffNoteResponse:
    """Write a handoff_note memory object with visibility_scope='service'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
    )

    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="handoff_note",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail={},
        visibility_scope="service",
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("service_memory.create_handoff_note error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Service handoff note write failed"})

    logger.info(
        "service_memory.create_handoff_note: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return CreateHandoffNoteResponse(memory_id=result.memory_id, status="success")


@router.post("/v1/service-memory/save-session-summary", response_model=SaveSessionSummaryResponse)
async def service_save_session_summary(
    body: SaveSessionSummaryRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SaveSessionSummaryResponse:
    """Write a session_summary memory object with visibility_scope='service'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
    )

    detail: dict[str, Any] = {}
    if body.session_duration_seconds is not None:
        detail["session_duration_seconds"] = body.session_duration_seconds

    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="session_summary",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail=detail,
        visibility_scope="service",
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("service_memory.save_session_summary error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Service session summary write failed"})

    logger.info(
        "service_memory.save_session_summary: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return SaveSessionSummaryResponse(memory_id=result.memory_id, status="success")


@router.post("/v1/service-memory/promote-artifact", response_model=PromoteArtifactResponse)
async def service_promote_artifact(
    body: PromoteArtifactRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> PromoteArtifactResponse:
    """Promote an artifact with visibility_scope='service', status='promoted'."""
    _assert_tenant_match(scope, body.scope)

    provenance = _build_provenance(
        correlation_id=body.correlation_id,
        trace_id=body.trace_id,
        source_agent=body.source_agent,
        artifact_origin=body.artifact_origin,
    )

    now = datetime.now(tz=timezone.utc)
    obj_in = MemoryObjectIn(
        scope=body.scope,
        provenance=provenance,
        memory_type="artifact_reference",
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        title=body.title,
        summary=body.summary,
        detail={"artifact_origin": body.artifact_origin},
        visibility_scope="service",
        status="promoted",
        promoted_at=now,
        linked_artifact_ids=body.linked_artifact_ids,
        idempotency_key=body.idempotency_key,
    )

    try:
        svc = MemoryService()
        result = await svc.write(obj_in, scope=body.scope)
    except MemoryServiceError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        logger.exception("service_memory.promote_artifact error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail={"code": "WRITE_FAILED", "message": "Service artifact promotion failed"})

    logger.info(
        "service_memory.promote_artifact: memory_id=%s trace_id=%s",
        str(result.memory_id)[:8],
        str(body.trace_id)[:8],
    )
    return PromoteArtifactResponse(memory_id=result.memory_id, status="promoted")
