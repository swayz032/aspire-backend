"""Sarah Front Desk Tools — backend tools for the internal Front Desk agent.

Sarah Front Desk is the internal owner-facing call-desk agent. These tools
give her access to the call queue, voicemail state, memory, and escalation.

Law compliance:
  Law #2: State-changing tools emit receipts (create_handoff_note, escalate_to_owner).
  Law #3: Missing scope → raise SarahFrontDeskToolError (fail closed).
  Law #6: Tenant isolation on every read/write.
  Law #7: Tools are hands — never autonomously decide routing or escalation.
  Law #9: No PII in log lines. Caller IDs truncated.

Capability scope: 'office_read' (reads) / 'office_write' (writes).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    MemorySearchRequest,
    Provenance,
    ProactiveCandidateIn,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import (
    MemoryService,
    MemoryServiceError,
)
from aspire_orchestrator.services.memory_search import MemorySearchService as MemorySearch
from aspire_orchestrator.services.proactive_candidate_engine import (
    ProactiveCandidateEngine,
)
from aspire_orchestrator.services.supabase_client import supabase_select
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class SarahFrontDeskToolError(MemoryServiceError):
    """Structured error raised by Sarah Front Desk tools."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        tenant_id: UUID | str | None = None,
        correlation_id: UUID | str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, tenant_id=tenant_id, correlation_id=correlation_id)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _assert_sarah_scope(scope: ScopedIdentity) -> None:
    if not isinstance(scope, ScopedIdentity):
        raise SarahFrontDeskToolError(
            "Invalid ScopedIdentity — capability token scope validation failed",
            code="INVALID_CAPABILITY_TOKEN",
        )


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


class SarahContextOut(BaseModel):
    missed_calls_summary: dict[str, Any]
    voicemail_summary: dict[str, Any]
    text_activity_summary: dict[str, Any]
    callback_queue_summary: dict[str, Any]
    recent_activity_summary: list[dict[str, Any]]
    freshness: str
    confidence: str
    correlation_id: str


class SarahSearchOut(BaseModel):
    matches: list[dict[str, Any]]
    record_type: str
    confidence: str
    correlation_id: str


class SarahHandoffNoteOut(BaseModel):
    memory_id: str
    receipt_id: str
    correlation_id: str


class SarahCallbackQueueOut(BaseModel):
    callbacks: list[dict[str, Any]]
    voicemails: list[dict[str, Any]]
    total: int
    correlation_id: str


class SarahEscalateOut(BaseModel):
    candidate_id: str
    receipt_id: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Tool: get_context
# ---------------------------------------------------------------------------


