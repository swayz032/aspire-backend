"""Workflow execution tracker and Temporal visibility sync.

This module persists workflow lifecycle state into Supabase `workflow_executions`.
The admin portal reads this table as the operator-facing workflow surface.

Design:
- Best-effort persistence: workflow execution never depends on visibility sync
- Works for both direct LangGraph requests and Temporal-backed workflows
- Uses workflow_id as the stable logical execution id
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_TABLE = "workflow_executions"
_MISSING = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_duration_ms(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except Exception:
        return None
    return max((completed - started).total_seconds() * 1000, 0.0)


class WorkflowTracker:
    """Tracks workflow executions in Supabase."""

    def _get_client(self) -> Any | None:
        try:
            from aspire_orchestrator.services.admin_store import _get_supabase

            return _get_supabase()
        except Exception as e:  # pragma: no cover - defensive path
            logger.warning("WorkflowTracker: failed to initialize Supabase client: %s", e)
            return None

    def _ensure_reserved_tenant(self, client: Any, tenant_id: str) -> None:
        if not tenant_id:
            return
        if tenant_id != "system":
            return
        try:
            from aspire_orchestrator.services.admin_store import _ensure_reserved_tenant

            _ensure_reserved_tenant(client, tenant_id)
        except Exception as e:  # pragma: no cover - defensive path
            logger.warning("WorkflowTracker: failed to ensure reserved tenant: %s", e)

    def _fetch_existing(self, client: Any, workflow_id: str) -> dict[str, Any] | None:
        try:
            result = (
                client.table(_TABLE)
                .select("*")
                .eq("workflow_id", workflow_id)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            return rows[0] if rows else None
        except Exception as e:
            logger.warning("WorkflowTracker: failed to fetch workflow %s: %s", workflow_id, e)
            return None

    def sync_workflow(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workflow_type: str = "intent",
        status: str = "running",
        correlation_id: str | None = None,
        run_id: str | None = None,
        parent_workflow_id: str | None = None,
        thread_id: str | None = None,
        current_agent: str | None = None,
        current_wait_type: str | None | object = _MISSING,
        input_summary: dict[str, Any] | None | object = _MISSING,
        output_summary: dict[str, Any] | None | object = _MISSING,
        latest_response: dict[str, Any] | None | object = _MISSING,
        metadata: dict[str, Any] | None = None,
        error_type: str | None | object = _MISSING,
        error_message: str | None | object = _MISSING,
        started_at: str | None = None,
        completed_at: str | None | object = _MISSING,
    ) -> str:
        """Insert or update a workflow execution row."""
        safe_tenant_id = str(tenant_id or "").strip() or "system"
        metadata = metadata or {}

        client = self._get_client()
        if not client:
            logger.debug("WorkflowTracker: no Supabase client; workflow %s not persisted", workflow_id)
            return workflow_id

        self._ensure_reserved_tenant(client, safe_tenant_id)

        existing = self._fetch_existing(client, workflow_id)
        if existing:
            update_payload: dict[str, Any] = {
                "tenant_id": safe_tenant_id,
                "workflow_type": workflow_type,
                "status": status,
                "updated_at": _now_iso(),
            }
            if correlation_id is not None:
                update_payload["correlation_id"] = correlation_id
            if run_id is not None:
                update_payload["run_id"] = run_id
            if parent_workflow_id is not None:
                update_payload["parent_workflow_id"] = parent_workflow_id
            if thread_id is not None:
                update_payload["thread_id"] = thread_id
            if current_agent is not None:
                update_payload["current_agent"] = current_agent
            if current_wait_type is not _MISSING:
                update_payload["current_wait_type"] = current_wait_type
            if input_summary is not _MISSING:
                update_payload["input_summary"] = input_summary or {}
            if output_summary is not _MISSING:
                update_payload["output_summary"] = output_summary or {}
            if latest_response is not _MISSING:
                update_payload["latest_response"] = latest_response or {}
            if error_type is not _MISSING:
                update_payload["error_type"] = error_type
            if error_message is not _MISSING:
                update_payload["error_message"] = str(error_message or "")[:500] or None
            if completed_at is not _MISSING:
                update_payload["completed_at"] = completed_at

            merged_metadata = {
                **(existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}),
                **metadata,
            }
            update_payload["metadata"] = merged_metadata

            if started_at and not existing.get("started_at"):
                update_payload["started_at"] = started_at

            duration_ms = _safe_duration_ms(
                str(existing.get("started_at") or started_at or ""),
                str(
                    update_payload.get("completed_at")
                    or existing.get("completed_at")
                    or ""
                ),
            )
            if duration_ms is not None:
                update_payload["duration_ms"] = duration_ms

            try:
                client.table(_TABLE).update(update_payload).eq("workflow_id", workflow_id).execute()
            except Exception as e:
                logger.warning("WorkflowTracker: failed to update workflow %s: %s", workflow_id, e)
            return workflow_id

        insert_payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "tenant_id": safe_tenant_id,
            "workflow_type": workflow_type,
            "status": status,
            "correlation_id": correlation_id or "",
            "run_id": run_id,
            "parent_workflow_id": parent_workflow_id,
            "thread_id": thread_id,
            "current_agent": current_agent,
            "current_wait_type": None if current_wait_type is _MISSING else current_wait_type,
            "input_summary": {} if input_summary is _MISSING else (input_summary or {}),
            "output_summary": {} if output_summary is _MISSING else (output_summary or {}),
            "latest_response": {} if latest_response is _MISSING else (latest_response or {}),
            "metadata": metadata,
            "error_type": None if error_type is _MISSING else error_type,
            "error_message": None if error_message is _MISSING else str(error_message or "")[:500] or None,
            "started_at": started_at or _now_iso(),
            "completed_at": None if completed_at is _MISSING else completed_at,
        }

        duration_ms = _safe_duration_ms(
            str(insert_payload.get("started_at") or ""),
            str(insert_payload.get("completed_at") or ""),
        )
        if duration_ms is not None:
            insert_payload["duration_ms"] = duration_ms

        try:
            client.table(_TABLE).insert(insert_payload).execute()
        except Exception as e:
            logger.warning("WorkflowTracker: failed to insert workflow %s: %s", workflow_id, e)

        return workflow_id

    def start_workflow(
        self,
        *,
        tenant_id: str,
        workflow_type: str = "intent",
        correlation_id: str | None = None,
        input_summary: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
        parent_workflow_id: str | None = None,
        thread_id: str | None = None,
        current_agent: str | None = None,
    ) -> str:
        """Record workflow start and return workflow_id."""
        workflow_id = workflow_id or str(uuid.uuid4())
        return self.sync_workflow(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workflow_type=workflow_type,
            status="running",
            correlation_id=correlation_id,
            run_id=run_id,
            parent_workflow_id=parent_workflow_id,
            thread_id=thread_id,
            current_agent=current_agent,
            input_summary=input_summary or {},
            metadata=metadata or {},
            started_at=_now_iso(),
        )

    def update_workflow(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        status: str = "running",
        workflow_type: str = "intent",
        correlation_id: str | None = None,
        run_id: str | None = None,
        parent_workflow_id: str | None = None,
        thread_id: str | None = None,
        current_agent: str | None = None,
        current_wait_type: str | None | object = _MISSING,
        input_summary: dict[str, Any] | None | object = _MISSING,
        output_summary: dict[str, Any] | None | object = _MISSING,
        latest_response: dict[str, Any] | None | object = _MISSING,
        metadata: dict[str, Any] | None = None,
        error_type: str | None | object = _MISSING,
        error_message: str | None | object = _MISSING,
        completed_at: str | None | object = _MISSING,
    ) -> str:
        """Update workflow state without changing the logical workflow id."""
        return self.sync_workflow(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workflow_type=workflow_type,
            status=status,
            correlation_id=correlation_id,
            run_id=run_id,
            parent_workflow_id=parent_workflow_id,
            thread_id=thread_id,
            current_agent=current_agent,
            current_wait_type=current_wait_type,
            input_summary=input_summary,
            output_summary=output_summary,
            latest_response=latest_response,
            metadata=metadata or {},
            error_type=error_type,
            error_message=error_message,
            completed_at=completed_at,
        )

    def complete_workflow(
        self,
        *,
        workflow_id: str,
        tenant_id: str = "system",
        output_summary: dict[str, Any] | None = None,
        latest_response: dict[str, Any] | None | object = _MISSING,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record workflow completion."""
        self.update_workflow(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            status="completed",
            output_summary=output_summary or {},
            latest_response=latest_response,
            metadata=metadata or {},
            current_wait_type=None,
            completed_at=_now_iso(),
        )

    def fail_workflow(
        self,
        *,
        workflow_id: str,
        tenant_id: str = "system",
        error_type: str = "unknown",
        error_message: str = "",
        latest_response: dict[str, Any] | None | object = _MISSING,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record workflow failure."""
        self.update_workflow(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            status="failed",
            error_type=error_type,
            error_message=error_message,
            latest_response=latest_response,
            metadata=metadata or {},
            current_wait_type=None,
            completed_at=_now_iso(),
        )


_tracker: WorkflowTracker | None = None


def get_workflow_tracker() -> WorkflowTracker:
    """Get singleton WorkflowTracker."""
    global _tracker
    if _tracker is None:
        _tracker = WorkflowTracker()
    return _tracker

