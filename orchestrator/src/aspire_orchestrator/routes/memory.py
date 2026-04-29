"""Memory Spine API — 10 routes for the Office Memory Engine Coordination Spine.

Implements plan §5 (spine routes). Mounted in server.py under tag "memory-spine".

Routes:
  POST /v1/session-broker/start         — Session bootstrap (brief + dynamic_variables)
  POST /v1/memory-events                — Ingest MemoryEventEnvelope → inbox + async refinery
  POST /v1/refinery/run                 — Sync refinery invocation for a single event_id
  POST /v1/memory/search                — Hybrid search (Pass 5: MemorySearchService + 6-tier ranking)
  POST /v1/proactive-candidates/query   — Query proactive candidates by scope + filters
  POST /v1/approvals/request            — Create approval binding + approval_links row
  POST /v1/receipts/write               — Store receipts + write receipt_memory_links
  GET  /v1/briefs/office/{office_id}    — Office brief (build_office_brief)
  GET  /v1/briefs/finance/{office_id}   — Finance brief (build_finance_brief)
  GET  /v1/briefs/thread/{thread_id}    — Thread brief (build_thread_brief)

Law compliance:
  Law #2: Every state-changing route writes a receipt via existing receipt_store.
  Law #3: Missing scope headers → 401 SCOPE_MISSING. Invalid token → 403 CAPABILITY_DENIED.
          Cross-tenant attempt → 403 TENANT_ISOLATION_VIOLATION.
  Law #6: All scope fields required (tenant_id / suite_id / office_id).
  Law #9: No PII in structured log lines; error messages carry only IDs and codes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aspire_orchestrator.schemas.memory_v1 import (
    CandidateQuery,
    FinanceBriefOut,
    MemoryEventEnvelope,
    MemoryObjectIn,
    MemoryObjectOut,
    MemorySearchRequest as SpineMemorySearchRequest,
    OfficeBriefOut,
    ProactiveCandidateOut,
    RefineResult,
    ScopedIdentity,
    ThreadBriefOut,
)
from aspire_orchestrator.services.memory_search import MemorySearchService
from aspire_orchestrator.services.memory_service import MemoryService, MemoryServiceError
from aspire_orchestrator.services.brief_materializer import BriefMaterializer
from aspire_orchestrator.services.proactive_candidate_engine import ProactiveCandidateEngine
from aspire_orchestrator.services.transcript_event_refinery import (
    TranscriptEventRefinery,
    RefineryError,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import (
    supabase_insert,
    supabase_select,
    SupabaseClientError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Shared dependency: ScopedIdentity from X- headers (Law #3 fail-closed)
# ---------------------------------------------------------------------------


class ScopedIdentityFromHeaders(BaseModel):
    """Extracted + validated scope from gateway-injected request headers."""

    tenant_id: UUID
    suite_id: UUID
    office_id: UUID
    actor_id: UUID | None = None


def get_scope(request: Request) -> ScopedIdentityFromHeaders:
    """FastAPI dependency — extract scope from X- headers, fail closed if missing.

    Missing any of X-Tenant-Id / X-Suite-Id / X-Office-Id → raises 401 immediately.
    Returns ScopedIdentityFromHeaders for downstream validation.
    """
    tenant_raw = request.headers.get("x-tenant-id")
    suite_raw = request.headers.get("x-suite-id")
    office_raw = request.headers.get("x-office-id")
    actor_raw = request.headers.get("x-actor-id")

    missing: list[str] = []
    if not tenant_raw:
        missing.append("X-Tenant-Id")
    if not suite_raw:
        missing.append("X-Suite-Id")
    if not office_raw:
        missing.append("X-Office-Id")

    if missing:
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        _deny_receipt(
            correlation_id=correlation_id,
            suite_id=suite_raw or "unknown",
            office_id=office_raw or "unknown",
            actor_id="fail_closed_guard",
            action_type="memory.scope_check",
            reason_code="SCOPE_MISSING",
            details={"missing_headers": missing},
        )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SCOPE_MISSING",
                "message": f"Missing required headers: {', '.join(missing)}",
                "tenant_id": tenant_raw or "unknown",
                "correlation_id": correlation_id,
            },
        )

    try:
        return ScopedIdentityFromHeaders(
            tenant_id=UUID(tenant_raw),
            suite_id=UUID(suite_raw),
            office_id=UUID(office_raw),
            actor_id=UUID(actor_raw) if actor_raw else None,
        )
    except ValueError:
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SCOPE_INVALID",
                "message": "One or more scope headers are not valid UUIDs",
                "correlation_id": correlation_id,
            },
        )


# ---------------------------------------------------------------------------
# Receipt helper (Law #2 — every denial produces a receipt)
# ---------------------------------------------------------------------------


def _deny_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    actor_id: str,
    action_type: str,
    reason_code: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Emit a denial receipt synchronously (fire-and-forget enqueue)."""
    receipt: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "memory_spine_router",
        "outcome": "denied",
        "reason_code": reason_code,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "receipt_type": "denial",
        "receipt_hash": "",
        "redacted_inputs": details,
        "redacted_outputs": None,
    }
    store_receipts([receipt])


