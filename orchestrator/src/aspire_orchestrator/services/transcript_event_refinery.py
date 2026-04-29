"""Transcript Event Refinery — convert inbox events into durable memory.

Reads a row from public.memory_event_inbox, routes by event_type, and produces
N memory_objects + 0..M proactive_candidates. On success the inbox row is
marked 'processed'; on exception it is dead-lettered and an incident is
reported via the receipt_store -> incident_writer pipeline (Law #2).

V1 routing:
  voice_session_ended | voice_transcript_chunk -> _refine_voice_transcript
                                                  (1 session_summary +
                                                   0..1 pending_intent)
  email_thread_updated                          -> _refine_email_thread
                                                  (1 thread_summary)
  meeting_recap_ready                           -> _refine_meeting_recap
                                                  (1 session_summary +
                                                   N followup_task)
  finance_state_change                          -> _refine_finance_state_change
                                                  (1 decision_fact +
                                                   0..1 risk_flag if RED)
  provider_webhook_received                     -> _refine_provider_webhook
                                                  (1 timeline_event)

Each refinement step:
  1. Calls EntityThreadResolver.resolve(envelope) for the canonical thread.
  2. Builds 1..N MemoryObjectIn payloads and writes them via MemoryService.
     Each output has idempotency_key = f"refine:{event_id}:{output_index}"
     so re-runs of a dead-lettered event don't duplicate memory.
  3. Optionally builds ProactiveCandidateIn payloads and creates them via
     ProactiveCandidateEngine.create_candidate (also idempotent via the
     active-window dedup index).

Idempotency:
  At the inbox layer (event_id), the caller (Temporal activity) checks
  status before invoking refine(). At the write layer, idempotency_key on
  memory_objects and the active-window dedup on proactive_candidates ensure
  duplicate writes degrade to a no-op.

Tenant isolation (Law #6):
  All writes use the envelope's scope, validated by MemoryService and
  ProactiveCandidateEngine. We never attempt cross-tenant writes.

Law compliance:
  Law #1 — refinery proposes; orchestrator disposes (we only write memory +
           candidates; the orchestrator decides what to do with them).
  Law #2 — every memory write emits a receipt via MemoryService; every
           candidate emits a receipt via ProactiveCandidateEngine; the
           dead-letter path causes an incident receipt cascade.
  Law #3 — on any exception, we mark the inbox row 'dead_letter' and
           re-raise; we never silently drop events.
  Law #6 — scope validation is delegated to MemoryService and the engine;
           we use only the envelope's tenant/suite/office.
  Law #9 — payload contents are never logged; only IDs and labels appear
           in log lines and receipts.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryEventEnvelope,
    MemoryObjectIn,
    MemoryType,
    ProactiveCandidateIn,
    Provenance,
    RecommendedAction,
    RefineResult,
    RiskTier,
    ScopedIdentity,
    SourceAgent,
    SourceSurface,
    ThreadOut,
)
from aspire_orchestrator.services.entity_thread_resolver import EntityThreadResolver
from aspire_orchestrator.services.memory_service import (
    MemoryService,
    MemoryServiceError,
)
from aspire_orchestrator.services.proactive_candidate_engine import (
    ProactiveCandidateEngine,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)


_VOICE_EVENT_TYPES: frozenset[str] = frozenset(
    {"voice_session_ended", "voice_transcript_chunk"}
)
_EMAIL_EVENT_TYPES: frozenset[str] = frozenset({"email_thread_updated"})
_MEETING_EVENT_TYPES: frozenset[str] = frozenset({"meeting_recap_ready"})
_FINANCE_EVENT_TYPES: frozenset[str] = frozenset({"finance_state_change"})
_WEBHOOK_EVENT_TYPES: frozenset[str] = frozenset({"provider_webhook_received"})


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _coerce_uuid(value: Any) -> UUID | None:
    """Best-effort UUID coercion. Returns None if value is None or invalid."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


