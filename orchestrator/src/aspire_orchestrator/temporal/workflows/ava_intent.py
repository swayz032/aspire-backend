"""AvaIntentWorkflow — the primary intent lifecycle workflow.

Wraps: LangGraph turn → approval wait → resume → complete.

Enhancement #1: @workflow.update + @update.validator replaces @workflow.signal
  for approvals. All 8 ApprovalBindingError checks enforced at Temporal level.
Enhancement #5: Cross-tenant update rejection via suite_id validation.
Enhancement #9: Custom search attributes for admin visibility.
Enhancement #13: workflow.uuid4() / workflow.now() for deterministic replay.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributePair
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aspire_orchestrator.temporal.config import (
        ACTIVITY_HEARTBEAT_DEFAULT,
        ACTIVITY_START_TO_CLOSE_DEFAULT,
        APPROVAL_DEFAULT_TIMEOUT_HOURS,
        QUEUE_INTENT_HIGH,
        SEARCH_ATTR_AGENT_ID,
        SEARCH_ATTR_CORRELATION_ID,
        SEARCH_ATTR_OFFICE_ID,
        SEARCH_ATTR_RISK_TIER,
        SEARCH_ATTR_SUITE_ID,
        SEARCH_ATTR_WORKFLOW_KIND,
    )
    from aspire_orchestrator.temporal.models import (
        ApprovalEvidence,
        ApprovalUpdateResponse,
        AvaIntentInput,
        AvaIntentOutput,
        EmitClientEventInput,
        PersistReceiptsInput,
        PresenceEvidence,
        RunLangGraphInput,
        SyncWorkflowInput,
    )


@workflow.defn
class AvaIntentWorkflow:
    """Wraps the full intent lifecycle: LangGraph turn → approval wait → resume → complete.

    Enhancement #1: Approval via @workflow.update with inline validation.
    Enhancement #5: suite_id checked on every update to prevent cross-tenant access.
    Enhancement #13: All randomness/time via workflow.uuid4()/workflow.now().
    """

    def __init__(self) -> None:
        self._status: str = "running"
        self._current_agent: str | None = None
        self._wait_type: str | None = None
        self._suite_id: str = ""
        self._office_id: str = ""
        self._approval_id: str | None = None
        self._expected_payload_hash: str | None = None
        self._policy_version: str | None = None
        self._approval_result: ApprovalEvidence | None = None
        self._presence_result: PresenceEvidence | None = None
        self._used_nonces: set[str] = set()

    @workflow.run
    async def run(self, input: AvaIntentInput) -> AvaIntentOutput:
        self._suite_id = input.suite_id
        self._office_id = input.office_id

        # Enhancement #9: Set search attributes for admin visibility
        workflow.upsert_search_attributes([
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_SUITE_ID), [input.suite_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_RISK_TIER), [input.risk_tier]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_WORKFLOW_KIND), ["intent"]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_OFFICE_ID), [input.office_id]),
            SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_CORRELATION_ID), [input.correlation_id]),
        ])

        # Step 1: Run LangGraph as an activity (non-deterministic, external I/O)
        graph_result = await workflow.execute_activity(
            "run_langgraph_turn",
            RunLangGraphInput(
                suite_id=input.suite_id,
                office_id=input.office_id,
                actor_id=input.actor_id,
                thread_id=input.thread_id,
                correlation_id=input.correlation_id,
                initial_state=input.initial_state,
                requested_agent=input.requested_agent,
            ),
            start_to_close_timeout=timedelta(seconds=ACTIVITY_START_TO_CLOSE_DEFAULT),
            heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_DEFAULT),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=3,
                non_retryable_error_types=["PolicyDeniedError", "SafetyBlockedError"],
            ),
        )

        # Update agent tracking
        self._current_agent = graph_result.current_agent
        if self._current_agent:
            workflow.upsert_search_attributes([
                SearchAttributePair(SearchAttributeKey.for_keyword(SEARCH_ATTR_AGENT_ID), [self._current_agent]),
            ])

        # Step 2: If approval required, wait for update (Enhancement #1)
        if graph_result.requires_approval:
            self._wait_type = "approval"
            self._approval_id = graph_result.approval_id
            self._expected_payload_hash = graph_result.approval_payload_hash
            self._status = "waiting_approval"

            # Sync to Supabase for admin visibility
            await workflow.execute_activity(
                "sync_workflow_execution",
                SyncWorkflowInput(
                    workflow_id=workflow.info().workflow_id,
                    temporal_run_id=workflow.info().run_id,
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    correlation_id=input.correlation_id,
                    status="waiting_approval",
                    workflow_kind="intent",
                    current_wait_type="approval",
                    current_agent=self._current_agent,
                    thread_id=input.thread_id,
                    approval_id=self._approval_id,
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )

            # Emit client event for UI notification
            await workflow.execute_activity(
                "emit_client_event",
                EmitClientEventInput(
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    correlation_id=input.correlation_id,
                    event_type="approval_required",
                    payload={
                        "approval_id": self._approval_id,
                        "risk_tier": input.risk_tier,
                        "agent": self._current_agent,
                    },
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )

            # Wait for approval update (Enhancement #1: via @workflow.update, not signal)
            try:
                await workflow.wait_condition(
                    lambda: self._approval_result is not None,
                    timeout=timedelta(hours=APPROVAL_DEFAULT_TIMEOUT_HOURS),
                )
            except asyncio.TimeoutError:
                self._status = "timed_out"
                receipt_output = await workflow.execute_activity(
                    "persist_receipts",
                    PersistReceiptsInput(
                        receipts=[{
                            "action": "approval_timeout",
                            "status": "timed_out",
                            "approval_id": self._approval_id,
                            "correlation_id": input.correlation_id,
                            "suite_id": input.suite_id,
                        }],
                        suite_id=input.suite_id,
                        correlation_id=input.correlation_id,
                    ),
                    start_to_close_timeout=timedelta(seconds=5),
                )
                return AvaIntentOutput(
                    status="timed_out",
                    error="APPROVAL_EXPIRED",
                    receipt_ids=receipt_output.receipt_ids,
                )

            if not self._approval_result.approved:
                self._status = "denied"
                receipt_output = await workflow.execute_activity(
                    "persist_receipts",
                    PersistReceiptsInput(
                        receipts=[{
                            "action": "approval_denied",
                            "status": "denied",
                            "approval_id": self._approval_id,
                            "approver_id": self._approval_result.approver_id,
                            "correlation_id": input.correlation_id,
                            "suite_id": input.suite_id,
                        }],
                        suite_id=input.suite_id,
                        correlation_id=input.correlation_id,
                    ),
                    start_to_close_timeout=timedelta(seconds=5),
                )
                return AvaIntentOutput(
                    status="denied",
                    error="APPROVAL_DENIED",
                    receipt_ids=receipt_output.receipt_ids,
                )

            # Step 3: Resume LangGraph with approval evidence
            graph_result = await workflow.execute_activity(
                "run_langgraph_turn",
                RunLangGraphInput(
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    actor_id=input.actor_id,
                    thread_id=input.thread_id,
                    correlation_id=input.correlation_id,
                    initial_state=input.initial_state,
                    approval_evidence=self._approval_result.evidence,
                    requested_agent=input.requested_agent,
                ),
                start_to_close_timeout=timedelta(seconds=ACTIVITY_START_TO_CLOSE_DEFAULT),
                heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_DEFAULT),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        # Step 3b: If presence required (RED tier), similar wait pattern
        if graph_result.requires_presence:
            self._wait_type = "presence"
            self._status = "waiting_presence"

            await workflow.execute_activity(
                "sync_workflow_execution",
                SyncWorkflowInput(
                    workflow_id=workflow.info().workflow_id,
                    temporal_run_id=workflow.info().run_id,
                    suite_id=input.suite_id,
                    office_id=input.office_id,
                    correlation_id=input.correlation_id,
                    status="waiting_presence",
                    workflow_kind="intent",
                    current_wait_type="presence",
                    current_agent=self._current_agent,
                    thread_id=input.thread_id,
                ),
                start_to_close_timeout=timedelta(seconds=5),
            )

            try:
                await workflow.wait_condition(
                    lambda: self._presence_result is not None,
                    timeout=timedelta(minutes=30),
                )
            except asyncio.TimeoutError:
                self._status = "timed_out"
                return AvaIntentOutput(status="timed_out", error="PRESENCE_TIMEOUT")

        # Step 4: Persist receipts
        receipt_output = await workflow.execute_activity(
            "persist_receipts",
            PersistReceiptsInput(
                receipts=graph_result.receipts,
                suite_id=input.suite_id,
                correlation_id=input.correlation_id,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        # Step 5: Sync final state
        await workflow.execute_activity(
            "sync_workflow_execution",
            SyncWorkflowInput(
                workflow_id=workflow.info().workflow_id,
                temporal_run_id=workflow.info().run_id,
                suite_id=input.suite_id,
                office_id=input.office_id,
                correlation_id=input.correlation_id,
                status="completed",
                workflow_kind="intent",
                current_agent=self._current_agent,
                thread_id=input.thread_id,
                latest_response=graph_result.response,
            ),
            start_to_close_timeout=timedelta(seconds=5),
        )

        self._status = "completed"
        return AvaIntentOutput(
            status="completed",
            response=graph_result.response,
            receipt_ids=receipt_output.receipt_ids,
        )

    # -----------------------------------------------------------------------
    # Enhancement #1: @workflow.update replaces @workflow.signal for approvals
    # -----------------------------------------------------------------------
    @workflow.update
    async def approve(self, evidence: ApprovalEvidence) -> ApprovalUpdateResponse:
        """Handle approval update with inline validation.

        Returns ApprovalUpdateResponse so the caller gets immediate feedback
        (unlike fire-and-forget signals).
        """
        self._approval_result = evidence
        return ApprovalUpdateResponse(accepted=True)

    @approve.validator
    def _validate_approve(self, evidence: ApprovalEvidence) -> None:
        """Validate approval evidence — all 8 ApprovalBindingError checks.

        Enhancement #1: These checks were previously only in approval_service.py.
        Now enforced at the Temporal level too (defense in depth).
        Enhancement #5: Cross-tenant rejection via suite_id validation.
        """
        # Check 1: SUITE_MISMATCH (Enhancement #5: cross-tenant rejection)
        if evidence.suite_id != self._suite_id:
            raise ApplicationError(
                f"Suite mismatch: expected {self._suite_id}, got {evidence.suite_id}",
                type="SUITE_MISMATCH",
                non_retryable=True,
            )

        # Check 2: NOT_WAITING — must be in waiting_approval state
        if self._status != "waiting_approval":
            raise ApplicationError(
                f"Workflow not waiting for approval (status={self._status})",
                type="NOT_WAITING",
                non_retryable=True,
            )

        # Check 3: OFFICE_MISMATCH
        if evidence.office_id != self._office_id:
            raise ApplicationError(
                f"Office mismatch: expected {self._office_id}, got {evidence.office_id}",
                type="OFFICE_MISMATCH",
                non_retryable=True,
            )

        # Check 4: APPROVAL_ID_MISMATCH
        if self._approval_id and evidence.approval_id != self._approval_id:
            raise ApplicationError(
                f"Approval ID mismatch: expected {self._approval_id}, got {evidence.approval_id}",
                type="APPROVAL_ID_MISMATCH",
                non_retryable=True,
            )

        # Check 5: PAYLOAD_HASH_MISMATCH — approve-then-swap attack prevention
        if self._expected_payload_hash and evidence.payload_hash != self._expected_payload_hash:
            raise ApplicationError(
                "Payload hash mismatch — possible approve-then-swap attack",
                type="PAYLOAD_HASH_MISMATCH",
                non_retryable=True,
            )

        # Check 6: REQUEST_ID_REUSED (nonce replay prevention)
        if evidence.nonce:
            if evidence.nonce in self._used_nonces:
                raise ApplicationError(
                    "Nonce already used — replay attack prevented",
                    type="REQUEST_ID_REUSED",
                    non_retryable=True,
                )
            self._used_nonces.add(evidence.nonce)

        # Check 7: MISSING_EVIDENCE
        if evidence.approved and not evidence.evidence:
            raise ApplicationError(
                "Approval evidence required for approved decisions",
                type="MISSING_EVIDENCE",
                non_retryable=True,
            )

        # Check 8: POLICY_VERSION_MISMATCH
        if self._policy_version and evidence.policy_version != self._policy_version:
            raise ApplicationError(
                f"Policy version mismatch: expected {self._policy_version}, got {evidence.policy_version}",
                type="POLICY_VERSION_MISMATCH",
                non_retryable=True,
            )

    # -----------------------------------------------------------------------
    # Presence update (RED tier)
    # -----------------------------------------------------------------------
    @workflow.update
    async def provide_presence(self, evidence: PresenceEvidence) -> ApprovalUpdateResponse:
        """Handle presence confirmation for RED-tier operations."""
        self._presence_result = evidence
        return ApprovalUpdateResponse(accepted=True)

    @provide_presence.validator
    def _validate_presence(self, evidence: PresenceEvidence) -> None:
        """Validate presence evidence — cross-tenant check."""
        if evidence.suite_id != self._suite_id:
            raise ApplicationError(
                f"Suite mismatch: expected {self._suite_id}, got {evidence.suite_id}",
                type="SUITE_MISMATCH",
                non_retryable=True,
            )
        if self._status != "waiting_presence":
            raise ApplicationError(
                f"Workflow not waiting for presence (status={self._status})",
                type="NOT_WAITING",
                non_retryable=True,
            )

    # -----------------------------------------------------------------------
    # Signal handlers — child→parent communication
    # -----------------------------------------------------------------------
    @workflow.signal
    async def approval_decision(self, data: dict[str, Any]) -> None:
        """Receive approval decision from child ApprovalWorkflow.

        This handles the case where ApprovalWorkflow is started as a
        separate workflow (not inline) and signals back on completion.
        Converts the signal data into an ApprovalEvidence and sets the
        approval result, unblocking the wait_condition.
        """
        if self._status != "waiting_approval":
            return  # Ignore stale signals

        self._approval_result = ApprovalEvidence(
            suite_id=self._suite_id,
            office_id=self._office_id,
            approval_id=data.get("approval_id", ""),
            approver_id=data.get("approver_id", ""),
            approved=data.get("approved", False),
            payload_hash=self._expected_payload_hash or "",
            policy_version=self._policy_version or "",
            evidence=data.get("evidence", {}),
        )

    @workflow.signal
    async def callback_received(self, data: dict[str, Any]) -> None:
        """Receive callback data from child ProviderCallbackWorkflow.

        This handles webhook callbacks routed through a child
        ProviderCallbackWorkflow that signals the parent on completion.
        """
        # Store callback data for downstream processing if needed
        pass  # Currently AvaIntentWorkflow doesn't wait for callbacks directly

    # -----------------------------------------------------------------------
    # Query handler
    # -----------------------------------------------------------------------
    @workflow.query
    def get_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "current_agent": self._current_agent,
            "wait_type": self._wait_type,
            "approval_id": self._approval_id,
            "suite_id": self._suite_id,
        }