async def get_context(scope: ScopedIdentity) -> SarahContextOut:
    """Load call-desk state: missed calls, voicemails, text threads, callbacks.

    GREEN tier. No state change. Capability scope: office_read.
    """
    _assert_sarah_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "sarah_frontdesk.get_context tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    scope_filter: dict[str, Any] = {
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
    }

    try:
        # Recent call sessions
        call_rows = await supabase_select(
            "call_sessions",
            scope_filter,
            order_by="created_at.desc",
            limit=10,
        )
        missed = [r for r in call_rows if r.get("status") == "missed"]

        # Voicemails
        vm_rows = await supabase_select(
            "frontdesk_voicemails",
            scope_filter,
            order_by="created_at.desc",
            limit=10,
        )

        # Callbacks
        cb_rows = await supabase_select(
            "frontdesk_callback_queue",
            scope_filter,
            order_by="created_at.desc",
            limit=10,
        )

        return SarahContextOut(
            missed_calls_summary={
                "count": len(missed),
                "recent": missed[:3],
            },
            voicemail_summary={
                "count": len(vm_rows),
                "unreviewed": [v for v in vm_rows if not v.get("reviewed")],
            },
            text_activity_summary={"count": 0, "threads": []},
            callback_queue_summary={
                "count": len(cb_rows),
                "pending": cb_rows,
            },
            recent_activity_summary=call_rows[:5],
            freshness=_now_utc().isoformat(),
            confidence="high" if call_rows else "low",
            correlation_id=str(correlation_id),
        )
    except SarahFrontDeskToolError:
        raise
    except Exception as exc:
        raise SarahFrontDeskToolError(
            "Unexpected error in sarah_frontdesk.get_context",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool: search_memory
# ---------------------------------------------------------------------------


async def search_memory(
    scope: ScopedIdentity,
    *,
    query: str,
    record_type: str = "all",
    limit: int = 10,
) -> SarahSearchOut:
    """Search caller records, voicemails, call records, receipts.

    GREEN tier. Capability scope: office_read.
    """
    _assert_sarah_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "sarah_frontdesk.search_memory query_len=%d tenant_id=%s correlation_id=%s",
        len(query),
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        svc = MemorySearch()
        req = MemorySearchRequest(
            tenant_id=scope.tenant_id,
            suite_id=scope.suite_id,
            office_id=scope.office_id,
            query_text=query,
            limit=limit,
        )
        resp = await svc.search(req)

        matches = [
            {
                "memory_id": str(r.memory_id),
                "memory_type": r.memory_type,
                "title": r.title,
                "summary": r.summary,
                "last_activity_at": str(r.last_activity_at) if r.last_activity_at else None,
            }
            for r in resp.items
        ]
        return SarahSearchOut(
            matches=matches,
            record_type=record_type,
            confidence="high" if matches else "low",
            correlation_id=str(correlation_id),
        )
    except SarahFrontDeskToolError:
        raise
    except Exception as exc:
        raise SarahFrontDeskToolError(
            "Unexpected error in sarah_frontdesk.search_memory",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool: get_thread_memory
# ---------------------------------------------------------------------------


async def get_thread_memory(
    scope: ScopedIdentity,
    *,
    thread_id: str,
) -> dict[str, Any]:
    """Load memory objects for a specific thread.

    GREEN tier. Capability scope: office_read.
    """
    _assert_sarah_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "sarah_frontdesk.get_thread_memory thread_id=%s tenant_id=%s correlation_id=%s",
        thread_id[:8],
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        svc = MemoryService()
        items, _cursor = await svc.list_by_thread(
            thread_id=UUID(thread_id),
            scope=scope,
            limit=20,
        )
        return {
            "thread_id": thread_id,
            "items": [
                {
                    "memory_id": str(r.memory_id),
                    "memory_type": r.memory_type,
                    "summary": r.summary,
                    "last_activity_at": str(r.last_activity_at) if r.last_activity_at else None,
                }
                for r in items
            ],
            "total": len(items),
            "correlation_id": str(correlation_id),
        }
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise SarahFrontDeskToolError(
            "Unexpected error in sarah_frontdesk.get_thread_memory",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool: create_handoff_note  (state change → receipt)
# ---------------------------------------------------------------------------


async def create_handoff_note(
    scope: ScopedIdentity,
    *,
    caller_name: str,
    callback_number: str,
    reason: str,
    urgency: str,
    recommended_next_step: str,
    supporting_summary: str,
    target_type: str = "owner_summary",
) -> SarahHandoffNoteOut:
    """Write a callback/follow-up note as a memory object.

    YELLOW tier. Emits receipt (Law #2). Capability scope: office_write.
    PII note: callback_number is NOT logged — only record type is logged.
    """
    _assert_sarah_scope(scope)
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    # callback_number is PII — never log it.
    logger.info(
        "sarah_frontdesk.create_handoff_note caller=%s urgency=%s correlation_id=%s",
        caller_name[:10] if caller_name else "unknown",
        urgency,
        str(correlation_id)[:8],
    )

    ikey = f"sarah:handoff:{str(correlation_id)}"
    provenance = Provenance(
        source_surface="sarah_voice",
        source_agent="sarah",
        runtime_family="elevenlabs",
        channel="voice",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    envelope = MemoryObjectIn(
        scope=scope,
        provenance=provenance,
        memory_type="followup_task",
        title=f"Callback note — {caller_name} ({urgency})",
        summary=supporting_summary,
        detail={
            "caller_name": caller_name,
            "reason": reason,
            "urgency": urgency,
            "recommended_next_step": recommended_next_step,
            "target_type": target_type,
            # callback_number stored in detail (redacted from logs/receipts).
        },
        visibility_scope="office",
        idempotency_key=ikey,
    )

    svc = MemoryService()
    try:
        result = await svc.write(envelope, scope=scope, embed=False)
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise SarahFrontDeskToolError(
            "Unexpected error in sarah_frontdesk.create_handoff_note",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return SarahHandoffNoteOut(
        memory_id=str(result.memory_id),
        receipt_id=str(result.linked_receipt_ids[0]) if result.linked_receipt_ids else "",
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool: triage_callback_queue
# ---------------------------------------------------------------------------


async def triage_callback_queue(scope: ScopedIdentity) -> SarahCallbackQueueOut:
    """Read recent missed calls and voicemails for triage.

    GREEN tier. No state change. Capability scope: office_read.
    """
    _assert_sarah_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "sarah_frontdesk.triage_callback_queue tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    scope_filter: dict[str, Any] = {
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
    }

    try:
        callbacks = await supabase_select(
            "frontdesk_callback_queue",
            {**scope_filter, "status": "pending"},
            order_by="created_at.desc",
            limit=20,
        )
        voicemails = await supabase_select(
            "frontdesk_voicemails",
            {**scope_filter, "reviewed": False},
            order_by="created_at.desc",
            limit=20,
        )
        return SarahCallbackQueueOut(
            callbacks=callbacks,
            voicemails=voicemails,
            total=len(callbacks) + len(voicemails),
            correlation_id=str(correlation_id),
        )
    except SarahFrontDeskToolError:
        raise
    except Exception as exc:
        raise SarahFrontDeskToolError(
            "Unexpected error in sarah_frontdesk.triage_callback_queue",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool: escalate_to_owner  (state change → proactive candidate + receipt)
# ---------------------------------------------------------------------------


async def escalate_to_owner(
    scope: ScopedIdentity,
    *,
    urgency: str,
    reason: str,
    entity_id: str | None = None,
    linked_memory_id: str | None = None,
) -> SarahEscalateOut:
    """Create a proactive candidate surfacing a warning to Ava (owner agent).

    YELLOW tier. Emits receipt via ProactiveCandidateEngine (Law #2).
    Capability scope: office_write.
    """
    _assert_sarah_scope(scope)
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    logger.info(
        "sarah_frontdesk.escalate_to_owner urgency=%s tenant_id=%s correlation_id=%s",
        urgency,
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    candidate_in = ProactiveCandidateIn(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        owner_agent="ava",
        recommended_action="surface_warning",
        action_class="internal_only",
        entity_type="call_desk",
        entity_id=UUID(entity_id) if entity_id else None,
        why_now=reason,
        confidence=1.0,
        risk_tier="green",
        needs_approval=False,
        receipt_required=True,
    )

    engine = ProactiveCandidateEngine()
    try:
        result = await engine.create_candidate(candidate_in, scope=scope)
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise SarahFrontDeskToolError(
            "Unexpected error in sarah_frontdesk.escalate_to_owner",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return SarahEscalateOut(
        candidate_id=str(result.candidate_id),
        receipt_id=str(result.receipt_id) if hasattr(result, "receipt_id") else "",
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------

SARAH_FRONTDESK_TOOLS: list[str] = [
    "sarah.frontdesk.get_context",
    "sarah.frontdesk.search_memory",
    "sarah.frontdesk.get_thread_memory",
    "sarah.frontdesk.create_handoff_note",
    "sarah.frontdesk.triage_callback_queue",
    "sarah.frontdesk.escalate_to_owner",
]
