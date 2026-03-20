"""LangGraph activity — wraps invoke_orchestrator_graph for Temporal.

Enhancement #3: Heartbeat calls before/after graph invocation so Temporal
can distinguish "slow" from "dead" activities.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity

from aspire_orchestrator.temporal.models import RunLangGraphInput, RunLangGraphOutput

logger = logging.getLogger(__name__)


@activity.defn
async def run_langgraph_turn(input: RunLangGraphInput) -> RunLangGraphOutput:
    """Run a single LangGraph turn as a Temporal activity.

    This wraps the existing invoke_orchestrator_graph() function.
    The graph handles all reasoning (Law #1: Single Brain).
    This activity is a bounded command (Law #7: Tools Are Hands).
    """
    from aspire_orchestrator.services.orchestrator_runtime import (
        invoke_orchestrator_graph,
    )

    # Enhancement #3: Heartbeat before graph invocation
    activity.heartbeat({"phase": "graph_init", "thread_id": input.thread_id})

    try:
        initial_state: dict[str, Any] = {
            **input.initial_state,
            "suite_id": input.suite_id,
            "office_id": input.office_id,
            "actor_id": input.actor_id,
            "correlation_id": input.correlation_id,
            "requested_agent": input.requested_agent,
        }

        # Inject approval evidence if resuming after approval
        if input.approval_evidence:
            initial_state["approval_evidence"] = input.approval_evidence

        result = await invoke_orchestrator_graph(
            initial_state,
            thread_id=input.thread_id,
        )

        # Enhancement #3: Heartbeat after graph completion
        activity.heartbeat({"phase": "graph_complete", "thread_id": input.thread_id})

        # Extract structured output from graph result
        response = result if isinstance(result, dict) else {"raw": str(result)}
        receipts = response.pop("_receipts", [])
        requires_approval = response.pop("_requires_approval", False)
        approval_id = response.pop("_approval_id", None)
        approval_payload_hash = response.pop("_approval_payload_hash", None)
        requires_presence = response.pop("_requires_presence", False)
        presence_token = response.pop("_presence_token", None)
        current_agent = response.pop("_current_agent", None)

        return RunLangGraphOutput(
            response=response,
            receipts=receipts if isinstance(receipts, list) else [],
            requires_approval=bool(requires_approval),
            approval_id=approval_id,
            approval_payload_hash=approval_payload_hash,
            requires_presence=bool(requires_presence),
            presence_token=presence_token,
            current_agent=current_agent,
        )

    except Exception as e:
        activity.heartbeat({"phase": "graph_error", "error": str(e)[:200]})
        logger.exception(
            "LangGraph turn failed: thread_id=%s correlation_id=%s",
            input.thread_id,
            input.correlation_id,
        )
        raise