class RefineryError(MemoryServiceError):
    """Refinery-specific error subclass for clarity in tracebacks.

    Inherits from MemoryServiceError so callers that catch
    MemoryServiceError still handle refinery failures (Law #3 fail-closed).
    """


class TranscriptEventRefinery:
    """Route inbox events into durable memory + proactive candidates.

    Stateless. Composes MemoryService, EntityThreadResolver, and
    ProactiveCandidateEngine — does not own DB connections directly except
    for the inbox status lifecycle (mark processing -> processed |
    dead_letter).
    """

    def __init__(
        self,
        memory_service: MemoryService,
        thread_resolver: EntityThreadResolver,
        candidate_engine: ProactiveCandidateEngine,
    ) -> None:
        self._memory = memory_service
        self._threads = thread_resolver
        self._candidates = candidate_engine

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def refine(self, event_id: UUID) -> RefineResult:
        """Refine a single inbox event into durable memory.

        Phases:
          1. Load + lock the inbox row (status='processing', attempts += 1).
          2. Resolve the canonical thread.
          3. Route by event_type to a _refine_* method.
          4. Mark inbox row 'processed' on success; 'dead_letter' on exception
             (and re-raise the exception after reporting the incident).
        """
        envelope_row = await self._claim_inbox_row(event_id)
        envelope = self._envelope_from_row(envelope_row)

        # Resolve canonical thread up front. If this fails, dead-letter the event.
        try:
            thread = await self._threads.resolve(envelope)
        except Exception as exc:
            await self._dead_letter(event_id, envelope, error=exc)
            raise

        # Route by event_type
        try:
            if envelope.event_type in _VOICE_EVENT_TYPES:
                result = await self._refine_voice_transcript(
                    event_id, envelope, thread
                )
            elif envelope.event_type in _EMAIL_EVENT_TYPES:
                result = await self._refine_email_thread(
                    event_id, envelope, thread
                )
            elif envelope.event_type in _MEETING_EVENT_TYPES:
                result = await self._refine_meeting_recap(
                    event_id, envelope, thread
                )
            elif envelope.event_type in _FINANCE_EVENT_TYPES:
                result = await self._refine_finance_state_change(
                    event_id, envelope, thread
                )
            elif envelope.event_type in _WEBHOOK_EVENT_TYPES:
                result = await self._refine_provider_webhook(
                    event_id, envelope, thread
                )
            else:
                # Fail closed on unknown event_type (Law #3). Surfaces as a
                # dead-letter for ops review; never silently dropped.
                raise RefineryError(
                    f"Unknown event_type '{envelope.event_type}' for "
                    f"event_id={event_id}",
                    code="UNKNOWN_EVENT_TYPE",
                    tenant_id=envelope.tenant_id,
                    correlation_id=envelope.correlation_id,
                )
        except Exception as exc:
            await self._dead_letter(event_id, envelope, error=exc)
            raise

        # Mark processed (Law #2 — successful state change)
        await self._mark_processed(event_id)

        logger.info(
            "transcript_event_refinery: event_id=%s event_type=%s "
            "memory_count=%d candidate_count=%d tenant=%s",
            event_id,
            envelope.event_type,
            len(result.memory_ids),
            len(result.candidate_ids),
            str(envelope.tenant_id),
        )
        return result

    # ---------------------------------------------------------------------------
    # Refiner: voice transcript / session ended
    # ---------------------------------------------------------------------------

    async def _refine_voice_transcript(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        thread: ThreadOut,
    ) -> RefineResult:
        """Voice session: 1 session_summary memory + 0..1 pending_intent.

        Reads from envelope.payload:
          - summary: str (defaults to a stub)
          - pending_intent: dict {summary, action, confidence, ...} (optional)
        """
        payload = envelope.payload or {}
        memory_ids: list[UUID] = []
        candidate_ids: list[UUID] = []

        # Output index 0: session_summary
        summary_text = self._safe_str(
            payload.get("summary"),
            default=f"Voice session {envelope.event_type} ({envelope.event_at.isoformat()})",
        )
        title = self._safe_str(payload.get("title"), default="Voice session")
        memory_in = self._build_memory_in(
            event_id=event_id,
            output_index=0,
            envelope=envelope,
            thread_id=thread.thread_id,
            memory_type="session_summary",
            title=title,
            summary=summary_text,
            detail=self._safe_dict(payload.get("detail")),
        )
        out = await self._memory.write(memory_in, scope=self._scope(envelope), embed=False)
        memory_ids.append(out.memory_id)

        # Output index 1: pending_intent (optional)
        pending = payload.get("pending_intent")
        if isinstance(pending, dict) and pending:
            pi_summary = self._safe_str(pending.get("summary"))
            if pi_summary:
                pi_in = self._build_memory_in(
                    event_id=event_id,
                    output_index=1,
                    envelope=envelope,
                    thread_id=thread.thread_id,
                    memory_type="pending_intent",
                    title=self._safe_str(pending.get("title"), default="Pending intent"),
                    summary=pi_summary,
                    detail=self._safe_dict(pending.get("detail")),
                    status="requested",
                )
                pi_out = await self._memory.write(
                    pi_in, scope=self._scope(envelope), embed=False
                )
                memory_ids.append(pi_out.memory_id)

        return RefineResult(memory_ids=memory_ids, candidate_ids=candidate_ids)

    # ---------------------------------------------------------------------------
    # Refiner: email thread updated
    # ---------------------------------------------------------------------------

    async def _refine_email_thread(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        thread: ThreadOut,
    ) -> RefineResult:
        """Email thread updated: 1 thread_summary memory."""
        payload = envelope.payload or {}
        summary_text = self._safe_str(
            payload.get("summary"),
            default=f"Email thread updated at {envelope.event_at.isoformat()}",
        )
        title = self._safe_str(
            payload.get("subject"), default=self._safe_str(payload.get("title"), default="Email thread")
        )

        memory_in = self._build_memory_in(
            event_id=event_id,
            output_index=0,
            envelope=envelope,
            thread_id=thread.thread_id,
            memory_type="thread_summary",
            title=title,
            summary=summary_text,
            detail=self._safe_dict(payload.get("detail")),
        )
        out = await self._memory.write(memory_in, scope=self._scope(envelope), embed=False)
        return RefineResult(memory_ids=[out.memory_id], candidate_ids=[])

    # ---------------------------------------------------------------------------
    # Refiner: meeting recap ready
    # ---------------------------------------------------------------------------

    async def _refine_meeting_recap(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        thread: ThreadOut,
    ) -> RefineResult:
        """Meeting recap: 1 session_summary + N followup_task per action item.

        payload.action_items: list[dict] — each dict produces one followup_task
        with idempotency_key = f"refine:{event_id}:{1 + i}".
        """
        payload = envelope.payload or {}
        memory_ids: list[UUID] = []

        # Output index 0: session_summary
        summary_text = self._safe_str(
            payload.get("summary"),
            default=f"Meeting recap ({envelope.event_at.isoformat()})",
        )
        title = self._safe_str(payload.get("title"), default="Meeting recap")
        recap_in = self._build_memory_in(
            event_id=event_id,
            output_index=0,
            envelope=envelope,
            thread_id=thread.thread_id,
            memory_type="session_summary",
            title=title,
            summary=summary_text,
            detail=self._safe_dict(payload.get("detail")),
        )
        recap_out = await self._memory.write(
            recap_in, scope=self._scope(envelope), embed=False
        )
        memory_ids.append(recap_out.memory_id)

        # Output indices 1..N: one followup_task per action item
        action_items = payload.get("action_items") or []
        if not isinstance(action_items, list):
            action_items = []
        for i, item in enumerate(action_items):
            if not isinstance(item, dict):
                continue
            item_summary = self._safe_str(item.get("summary"))
            if not item_summary:
                continue
            task_in = self._build_memory_in(
                event_id=event_id,
                output_index=1 + i,
                envelope=envelope,
                thread_id=thread.thread_id,
                memory_type="followup_task",
                title=self._safe_str(item.get("title"), default="Follow-up task"),
                summary=item_summary,
                detail=self._safe_dict(item.get("detail")),
                status="requested",
            )
            task_out = await self._memory.write(
                task_in, scope=self._scope(envelope), embed=False
            )
            memory_ids.append(task_out.memory_id)

        return RefineResult(memory_ids=memory_ids, candidate_ids=[])

    # ---------------------------------------------------------------------------
    # Refiner: finance state change
    # ---------------------------------------------------------------------------

    async def _refine_finance_state_change(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        thread: ThreadOut,
    ) -> RefineResult:
        """Finance event: 1 decision_fact + 0..1 risk_flag (only if RED tier).

        Finance memory is always visibility_scope='finance' so it cannot be
        read by office-scoped agents (Law #6 + visibility_scope enforcement).
        """
        payload = envelope.payload or {}
        memory_ids: list[UUID] = []

        # Output index 0: decision_fact
        summary_text = self._safe_str(
            payload.get("summary"),
            default=f"Finance state change ({envelope.event_at.isoformat()})",
        )
        title = self._safe_str(payload.get("title"), default="Finance state change")
        fact_in = self._build_memory_in(
            event_id=event_id,
            output_index=0,
            envelope=envelope,
            thread_id=thread.thread_id,
            memory_type="decision_fact",
            title=title,
            summary=summary_text,
            detail=self._safe_dict(payload.get("detail")),
            visibility_scope="finance",
        )
        fact_out = await self._memory.write(
            fact_in, scope=self._scope(envelope), embed=False
        )
        memory_ids.append(fact_out.memory_id)

        # Output index 1: risk_flag (only when RED-tier finance state)
        risk_tier = self._safe_str(payload.get("risk_tier"), default="").lower()
        if risk_tier == "red":
            risk_summary = self._safe_str(
                payload.get("risk_summary"), default=summary_text
            )
            risk_in = self._build_memory_in(
                event_id=event_id,
                output_index=1,
                envelope=envelope,
                thread_id=thread.thread_id,
                memory_type="risk_flag",
                title=self._safe_str(payload.get("risk_title"), default="Finance risk flag"),
                summary=risk_summary,
                detail=self._safe_dict(payload.get("risk_detail")),
                visibility_scope="finance",
            )
            risk_out = await self._memory.write(
                risk_in, scope=self._scope(envelope), embed=False
            )
            memory_ids.append(risk_out.memory_id)

        return RefineResult(memory_ids=memory_ids, candidate_ids=[])

    # ---------------------------------------------------------------------------
    # Refiner: provider webhook received
    # ---------------------------------------------------------------------------

    async def _refine_provider_webhook(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        thread: ThreadOut,
    ) -> RefineResult:
        """Provider webhook: 1 timeline_event memory."""
        payload = envelope.payload or {}
        provider = self._safe_str(payload.get("provider"), default="unknown_provider")
        webhook_event = self._safe_str(payload.get("webhook_event"), default="webhook")
        summary_text = self._safe_str(
            payload.get("summary"),
            default=f"Webhook from {provider}: {webhook_event}",
        )
        title = self._safe_str(
            payload.get("title"),
            default=f"{provider} {webhook_event}",
        )
        timeline_in = self._build_memory_in(
            event_id=event_id,
            output_index=0,
            envelope=envelope,
            thread_id=thread.thread_id,
            memory_type="timeline_event",
            title=title,
            summary=summary_text,
            detail=self._safe_dict(payload.get("detail")),
        )
        out = await self._memory.write(
            timeline_in, scope=self._scope(envelope), embed=False
        )
        return RefineResult(memory_ids=[out.memory_id], candidate_ids=[])

    # ---------------------------------------------------------------------------
    # Inbox lifecycle helpers
    # ---------------------------------------------------------------------------

    async def _claim_inbox_row(self, event_id: UUID) -> dict[str, Any]:
        """Set status='processing', attempts += 1, return the row.

        We rely on the application to call this only when the row is in
        a claimable state (pending or dead_letter being replayed). Concurrent
        claim races are tolerated — the worst case is two parallel refines,
        both protected from duplicate writes by idempotency_key.
        """
        try:
            rows = await supabase_select(
                "memory_event_inbox",
                f"event_id=eq.{event_id}",
                limit=1,
            )
        except SupabaseClientError as exc:
            raise RefineryError(
                f"DB select memory_event_inbox failed: {exc.detail}",
                code="DB_SELECT_FAILED",
            ) from exc

        if not rows:
            raise RefineryError(
                f"event_id={event_id} not found in memory_event_inbox",
                code="EVENT_NOT_FOUND",
            )

        row = rows[0]
        attempts = int(row.get("attempts", 0)) + 1
        try:
            await supabase_update(
                "memory_event_inbox",
                f"event_id=eq.{event_id}",
                {"status": "processing", "attempts": attempts},
            )
        except SupabaseClientError as exc:
            raise RefineryError(
                f"DB update memory_event_inbox failed: {exc.detail}",
                code="DB_UPDATE_FAILED",
            ) from exc

        # Reflect the claim locally so callers get the right attempt count
        row["status"] = "processing"
        row["attempts"] = attempts
        return row

    async def _mark_processed(self, event_id: UUID) -> None:
        """Mark inbox row as successfully processed."""
        try:
            await supabase_update(
                "memory_event_inbox",
                f"event_id=eq.{event_id}",
                {
                    "status": "processed",
                    "processed_at": _now_utc().isoformat(),
                },
            )
        except SupabaseClientError as exc:
            # Don't dead-letter on this failure — the work was already done.
            # Log and let the next sweep clean up the status.
            logger.error(
                "transcript_event_refinery: failed to mark processed event_id=%s: %s",
                event_id,
                exc.detail,
            )

    async def _dead_letter(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        *,
        error: BaseException,
    ) -> None:
        """Mark inbox row 'dead_letter' and emit a failure receipt for incident creation.

        The receipt cascade triggers incident_writer.maybe_create_incident_async
        which upserts a row into the incidents table for ops review.
        """
        try:
            await supabase_update(
                "memory_event_inbox",
                f"event_id=eq.{event_id}",
                {
                    "status": "dead_letter",
                    "dead_lettered_at": _now_utc().isoformat(),
                    "last_error": str(error)[:1000],
                },
            )
        except SupabaseClientError as exc:
            # Best-effort: even if the DLQ update fails, we still emit the
            # incident receipt. Log loudly so ops sees both failures.
            logger.error(
                "transcript_event_refinery: failed to mark dead_letter event_id=%s: %s",
                event_id,
                exc.detail,
            )

        # Emit a failure receipt — incident_writer cascades this into the
        # incidents table automatically.
        receipt = self._build_dead_letter_receipt(event_id, envelope, error)
        store_receipts([receipt])

        logger.error(
            "transcript_event_refinery: dead_letter event_id=%s event_type=%s "
            "tenant=%s err=%s",
            event_id,
            envelope.event_type,
            str(envelope.tenant_id),
            str(error)[:200],
        )

    # ---------------------------------------------------------------------------
    # Builders
    # ---------------------------------------------------------------------------

    def _scope(self, envelope: MemoryEventEnvelope) -> ScopedIdentity:
        """Build a ScopedIdentity from an envelope (no auth bypass — caller scope)."""
        return ScopedIdentity(
            tenant_id=envelope.tenant_id,
            suite_id=envelope.suite_id,
            office_id=envelope.office_id,
            actor_id=envelope.actor_id,
            user_id=envelope.user_id,
        )

    def _provenance(self, envelope: MemoryEventEnvelope) -> Provenance:
        return Provenance(
            source_surface=envelope.source_surface,
            source_agent=envelope.source_agent,
            runtime_family=envelope.runtime_family,
            channel=envelope.channel,
            external_session_id=str(envelope.session_id) if envelope.session_id else None,
            source_record_id=envelope.source_record_id,
            trace_id=envelope.trace_id,
            correlation_id=envelope.correlation_id,
        )

    def _build_memory_in(
        self,
        *,
        event_id: UUID,
        output_index: int,
        envelope: MemoryEventEnvelope,
        thread_id: UUID,
        memory_type: MemoryType,
        title: str | None,
        summary: str,
        detail: dict | None = None,
        status: str | None = None,
        visibility_scope: str = "office",
    ) -> MemoryObjectIn:
        """Construct a MemoryObjectIn for a single refinery output."""
        idem_key = f"refine:{event_id}:{output_index}"
        return MemoryObjectIn(
            scope=self._scope(envelope),
            provenance=self._provenance(envelope),
            memory_type=memory_type,
            entity_type=envelope.entity_type,
            entity_id=envelope.entity_id,
            thread_id=thread_id,
            title=title,
            summary=summary,
            detail=detail or {},
            visibility_scope=visibility_scope,
            status=status,
            event_at=envelope.event_at,
            source_updated_at=envelope.source_updated_at,
            idempotency_key=idem_key,
        )

    def _build_dead_letter_receipt(
        self,
        event_id: UUID,
        envelope: MemoryEventEnvelope,
        error: BaseException,
    ) -> dict[str, Any]:
        """Build a 'memory_event_dead_letter' receipt with error context.

        outcome='failed' triggers the incident_writer cascade automatically.
        """
        return {
            "id": str(uuid.uuid4()),
            "receipt_type": "memory_event_dead_letter",
            "tenant_id": str(envelope.tenant_id),
            "suite_id": str(envelope.suite_id),
            "office_id": str(envelope.office_id),
            "actor_id": str(envelope.actor_id) if envelope.actor_id else None,
            "actor_type": "WORKER",
            "action_type": "memory_event_dead_letter",
            "tool_used": "transcript_event_refinery",
            "risk_tier": envelope.risk_tier,
            "trace_id": str(envelope.trace_id),
            "correlation_id": str(envelope.correlation_id),
            "redacted_inputs": {
                "event_id": str(event_id),
                "event_type": envelope.event_type,
            },
            "redacted_outputs": {},
            "outcome": "failed",
            "reason_code": str(error)[:200],
            "error_message": str(error)[:500],
            "created_at": _now_utc().isoformat(),
        }

    def _envelope_from_row(self, row: dict[str, Any]) -> MemoryEventEnvelope:
        """Reconstruct a MemoryEventEnvelope from a memory_event_inbox row."""
        return MemoryEventEnvelope(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
            actor_id=_coerce_uuid(row.get("actor_id")),
            user_id=_coerce_uuid(row.get("user_id")),
            event_type=row["event_type"],
            source_surface=row.get("source_surface"),
            source_agent=row.get("source_agent"),
            runtime_family=row.get("runtime_family"),
            channel=row.get("channel"),
            trace_id=UUID(row["trace_id"]),
            correlation_id=UUID(row["correlation_id"]),
            source_record_id=row.get("source_record_id"),
            session_id=_coerce_uuid(row.get("session_id")),
            thread_id=_coerce_uuid(row.get("thread_id")),
            entity_type=row.get("entity_type"),
            entity_id=_coerce_uuid(row.get("entity_id")),
            payload=row.get("payload") or {},
            risk_tier=row.get("risk_tier", "yellow"),
            needs_approval=bool(row.get("needs_approval", False)),
            receipt_required=bool(row.get("receipt_required", False)),
            event_at=row["event_at"],
            source_updated_at=row.get("source_updated_at"),
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _safe_str(value: Any, *, default: str = "") -> str:
        """Coerce to non-empty string, falling back to default."""
        if value is None:
            return default
        s = str(value).strip()
        return s if s else default

    @staticmethod
    def _safe_dict(value: Any) -> dict:
        """Coerce to dict, returning {} for non-dict input."""
        if isinstance(value, dict):
            return value
        return {}
