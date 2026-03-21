"""AgentFanOutWorkflow — parent/child workflows for multi-agent coordination.

Enhancement #2: Cancellation scopes with WAIT_CANCELLATION_COMPLETED.
On parent SLA timeout, explicitly cancel all child workflows and wait
for cancellation to complete. Prevents orphaned child workflows.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from aspire_orchestrator.temporal.config import (
        FANOUT_SLA_TIMEOUT_MINUTES,
        QUEUE_BACKGROUND,
        SEARCH_ATTR_CORRELATION_ID,
        SEARCH_ATTR_OFFICE_ID,
        SEARCH_ATTR_SUITE_ID,
        SEARCH_ATTR_WORKFLOW_KIND,
        safe_upsert_search_attributes,
        workflow_id_agent,
    )
    from aspire_orchestrator.temporal.models import (
        AgentResult,
        AgentTask,
        EmitClientEventInput,
        FanOutInput,
        FanOutOutput,
        PersistReceiptsInput,
        SpecialistInput,
        SpecialistOutput,
    )
    from aspire_orchestrator.temporal.workflows.specialist_agent import (
        SpecialistAgentWorkflow,
    )


@workflow.defn
class AgentFanOutWorkflow:
    """Parent workflow for multi-agent coordination with SLA supervision.

    Enhancement #2: Explicit child cancellation on SLA timeout with
    WAIT_CANCELLATION_COMPLETED to prevent orphaned workflows.
    """

    def __init__(self) -> None:
        self._child_statuses: dict[str, str] = {}

    @workflow.run
    async def run(self, input: FanOutInput) -> FanOutOutput:
        # Enhancement #9: Search attributes
        safe_upsert_search_attributes([
            SearchAttributeKey.for_keyword(SEARCH_ATTR_SUITE_ID).value_set(input.suite_id),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_WORKFLOW_KIND).value_set("agent_fanout"),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_OFFICE_ID).value_set(input.office_id),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_CORRELATION_ID).value_set(input.correlation_id),
        ])

        if not input.agent_tasks:
            return FanOutOutput(results=[], partial=False)

        # Start child workflows for each specialist
        # Enhancement #2: WAIT_CANCELLATION_COMPLETED on child start
        child_handles: list[tuple[str, workflow.ChildWorkflowHandle]] = []

        for task in input.agent_tasks:
            child_id = workflow_id_agent(
                input.suite_id, task.agent_id, input.correlation_id
            )
            handle = await workflow.start_child_workflow(
                SpecialistAgentWorkflow.run,
                SpecialistInput(
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    correlation_id=input.correlation_id,
                    agent_id=task.agent_id,
                    skill_pack=task.skill_pack,
                    input=task.input,
                ),
                id=child_id,
                task_queue=QUEUE_BACKGROUND,
                execution_timeout=timedelta(minutes=task.timeout_minutes),
                cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            child_handles.append((task.agent_id, handle))
            self._child_statuses[task.agent_id] = "running"

        # Wait for all children with overall SLA timeout
        sla_timeout = timedelta(
            minutes=input.sla_timeout_minutes or FANOUT_SLA_TIMEOUT_MINUTES
        )

        results: list[AgentResult] = []
        partial = False

        # Gather child results concurrently
        # Each child has its own execution_timeout; the parent SLA covers the whole fan-out
        all_done = False

        async def _collect_results() -> None:
            nonlocal all_done
            child_results = await asyncio.gather(
                *[h.result() for _, h in child_handles],
                return_exceptions=True,
            )
            for i, (agent_id, _) in enumerate(child_handles):
                child_result = child_results[i]
                if isinstance(child_result, Exception):
                    results.append(AgentResult(
                        agent_id=agent_id,
                        status="failed",
                        error=str(child_result)[:200],
                    ))
                    self._child_statuses[agent_id] = "failed"
                else:
                    # child_result is SpecialistOutput
                    results.append(AgentResult(
                        agent_id=agent_id,
                        status=child_result.status if hasattr(child_result, "status") else "completed",
                        result=child_result.result if hasattr(child_result, "result") else {},
                    ))
                    self._child_statuses[agent_id] = "completed"
            all_done = True

        # Start collection task
        collector = asyncio.ensure_future(_collect_results())

        # Wait with SLA timeout (Temporal-safe: workflow.wait_condition)
        try:
            await workflow.wait_condition(
                lambda: all_done,
                timeout=sla_timeout,
            )
        except asyncio.TimeoutError:
            # Enhancement #2: SLA timeout — cancel all running children
            partial = True

            for agent_id, handle in child_handles:
                if self._child_statuses.get(agent_id) in ("completed", "failed"):
                    continue

                # Cancel running children
                try:
                    handle.cancel()
                    self._child_statuses[agent_id] = "cancelled"
                except Exception:
                    self._child_statuses[agent_id] = "cancel_failed"

                results.append(AgentResult(
                    agent_id=agent_id,
                    status="cancelled",
                    error="SLA timeout exceeded",
                ))

        # Check if any children failed
        if not partial:
            partial = any(r.status == "failed" for r in results)

        # Emit receipt for fan-out completion
        await workflow.execute_activity(
            "persist_receipts",
            PersistReceiptsInput(
                receipts=[{
                    "action": "agent_fanout_complete",
                    "total_agents": len(input.agent_tasks),
                    "completed": sum(1 for r in results if r.status == "completed"),
                    "failed": sum(1 for r in results if r.status == "failed"),
                    "cancelled": sum(1 for r in results if r.status == "cancelled"),
                    "partial": partial,
                    "suite_id": input.suite_id,
                    "correlation_id": input.correlation_id,
                }],
                suite_id=input.suite_id,
                correlation_id=input.correlation_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        return FanOutOutput(results=results, partial=partial)

    @workflow.query
    def get_child_statuses(self) -> dict[str, str]:
        return self._child_statuses
