"""Sync activity — sync Temporal workflow state to Supabase workflow_executions.

Bridges Temporal's internal state to the Supabase visibility layer
so admin portal can display workflow status without querying Temporal directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from temporalio import activity

from aspire_orchestrator.temporal.models import SyncWorkflowInput

logger = logging.getLogger(__name__)


@activity.defn
async def sync_workflow_execution(input: SyncWorkflowInput) -> None:
    """Upsert workflow state into workflow_executions table."""
    from aspire_orchestrator.services.supabase_client import supabase_rpc

    try:
        await supabase_rpc(
            "upsert_workflow_execution",
            {
                "p_workflow_id": input.workflow_id,
                "p_temporal_run_id": input.temporal_run_id,
                "p_suite_id": input.suite_id,
                "p_office_id": input.office_id,
                "p_correlation_id": input.correlation_id,
                "p_status": input.status,
                "p_workflow_kind": input.workflow_kind,
                "p_current_wait_type": input.current_wait_type,
                "p_current_agent": input.current_agent,
                "p_thread_id": input.thread_id,
                "p_approval_id": input.approval_id,
                "p_outbox_job_id": input.outbox_job_id,
                "p_parent_workflow_id": input.parent_workflow_id,
                "p_latest_response": input.latest_response or {},
                "p_updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        logger.info(
            "Synced workflow execution: wf_id=%s status=%s kind=%s",
            input.workflow_id,
            input.status,
            input.workflow_kind,
        )

    except Exception:
        logger.exception(
            "Workflow sync failed: wf_id=%s status=%s",
            input.workflow_id,
            input.status,
        )
        raise