def _error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    tenant_id: str = "unknown",
    correlation_id: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "tenant_id": tenant_id,
            "correlation_id": correlation_id or str(uuid.uuid4()),
        },
    )


# ---------------------------------------------------------------------------
# Request / Response models for routes not fully covered by existing schemas
# ---------------------------------------------------------------------------


class SessionStartRequest(BaseModel):
    """Input to POST /v1/session-broker/start."""

    agent_name: str = Field(min_length=1)
    scope: ScopedIdentity
    channel: str = Field(min_length=1)
    session_channel: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    handoff_id: UUID | None = None
    runtime_family: str | None = None
    dynamic_variables_hint: dict[str, Any] = Field(default_factory=dict)


class SessionStartResponse(BaseModel):
    """Response from POST /v1/session-broker/start."""

    session_id: UUID
    thread_id: UUID | None
    dynamic_variables: dict[str, Any]
    allowed_tools: list[str]
    trace_id: UUID


class MemoryEventResponse(BaseModel):
    """Response from POST /v1/memory-events."""

    event_id: UUID
    status: str
    trace_id: UUID


class ReceiptsWriteRequest(BaseModel):
    """Input to POST /v1/receipts/write — wraps existing receipt store + links."""

    receipts: list[dict[str, Any]] = Field(min_length=1)
    memory_links: list[dict[str, Any]] = Field(default_factory=list)


class ReceiptsWriteResponse(BaseModel):
    """Response from POST /v1/receipts/write."""

    receipt_ids: list[str]
    links_written: int


class ApprovalsRequestBody(BaseModel):
    """Input to POST /v1/approvals/request."""

    approval_id: str = Field(min_length=1)
    scope: ScopedIdentity
    requested_by_agent: str
    linked_candidate_id: UUID | None = None
    linked_memory_ids: list[UUID] = Field(default_factory=list)
    linked_workflow_run_id: UUID | None = None
    reason: str | None = None


class ApprovalsRequestResponse(BaseModel):
    """Response from POST /v1/approvals/request."""

    approval_link_id: UUID
    approval_id: str
    status: str


class MemorySearchRequest(BaseModel):
    """Input to POST /v1/memory/search."""

    scope: ScopedIdentity
    q: str = Field(min_length=1)
    visibility_scope: str | None = None
    memory_type: list[str] | None = None
    entity_id: UUID | None = None
    thread_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=200)
    include_raw: bool = False


class MemorySearchResponse(BaseModel):
    """Response from POST /v1/memory/search (Pass 5 will populate results)."""

    results: list[MemoryObjectOut]
    total: int
    note: str | None = None


# ---------------------------------------------------------------------------
# Anam handoff resolution helper (plan §7)
# ---------------------------------------------------------------------------

_HANDOFF_MEMORY_TYPES = ("pending_intent", "authority_context", "handoff_note")
# Maximum brief length in characters — keeps Anam prompt under context limits
_BRIEF_MAX_CHARS = 400


