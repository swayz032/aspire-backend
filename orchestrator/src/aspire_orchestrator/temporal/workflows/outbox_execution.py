"""OutboxExecutionWorkflow — RED-tier job lifecycle with saga compensation.

Claim → execute steps → receipt → notify.

Enhancement #10: Provider-specific retry policies.
Enhancement #11: Saga compensation — on step failure, compensate completed steps in reverse.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aspire_orchestrator.temporal.config import (
        ACTIVITY_HEARTBEAT_DEFAULT,
        SEARCH_ATTR_CORRELATION_ID,
        SEARCH_ATTR_OFFICE_ID,
        SEARCH_ATTR_RISK_TIER,
        SEARCH_ATTR_SUITE_ID,
        SEARCH_ATTR_WORKFLOW_KIND,
        safe_upsert_search_attributes,
    )
    from aspire_orchestrator.temporal.models import (
        ClaimJobInput,
        CompensationAction,
        CompletedStep,
        CompleteJobInput,
        EmitClientEventInput,
        FailJobInput,
        OutboxJobInput,
        OutboxJobOutput,
        PersistReceiptsInput,
        ProviderCallInput,
        ProviderCallOutput,
        SyncWorkflowInput,
    )
    from aspire_orchestrator.temporal.retry_policies import (
        NON_RETRYABLE_ERROR_TYPES,
        get_retry_policy,
    )


@workflow.defn
class OutboxExecutionWorkflow:
    """RED-tier job lifecycle with saga compensation.

    Enhancement #11: Forward steps tracked in completed_steps[].
    On failure, compensate in reverse order.
    Compensation failure = manual intervention receipt.
    """

    def __init__(self) -> None:
        self._status: str = "pending"
        self._completed_steps: list[CompletedStep] = []

    @workflow.run
    async def run(self, input: OutboxJobInput) -> OutboxJobOutput:
        # Enhancement #9: Search attributes
        safe_upsert_search_attributes([
            SearchAttributeKey.for_keyword(SEARCH_ATTR_SUITE_ID).value_set(input.suite_id),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_RISK_TIER).value_set(input.risk_tier),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_WORKFLOW_KIND).value_set("outbox_execution"),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_OFFICE_ID).value_set(input.office_id),
            SearchAttributeKey.for_keyword(SEARCH_ATTR_CORRELATION_ID).value_set(input.correlation_id),
        ])

        # Step 1: Claim the job (idempotent)
        self._status = "claiming"
        await workflow.execute_activity(
            "claim_outbox_job",
            ClaimJobInput(
                job_id=input.job_id,
                suite_id=input.suite_id,
                worker_id=workflow.info().workflow_id,
            ),
            retry_policy=RetryPolicy(maximum_attempts=3),
            start_to_close_timeout=timedelta(seconds=10),
        )

        self._status = "executing"

        # Sync status
        await workflow.execute_activity(
            "sync_workflow_execution",
            SyncWorkflowInput(
                workflow_id=workflow.info().workflow_id,
                temporal_run_id=workflow.info().run_id,
                suite_id=input.suite_id,
                office_id=input.office_id,
                correlation_id=input.correlation_id,
                status="executing",
                workflow_kind="outbox_execution",
                outbox_job_id=input.job_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        # Step 2: Execute provider call with saga tracking
        # Enhancement #10: Provider-specific retry policy
        provider = input.provider or "default"
        retry_policy = get_retry_policy(provider)

        try:
            result = await workflow.execute_activity(
                "execute_provider_call",
                ProviderCallInput(
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    correlation_id=input.correlation_id,
                    provider=provider,
                    action=input.action_type,
                    payload=input.payload,
                    idempotency_key=input.idempotency_key,
                    capability_token_id=input.capability_token_id,
                ),
                result_type=ProviderCallOutput,
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_DEFAULT),
                retry_policy=retry_policy,
            )

            # Enhancement #11: Track completed step for potential compensation
            self._completed_steps.append(CompletedStep(
                step_name="provider_call",
                result=result.result or {},
                compensation_activity="compensate_provider_call" if result.success else None,
                compensation_input={
                    "provider": provider,
                    "action": input.action_type,
                    "original_result": result.result,
                    "suite_id": input.suite_id,
                    "correlation_id": input.correlation_id,
                } if result.success else None,
            ))

        except Exception as e:
            # Enhancement #11: Saga compensation on failure
            compensation_results = await self._compensate(input)

            self._status = "failed"
            error_msg = str(e)[:500]

            await workflow.execute_activity(
                "fail_outbox_job",
                FailJobInput(
                    job_id=input.job_id,
                    suite_id=input.suite_id,
                    error=error_msg,
                ),
                start_to_close_timeout=timedelta(seconds=10),
            )

            await workflow.execute_activity(
                "persist_receipts",
                PersistReceiptsInput(
                    receipts=[{
                        "action": "outbox_execution_failed",
                        "job_id": input.job_id,
                        "provider": provider,
                        "error": error_msg,
                        "compensated": len(compensation_results) > 0,
                        "compensation_results": compensation_results,
                        "suite_id": input.suite_id,
                        "correlation_id": input.correlation_id,
                    }],
                    suite_id=input.suite_id,
                    correlation_id=input.correlation_id,
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )

            return OutboxJobOutput(
                status="compensated" if compensation_results else "failed",
                error=error_msg,
                compensation_results=compensation_results,
            )

        # Step 3: Complete job + write receipt
        self._status = "completing"
        await workflow.execute_activity(
            "complete_outbox_job",
            CompleteJobInput(
                job_id=input.job_id,
                suite_id=input.suite_id,
                result=result.result or {},
            ),
            start_to_close_timeout=timedelta(seconds=10),
        )

        await workflow.execute_activity(
            "persist_receipts",
            PersistReceiptsInput(
                receipts=[{
                    "action": "outbox_execution_completed",
                    "job_id": input.job_id,
                    "provider": provider,
                    "action_type": input.action_type,
                    "suite_id": input.suite_id,
                    "correlation_id": input.correlation_id,
                }],
                suite_id=input.suite_id,
                correlation_id=input.correlation_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        # Sync final state
        await workflow.execute_activity(
            "sync_workflow_execution",
            SyncWorkflowInput(
                workflow_id=workflow.info().workflow_id,
                temporal_run_id=workflow.info().run_id,
                suite_id=input.suite_id,
                office_id=input.office_id,
                correlation_id=input.correlation_id,
                status="completed",
                workflow_kind="outbox_execution",
                outbox_job_id=input.job_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        self._status = "completed"
        return OutboxJobOutput(status="completed", result=result.result)

    async def _compensate(self, input: OutboxJobInput) -> list[dict[str, Any]]:
        """Enhancement #11: Saga compensation — reverse completed steps."""
        compensation_results: list[dict[str, Any]] = []

        for step in reversed(self._completed_steps):
            if not step.compensation_activity or not step.compensation_input:
                continue

            try:
                await workflow.execute_activity(
                    step.compensation_activity,
                    step.compensation_input,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                compensation_results.append({
                    "step": step.step_name,
                    "status": "compensated",
                })
            except Exception as comp_error:
                # Compensation failure = manual intervention needed
                compensation_results.append({
                    "step": step.step_name,
                    "status": "compensation_failed",
                    "error": str(comp_error)[:200],
                })
                # Emit receipt for manual intervention
                await workflow.execute_activity(
                    "persist_receipts",
                    PersistReceiptsInput(
                        receipts=[{
                            "action": "compensation_failed",
                            "step": step.step_name,
                            "error": str(comp_error)[:200],
                            "requires_manual_intervention": True,
                            "suite_id": input.suite_id,
                            "correlation_id": input.correlation_id,
                        }],
                        suite_id=input.suite_id,
                        correlation_id=input.correlation_id,
                    ),
                    start_to_close_timeout=timedelta(seconds=5),
                )

        return compensation_results

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "completed_steps": len(self._completed_steps),
        }
