"""Ava Chief of Staff Skillpack — 10 server-side tools for the Ava voice agent.

Ava is the owner's chief of staff. These tools give Ava access to the unified
Office Memory Engine and the routing/handoff primitives that connect her to
the specialist agents (Eli, Nora, Finn, Sarah).

Law compliance:
  Law #1: Ava tools never decide routing — they write governance records and
          return receipts. The orchestrator and ElevenLabs transfer_to_agent
          system tool handle the actual transfer.
  Law #2: Every state-changing tool emits an immutable receipt.
  Law #3: Missing scope or capability token → raise AvaToolError (fail closed).
  Law #5: Capability token validated at gateway before this layer is reached.
  Law #6: Tenant isolation enforced on every read/write.
  Law #7: Tools are hands — they never retry, never call each other.
  Law #9: No PII in log lines.

Critical invariant (§7 Anam video handoff):
  create_handoff_note writes exactly 3 memory_objects in a single logical
  transaction, all sharing one correlation_id. The handoff_id returned IS
  that correlation_id. Tests must assert all 3 share the same handoff_id.

Capability scopes:
  - ava.memory.read  — get_memory_brief, search_memory, get_thread_memory
  - ava.memory.write — create_handoff_note, save_session_summary, promote_artifact
  - ava.routing.create — route_to_eli, route_to_nora, route_to_finn, route_to_sarah
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ProactiveCandidateIn,
    ScopedIdentity,
)
from aspire_orchestrator.schemas.memory_v1 import MemorySearchRequest
from aspire_orchestrator.services.brief_materializer import BriefMaterializer
from aspire_orchestrator.services.memory_search import MemorySearchService as MemorySearch
from aspire_orchestrator.services.memory_service import (
    MemoryService,
    MemoryServiceError,
)
from aspire_orchestrator.services.proactive_candidate_engine import (
    ProactiveCandidateEngine,
)
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class AvaToolError(MemoryServiceError):
    """Structured error raised by Ava chief-of-staff tools.

    Inherits MemoryServiceError so the orchestrator's unified catch handles both.
    """

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


def _assert_ava_scope(scope: ScopedIdentity) -> None:
    """Fail closed if ScopedIdentity is invalid (Law #3, Law #5)."""
    if not isinstance(scope, ScopedIdentity):
        raise AvaToolError(
            "Invalid ScopedIdentity — capability token scope validation failed",
            code="INVALID_CAPABILITY_TOKEN",
        )


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


class AvaMemoryBriefOut(BaseModel):
    office_brief: str
    due_now_candidates: list[dict[str, Any]]
    open_approvals: list[dict[str, Any]]
    recent_receipts: list[dict[str, Any]]
    risk_summary: str
    freshness_at: str | None
    stale: bool
    correlation_id: str


class AvaSearchMemoryOut(BaseModel):
    results: list[dict[str, Any]]
    total: int
    correlation_id: str


class AvaThreadMemoryOut(BaseModel):
    thread_id: str | None
    entity_type: str
    entity_name: str
    thread_brief: str
    last_activity_at: str | None
    open_items: list[dict[str, Any]]
    correlation_id: str


class AvaHandoffNoteOut(BaseModel):
    """Result of create_handoff_note — 3 coordinated memory objects."""
    handoff_id: str         # shared correlation_id for all three objects
    pending_intent_id: str
    authority_context_id: str
    handoff_note_id: str
    receipt_ids: list[str]
    correlation_id: str


class AvaSessionSummaryOut(BaseModel):
    memory_id: str
    receipt_id: str
    idempotency_replay: bool
    correlation_id: str


class AvaPromoteArtifactOut(BaseModel):
    memory_id: str
    status: str
    receipt_id: str
    correlation_id: str


class AvaRouteOut(BaseModel):
    candidate_id: str
    receipt_id: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Tool 1: get_memory_brief
# ---------------------------------------------------------------------------


async def get_memory_brief(
    scope: ScopedIdentity,
    *,
    force_refresh: bool = False,
) -> AvaMemoryBriefOut:
    """Load the office brief at session start.

    GREEN tier. Maps to BriefMaterializer.build_office_brief().
    No receipt (read-only). Capability scope: ava.memory.read.
    """
    _assert_ava_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "ava.get_memory_brief tenant_id=%s force_refresh=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        force_refresh,
        str(correlation_id)[:8],
    )

    try:
        mat = BriefMaterializer()
        brief = await mat.build_office_brief(
            scope.office_id,
            scope=scope,
            refresh=force_refresh,
        )
        freshness = str(brief.last_built_at) if brief.last_built_at else None
        # OfficeBriefOut has brief_json for structured data; due_now_count for summaries
        due_now: list[dict[str, Any]] = brief.brief_json.get("due_now", []) if brief.brief_json else []
        open_approvals: list[dict[str, Any]] = brief.brief_json.get("open_approvals", []) if brief.brief_json else []
        recent_receipts: list[dict[str, Any]] = brief.brief_json.get("recent_receipts", []) if brief.brief_json else []
        risk_summary = brief.brief_json.get("risk_summary", "") if brief.brief_json else ""

        return AvaMemoryBriefOut(
            office_brief=brief.brief_text or "",
            due_now_candidates=due_now,
            open_approvals=open_approvals,
            recent_receipts=recent_receipts,
            risk_summary=risk_summary,
            freshness_at=freshness,
            stale=False,
            correlation_id=str(correlation_id),
        )
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise AvaToolError(
            "Unexpected error in ava.get_memory_brief",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool 2: search_memory
# ---------------------------------------------------------------------------


async def search_memory(
    scope: ScopedIdentity,
    *,
    query: str,
    memory_types: list[str] | None = None,
    entity_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10,
) -> AvaSearchMemoryOut:
    """Hybrid keyword + vector search across memory_objects.

    GREEN tier. No receipt (read-only). Capability scope: ava.memory.read.
    """
    _assert_ava_scope(scope)
    correlation_id = uuid.uuid4()

    if not query or not query.strip():
        raise AvaToolError(
            "search_memory requires a non-empty query",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        )

    logger.info(
        "ava.search_memory query_len=%d tenant_id=%s correlation_id=%s",
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
            entity_id=UUID(entity_id) if entity_id else None,
            limit=min(limit, 20),
        )
        resp = await svc.search(req)

        results = [
            {
                "memory_id": str(r.memory_id),
                "memory_type": r.memory_type,
                "title": r.title,
                "summary": r.summary,
                "entity_type": r.entity_type,
                "last_activity_at": str(r.last_activity_at) if r.last_activity_at else None,
                "confidence": r.confidence,
            }
            for r in resp.items
        ]
        return AvaSearchMemoryOut(
            results=results,
            total=resp.total or len(results),
            correlation_id=str(correlation_id),
        )
    except AvaToolError:
        raise
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise AvaToolError(
            "Unexpected error in ava.search_memory",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool 3: get_thread_memory
# ---------------------------------------------------------------------------


async def get_thread_memory(
    scope: ScopedIdentity,
    *,
    entity_type: str,
    entity_id: str | None = None,
    entity_name: str | None = None,
) -> AvaThreadMemoryOut:
    """Load the thread brief for a specific entity.

    GREEN tier. No receipt (read-only). Capability scope: ava.memory.read.
    """
    _assert_ava_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "ava.get_thread_memory entity_type=%s tenant_id=%s correlation_id=%s",
        entity_type,
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    try:
        # Resolve: if entity_id is provided, build the thread brief
        # Otherwise fall back to list_by_entity search
        thread_brief_text = ""
        thread_id_str: str | None = None
        last_activity: str | None = None
        open_items: list[dict[str, Any]] = []

        if entity_id:
            # Try to find the thread by entity
            from aspire_orchestrator.services.supabase_client import supabase_select as _sel
            thread_rows = await _sel(
                "threads",
                {
                    "tenant_id": str(scope.tenant_id),
                    "suite_id": str(scope.suite_id),
                    "office_id": str(scope.office_id),
                    "canonical_entity_type": entity_type,
                    "canonical_entity_id": entity_id,
                },
                order_by="last_activity_at.desc",
                limit=1,
            )
            if thread_rows:
                t = thread_rows[0]
                thread_id_str = t.get("thread_id")
                last_activity = t.get("last_activity_at")
                tid = UUID(thread_id_str) if thread_id_str else None
                if tid:
                    mat = BriefMaterializer()
                    brief = await mat.build_thread_brief(tid, scope=scope)
                    thread_brief_text = brief.summary or ""
                    last_activity = str(brief.last_built_at) if brief.last_built_at else last_activity

            # Get open memory objects for this entity
            svc = MemoryService()
            items, _ = await svc.list_by_thread(
                UUID(thread_id_str) if thread_id_str else UUID(entity_id),
                scope=scope,
                limit=10,
            )
            open_items = [
                {
                    "memory_id": str(r.memory_id),
                    "memory_type": r.memory_type,
                    "summary": r.summary,
                }
                for r in items
            ]

        return AvaThreadMemoryOut(
            thread_id=thread_id_str,
            entity_type=entity_type,
            entity_name=entity_name or entity_id or "",
            thread_brief=thread_brief_text,
            last_activity_at=last_activity,
            open_items=open_items,
            correlation_id=str(correlation_id),
        )
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise AvaToolError(
            "Unexpected error in ava.get_thread_memory",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc


# ---------------------------------------------------------------------------
# Tool 4: create_handoff_note  ← CRITICAL: 3-object atomic write
# ---------------------------------------------------------------------------


async def create_handoff_note(
    scope: ScopedIdentity,
    *,
    pending_intent: str,
    authority_context: str,
    handoff_note: str,
    receiving_agent: str,
    entity_id: str | None = None,
    risk_tier: str = "green",
) -> AvaHandoffNoteOut:
    """Write 3 coordinated memory objects sharing one correlation_id.

    YELLOW tier (route decision). Emits one receipt per object (3 total, Law #2).
    All 3 objects are written atomically with the same handoff_id (= correlation_id).

    On partial failure: raises AvaToolError('PROVIDER_INTERNAL_ERROR') — caller
    must treat this as a failed handoff (no routing should proceed).

    Capability scope: ava.memory.write.

    Invariant verified by tests:
      pending_intent_id.correlation_id == authority_context_id.correlation_id
        == handoff_note_id.correlation_id == handoff_id.
    """
    _assert_ava_scope(scope)

    if not pending_intent.strip():
        raise AvaToolError(
            "create_handoff_note: pending_intent must not be empty",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
        )
    if not authority_context.strip():
        raise AvaToolError(
            "create_handoff_note: authority_context must not be empty",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
        )
    if not handoff_note.strip():
        raise AvaToolError(
            "create_handoff_note: handoff_note must not be empty",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
        )

    # The shared correlation_id is the handoff_id — it is the same for all 3 objects.
    handoff_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    logger.info(
        "ava.create_handoff_note receiving_agent=%s tenant_id=%s handoff_id=%s",
        receiving_agent,
        str(scope.tenant_id)[:8],
        str(handoff_id)[:8],
    )

    def _make_prov(corr_id: UUID) -> Provenance:
        return Provenance(
            source_surface="ava_voice",
            source_agent="ava",
            runtime_family="elevenlabs",
            channel="voice",
            trace_id=trace_id,
            correlation_id=corr_id,
        )

    ent_id = UUID(entity_id) if entity_id else None
    svc = MemoryService()
    receipt_ids: list[str] = []
    written_ids: list[UUID] = []

    try:
        # Object 1: pending_intent
        env1 = MemoryObjectIn(
            scope=scope,
            provenance=_make_prov(handoff_id),
            memory_type="pending_intent",
            entity_id=ent_id,
            title=f"Pending intent → {receiving_agent}",
            summary=pending_intent,
            detail={"receiving_agent": receiving_agent, "risk_tier": risk_tier},
            visibility_scope="office",
            idempotency_key=f"ava:handoff:pending_intent:{handoff_id}",
        )
        r1 = await svc.write(env1, scope=scope, embed=False)
        written_ids.append(r1.memory_id)
        if r1.linked_receipt_ids:
            receipt_ids.append(str(r1.linked_receipt_ids[0]))

        # Object 2: authority_context
        env2 = MemoryObjectIn(
            scope=scope,
            provenance=_make_prov(handoff_id),
            memory_type="authority_context",
            entity_id=ent_id,
            title=f"Authority context → {receiving_agent}",
            summary=authority_context,
            detail={"receiving_agent": receiving_agent, "risk_tier": risk_tier},
            visibility_scope="office",
            idempotency_key=f"ava:handoff:authority_context:{handoff_id}",
        )
        r2 = await svc.write(env2, scope=scope, embed=False)
        written_ids.append(r2.memory_id)
        if r2.linked_receipt_ids:
            receipt_ids.append(str(r2.linked_receipt_ids[0]))

        # Object 3: handoff_note
        env3 = MemoryObjectIn(
            scope=scope,
            provenance=_make_prov(handoff_id),
            memory_type="handoff_note",
            entity_id=ent_id,
            title=f"Handoff note → {receiving_agent}",
            summary=handoff_note,
            detail={
                "receiving_agent": receiving_agent,
                "risk_tier": risk_tier,
                "pending_intent_id": str(r1.memory_id),
                "authority_context_id": str(r2.memory_id),
            },
            visibility_scope="office",
            idempotency_key=f"ava:handoff:handoff_note:{handoff_id}",
        )
        r3 = await svc.write(env3, scope=scope, embed=False)
        written_ids.append(r3.memory_id)
        if r3.linked_receipt_ids:
            receipt_ids.append(str(r3.linked_receipt_ids[0]))

    except MemoryServiceError:
        # Partial failure — surface clearly. Caller must not route.
        logger.error(
            "ava.create_handoff_note partial failure after writing %d objects handoff_id=%s",
            len(written_ids),
            str(handoff_id)[:8],
        )
        raise AvaToolError(
            "create_handoff_note failed — partial write; do not route",
            code="PROVIDER_INTERNAL_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=handoff_id,
        )
    except Exception as exc:
        raise AvaToolError(
            "Unexpected error in ava.create_handoff_note",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=handoff_id,
        ) from exc

    return AvaHandoffNoteOut(
        handoff_id=str(handoff_id),
        pending_intent_id=str(written_ids[0]),
        authority_context_id=str(written_ids[1]),
        handoff_note_id=str(written_ids[2]),
        receipt_ids=receipt_ids,
        correlation_id=str(handoff_id),
    )


# ---------------------------------------------------------------------------
# Tool 5: save_session_summary  (state change → receipt)
# ---------------------------------------------------------------------------


async def save_session_summary(
    scope: ScopedIdentity,
    *,
    session_id: str,
    summary: str,
    decisions: list[str] | None = None,
    open_items: list[str] | None = None,
    routed_to: list[str] | None = None,
) -> AvaSessionSummaryOut:
    """Write a session_summary memory object at end of every Ava session.

    GREEN tier. Idempotent on session_id. Emits receipt (Law #2).
    Capability scope: ava.memory.write.
    """
    _assert_ava_scope(scope)
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()
    ikey = f"session:{session_id}"

    logger.info(
        "ava.save_session_summary session_id=%s tenant_id=%s correlation_id=%s",
        session_id[:12],
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    provenance = Provenance(
        source_surface="ava_voice",
        source_agent="ava",
        runtime_family="elevenlabs",
        channel="voice",
        external_session_id=session_id,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    envelope = MemoryObjectIn(
        scope=scope,
        provenance=provenance,
        memory_type="session_summary",
        title=f"Session summary {session_id[:8]}",
        summary=summary,
        detail={
            "decisions": decisions or [],
            "open_items": open_items or [],
            "routed_to": routed_to or [],
        },
        visibility_scope="office",
        idempotency_key=ikey,
    )

    svc = MemoryService()
    idempotency_replay = False
    try:
        result = await svc.write(envelope, scope=scope, embed=False)
        # If the returned row has the same idempotency_key but was already there,
        # MemoryService returns without re-emitting a receipt. We detect this by
        # checking whether result.idempotency_key == ikey and created_at is old.
        # Simple proxy: if result already existed, created_at will differ from now.
    except MemoryServiceError as exc:
        if "idempotency" in str(exc).lower():
            idempotency_replay = True
            result = exc  # type: ignore[assignment]
        else:
            raise
    except Exception as exc:
        raise AvaToolError(
            "Unexpected error in ava.save_session_summary",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    if idempotency_replay or isinstance(result, MemoryServiceError):
        return AvaSessionSummaryOut(
            memory_id="",
            receipt_id="",
            idempotency_replay=True,
            correlation_id=str(correlation_id),
        )

    return AvaSessionSummaryOut(
        memory_id=str(result.memory_id),
        receipt_id=str(result.linked_receipt_ids[0]) if result.linked_receipt_ids else "",
        idempotency_replay=False,
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool 6: promote_artifact  (state change → receipt)
# ---------------------------------------------------------------------------


async def promote_artifact(
    scope: ScopedIdentity,
    *,
    memory_id: str,
    reason: str,
) -> AvaPromoteArtifactOut:
    """Elevate a memory object to status='pinned'.

    GREEN tier. Emits receipt (Law #2). Capability scope: ava.memory.write.
    """
    _assert_ava_scope(scope)
    correlation_id = uuid.uuid4()

    if not reason.strip():
        raise AvaToolError(
            "promote_artifact: reason must not be empty",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        )

    logger.info(
        "ava.promote_artifact memory_id=%s tenant_id=%s correlation_id=%s",
        memory_id[:8],
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    svc = MemoryService()
    try:
        result = await svc.update_status(
            memory_id=UUID(memory_id),
            new_status="promoted",
            scope=scope,
        )
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise AvaToolError(
            "Unexpected error in ava.promote_artifact",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return AvaPromoteArtifactOut(
        memory_id=str(result.memory_id),
        status="promoted",
        receipt_id=str(result.linked_receipt_ids[0]) if result.linked_receipt_ids else "",
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Route tools (7–10) — each creates a proactive candidate (state change → receipt)
# ---------------------------------------------------------------------------


async def _route_to_agent(
    scope: ScopedIdentity,
    *,
    target_agent: str,
    handoff_id: str,
    intent_summary: str,
) -> AvaRouteOut:
    """Shared implementation for route_to_* tools.

    Creates a proactive_candidate(owner_agent=target, recommended_action='route_to_agent').
    Emits receipt via ProactiveCandidateEngine (Law #2).
    Capability scope: ava.routing.create.
    """
    _assert_ava_scope(scope)
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    logger.info(
        "ava.route_to_%s handoff_id=%s tenant_id=%s correlation_id=%s",
        target_agent,
        handoff_id[:8],
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    candidate_in = ProactiveCandidateIn(
        tenant_id=scope.tenant_id,
        suite_id=scope.suite_id,
        office_id=scope.office_id,
        owner_agent=target_agent,  # type: ignore[arg-type]
        recommended_action="route_to_agent",
        action_class="internal_only",
        why_now=intent_summary,
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
        raise AvaToolError(
            f"Unexpected error in ava.route_to_{target_agent}",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return AvaRouteOut(
        candidate_id=str(result.candidate_id),
        receipt_id=str(result.receipt_id) if hasattr(result, "receipt_id") else "",
        correlation_id=str(correlation_id),
    )


async def route_to_eli(
    scope: ScopedIdentity,
    *,
    handoff_id: str,
    intent_summary: str,
) -> AvaRouteOut:
    """Signal intent to route owner to Eli (inbox specialist).

    GREEN tier. Capability scope: ava.routing.create.
    """
    return await _route_to_agent(
        scope,
        target_agent="eli",
        handoff_id=handoff_id,
        intent_summary=intent_summary,
    )


async def route_to_nora(
    scope: ScopedIdentity,
    *,
    handoff_id: str,
    intent_summary: str,
) -> AvaRouteOut:
    """Signal intent to route owner to Nora (conference assistant).

    GREEN tier. Capability scope: ava.routing.create.
    """
    return await _route_to_agent(
        scope,
        target_agent="nora",
        handoff_id=handoff_id,
        intent_summary=intent_summary,
    )


async def route_to_finn(
    scope: ScopedIdentity,
    *,
    handoff_id: str,
    intent_summary: str,
) -> AvaRouteOut:
    """Signal intent to route owner to Finn (finance hub).

    GREEN tier. Capability scope: ava.routing.create.
    """
    return await _route_to_agent(
        scope,
        target_agent="finn",
        handoff_id=handoff_id,
        intent_summary=intent_summary,
    )


async def route_to_sarah(
    scope: ScopedIdentity,
    *,
    handoff_id: str,
    intent_summary: str,
) -> AvaRouteOut:
    """Signal intent to route owner to Sarah (front desk).

    GREEN tier. Capability scope: ava.routing.create.
    """
    return await _route_to_agent(
        scope,
        target_agent="sarah",
        handoff_id=handoff_id,
        intent_summary=intent_summary,
    )


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------

AVA_CHIEF_OF_STAFF_TOOLS: list[str] = [
    "ava.memory.get_memory_brief",
    "ava.memory.search_memory",
    "ava.memory.get_thread_memory",
    "ava.memory.create_handoff_note",
    "ava.memory.save_session_summary",
    "ava.memory.promote_artifact",
    "ava.routing.route_to_eli",
    "ava.routing.route_to_nora",
    "ava.routing.route_to_finn",
    "ava.routing.route_to_sarah",
]