async def _resolve_anam_handoff(
    *,
    handoff_id: UUID,
    scope: ScopedIdentity,
    header_scope: ScopedIdentityFromHeaders,
    correlation_id: str,
    trace_id: UUID,
) -> dict[str, Any] | None:
    """Fetch the 3 handoff memory objects for an Anam video session.

    Queries memory_objects by correlation_id + tenant scope + memory_type IN
    (pending_intent, authority_context, handoff_note).  Builds a voiceHandoffBrief
    string (200-400 chars) ordered: handoff_note → authority_context → pending_intent.

    Returns:
        dict[str, Any] — dynamic_variables additions including voiceHandoffBrief
            and per-object IDs for downstream linking.
        None — ONLY when a cross-tenant attempt is detected (caller must 403).

    Degraded mode: if the handoff_id is valid UUID but no memory objects match,
    logs WARN and returns an empty dict (session continues without brief per plan §7.6).

    Law compliance:
        Law #3: Cross-tenant isolation violation → return None → caller raises 403.
        Law #9: No PII in log messages.  Only IDs and type labels logged.
    """
    # PostgREST IN filter for memory_type
    type_list = ",".join(_HANDOFF_MEMORY_TYPES)
    filter_str = (
        f"correlation_id=eq.{handoff_id}"
        f"&memory_type=in.({type_list})"
        f"&status=not.in.(rejected,superseded)"
    )
    try:
        rows = await supabase_select(
            "memory_objects",
            filter_str,
            order_by="created_at.asc",
            limit=5,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "session_broker.anam_handoff: DB query failed (degraded): %s trace_id=%s",
            exc.status_code,
            str(trace_id)[:8],
        )
        return {}

    if not rows:
        logger.warning(
            "session_broker.anam_handoff: no memory objects for handoff_id=%s "
            "(non-existent or already executed) — continuing without brief trace_id=%s",
            str(handoff_id)[:8],
            str(trace_id)[:8],
        )
        return {}

    # Law #6: Cross-tenant check — every row must belong to caller's scope
    for row in rows:
        row_tenant = str(row.get("tenant_id", "")).lower()
        row_suite = str(row.get("suite_id", "")).lower()
        caller_tenant = str(scope.tenant_id).lower()
        caller_suite = str(scope.suite_id).lower()
        if row_tenant != caller_tenant or row_suite != caller_suite:
            _deny_receipt(
                correlation_id=correlation_id,
                suite_id=str(header_scope.suite_id),
                office_id=str(header_scope.office_id),
                actor_id=str(header_scope.actor_id) if header_scope.actor_id else "unknown",
                action_type="session_broker.anam_handoff",
                reason_code="TENANT_ISOLATION_VIOLATION",
                details={
                    "handoff_id": str(handoff_id)[:8],
                    "row_tenant": row_tenant[:8],
                    "caller_tenant": caller_tenant[:8],
                },
            )
            logger.error(
                "session_broker.anam_handoff: TENANT_ISOLATION_VIOLATION "
                "handoff_id=%s caller_tenant=%s trace_id=%s",
                str(handoff_id)[:8],
                caller_tenant[:8],
                str(trace_id)[:8],
            )
            return None  # signals caller to raise 403

    # Index by memory_type — take the first of each type if duplicates exist
    by_type: dict[str, dict[str, Any]] = {}
    for row in rows:
        mt = row.get("memory_type", "")
        if mt in _HANDOFF_MEMORY_TYPES and mt not in by_type:
            by_type[mt] = row

    # Build voiceHandoffBrief: handoff_note first, then authority_context, then pending_intent
    parts: list[str] = []
    for mt in ("handoff_note", "authority_context", "pending_intent"):
        if mt in by_type:
            text = (by_type[mt].get("summary") or "").strip()
            if text:
                parts.append(text)

    brief_full = " | ".join(parts)
    # Clamp to _BRIEF_MAX_CHARS without splitting mid-word
    if len(brief_full) > _BRIEF_MAX_CHARS:
        brief_full = brief_full[: _BRIEF_MAX_CHARS].rsplit(" ", 1)[0] + " …"

    result: dict[str, Any] = {
        "voiceHandoffBrief": brief_full,
        "handoff_correlation_id": str(handoff_id),
    }

    if "handoff_note" in by_type:
        result["handoff_note_id"] = str(by_type["handoff_note"].get("memory_id", ""))
    if "authority_context" in by_type:
        result["handoff_authority_context_id"] = str(by_type["authority_context"].get("memory_id", ""))
    if "pending_intent" in by_type:
        result["handoff_pending_intent_id"] = str(by_type["pending_intent"].get("memory_id", ""))

    logger.info(
        "session_broker.anam_handoff: resolved handoff_id=%s types=%s brief_len=%d trace_id=%s",
        str(handoff_id)[:8],
        list(by_type.keys()),
        len(brief_full),
        str(trace_id)[:8],
    )
    return result


