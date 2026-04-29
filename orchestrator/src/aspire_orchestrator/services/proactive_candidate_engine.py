"""Proactive Candidate Engine — surface-level action suggestions for the spine.

A "candidate" is an agent's recommendation that the orchestrator should act
soon. Examples: "queue_callback for missed call", "create_draft for unread
inquiry", "request_approval for invoice aging risk". The orchestrator decides
disposition; the candidate is just a proposal (Law #1: workers propose, the
single brain disposes).

Active-window dedup:
  Migration 097 enforces a UNIQUE INDEX on
  (tenant_id, suite_id, office_id, owner_agent, entity_type, entity_id,
   recommended_action) WHERE status IN ('open','snoozed').
  We pre-check before INSERT for a clean error path and to honor cooldown.

Cooldown:
  If an existing row in the dedup tuple has cooldown_until > now(), we return
  that row instead of creating a new candidate. This prevents agents from
  re-recommending the same action in a tight loop (e.g., callback nudge every
  30s after a missed call).

State machine:
  open      -> snoozed, approved, dismissed, expired
  snoozed   -> open, approved, dismissed, expired
  approved  -> executed, dismissed
  executed  -> (terminal)
  dismissed -> (terminal)
  expired   -> (terminal)

Receipt:
  Every successful create_candidate emits a 'proactive_candidate_created'
  receipt. Every transition emits 'proactive_candidate_transition'. Both
  comply with Law #2 (Receipt for All Actions).

Tenant isolation (Law #6):
  ScopedIdentity is required on every public method. We validate the
  envelope's scope matches the caller's scope before any DB I/O.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    CandidateQuery,
    CandidateStatus,
    ProactiveCandidateIn,
    ProactiveCandidateOut,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import (
    MemoryServiceError,
    _assert_scope_match,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

# State machine: source -> {valid targets}
_VALID_TRANSITIONS: dict[CandidateStatus, set[CandidateStatus]] = {
    "open": {"snoozed", "approved", "dismissed", "expired"},
    "snoozed": {"open", "approved", "dismissed", "expired"},
    "approved": {"executed", "dismissed"},
    "executed": set(),
    "dismissed": set(),
    "expired": set(),
}

_TERMINAL_STATUSES: frozenset[CandidateStatus] = frozenset(
    {"executed", "dismissed", "expired"}
)
_ACTIVE_STATUSES: frozenset[CandidateStatus] = frozenset({"open", "snoozed"})


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _row_to_candidate_out(row: dict[str, Any]) -> ProactiveCandidateOut:
    """Map a flat DB row to ProactiveCandidateOut.

    Matches migration 097 proactive_candidates column shape.
    """
    return ProactiveCandidateOut(
        candidate_id=UUID(row["candidate_id"]),
        schema_version=row.get("schema_version", "v1"),
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
        owner_agent=row["owner_agent"],
        source_event_ids=[UUID(x) for x in (row.get("source_event_ids") or [])],
        source_memory_ids=[UUID(x) for x in (row.get("source_memory_ids") or [])],
        entity_type=row.get("entity_type"),
        entity_id=UUID(row["entity_id"]) if row.get("entity_id") else None,
        thread_id=UUID(row["thread_id"]) if row.get("thread_id") else None,
        recommended_action=row["recommended_action"],
        action_class=row["action_class"],
        why_now=row["why_now"],
        confidence=float(row["confidence"]),
        risk_tier=row["risk_tier"],
        needs_approval=bool(row.get("needs_approval", False)),
        receipt_required=bool(row.get("receipt_required", False)),
        due_at=row.get("due_at"),
        cooldown_until=row.get("cooldown_until"),
        status=row["status"],
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
    )


def _build_candidate_receipt(
    *,
    receipt_type: str,
    candidate_id: UUID,
    owner_agent: str,
    recommended_action: str,
    risk_tier: str,
    scope: ScopedIdentity,
    outcome: str = "success",
    reason_code: str | None = None,
    extra_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for proactive candidate creation/transition.

    PII never appears — only IDs, agent label, action label, risk_tier.
    """
    inputs: dict[str, Any] = {
        "owner_agent": owner_agent,
        "recommended_action": recommended_action,
    }
    if extra_inputs:
        inputs.update(extra_inputs)
    return {
        "id": str(uuid.uuid4()),
        "receipt_type": receipt_type,
        "tenant_id": str(scope.tenant_id),
        "suite_id": str(scope.suite_id),
        "office_id": str(scope.office_id),
        "actor_id": str(scope.actor_id) if scope.actor_id else None,
        "actor_type": "WORKER",
        "action_type": receipt_type,
        "tool_used": "proactive_candidate_engine",
        "risk_tier": risk_tier,
        "trace_id": None,
        "correlation_id": None,
        "redacted_inputs": inputs,
        "redacted_outputs": {
            "candidate_id": str(candidate_id),
        },
        "outcome": outcome,
        "reason_code": reason_code,
        "created_at": _now_utc().isoformat(),
    }


