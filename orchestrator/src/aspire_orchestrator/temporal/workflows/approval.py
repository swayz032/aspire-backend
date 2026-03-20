"""ApprovalWorkflow — manages approval lifecycle with durable timers.

Request → remind → expire/approve/deny.

Enhancement #1: @workflow.update for decision handler (validates inline).
Enhancement #7: Continue-as-new when history approaches 51,200 event limit.
Enhancement #13: workflow.uuid4() / workflow.now() for determinism.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import SearchAttributeKey, SearchAttributePair
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aspire_orchestrator.temporal.config import (
        CONTINUE_AS_NEW_EVENT_THRESHOLD,
        SEARCH_ATTR_CORRELATION_ID,
        SEARCH_ATTR_OFFICE_ID,
        SEARCH_ATTR_RISK_TIER,
        SEARCH_ATTR_SUITE_ID,
        SEARCH_ATTR_WORKFLOW_KIND,
    )
    from aspire_orchestrator.temporal.models import (
        ApprovalEvidence,
        ApprovalUpdateResponse,
        ApprovalWorkflowInput,
        ApprovalWorkflowOutput,
        EmitClientEventInput,
        PersistReceiptsInput,
    )


@workflow.defn
class ApprovalWorkflow:
    """Manages approval lifecycle: request → remind → expire/approve/deny.

    Enhancement #1: Decision via @workflow.update (not signal).
    Enhancement #7: Continue-as-new in reminder loop for long-running approvals.
    """

    def __init__(self) -> None:
        self._decision: ApprovalEvidence | None = None
        self._suite_id: str = ""
        self._office_id: str = ""
        self._approval_id: str = ""
        self._expected_payload_hash: str = ""
        self._policy_version: str = ""
        self._used_nonces: set[str] = set()
        self._reminders_sent: int = 0

    @workflow.run
    async def run(self, input: ApprovalWorkflowInput) -> ApprovalWorkflowOutput:
        self._suite_id = input.suite_id
        self._office_id = input.office_id
        self._approval_id = input.approval_id
        self._expected_payload_hash = input.payload_hash
        self._policy_version = input.policy_version
        self._reminders_sent = input.reminders_sent  # Restore from continue-as-new

        # Enhancement #9: Search attributes
        workflow.upsert_search_attributes([
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_SUITE_ID), [input.suite_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_RISK_TIER), [input.risk_tier]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_WORKFLOW_KIND), ["approval"]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_OFFICE_ID), [input.office_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_CORRELATION_ID), [input.correlation_id]),
        ])

        # Send initial notification
        await workflow.execute_activity(
            "emit_client_event",
            EmitClientEventInput(
                suite_id=input.suite_id,
                office_id=input.office_id,
                correlation_id=input.correlation_id,
                event_type="approval_requested",
                payload={
                    "approval_id": input.approval_id,
                    "action_type": input.action_type,
                    "risk_tier": input.risk_tier,
                },
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        # Wait with periodic reminders
        for reminder_idx in range(self._reminders_sent, input.max_reminders):
            # Enhancement #7: Check history length before each loop iteration
            history_length = workflow.info().get_current_history_length()
            if history_length > CONTINUE_AS_NEW_EVENT_THRESHOLD:
                # Carry forward reminders_sent so the new run resumes the loop
                continued_input = ApprovalWorkflowInput(
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    correlation_id=input.correlation_id,
                    approval_id=input.approval_id,
                    action_type=input.action_type,
                    risk_tier=input.risk_tier,
                    payload_hash=input.payload_hash,
                    policy_version=input.policy_version,
                    parent_workflow_id=input.parent_workflow_id,
                    max_reminders=input.max_reminders,
                    reminder_interval_hours=input.reminder_interval_hours,
                    timeout_hours=input.timeout_hours,
                    required_approvers=input.required_approvers,
                    reminders_sent=reminder_idx + 1,
                )
                workflow.continue_as_new(
                    args=[continued_input],
                )

            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None,
                    timeout=timedelta(hours=input.reminder_interval_hours),
                )
                break  # Decision received
            except asyncio.TimeoutError:
                self._reminders_sent = reminder_idx + 1
                # Send reminder
                await workflow.execute_activity(
                    "emit_client_event",
                    EmitClientEventInput(
                        suite_id=input.suite_id,
                        office_id=input.office_id,
                        correlation_id=input.correlation_id,
                        event_type="approval_reminder",
                        payload={
                            "approval_id": input.approval_id,
                            "reminder_number": self._reminders_sent,
                            "max_reminders": input.max_reminders,
                        },
                    ),
                    start_to_close_timeout=timedelta(seconds=5),
                )

        # Final wait if reminders exhausted and no decision yet
        if self._decision is None:
            remaining_hours = input.timeout_hours - (
                input.reminder_interval_hours * input.max_reminders
            )
            if remaining_hours > 0:
                try:
                    await workflow.wait_condition(
                        lambda: self._decision is not None,
                        timeout=timedelta(hours=remaining_hours),
                    )
                except asyncio.TimeoutError:
                    pass

        if self._decision is None:
            # Expired — emit receipt
            await workflow.execute_activity(
                "persist_receipts",
                PersistReceiptsInput(
                    receipts=[{
                        "action": "approval_expired",
                        "approval_id": input.approval_id,
                        "correlation_id": input.correlation_id,
                        "suite_id": input.suite_id,
                        "reminders_sent": self._reminders_sent,
                    }],
                    suite_id=input.suite_id,
                    correlation_id=input.correlation_id,
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )
            return ApprovalWorkflowOutput(status="expired")

        # Decision received — emit receipt
        status = "approved" if self._decision.approved else "denied"
        await workflow.execute_activity(
            "persist_receipts",
            PersistReceiptsInput(
                receipts=[{
                    "action": f"approval_{status}",
                    "approval_id": input.approval_id,
                    "approver_id": self._decision.approver_id,
                    "correlation_id": input.correlation_id,
                    "suite_id": input.suite_id,
                }],
                suite_id=input.suite_id,
                correlation_id=input.correlation_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        # Signal parent workflow if this is a child
        if input.parent_workflow_id:
            parent = workflow.get_external_workflow_handle(input.parent_workflow_id)
            await parent.signal("approval_decision", {
                "approval_id": input.approval_id,
                "approved": self._decision.approved,
                "approver_id": self._decision.approver_id,
            })

        return ApprovalWorkflowOutput(
            status=status,
            approver_id=self._decision.approver_id,
            evidence=self._decision.evidence,
        )

    # -----------------------------------------------------------------------
    # Enhancement #1: @workflow.update for decision (not signal)
    # -----------------------------------------------------------------------
    @workflow.update
    async def decide(self, evidence: ApprovalEvidence) -> ApprovalUpdateResponse:
        """Handle approval/denial with inline validation."""
        self._decision = evidence
        return ApprovalUpdateResponse(accepted=True)

    @decide.validator
    def _validate_decide(self, evidence: ApprovalEvidence) -> None:
        """Validate approval evidence — same checks as AvaIntentWorkflow."""
        if evidence.suite_id != self._suite_id:
            raise ApplicationError(
                "Suite mismatch — cross-tenant attempt blocked (Law #6)",
                type="SUITE_MISMATCH",
                non_retryable=True,
            )
        if evidence.office_id != self._office_id:
            raise ApplicationError("Office mismatch", type="OFFICE_MISMATCH", non_retryable=True)
        if evidence.approval_id != self._approval_id:
            raise ApplicationError("Approval ID mismatch", type="APPROVAL_ID_MISMATCH", non_retryable=True)
        if evidence.payload_hash != self._expected_payload_hash:
            raise ApplicationError(
                "Payload hash mismatch — approve-then-swap blocked",
                type="PAYLOAD_HASH_MISMATCH",
                non_retryable=True,
            )
        if evidence.policy_version != self._policy_version:
            raise ApplicationError("Policy version mismatch", type="POLICY_VERSION_MISMATCH", non_retryable=True)
        if evidence.nonce:
            if evidence.nonce in self._used_nonces:
                raise ApplicationError("Nonce replay blocked", type="REQUEST_ID_REUSED", non_retryable=True)
            self._used_nonces.add(evidence.nonce)
        if evidence.approved and not evidence.evidence:
            raise ApplicationError("Evidence required", type="MISSING_EVIDENCE", non_retryable=True)

    # -----------------------------------------------------------------------
    # Query
    # -----------------------------------------------------------------------
    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {
            "approval_id": self._approval_id,
            "suite_id": self._suite_id,
            "decided": self._decision is not None,
            "reminders_sent": self._reminders_sent,
        }