# ---------------------------------------------------------------------------
# POST /v1/session-broker/start
# ---------------------------------------------------------------------------


@router.post("/v1/session-broker/start", response_model=SessionStartResponse)
async def session_broker_start(
    body: SessionStartRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> SessionStartResponse:
    """Bootstrap a new agent session with brief context + allowed tools.

    Tenant isolation: scope headers must match body.scope (Law #6).
    Receipt: emitted on completion (Law #2).
    """
    trace_id = uuid.uuid4()
    correlation_id = str(uuid.uuid4())

    # Law #6: scope headers must match body scope
    if str(scope.tenant_id) != str(body.scope.tenant_id):
        _deny_receipt(
            correlation_id=correlation_id,
            suite_id=str(scope.suite_id),
            office_id=str(scope.office_id),
            actor_id=str(scope.actor_id) if scope.actor_id else "unknown",
            action_type="session_broker.start",
            reason_code="TENANT_ISOLATION_VIOLATION",
            details={"header_tenant": str(scope.tenant_id), "body_tenant": str(body.scope.tenant_id)},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "Scope header tenant_id does not match request body tenant_id",
                "correlation_id": correlation_id,
            },
        )

    session_id = uuid.uuid4()

    # Build dynamic_variables from brief cache — best-effort (brief may not exist yet)
    dynamic_variables: dict[str, Any] = dict(body.dynamic_variables_hint)
    try:
        materializer = BriefMaterializer()
        brief = await materializer.build_office_brief(
            office_id=body.scope.office_id,
            scope=body.scope,
        )
        dynamic_variables["office_brief_text"] = brief.brief_text or ""
        dynamic_variables["due_now_count"] = brief.due_now_count
        dynamic_variables["pending_approval_count"] = brief.pending_approval_count
    except Exception as exc:
        # Non-blocking — session starts even without brief
        logger.warning(
            "session_broker.start: brief fetch failed (non-fatal): %s trace_id=%s",
            type(exc).__name__,
            trace_id,
        )

    # Handoff resolution: anam_video runtime with handoff_id → fetch 3 memory objects
    # and build voiceHandoffBrief for Anam prompt interpolation (plan §7).
    # Non-anam runtimes skip this block entirely.
    if body.runtime_family == "anam_video" and body.handoff_id:
        _resolve_result = await _resolve_anam_handoff(
            handoff_id=body.handoff_id,
            scope=body.scope,
            header_scope=scope,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )
        if _resolve_result is None:
            # Cross-tenant attempt was detected — deny_receipt already emitted
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "TENANT_ISOLATION_VIOLATION",
                    "message": "handoff_id references memory objects outside caller's tenant scope",
                    "correlation_id": correlation_id,
                },
            )
        dynamic_variables.update(_resolve_result)

    # Allowed tools registry (simplified — full registry integration in Pass 6)
    allowed_tools = ["memory_search", "create_handoff_note", "save_session_summary", "get_thread_memory"]

    # Law #2 — receipt for session start
    receipt: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "trace_id": str(trace_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "actor_type": "system",
        "actor_id": str(scope.actor_id) if scope.actor_id else "session_broker",
        "action_type": "session_broker.start",
        "risk_tier": "green",
        "tool_used": "session_broker",
        "outcome": "success",
        "reason_code": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "receipt_type": "session_start",
        "receipt_hash": "",
        "redacted_inputs": {"agent_name": body.agent_name, "channel": body.channel},
        "redacted_outputs": {"session_id": str(session_id)},
    }
    store_receipts([receipt])

    logger.info(
        "session_broker.start: agent=%s session_id=%s trace_id=%s",
        body.agent_name,
        str(session_id)[:8],
        str(trace_id)[:8],
    )

    return SessionStartResponse(
        session_id=session_id,
        thread_id=body.thread_id,
        dynamic_variables=dynamic_variables,
        allowed_tools=allowed_tools,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /v1/memory-events
# ---------------------------------------------------------------------------


@router.post("/v1/memory-events", response_model=MemoryEventResponse)
async def ingest_memory_event(
    body: MemoryEventEnvelope,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> MemoryEventResponse:
    """Append a MemoryEventEnvelope to memory_event_inbox and kick async refinery.

    Tenant isolation enforced: envelope tenant_id must match scope header.
    Law #2: receipt emitted on success or failure.
    """
    trace_id = uuid.uuid4()
    correlation_id = str(body.correlation_id)

    # Law #6 check
    if str(scope.tenant_id) != str(body.tenant_id):
        _deny_receipt(
            correlation_id=correlation_id,
            suite_id=str(scope.suite_id),
            office_id=str(scope.office_id),
            actor_id=str(scope.actor_id) if scope.actor_id else "unknown",
            action_type="memory_events.ingest",
            reason_code="TENANT_ISOLATION_VIOLATION",
            details={"header_tenant": str(scope.tenant_id), "envelope_tenant": str(body.tenant_id)},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "Envelope tenant_id does not match request scope",
                "correlation_id": correlation_id,
            },
        )

    # Insert into memory_event_inbox
    event_id = uuid.uuid4()
    row: dict[str, Any] = {
        "event_id": str(event_id),
        "tenant_id": str(body.tenant_id),
        "suite_id": str(body.suite_id),
        "office_id": str(body.office_id),
        "actor_id": str(body.actor_id) if body.actor_id else None,
        "user_id": str(body.user_id) if body.user_id else None,
        "event_type": body.event_type,
        "source_surface": body.source_surface,
        "source_agent": body.source_agent,
        "runtime_family": body.runtime_family,
        "channel": body.channel,
        "trace_id": str(body.trace_id),
        "correlation_id": str(body.correlation_id),
        "source_record_id": body.source_record_id,
        "session_id": str(body.session_id) if body.session_id else None,
        "thread_id": str(body.thread_id) if body.thread_id else None,
        "entity_type": body.entity_type,
        "entity_id": str(body.entity_id) if body.entity_id else None,
        "payload": body.payload,
        "risk_tier": body.risk_tier,
        "needs_approval": body.needs_approval,
        "receipt_required": body.receipt_required,
        "event_at": body.event_at.isoformat(),
        "source_updated_at": body.source_updated_at.isoformat() if body.source_updated_at else None,
        "idempotency_key": body.idempotency_key,
        "status": "pending",
    }

    try:
        await supabase_insert("memory_event_inbox", [row])
    except SupabaseClientError as exc:
        # Law #2 — failure receipt
        _deny_receipt(
            correlation_id=correlation_id,
            suite_id=str(scope.suite_id),
            office_id=str(scope.office_id),
            actor_id=str(scope.actor_id) if scope.actor_id else "unknown",
            action_type="memory_events.ingest",
            reason_code="DB_INSERT_FAILED",
            details={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MEMORY_INBOX_INSERT_FAILED",
                "message": "Failed to persist memory event to inbox",
                "correlation_id": correlation_id,
            },
        )

    # Kick async refinery — best effort (if no Temporal client, call refinery directly)
    _kick_refinery_async(event_id=event_id)

    # Law #2 — success receipt
    receipt: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "trace_id": str(trace_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "actor_type": "system",
        "actor_id": str(scope.actor_id) if scope.actor_id else "memory_router",
        "action_type": "memory_events.ingest",
        "risk_tier": "green",
        "tool_used": "memory_spine_router",
        "outcome": "success",
        "reason_code": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "receipt_type": "event_ingested",
        "receipt_hash": "",
        "redacted_inputs": {
            "event_type": body.event_type,
            "idempotency_key": body.idempotency_key,
        },
        "redacted_outputs": {"event_id": str(event_id)},
    }
    store_receipts([receipt])

    logger.info(
        "memory_events.ingest: event_id=%s event_type=%s trace_id=%s",
        str(event_id)[:8],
        body.event_type,
        str(trace_id)[:8],
    )

    return MemoryEventResponse(
        event_id=event_id,
        status="pending",
        trace_id=trace_id,
    )


def _kick_refinery_async(*, event_id: UUID) -> None:
    """Fire-and-forget refinery invocation.

    In production this would enqueue a Temporal MemorySyncWorkflow signal.
    Currently: schedule as a background asyncio task (same pattern as task_queue.py).
    The orchestrator (Law #1) decides whether to use Temporal or direct execution.
    """
    import asyncio

    async def _run() -> None:
        try:
            refinery = TranscriptEventRefinery()
            await refinery.refine(event_id)
        except Exception as exc:
            # Refinery logs its own DLQ receipt — do not re-raise here
            logger.warning(
                "background refinery kick failed: event_id=%s error=%s",
                str(event_id)[:8],
                type(exc).__name__,
            )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_run())
    except RuntimeError:
        logger.warning("No running event loop — refinery kick deferred to Temporal")


