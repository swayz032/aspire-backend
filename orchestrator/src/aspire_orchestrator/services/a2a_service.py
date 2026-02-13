"""A2A (Agent-to-Agent) Task Router Service.

Per architecture.md: the A2A router handles cross-skill-pack task dispatch,
allowing the orchestrator (Law #1) to delegate work to specialized agents.

Responsibilities:
1. Dispatch tasks to skill packs (enqueue with routing metadata)
2. Claim tasks (lease-based, FOR UPDATE SKIP LOCKED semantics)
3. Complete/fail tasks with receipt emission
4. Enforce tenant isolation (Law #6) — all tasks scoped to suite_id
5. Emit receipts for all state changes (Law #2)

Phase 1: In-memory with thread-safe locking (matches receipt_store.py pattern).
Phase 2+: Moves to Supabase (a2a_tasks table, claim_a2a_tasks function).

Task lifecycle: created → claimed → in_progress → done|failed
                created → blocked (waiting on dependencies)
                claimed → failed → (requeued as created if attempt_count < max)
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Enums (mirror a2a_task_status/a2a_task_event_type from Supabase schema)
# =============================================================================


class A2ATaskStatus(str, Enum):
    """Task lifecycle states (mirrors Supabase enum)."""

    CREATED = "created"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    CANCELED = "canceled"


class A2AEventType(str, Enum):
    """Task event types for audit trail (mirrors Supabase enum)."""

    CREATED = "created"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    REQUEUED = "requeued"
    QUARANTINED = "quarantined"
    CANCELED = "canceled"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class A2ATask:
    """An agent-to-agent task in the queue."""

    task_id: str
    suite_id: str
    office_id: str
    correlation_id: str
    task_type: str  # action_type for routing
    assigned_to_agent: str  # skill pack owner
    status: A2ATaskStatus
    priority: int  # 1=highest, 5=lowest
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: str
    updated_at: str
    claimed_by: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class A2ATaskEvent:
    """Audit event for a task state change."""

    event_id: str
    task_id: str
    suite_id: str
    event_type: A2AEventType
    actor_type: str  # system | user | agent
    actor_id: str
    details: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class A2ADispatchResult:
    """Result of dispatching a task."""

    success: bool
    task_id: str | None = None
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class A2AClaimResult:
    """Result of claiming a task."""

    success: bool
    task: A2ATask | None = None
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class A2ACompleteResult:
    """Result of completing/failing a task."""

    success: bool
    task_id: str | None = None
    new_status: A2ATaskStatus | None = None
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# A2A Service
# =============================================================================


class A2AService:
    """Thread-safe A2A task router.

    Implements the core dispatch/claim/complete lifecycle with
    receipt data emission at every state change (Law #2).
    """

    def __init__(self, *, default_lease_seconds: int = 300, max_attempts: int = 3):
        self._lock = threading.Lock()
        self._tasks: dict[str, A2ATask] = {}
        self._events: list[A2ATaskEvent] = []
        self._idempotency_keys: set[str] = set()
        self._default_lease_seconds = default_lease_seconds
        self._max_attempts = max_attempts

    def dispatch(
        self,
        *,
        suite_id: str,
        office_id: str,
        correlation_id: str,
        task_type: str,
        assigned_to_agent: str,
        payload: dict[str, Any],
        priority: int = 3,
        idempotency_key: str | None = None,
        actor_id: str = "orchestrator",
    ) -> A2ADispatchResult:
        """Dispatch a new task to a skill pack agent.

        Idempotency: if idempotency_key is provided and already exists,
        returns the existing task_id without creating a duplicate.
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            # Idempotency check
            if idempotency_key and idempotency_key in self._idempotency_keys:
                existing = next(
                    (t for t in self._tasks.values()
                     if t.idempotency_key == idempotency_key and t.suite_id == suite_id),
                    None,
                )
                if existing:
                    logger.info(
                        "A2A idempotency: task already exists key=%s task_id=%s",
                        idempotency_key, existing.task_id,
                    )
                    return A2ADispatchResult(
                        success=True,
                        task_id=existing.task_id,
                        receipt_data=self._make_receipt_data(
                            task_id=existing.task_id,
                            suite_id=suite_id,
                            office_id=office_id,
                            correlation_id=correlation_id,
                            action_type="a2a.dispatch.idempotent",
                            outcome="success",
                            details={"idempotency_key": idempotency_key, "existing": True},
                        ),
                    )

            task_id = str(uuid.uuid4())
            task = A2ATask(
                task_id=task_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                task_type=task_type,
                assigned_to_agent=assigned_to_agent,
                status=A2ATaskStatus.CREATED,
                priority=priority,
                payload=payload,
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
                max_attempts=self._max_attempts,
            )

            self._tasks[task_id] = task
            if idempotency_key:
                self._idempotency_keys.add(idempotency_key)

            self._emit_event(
                task_id=task_id,
                suite_id=suite_id,
                event_type=A2AEventType.CREATED,
                actor_type="system",
                actor_id=actor_id,
                details={
                    "task_type": task_type,
                    "assigned_to_agent": assigned_to_agent,
                    "priority": priority,
                },
            )

        logger.info(
            "A2A dispatch: task_id=%s type=%s agent=%s suite=%s",
            task_id, task_type, assigned_to_agent, suite_id,
        )

        return A2ADispatchResult(
            success=True,
            task_id=task_id,
            receipt_data=self._make_receipt_data(
                task_id=task_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="a2a.dispatch",
                outcome="success",
                details={
                    "task_type": task_type,
                    "assigned_to_agent": assigned_to_agent,
                    "priority": priority,
                },
            ),
        )

    def claim(
        self,
        *,
        agent_id: str,
        suite_id: str,
        task_types: list[str] | None = None,
        max_tasks: int = 1,
        lease_seconds: int | None = None,
    ) -> A2AClaimResult:
        """Claim available tasks for an agent.

        Implements FOR UPDATE SKIP LOCKED semantics:
        - Only returns unclaimed tasks (status=CREATED)
        - Sets lease expiry for automatic release
        - Scoped to suite_id (Law #6)
        - Optionally filtered by task_type

        Returns the first claimable task, or error if none available.
        """
        now = datetime.now(timezone.utc)
        lease_secs = lease_seconds or self._default_lease_seconds

        with self._lock:
            # Find claimable tasks (CREATED status, matching suite, not expired)
            candidates = [
                t for t in self._tasks.values()
                if t.suite_id == suite_id
                and t.status == A2ATaskStatus.CREATED
                and (task_types is None or t.task_type in task_types)
            ]

            # Sort by priority (ascending = highest first), then created_at
            candidates.sort(key=lambda t: (t.priority, t.created_at))

            if not candidates:
                return A2AClaimResult(
                    success=False,
                    error="No tasks available for claiming",
                    receipt_data=self._make_receipt_data(
                        task_id="none",
                        suite_id=suite_id,
                        office_id="unknown",
                        correlation_id=str(uuid.uuid4()),
                        action_type="a2a.claim",
                        outcome="denied",
                        details={"reason": "NO_TASKS_AVAILABLE", "agent_id": agent_id},
                    ),
                )

            # Claim the first available task
            task = candidates[0]
            lease_expiry = datetime(
                now.year, now.month, now.day,
                now.hour, now.minute, now.second,
                tzinfo=timezone.utc,
            )
            # Add lease seconds
            from datetime import timedelta
            lease_expiry = now + timedelta(seconds=lease_secs)

            task.status = A2ATaskStatus.CLAIMED
            task.claimed_by = agent_id
            task.claimed_at = now.isoformat()
            task.lease_expires_at = lease_expiry.isoformat()
            task.attempt_count += 1
            task.updated_at = now.isoformat()

            self._emit_event(
                task_id=task.task_id,
                suite_id=suite_id,
                event_type=A2AEventType.CLAIMED,
                actor_type="agent",
                actor_id=agent_id,
                details={
                    "attempt_count": task.attempt_count,
                    "lease_expires_at": task.lease_expires_at,
                },
            )

        logger.info(
            "A2A claim: task_id=%s agent=%s type=%s attempt=%d",
            task.task_id, agent_id, task.task_type, task.attempt_count,
        )

        return A2AClaimResult(
            success=True,
            task=task,
            receipt_data=self._make_receipt_data(
                task_id=task.task_id,
                suite_id=suite_id,
                office_id=task.office_id,
                correlation_id=task.correlation_id,
                action_type="a2a.claim",
                outcome="success",
                details={
                    "agent_id": agent_id,
                    "task_type": task.task_type,
                    "attempt_count": task.attempt_count,
                },
            ),
        )

    def complete(
        self,
        *,
        task_id: str,
        agent_id: str,
        suite_id: str,
        result: dict[str, Any] | None = None,
    ) -> A2ACompleteResult:
        """Mark a task as completed.

        Only the agent that claimed the task can complete it.
        Tenant-scoped (Law #6).
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return A2ACompleteResult(
                    success=False,
                    error=f"Task not found: {task_id}",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id="unknown",
                        correlation_id=str(uuid.uuid4()),
                        action_type="a2a.complete",
                        outcome="denied",
                        details={"reason": "TASK_NOT_FOUND", "agent_id": agent_id},
                    ),
                )

            # Tenant isolation check (Law #6)
            if task.suite_id != suite_id:
                return A2ACompleteResult(
                    success=False,
                    error="TENANT_ISOLATION_VIOLATION",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id="unknown",
                        correlation_id=task.correlation_id,
                        action_type="a2a.complete",
                        outcome="denied",
                        details={"reason": "TENANT_ISOLATION_VIOLATION", "agent_id": agent_id},
                    ),
                )

            # Only claimed tasks can be completed
            if task.status not in (A2ATaskStatus.CLAIMED, A2ATaskStatus.IN_PROGRESS):
                return A2ACompleteResult(
                    success=False,
                    error=f"Task cannot be completed in status: {task.status.value}",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id=task.office_id,
                        correlation_id=task.correlation_id,
                        action_type="a2a.complete",
                        outcome="denied",
                        details={"reason": "INVALID_STATUS", "status": task.status.value, "agent_id": agent_id},
                    ),
                )

            # Only the claiming agent can complete
            if task.claimed_by != agent_id:
                return A2ACompleteResult(
                    success=False,
                    error=f"Task claimed by {task.claimed_by}, not {agent_id}",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id=task.office_id,
                        correlation_id=task.correlation_id,
                        action_type="a2a.complete",
                        outcome="denied",
                        details={"reason": "WRONG_CLAIMER", "claimed_by": task.claimed_by, "agent_id": agent_id},
                    ),
                )

            task.status = A2ATaskStatus.DONE
            task.result = result
            task.updated_at = now

            self._emit_event(
                task_id=task_id,
                suite_id=suite_id,
                event_type=A2AEventType.COMPLETED,
                actor_type="agent",
                actor_id=agent_id,
                details={"result_keys": list((result or {}).keys())},
            )

        logger.info("A2A complete: task_id=%s agent=%s", task_id, agent_id)

        return A2ACompleteResult(
            success=True,
            task_id=task_id,
            new_status=A2ATaskStatus.DONE,
            receipt_data=self._make_receipt_data(
                task_id=task_id,
                suite_id=suite_id,
                office_id=task.office_id,
                correlation_id=task.correlation_id,
                action_type="a2a.complete",
                outcome="success",
                details={"agent_id": agent_id},
            ),
        )

    def fail(
        self,
        *,
        task_id: str,
        agent_id: str,
        suite_id: str,
        error: str,
    ) -> A2ACompleteResult:
        """Mark a task as failed.

        If attempt_count < max_attempts, requeues the task (status → CREATED).
        Otherwise, quarantines the task.
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return A2ACompleteResult(
                    success=False,
                    error=f"Task not found: {task_id}",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id="unknown",
                        correlation_id=str(uuid.uuid4()),
                        action_type="a2a.fail",
                        outcome="denied",
                        details={"reason": "TASK_NOT_FOUND", "agent_id": agent_id},
                    ),
                )

            if task.suite_id != suite_id:
                return A2ACompleteResult(
                    success=False,
                    error="TENANT_ISOLATION_VIOLATION",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id="unknown",
                        correlation_id=task.correlation_id,
                        action_type="a2a.fail",
                        outcome="denied",
                        details={"reason": "TENANT_ISOLATION_VIOLATION", "agent_id": agent_id},
                    ),
                )

            if task.status not in (A2ATaskStatus.CLAIMED, A2ATaskStatus.IN_PROGRESS):
                return A2ACompleteResult(
                    success=False,
                    error=f"Task cannot be failed in status: {task.status.value}",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id=task.office_id,
                        correlation_id=task.correlation_id,
                        action_type="a2a.fail",
                        outcome="denied",
                        details={"reason": "INVALID_STATUS", "status": task.status.value, "agent_id": agent_id},
                    ),
                )

            if task.claimed_by != agent_id:
                return A2ACompleteResult(
                    success=False,
                    error=f"Task claimed by {task.claimed_by}, not {agent_id}",
                    receipt_data=self._make_receipt_data(
                        task_id=task_id,
                        suite_id=suite_id,
                        office_id=task.office_id,
                        correlation_id=task.correlation_id,
                        action_type="a2a.fail",
                        outcome="denied",
                        details={"reason": "WRONG_CLAIMER", "claimed_by": task.claimed_by, "agent_id": agent_id},
                    ),
                )

            task.error = error
            task.updated_at = now

            if task.attempt_count < task.max_attempts:
                # Requeue for retry
                task.status = A2ATaskStatus.CREATED
                task.claimed_by = None
                task.claimed_at = None
                task.lease_expires_at = None
                new_status = A2ATaskStatus.CREATED

                self._emit_event(
                    task_id=task_id,
                    suite_id=suite_id,
                    event_type=A2AEventType.REQUEUED,
                    actor_type="agent",
                    actor_id=agent_id,
                    details={
                        "error": error,
                        "attempt_count": task.attempt_count,
                        "max_attempts": task.max_attempts,
                    },
                )
            else:
                # Quarantine — max attempts exceeded
                task.status = A2ATaskStatus.QUARANTINED
                new_status = A2ATaskStatus.QUARANTINED

                self._emit_event(
                    task_id=task_id,
                    suite_id=suite_id,
                    event_type=A2AEventType.QUARANTINED,
                    actor_type="system",
                    actor_id=agent_id,
                    details={
                        "error": error,
                        "attempt_count": task.attempt_count,
                        "max_attempts": task.max_attempts,
                        "reason": "max_attempts_exceeded",
                    },
                )

        action_type = "a2a.requeue" if new_status == A2ATaskStatus.CREATED else "a2a.quarantine"
        logger.info(
            "A2A fail: task_id=%s agent=%s new_status=%s error=%s",
            task_id, agent_id, new_status.value, error,
        )

        return A2ACompleteResult(
            success=True,
            task_id=task_id,
            new_status=new_status,
            receipt_data=self._make_receipt_data(
                task_id=task_id,
                suite_id=suite_id,
                office_id=task.office_id,
                correlation_id=task.correlation_id,
                action_type=action_type,
                outcome="failed",
                details={"agent_id": agent_id, "error": error},
            ),
        )

    def get_task(self, task_id: str, suite_id: str) -> A2ATask | None:
        """Get a task by ID, scoped to suite (Law #6)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.suite_id == suite_id:
                return task
            return None

    def list_tasks(
        self,
        *,
        suite_id: str,
        status: A2ATaskStatus | None = None,
        assigned_to_agent: str | None = None,
        limit: int = 50,
    ) -> list[A2ATask]:
        """List tasks for a suite with optional filters."""
        with self._lock:
            results = [t for t in self._tasks.values() if t.suite_id == suite_id]

        if status:
            results = [t for t in results if t.status == status]
        if assigned_to_agent:
            results = [t for t in results if t.assigned_to_agent == assigned_to_agent]

        results.sort(key=lambda t: (t.priority, t.created_at))
        return results[:limit]

    def get_events(self, task_id: str, suite_id: str) -> list[A2ATaskEvent]:
        """Get events for a task, scoped to suite (Law #6)."""
        with self._lock:
            return [
                e for e in self._events
                if e.task_id == task_id and e.suite_id == suite_id
            ]

    def get_task_count(self, suite_id: str | None = None) -> int:
        """Get total task count, optionally filtered by suite_id."""
        with self._lock:
            if suite_id:
                return sum(1 for t in self._tasks.values() if t.suite_id == suite_id)
            return len(self._tasks)

    def clear(self) -> None:
        """Clear all tasks and events. Testing only."""
        with self._lock:
            self._tasks.clear()
            self._events.clear()
            self._idempotency_keys.clear()

    # ─────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────

    def _emit_event(
        self,
        *,
        task_id: str,
        suite_id: str,
        event_type: A2AEventType,
        actor_type: str,
        actor_id: str,
        details: dict[str, Any],
    ) -> None:
        """Emit a task event (append-only audit trail)."""
        event = A2ATaskEvent(
            event_id=str(uuid.uuid4()),
            task_id=task_id,
            suite_id=suite_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            details=details,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._events.append(event)

    @staticmethod
    def _make_receipt_data(
        *,
        task_id: str,
        suite_id: str,
        office_id: str,
        correlation_id: str,
        action_type: str,
        outcome: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        """Create receipt data for a task state change (Law #2)."""
        return {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "actor_type": "system",
            "actor_id": "a2a_service",
            "action_type": action_type,
            "risk_tier": "green",
            "tool_used": "a2a_router",
            "outcome": outcome,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "receipt_hash": "",
            "redacted_inputs": {"task_id": task_id},
            "redacted_outputs": details,
        }


# =============================================================================
# Module-level singleton
# =============================================================================

_service: A2AService | None = None


def get_a2a_service() -> A2AService:
    """Get the singleton A2A service."""
    global _service
    if _service is None:
        _service = A2AService()
    return _service
