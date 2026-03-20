"""SpecialistAgentWorkflow — child workflow for individual agent execution.

Spawned by AgentFanOutWorkflow as a child. Each specialist runs a single
LangGraph turn scoped to one agent's skill pack.

Handles cancellation propagation from parent (Enhancement #2).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributePair

with workflow.unsafe.imports_passed_through():
    from aspire_orchestrator.temporal.config import (
        ACTIVITY_HEARTBEAT_DEFAULT,
        ACTIVITY_START_TO_CLOSE_DEFAULT,
        SEARCH_ATTR_AGENT_ID,
        SEARCH_ATTR_CORRELATION_ID,
        SEARCH_ATTR_OFFICE_ID,
        SEARCH_ATTR_SUITE_ID,
        SEARCH_ATTR_WORKFLOW_KIND,
    )
    from aspire_orchestrator.temporal.models import (
        PersistReceiptsInput,
        RunLangGraphInput,
        SpecialistInput,
        SpecialistOutput,
    )


@workflow.defn
class SpecialistAgentWorkflow:
    """Child workflow for a single specialist agent within a fan-out.

    Runs one LangGraph turn scoped to the agent's skill pack.
    Emits receipts for its execution. Handles cancellation from parent.
    """

    @workflow.run
    async def run(self, input: SpecialistInput) -> SpecialistOutput:
        # Enhancement #9: Search attributes for admin visibility
        workflow.upsert_search_attributes([
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_SUITE_ID), [input.suite_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_AGENT_ID), [input.agent_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_WORKFLOW_KIND), ["specialist_agent"]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_OFFICE_ID), [input.office_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_CORRELATION_ID), [input.correlation_id]),
        ])

        try:
            # Run LangGraph turn scoped to this specialist
            graph_result = await workflow.execute_activity(
                "run_langgraph_turn",
                RunLangGraphInput(
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    actor_id="system",  # System-initiated via fan-out
                    thread_id=f"{input.suite_id}:{input.agent_id}:{input.correlation_id}",
                    correlation_id=input.correlation_id,
                    initial_state=input.input,
                    requested_agent=input.agent_id,
                ),
                start_to_close_timeout=timedelta(seconds=ACTIVITY_START_TO_CLOSE_DEFAULT),
                heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_DEFAULT),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=10),
                    maximum_attempts=2,
                    non_retryable_error_types=["PolicyDeniedError", "SafetyBlockedError"],
                ),
            )

            # Persist receipts for this agent's execution
            receipt_output = await workflow.execute_activity(
                "persist_receipts",
                PersistReceiptsInput(
                    receipts=graph_result.receipts + [{
                        "action": "specialist_agent_completed",
                        "agent_id": input.agent_id,
                        "suite_id": input.suite_id,
                        "correlation_id": input.correlation_id,
                    }],
                    suite_id=input.suite_id,
                    correlation_id=input.correlation_id,
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )

            return SpecialistOutput(
                agent_id=input.agent_id,
                status="completed",
                result=graph_result.response,
                receipt_ids=receipt_output.receipt_ids,
            )

        except Exception as e:
            # Emit failure receipt even on error (Law #2)
            try:
                await workflow.execute_activity(
                    "persist_receipts",
                    PersistReceiptsInput(
                        receipts=[{
                            "action": "specialist_agent_failed",
                            "agent_id": input.agent_id,
                            "error": str(e)[:500],
                            "suite_id": input.suite_id,
                            "correlation_id": input.correlation_id,
                        }],
                        suite_id=input.suite_id,
                        correlation_id=input.correlation_id,
                    ),
                    start_to_close_timeout=timedelta(seconds=5),
                )
            except Exception:
                pass  # Don't fail receipt write on already-failing workflow

            return SpecialistOutput(
                agent_id=input.agent_id,
                status="failed",
                error=str(e)[:500],
            )

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {"agent_id": workflow.info().workflow_id}
