"""A2A service with durable Supabase backend and in-memory dev fallback."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.supabase_client import _get_sync_pool

logger = logging.getLogger(__name__)


class A2ATaskStatus(str, Enum):
    CREATED = "created"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    CANCELED = "canceled"


class A2AEventType(str, Enum):
    """Event types emitted during A2A task lifecycle."""
    CREATED = "created"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class A2ATask:
    task_id: str
    suite_id: str
    office_id: str
    correlation_id: str
    task_type: str
    assigned_to_agent: str
    status: A2ATaskStatus
    priority: int
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


@dataclass(frozen=True)
class A2ADispatchResult:
    success: bool
    task_id: str | None = None
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class A2AClaimResult:
    success: bool
    task: A2ATask | None = None
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class A2ACompleteResult:
    success: bool
    task_id: str | None = None
    new_status: A2ATaskStatus | None = None
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt(*, task_id: str, suite_id: str, office_id: str, correlation_id: str, action_type: str, outcome: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "suite_id": suite_id,
        "office_id": office_id,
        "correlation_id": correlation_id,
        "action_type": action_type,
        "risk_tier": "yellow",
        "tool_used": "a2a_service",
        "outcome": outcome,
        "created_at": _now(),
        "receipt_type": "a2a",
        "details": details | {"task_id": task_id},
    }


def _parse_status(raw: str | None) -> A2ATaskStatus:
    try:
        return A2ATaskStatus((raw or "").strip().lower())
    except Exception:
        return A2ATaskStatus.CREATED


def _uuid_or_none(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except Exception:
        return None


class A2AService:
    def __init__(self, *, default_lease_seconds: int = 300, max_attempts: int = 3):
        self._lock = threading.Lock()
        self._tasks: dict[str, A2ATask] = {}
        self._idempotency_keys: set[str] = set()
        self._default_lease_seconds = default_lease_seconds
        self._max_attempts = max_attempts
        self.backend = "memory"
        if settings.supabase_url and settings.supabase_service_role_key:
            self.backend = "supabase"

    def _should_fallback_to_memory(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "pgrst202" in msg
            or "could not find the function" in msg
            or "schema cache" in msg
        )

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
        if self.backend == "supabase":
            return self._dispatch_supabase(
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                task_type=task_type,
                assigned_to_agent=assigned_to_agent,
                payload=payload,
                priority=priority,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
            )

        now = _now()
        with self._lock:
            if idempotency_key and idempotency_key in self._idempotency_keys:
                existing = next((t for t in self._tasks.values() if t.idempotency_key == idempotency_key and t.suite_id == suite_id), None)
                if existing:
                    return A2ADispatchResult(
                        success=True,
                        task_id=existing.task_id,
                        receipt_data=_receipt(
                            task_id=existing.task_id,
                            suite_id=suite_id,
                            office_id=office_id,
                            correlation_id=correlation_id,
                            action_type="a2a.dispatch.idempotent",
                            outcome="success",
                            details={"idempotency_key": idempotency_key},
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
        return A2ADispatchResult(
            success=True,
            task_id=task_id,
            receipt_data=_receipt(
                task_id=task_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                action_type="a2a.dispatch",
                outcome="success",
                details={"task_type": task_type, "assigned_to_agent": assigned_to_agent, "actor_id": actor_id},
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
        if self.backend == "supabase":
            return self._claim_supabase(
                agent_id=agent_id,
                suite_id=suite_id,
                max_tasks=max_tasks,
                lease_seconds=lease_seconds,
            )

        now = datetime.now(timezone.utc)
        lease = lease_seconds or self._default_lease_seconds
        with self._lock:
            candidates = [
                t for t in self._tasks.values()
                if t.suite_id == suite_id
                and t.status == A2ATaskStatus.CREATED
                and (task_types is None or t.task_type in task_types)
            ]
            candidates.sort(key=lambda t: (t.priority, t.created_at))
            if not candidates:
                return A2AClaimResult(success=False, error="No tasks available for claiming")
            task = candidates[0]
            task.status = A2ATaskStatus.CLAIMED
            task.claimed_by = agent_id
            task.claimed_at = now.isoformat()
            task.lease_expires_at = (now + timedelta(seconds=lease)).isoformat()
            task.attempt_count += 1
            task.updated_at = now.isoformat()
        return A2AClaimResult(
            success=True,
            task=task,
            receipt_data=_receipt(
                task_id=task.task_id,
                suite_id=suite_id,
                office_id=task.office_id,
                correlation_id=task.correlation_id,
                action_type="a2a.claim",
                outcome="success",
                details={"agent_id": agent_id},
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
        if self.backend == "supabase":
            return self._complete_supabase(task_id=task_id, agent_id=agent_id, suite_id=suite_id, result=result)

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return A2ACompleteResult(success=False, error="TASK_NOT_FOUND")
            if task.suite_id != suite_id:
                return A2ACompleteResult(success=False, error="TENANT_ISOLATION_VIOLATION")
            if task.claimed_by and task.claimed_by != agent_id:
                return A2ACompleteResult(success=False, error="TASK_CLAIMED_BY_OTHER_AGENT")
            if task.status not in (A2ATaskStatus.CLAIMED, A2ATaskStatus.IN_PROGRESS):
                return A2ACompleteResult(success=False, error=f"Invalid task status: {task.status.value}")
            task.status = A2ATaskStatus.DONE
            task.updated_at = _now()
            task.result = result
        return A2ACompleteResult(
            success=True,
            task_id=task_id,
            new_status=A2ATaskStatus.DONE,
            receipt_data=_receipt(
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
        if self.backend == "supabase":
            return self._fail_supabase(task_id=task_id, agent_id=agent_id, suite_id=suite_id, error=error)

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return A2ACompleteResult(success=False, error="TASK_NOT_FOUND")
            if task.suite_id != suite_id:
                return A2ACompleteResult(success=False, error="TENANT_ISOLATION_VIOLATION")
            if task.claimed_by and task.claimed_by != agent_id:
                return A2ACompleteResult(success=False, error="TASK_CLAIMED_BY_OTHER_AGENT")
            task.status = A2ATaskStatus.FAILED
            task.error = error
            task.updated_at = _now()
            if task.attempt_count >= task.max_attempts:
                task.status = A2ATaskStatus.QUARANTINED
        return A2ACompleteResult(
            success=True,
            task_id=task_id,
            new_status=task.status,
            receipt_data=_receipt(
                task_id=task_id,
                suite_id=suite_id,
                office_id=task.office_id,
                correlation_id=task.correlation_id,
                action_type="a2a.fail",
                outcome="success",
                details={"agent_id": agent_id, "error": error},
            ),
        )

    def list_tasks(
        self,
        *,
        suite_id: str,
        status: A2ATaskStatus | None = None,
        assigned_to_agent: str | None = None,
        limit: int = 50,
    ) -> list[A2ATask]:
        if self.backend == "supabase":
            return self._list_tasks_supabase(suite_id=suite_id, status=status, assigned_to_agent=assigned_to_agent, limit=limit)

        with self._lock:
            tasks = [t for t in self._tasks.values() if t.suite_id == suite_id]
        if status:
            tasks = [t for t in tasks if t.status == status]
        if assigned_to_agent:
            tasks = [t for t in tasks if t.assigned_to_agent == assigned_to_agent]
        tasks.sort(key=lambda x: x.created_at, reverse=True)
        return tasks[:limit]

    # -------- durable backend helpers --------
    def _dispatch_supabase(self, *, suite_id: str, office_id: str, correlation_id: str, task_type: str, assigned_to_agent: str, payload: dict[str, Any], priority: int, idempotency_key: str | None, actor_id: str) -> A2ADispatchResult:
        key = idempotency_key or f"{correlation_id}:{assigned_to_agent}:{task_type}"
        params = {
            "p_suite_id": suite_id,
            "p_created_by_office_id": _uuid_or_none(office_id),
            "p_assigned_to_agent": assigned_to_agent,
            "p_task_type": task_type,
            "p_payload": payload,
            "p_priority": priority,
            "p_idempotency_key": key,
            "p_requires_approval": False,
            "p_approval_id": None,
            "p_assigned_to_office_id": None,
        }
        try:
            # Canonical RPC name from trust-spine migrations is app.create_a2a_task.
            # _rpc_sync already tries schema-qualified + unqualified variants.
            row = self._rpc_sync("app.create_a2a_task", params)
            task_id = str(row.get("task_id"))
            return A2ADispatchResult(
                success=True,
                task_id=task_id,
                receipt_data=_receipt(
                    task_id=task_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    correlation_id=correlation_id,
                    action_type="a2a.dispatch",
                    outcome="success",
                    details={"task_type": task_type, "assigned_to_agent": assigned_to_agent, "actor_id": actor_id},
                ),
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "duplicate key" in msg or "unique" in msg:
                existing = self._fetch_task_by_idempotency(suite_id=suite_id, key=key)
                if existing:
                    return A2ADispatchResult(success=True, task_id=existing, receipt_data={})
            if self._should_fallback_to_memory(exc):
                logger.warning("A2A supabase RPC unavailable; switching to in-memory fallback for process lifetime: %s", exc)
                self.backend = "memory"
                return self.dispatch(
                    suite_id=suite_id,
                    office_id=office_id,
                    correlation_id=correlation_id,
                    task_type=task_type,
                    assigned_to_agent=assigned_to_agent,
                    payload=payload,
                    priority=priority,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                )
            return A2ADispatchResult(success=False, error=f"A2A_DISPATCH_FAILED: {exc}")

    def _claim_supabase(self, *, agent_id: str, suite_id: str, max_tasks: int, lease_seconds: int | None) -> A2AClaimResult:
        try:
            rows = self._rpc_sync(
                "app.claim_a2a_tasks",
                {
                    "p_suite_id": suite_id,
                    "p_assigned_to_agent": agent_id,
                    "p_limit": max_tasks,
                    "p_lease_seconds": lease_seconds or self._default_lease_seconds,
                    "p_claimed_by": agent_id,
                    "p_actor_office_id": None,
                    "p_include_shared": True,
                },
            )
            if not rows:
                return A2AClaimResult(success=False, error="No tasks available for claiming")
            row = rows[0] if isinstance(rows, list) else rows
            task = self._to_task(row)
            return A2AClaimResult(success=True, task=task, receipt_data={})
        except Exception as exc:
            if self._should_fallback_to_memory(exc):
                logger.warning("A2A claim RPC unavailable; switching to in-memory fallback for process lifetime: %s", exc)
                self.backend = "memory"
                return self.claim(
                    agent_id=agent_id,
                    suite_id=suite_id,
                    task_types=None,
                    max_tasks=max_tasks,
                    lease_seconds=lease_seconds,
                )
            return A2AClaimResult(success=False, error=f"A2A_CLAIM_FAILED: {exc}")

    def _complete_supabase(self, *, task_id: str, agent_id: str, suite_id: str, result: dict[str, Any] | None) -> A2ACompleteResult:
        try:
            row = self._rpc_sync(
                "app.complete_a2a_task",
                {
                    "p_task_id": task_id,
                    "p_suite_id": suite_id,
                    "p_actor_id": agent_id,
                    "p_details": result or {},
                    "p_actor_office_id": None,
                },
            )
            task = self._to_task(row)
            return A2ACompleteResult(success=True, task_id=task.task_id, new_status=task.status, receipt_data={})
        except Exception as exc:
            if self._should_fallback_to_memory(exc):
                logger.warning("A2A complete RPC unavailable; switching to in-memory fallback for process lifetime: %s", exc)
                self.backend = "memory"
                return self.complete(task_id=task_id, agent_id=agent_id, suite_id=suite_id, result=result)
            msg = str(exc)
            if "invalid state" in msg.lower():
                return A2ACompleteResult(success=False, error="Invalid task status")
            return A2ACompleteResult(success=False, error=f"A2A_COMPLETE_FAILED: {exc}")

    def _fail_supabase(self, *, task_id: str, agent_id: str, suite_id: str, error: str) -> A2ACompleteResult:
        try:
            row = self._rpc_sync(
                "app.fail_a2a_task",
                {
                    "p_task_id": task_id,
                    "p_suite_id": suite_id,
                    "p_actor_id": agent_id,
                    "p_error": error,
                    "p_details": {},
                    "p_actor_office_id": None,
                },
            )
            task = self._to_task(row)
            return A2ACompleteResult(success=True, task_id=task.task_id, new_status=task.status, receipt_data={})
        except Exception as exc:
            if self._should_fallback_to_memory(exc):
                logger.warning("A2A fail RPC unavailable; switching to in-memory fallback for process lifetime: %s", exc)
                self.backend = "memory"
                return self.fail(task_id=task_id, agent_id=agent_id, suite_id=suite_id, error=error)
            return A2ACompleteResult(success=False, error=f"A2A_FAIL_FAILED: {exc}")

    def _list_tasks_supabase(self, *, suite_id: str, status: A2ATaskStatus | None, assigned_to_agent: str | None, limit: int) -> list[A2ATask]:
        try:
            query = "select=task_id,suite_id,assigned_to_agent,status,priority,payload,idempotency_key,created_at,updated_at,claimed_by,claimed_at,lease_expires_at,attempt_count"
            query += f"&suite_id=eq.{suite_id}"
            if status:
                query += f"&status=eq.{status.value}"
            if assigned_to_agent:
                query += f"&assigned_to_agent=eq.{assigned_to_agent}"
            query += f"&order=created_at.desc&limit={limit}"
            rows = self._select_sync("a2a_tasks", query)
            return [self._to_task(r) for r in rows]
        except Exception:
            return []

    def _to_task(self, row: dict[str, Any]) -> A2ATask:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        return A2ATask(
            task_id=str(row.get("task_id") or row.get("id")),
            suite_id=str(row.get("suite_id") or ""),
            office_id=str(payload.get("office_id") or row.get("created_by_office_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
            task_type=str(row.get("task_type") or ""),
            assigned_to_agent=str(row.get("assigned_to_agent") or ""),
            status=_parse_status(str(row.get("status") or "")),
            priority=int(row.get("priority") or 0),
            payload=payload if isinstance(payload, dict) else {},
            idempotency_key=row.get("idempotency_key"),
            created_at=str(row.get("created_at") or _now()),
            updated_at=str(row.get("updated_at") or _now()),
            claimed_by=row.get("claimed_by"),
            claimed_at=row.get("claimed_at"),
            lease_expires_at=row.get("lease_expires_at"),
            attempt_count=int(row.get("attempt_count") or 0),
            max_attempts=3,
        )

    def _fetch_task_by_idempotency(self, *, suite_id: str, key: str) -> str | None:
        rows = self._select_sync(
            "a2a_tasks",
            f"select=task_id&suite_id=eq.{suite_id}&idempotency_key=eq.{key}&limit=1",
        )
        if rows:
            return str(rows[0].get("task_id"))
        return None

    def _rpc_sync(self, fn_name: str, params: dict[str, Any]) -> Any:
        base = settings.supabase_url.rstrip("/")
        headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        attempts = [fn_name]
        if fn_name.startswith("app."):
            attempts.append(fn_name.split(".", 1)[1])
        client = _get_sync_pool()
        last_error = ""
        for name in attempts:
            url = f"{base}/rest/v1/rpc/{name}"
            resp = client.post(url, json=params, headers=headers, timeout=8.0)
            if resp.status_code < 400:
                return resp.json()
            last_error = resp.text
        raise RuntimeError(last_error or f"RPC failed: {fn_name}")

    def _select_sync(self, table: str, query: str) -> list[dict[str, Any]]:
        base = settings.supabase_url.rstrip("/")
        url = f"{base}/rest/v1/{table}?{query}"
        headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }
        client = _get_sync_pool()
        resp = client.get(url, headers=headers, timeout=8.0)
        if resp.status_code >= 400:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


_service: A2AService | None = None


def get_a2a_service(*, reload: bool = False) -> A2AService:
    global _service
    if _service is None or reload:
        _service = A2AService()
    return _service
