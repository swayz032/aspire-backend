"""Workflow execution tracker (Phase 6 — Temporal visibility proxy).

Records workflow execution lifecycle to Supabase `workflow_executions` table.
Called by the LangGraph orchestrator at workflow start/completion/failure.

When Temporal is deployed, this same table will be synced from Temporal's
visibility API. Until then, the orchestrator writes directly.

Design:
- Best-effort persistence (never blocks the actual workflow)
- Falls back gracefully when Supabase unavailable
- Singleton via get_workflow_tracker()
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Reuse admin_store's Supabase client
_TABLE = "workflow_executions"


class WorkflowTracker:
    """Tracks workflow executions in Supabase."""

    def start_workflow(
        self,
        *,
        tenant_id: str,
        workflow_type: str = "intent",
        correlation_id: str | None = None,
        input_summary: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record workflow start. Returns workflow_id."""
        workflow_id = str(uuid.uuid4())
        row = {
            "workflow_id": workflow_id,
            "tenant_id": tenant_id,
            "workflow_type": workflow_type,
            "status": "running",
            "correlation_id": correlation_id or "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "input_summary": input_summary or {},
            "metadata": metadata or {},
        }

        try:
            from aspire_orchestrator.services.admin_store import _get_supabase
            client = _get_supabase()
            if client:
                client.table(_TABLE).insert(row).execute()
                logger.debug("WorkflowTracker: started %s", workflow_id)
            else:
                logger.debug("WorkflowTracker: no Supabase — workflow %s not persisted", workflow_id)
        except Exception as e:
            logger.warning("WorkflowTracker: failed to record start: %s", e)

        return workflow_id

    def complete_workflow(
        self,
        *,
        workflow_id: str,
        output_summary: dict[str, Any] | None = None,
    ) -> None:
        """Record workflow completion."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            from aspire_orchestrator.services.admin_store import _get_supabase
            client = _get_supabase()
            if client:
                client.table(_TABLE).update({
                    "status": "completed",
                    "completed_at": now,
                    "output_summary": output_summary or {},
                }).eq("workflow_id", workflow_id).execute()
        except Exception as e:
            logger.warning("WorkflowTracker: failed to record completion: %s", e)

    def fail_workflow(
        self,
        *,
        workflow_id: str,
        error_type: str = "unknown",
        error_message: str = "",
    ) -> None:
        """Record workflow failure."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            from aspire_orchestrator.services.admin_store import _get_supabase
            client = _get_supabase()
            if client:
                client.table(_TABLE).update({
                    "status": "failed",
                    "completed_at": now,
                    "error_type": error_type,
                    "error_message": error_message[:500],
                }).eq("workflow_id", workflow_id).execute()
        except Exception as e:
            logger.warning("WorkflowTracker: failed to record failure: %s", e)


# Singleton
_tracker: WorkflowTracker | None = None


def get_workflow_tracker() -> WorkflowTracker:
    """Get singleton WorkflowTracker."""
    global _tracker
    if _tracker is None:
        _tracker = WorkflowTracker()
    return _tracker