# ---------------------------------------------------------------------------
# POST /v1/refinery/run
# ---------------------------------------------------------------------------


class RefineRequest(BaseModel):
    event_id: UUID


@router.post("/v1/refinery/run", response_model=RefineResult)
async def refinery_run(
    body: RefineRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> RefineResult:
    """Synchronously refine a single inbox event by event_id.

    Returns RefineResult with memory_ids + candidate_ids produced.
    """
    trace_id = uuid.uuid4()
    correlation_id = str(uuid.uuid4())

    try:
        refinery = TranscriptEventRefinery()
        result = await refinery.refine(body.event_id)
    except RefineryError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": str(exc),
                "correlation_id": correlation_id,
                "tenant_id": str(scope.tenant_id),
            },
        )
    except Exception as exc:
        logger.exception("refinery.run unexpected error: %s trace_id=%s", type(exc).__name__, trace_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "REFINERY_FAILED",
                "message": "Refinery encountered an unexpected error",
                "correlation_id": correlation_id,
            },
        )

    logger.info(
        "refinery.run: event_id=%s memory_ids=%d candidate_ids=%d trace_id=%s",
        str(body.event_id)[:8],
        len(result.memory_ids),
        len(result.candidate_ids),
        str(trace_id)[:8],
    )
    return result