class ProactiveCandidateEngine:
    """Create, query, and transition proactive candidates with active-window dedup.

    Stateless — safe to instantiate per-request.
    """

    async def create_candidate(
        self,
        candidate_in: ProactiveCandidateIn,
        *,
        scope: ScopedIdentity,
    ) -> ProactiveCandidateOut:
        """Create a new candidate, or return the existing dedup match.

        Dedup tuple:
            (tenant_id, suite_id, office_id, owner_agent,
             entity_type, entity_id, recommended_action)
            among rows with status IN ('open','snoozed').

        Cooldown:
            If an existing dedup match has cooldown_until > now(), we return
            it without inserting (no duplicate nudge during cooldown).

        Receipt: emits 'proactive_candidate_created' on insert. Idempotent
        replay (existing match) does NOT emit a second receipt.
        """
        _assert_scope_match(
            {
                "tenant_id": str(candidate_in.tenant_id),
                "suite_id": str(candidate_in.suite_id),
                "office_id": str(candidate_in.office_id),
            },
            scope,
        )

        # Active-window dedup pre-check
        existing = await self._find_active_dedup_match(candidate_in)
        if existing is not None:
            now = _now_utc()
            cooldown = existing.cooldown_until
            if cooldown is not None:
                # ProactiveCandidateOut.cooldown_until is datetime|None — Pydantic-parsed
                if cooldown > now:
                    logger.info(
                        "proactive_candidate_engine: cooldown active candidate_id=%s "
                        "until=%s tenant=%s",
                        existing.candidate_id,
                        cooldown.isoformat(),
                        str(scope.tenant_id),
                    )
                    return existing
            # Active dedup match exists (no cooldown bypass) — return it
            logger.info(
                "proactive_candidate_engine: dedup hit candidate_id=%s "
                "owner=%s action=%s tenant=%s",
                existing.candidate_id,
                candidate_in.owner_agent,
                candidate_in.recommended_action,
                str(scope.tenant_id),
            )
            return existing

        # Build INSERT row
        row: dict[str, Any] = {
            "tenant_id": str(candidate_in.tenant_id),
            "suite_id": str(candidate_in.suite_id),
            "office_id": str(candidate_in.office_id),
            "owner_agent": candidate_in.owner_agent,
            "source_event_ids": [str(x) for x in candidate_in.source_event_ids],
            "source_memory_ids": [str(x) for x in candidate_in.source_memory_ids],
            "entity_type": candidate_in.entity_type,
            "entity_id": str(candidate_in.entity_id) if candidate_in.entity_id else None,
            "thread_id": str(candidate_in.thread_id) if candidate_in.thread_id else None,
            "recommended_action": candidate_in.recommended_action,
            "action_class": candidate_in.action_class,
            "why_now": candidate_in.why_now,
            "confidence": candidate_in.confidence,
            "risk_tier": candidate_in.risk_tier,
            "needs_approval": candidate_in.needs_approval,
            "receipt_required": candidate_in.receipt_required,
            "due_at": candidate_in.due_at.isoformat() if candidate_in.due_at else None,
            "cooldown_until": (
                candidate_in.cooldown_until.isoformat()
                if candidate_in.cooldown_until
                else None
            ),
            "status": candidate_in.status,
        }

        try:
            inserted = await supabase_insert("proactive_candidates", row)
        except SupabaseClientError as exc:
            # Concurrent insert race against the partial unique index
            detail = exc.detail.lower()
            is_conflict = (
                exc.status_code == 409
                or "23505" in detail
                or "unique" in detail
            )
            if is_conflict:
                # Another worker won — re-fetch and return the surviving row
                race_winner = await self._find_active_dedup_match(candidate_in)
                if race_winner is not None:
                    logger.info(
                        "proactive_candidate_engine: race winner candidate_id=%s "
                        "tenant=%s",
                        race_winner.candidate_id,
                        str(scope.tenant_id),
                    )
                    return race_winner
            logger.error(
                "proactive_candidate_engine: insert failed owner=%s action=%s "
                "tenant=%s code=%s",
                candidate_in.owner_agent,
                candidate_in.recommended_action,
                str(scope.tenant_id),
                exc.status_code,
            )
            raise MemoryServiceError(
                f"DB insert proactive_candidates failed: {exc.detail}",
                code="DB_INSERT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        out = _row_to_candidate_out(inserted)

        # Receipt (Law #2)
        receipt = _build_candidate_receipt(
            receipt_type="proactive_candidate_created",
            candidate_id=out.candidate_id,
            owner_agent=out.owner_agent,
            recommended_action=out.recommended_action,
            risk_tier=out.risk_tier,
            scope=scope,
        )
        store_receipts([receipt])

        logger.info(
            "proactive_candidate_engine: created candidate_id=%s owner=%s "
            "action=%s risk=%s tenant=%s",
            out.candidate_id,
            out.owner_agent,
            out.recommended_action,
            out.risk_tier,
            str(scope.tenant_id),
        )
        return out

    async def query(
        self,
        q: CandidateQuery,
        *,
        scope: ScopedIdentity,
    ) -> list[ProactiveCandidateOut]:
        """Query candidates within scope.

        Filters supported:
        - owner_agent (list)
        - status (list)
        - due_before (datetime; matches due_at <= due_before)
        Order: last_activity_at DESC.
        """
        _assert_scope_match(
            {
                "tenant_id": str(q.tenant_id),
                "suite_id": str(q.suite_id),
                "office_id": str(q.office_id),
            },
            scope,
        )

        filter_parts = [
            f"tenant_id=eq.{q.tenant_id}",
            f"suite_id=eq.{q.suite_id}",
            f"office_id=eq.{q.office_id}",
        ]
        if q.owner_agent:
            filter_parts.append(f"owner_agent=in.({','.join(q.owner_agent)})")
        if q.status:
            filter_parts.append(f"status=in.({','.join(q.status)})")
        if q.due_before is not None:
            filter_parts.append(f"due_at=lte.{q.due_before.isoformat()}")
        filter_str = "&".join(filter_parts)

        try:
            rows = await supabase_select(
                "proactive_candidates",
                filter_str,
                order_by="last_activity_at.desc",
                limit=q.limit,
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB query proactive_candidates failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        return [_row_to_candidate_out(r) for r in rows]

    async def transition(
        self,
        candidate_id: UUID,
        new_status: CandidateStatus,
        *,
        scope: ScopedIdentity,
        reason: str | None = None,
    ) -> ProactiveCandidateOut:
        """Transition a candidate from its current status to new_status.

        State machine validated against _VALID_TRANSITIONS. Invalid transitions
        raise MemoryServiceError(code='INVALID_STATE_TRANSITION'). Receipt
        emitted on success.

        Raises:
            MemoryServiceError: on scope mismatch, NOT_FOUND, invalid transition,
                                or DB error.
        """
        # Fetch current row first to verify scope and current status
        current = await self._get(candidate_id, scope=scope)
        if current is None:
            raise MemoryServiceError(
                f"candidate_id={candidate_id} not found",
                code="NOT_FOUND",
                tenant_id=scope.tenant_id,
            )

        # Validate transition
        valid_targets = _VALID_TRANSITIONS.get(current.status, set())
        if new_status not in valid_targets:
            raise MemoryServiceError(
                f"Invalid state transition {current.status}->{new_status} "
                f"for candidate_id={candidate_id}; valid targets: "
                f"{sorted(valid_targets)}",
                code="INVALID_STATE_TRANSITION",
                tenant_id=scope.tenant_id,
            )

        match_filter = (
            f"candidate_id=eq.{candidate_id}"
            f"&tenant_id=eq.{scope.tenant_id}"
            f"&suite_id=eq.{scope.suite_id}"
            f"&office_id=eq.{scope.office_id}"
        )
        try:
            updated = await supabase_update(
                "proactive_candidates",
                match_filter,
                {"status": new_status},
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB update proactive_candidates failed: {exc.detail}",
                code="DB_UPDATE_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc

        out = _row_to_candidate_out(updated)

        # Receipt (Law #2)
        receipt = _build_candidate_receipt(
            receipt_type="proactive_candidate_transition",
            candidate_id=out.candidate_id,
            owner_agent=out.owner_agent,
            recommended_action=out.recommended_action,
            risk_tier=out.risk_tier,
            scope=scope,
            reason_code=reason or f"{current.status}_to_{new_status}",
            extra_inputs={
                "from_status": current.status,
                "to_status": new_status,
            },
        )
        store_receipts([receipt])

        logger.warning(
            "proactive_candidate_engine: transition candidate_id=%s %s->%s tenant=%s",
            out.candidate_id,
            current.status,
            new_status,
            str(scope.tenant_id),
        )
        return out

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _get(
        self,
        candidate_id: UUID,
        *,
        scope: ScopedIdentity,
    ) -> ProactiveCandidateOut | None:
        """Fetch a candidate by ID; validate scope match."""
        try:
            rows = await supabase_select(
                "proactive_candidates",
                f"candidate_id=eq.{candidate_id}",
                limit=1,
            )
        except SupabaseClientError as exc:
            raise MemoryServiceError(
                f"DB select proactive_candidates failed: {exc.detail}",
                code="DB_SELECT_FAILED",
                tenant_id=scope.tenant_id,
            ) from exc
        if not rows:
            return None
        row = rows[0]
        _assert_scope_match(row, scope)
        return _row_to_candidate_out(row)

    async def _find_active_dedup_match(
        self,
        candidate_in: ProactiveCandidateIn,
    ) -> ProactiveCandidateOut | None:
        """Find an active (open|snoozed) candidate matching the dedup tuple.

        Dedup tuple: (tenant, suite, office, owner_agent,
                      entity_type, entity_id, recommended_action).
        Returns the most recent match, or None.
        """
        filter_parts = [
            f"tenant_id=eq.{candidate_in.tenant_id}",
            f"suite_id=eq.{candidate_in.suite_id}",
            f"office_id=eq.{candidate_in.office_id}",
            f"owner_agent=eq.{candidate_in.owner_agent}",
            f"recommended_action=eq.{candidate_in.recommended_action}",
            f"status=in.({','.join(sorted(_ACTIVE_STATUSES))})",
        ]
        # entity_type / entity_id may be NULL — PostgREST uses is.null
        if candidate_in.entity_type is None:
            filter_parts.append("entity_type=is.null")
        else:
            filter_parts.append(f"entity_type=eq.{candidate_in.entity_type}")
        if candidate_in.entity_id is None:
            filter_parts.append("entity_id=is.null")
        else:
            filter_parts.append(f"entity_id=eq.{candidate_in.entity_id}")
        filter_str = "&".join(filter_parts)

        try:
            rows = await supabase_select(
                "proactive_candidates",
                filter_str,
                order_by="last_activity_at.desc",
                limit=1,
            )
        except SupabaseClientError:
            return None

        return _row_to_candidate_out(rows[0]) if rows else None
