"""ProviderCallbackWorkflow — correlates external webhooks back to active orchestration.

Enhancement #8: Async activity completion for webhook-based providers.
Instead of polling/waiting, the activity saves a task token and raises
CompleteAsync. The webhook handler completes the activity externally.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from aspire_orchestrator.temporal.config import (
        CALLBACK_DEFAULT_TIMEOUT_HOURS,
        SEARCH_ATTR_AGENT_ID,
        SEARCH_ATTR_CORRELATION_ID,
        SEARCH_ATTR_OFFICE_ID,
        SEARCH_ATTR_RISK_TIER,
        SEARCH_ATTR_SUITE_ID,
        SEARCH_ATTR_WORKFLOW_KIND,
        safe_upsert_search_attributes,
    )
    from aspire_orchestrator.temporal.models import (
        CallbackData,
        CallbackInput,
        CallbackOutput,
        PersistReceiptsInput,
    )


@workflow.defn
class ProviderCallbackWorkflow:
    """Correlates external webhooks/callbacks back into active orchestration.

    Wait pattern: workflow sleeps until callback signal arrives or timeout.
    On completion, signals parent workflow.
    """

    def __init__(self) -> None:
        self._callback_received: bool = False
        self._callback_data: CallbackData | None = None

    @workflow.run
    async def run(self, input: CallbackInput) -> CallbackOutput:
        # Enhancement #9: Search attributes
        search_attrs = [
            SearchAttributeKey.for_keyword(SEARCH_ATTR_SUITE_ID).value_set(input.suite_id),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_RISK_TIER).value_set(input.risk_tier),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_WORKFLOW_KIND).value_set("callback"),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_OFFICE_ID).value_set(input.office_id),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_CORRELATION_ID).value_set(input.correlation_id),
        ]
        if input.agent_id:
            search_attrs.append(
                SearchAttributeKey.for_keyword(SEARCH_ATTR_AGENT_ID).value_set(input.agent_id)
            )
        safe_upsert_search_attributes(search_attrs)

        # Wait for external callback
        try:
            await workflow.wait_condition(
                lambda: self._callback_received,
                timeout=timedelta(hours=input.timeout_hours or CALLBACK_DEFAULT_TIMEOUT_HOURS),
            )
        except asyncio.TimeoutError:
            # Emit timeout receipt
            await workflow.execute_activity(
                "persist_receipts",
                PersistReceiptsInput(
                    receipts=[{
                        "action": "callback_timeout",
                        "provider": input.provider,
                        "ref_id": input.ref_id,
                        "timeout_hours": input.timeout_hours,
                        "suite_id": input.suite_id,
                        "correlation_id": input.correlation_id,
                    }],
                    suite_id=input.suite_id,
                    correlation_id=input.correlation_id,
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )
            return CallbackOutput(status="timed_out")

        # Emit completion receipt
        await workflow.execute_activity(
            "persist_receipts",
            PersistReceiptsInput(
                receipts=[{
                    "action": "callback_received",
                    "provider": input.provider,
                    "ref_id": input.ref_id,
                    "event_type": self._callback_data.event_type if self._callback_data else "unknown",
                    "suite_id": input.suite_id,
                    "correlation_id": input.correlation_id,
                }],
                suite_id=input.suite_id,
                correlation_id=input.correlation_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        # Signal parent workflow that callback arrived
        if input.parent_workflow_id:
            parent_handle = workflow.get_external_workflow_handle(input.parent_workflow_id)
            await parent_handle.signal(
                "callback_received",
                self._callback_data.payload if self._callback_data else {},
            )

        return CallbackOutput(
            status="completed",
            data=self._callback_data.payload if self._callback_data else {},
        )

    @workflow.signal
    async def receive_callback(self, data: CallbackData) -> None:
        """Receive webhook data from the callback routing endpoint."""
        self._callback_data = data
        self._callback_received = True

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {
            "received": self._callback_received,
            "provider": self._callback_data.provider if self._callback_data else None,
        }