# ---------------------------------------------------------------------------
# POST /v1/memory/search  (Pass 5 — wired to MemorySearchService)
# ---------------------------------------------------------------------------


@router.post("/v1/memory/search", response_model=MemorySearchResponse)
async def memory_search(
    body: MemorySearchRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> MemorySearchResponse:
    """Hybrid memory search (Pass 5).

    Wraps MemorySearchService.search(req) — the canonical §3.4 ranking
    pipeline. The route accepts the legacy spine input shape (q + memory_type
    aliases) and translates to MemorySearchRequest internally so existing
    callers do not need to migrate.

    Law #6: scope match validated at the route boundary AND inside the service
    AND inside the SQL RPC (three independent gates).
    """
    # Law #6: scope match (route boundary)
    if str(scope.tenant_id) != str(body.scope.tenant_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "Search scope tenant_id does not match request headers",
            },
        )

    # Translate legacy route shape -> canonical MemorySearchRequest
    canonical_req = SpineMemorySearchRequest(
        tenant_id=body.scope.tenant_id,
        suite_id=body.scope.suite_id,
        office_id=body.scope.office_id,
        query_text=body.q,
        entity_id=body.entity_id,
        thread_id=body.thread_id,
        memory_types=body.memory_type,  # type: ignore[arg-type]
        visibility_scope=body.visibility_scope or "office",  # type: ignore[arg-type]
        include_raw=body.include_raw,
        limit=body.limit,
    )

    try:
        result = await MemorySearchService().search(canonical_req, scope=body.scope)
    except MemoryServiceError as exc:
        if exc.code == "TENANT_ISOLATION_VIOLATION":
            raise HTTPException(
                status_code=403,
                detail={"code": exc.code, "message": str(exc)},
            )
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("memory.search unexpected error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "MEMORY_SEARCH_FAILED", "message": "Search failed"},
        )

    logger.info(
        "memory.search: q=%r tenant=%s items=%d scope=%s",
        body.q[:40] if body.q else "",
        str(scope.tenant_id)[:8],
        len(result.items),
        canonical_req.visibility_scope,
    )

    return MemorySearchResponse(
        results=result.items,
        total=result.total or len(result.items),
        note=None,
    )


# ---------------------------------------------------------------------------
# POST /v1/proactive-candidates/query
# ---------------------------------------------------------------------------


@router.post("/v1/proactive-candidates/query", response_model=list[ProactiveCandidateOut])
async def proactive_candidates_query(
    body: CandidateQuery,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> list[ProactiveCandidateOut]:
    """Query proactive candidates filtered by scope + optional agent/status/due filters."""
    # Law #6
    if str(scope.tenant_id) != str(body.tenant_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "Query tenant_id does not match request scope",
            },
        )

    try:
        engine = ProactiveCandidateEngine()
        results = await engine.query(body)
    except MemoryServiceError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("proactive_candidates.query error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "QUERY_FAILED", "message": "Candidate query failed"},
        )

    logger.info(
        "proactive_candidates.query: count=%d tenant=%s",
        len(results),
        str(scope.tenant_id)[:8],
    )
    return results


# ---------------------------------------------------------------------------
# POST /v1/approvals/request
# ---------------------------------------------------------------------------


@router.post("/v1/approvals/request", response_model=ApprovalsRequestResponse)
async def approvals_request(
    body: ApprovalsRequestBody,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> ApprovalsRequestResponse:
    """Create an approval binding and write an approval_links row.

    Uses existing approval_service.create_approval_binding for the approval record,
    then writes the approval_links spine row via supabase_insert.
    Law #2: receipt emitted by approval_service; link row is the memory linkage.
    """
    from aspire_orchestrator.services.approval_service import create_approval_binding

    # Law #6
    if str(scope.tenant_id) != str(body.scope.tenant_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "Approval scope tenant_id does not match request headers",
            },
        )

    correlation_id = str(uuid.uuid4())
    trace_id = uuid.uuid4()

    # Create the approval binding (existing service)
    binding = create_approval_binding(
        suite_id=str(body.scope.suite_id),
        office_id=str(body.scope.office_id),
        action_type=f"approvals.{body.requested_by_agent}",
        risk_tier="yellow",
        payload={
            "approval_id": body.approval_id,
            "reason": body.reason,
            "linked_memory_ids": [str(m) for m in body.linked_memory_ids],
        },
        correlation_id=correlation_id,
    )

    # Write approval_links row (spine linkage table)
    link_id = uuid.uuid4()
    link_row: dict[str, Any] = {
        "approval_link_id": str(link_id),
        "tenant_id": str(body.scope.tenant_id),
        "suite_id": str(body.scope.suite_id),
        "approval_id": body.approval_id,
        "linked_candidate_id": str(body.linked_candidate_id) if body.linked_candidate_id else None,
        "linked_memory_ids": [str(m) for m in body.linked_memory_ids],
        "linked_workflow_run_id": str(body.linked_workflow_run_id) if body.linked_workflow_run_id else None,
        "requested_by_agent": body.requested_by_agent,
        "approval_status": "pending",
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        await supabase_insert("approval_links", [link_row])
    except SupabaseClientError as exc:
        logger.error(
            "approvals.request: approval_links insert failed: %s correlation_id=%s",
            type(exc).__name__,
            correlation_id,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "APPROVAL_LINK_INSERT_FAILED",
                "message": "Failed to write approval_links row",
                "correlation_id": correlation_id,
            },
        )

    logger.info(
        "approvals.request: approval_id=%s link_id=%s trace_id=%s",
        body.approval_id[:16] if body.approval_id else "?",
        str(link_id)[:8],
        str(trace_id)[:8],
    )

    return ApprovalsRequestResponse(
        approval_link_id=link_id,
        approval_id=body.approval_id,
        status="pending",
    )


# ---------------------------------------------------------------------------
# POST /v1/receipts/write
# ---------------------------------------------------------------------------


@router.post("/v1/receipts/write", response_model=ReceiptsWriteResponse)
async def receipts_write(
    body: ReceiptsWriteRequest,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> ReceiptsWriteResponse:
    """Store receipts via existing receipt_store and write receipt_memory_links rows.

    Thin wrapper — all receipts MUST already have suite_id matching scope headers.
    """
    from aspire_orchestrator.services.receipt_store import store_receipts as _store

    # Tenant isolation: all receipts must carry matching suite_id
    mismatched = [
        r.get("id", "unknown")
        for r in body.receipts
        if r.get("suite_id") and r.get("suite_id") != str(scope.suite_id)
    ]
    if mismatched:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "One or more receipts have suite_id that does not match scope headers",
                "mismatched_receipt_ids": mismatched[:5],
            },
        )

    _store(body.receipts)
    receipt_ids = [r.get("id", str(uuid.uuid4())) for r in body.receipts]

    # Write receipt_memory_links
    links_written = 0
    if body.memory_links:
        link_rows: list[dict[str, Any]] = []
        for link in body.memory_links:
            link_rows.append({
                "receipt_id": link.get("receipt_id"),
                "memory_id": link.get("memory_id"),
                "linked_via": link.get("linked_via"),
                "tenant_id": str(scope.tenant_id),
                "suite_id": str(scope.suite_id),
            })
        try:
            await supabase_insert("receipt_memory_links", link_rows)
            links_written = len(link_rows)
        except SupabaseClientError as exc:
            logger.error(
                "receipts.write: receipt_memory_links insert failed: %s",
                type(exc).__name__,
            )
            # Non-fatal — receipts were already stored; log the link failure
            logger.warning(
                "receipts.write: %d link rows failed to persist (receipts are stored)",
                len(link_rows),
            )

    logger.info(
        "receipts.write: receipt_count=%d links_written=%d tenant=%s",
        len(receipt_ids),
        links_written,
        str(scope.tenant_id)[:8],
    )

    return ReceiptsWriteResponse(
        receipt_ids=receipt_ids,
        links_written=links_written,
    )


# ---------------------------------------------------------------------------
# GET /v1/briefs/office/{office_id}
# GET /v1/briefs/finance/{office_id}
# GET /v1/briefs/thread/{thread_id}
# ---------------------------------------------------------------------------


@router.get("/v1/briefs/office/{office_id}", response_model=OfficeBriefOut)
async def brief_office(
    office_id: UUID,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> OfficeBriefOut:
    """Return (or rebuild) the office brief for the given office_id."""
    # Law #6: office_id path param must match scope
    if str(office_id) != str(scope.office_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "office_id path parameter does not match X-Office-Id header",
            },
        )

    body_scope = ScopedIdentity(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        actor_id=scope.actor_id,
    )

    try:
        materializer = BriefMaterializer()
        return await materializer.build_office_brief(office_id=office_id, scope=body_scope)
    except MemoryServiceError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("briefs.office error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "BRIEF_BUILD_FAILED", "message": "Office brief build failed"},
        )


@router.get("/v1/briefs/finance/{office_id}", response_model=FinanceBriefOut)
async def brief_finance(
    office_id: UUID,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> FinanceBriefOut:
    """Return (or rebuild) the finance brief for the given office_id."""
    if str(office_id) != str(scope.office_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_ISOLATION_VIOLATION",
                "message": "office_id path parameter does not match X-Office-Id header",
            },
        )

    body_scope = ScopedIdentity(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        actor_id=scope.actor_id,
    )

    try:
        materializer = BriefMaterializer()
        return await materializer.build_finance_brief(office_id=office_id, scope=body_scope)
    except MemoryServiceError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("briefs.finance error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "BRIEF_BUILD_FAILED", "message": "Finance brief build failed"},
        )


@router.get("/v1/briefs/thread/{thread_id}", response_model=ThreadBriefOut)
async def brief_thread(
    thread_id: UUID,
    scope: ScopedIdentityFromHeaders = Depends(get_scope),
) -> ThreadBriefOut:
    """Return (or rebuild) the thread brief for the given thread_id."""
    body_scope = ScopedIdentity(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        actor_id=scope.actor_id,
    )

    try:
        materializer = BriefMaterializer()
        return await materializer.build_thread_brief(thread_id=thread_id, scope=body_scope)
    except MemoryServiceError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        )
    except Exception as exc:
        logger.exception("briefs.thread error: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"code": "BRIEF_BUILD_FAILED", "message": "Thread brief build failed"},
        )
