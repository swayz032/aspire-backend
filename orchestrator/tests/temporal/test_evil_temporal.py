"""Evil tests for Temporal — Enhancement #5: Cross-tenant attack vectors.

Tests that cross-tenant signal/update/query attempts are rejected.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aspire_orchestrator.temporal.config import extract_suite_id_from_workflow_id, workflow_id_intent
from aspire_orchestrator.temporal.models import (
    ApprovalEvidence,
    AvaIntentInput,
    PersistReceiptsOutput,
    RunLangGraphOutput,
)
from aspire_orchestrator.temporal.workflows.ava_intent import AvaIntentWorkflow
from aspire_orchestrator.temporal.workflows.approval import ApprovalWorkflow
from aspire_orchestrator.temporal.models import ApprovalWorkflowInput


class TestCrossTenantAttacks:
    """Enhancement #5: Cross-tenant update/query rejection tests."""

    async def test_cross_tenant_update_rejected_by_validator(self) -> None:
        """Tenant B cannot approve Tenant A's workflow."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={}, receipts=[],
                requires_approval=True,
                approval_id="appr_evil_001",
                approval_payload_hash="hash_target",
                current_agent="quinn",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", yellow_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=[], count=0)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                # Tenant A starts workflow
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="tenant_A",
                        office_id="office_A",
                        actor_id="actor_A",
                        correlation_id="corr_evil_001",
                        thread_id="thread_evil_001",
                        initial_state={"message": "create invoice"},
                        risk_tier="yellow",
                    ),
                    id="test-evil-cross-tenant-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                # Tenant B tries to approve Tenant A's workflow
                evil_evidence = ApprovalEvidence(
                    suite_id="tenant_B",  # ATTACKER
                    office_id="office_B",
                    approval_id="appr_evil_001",
                    approver_id="evil_approver",
                    approved=True,
                    payload_hash="hash_target",
                    policy_version="",
                    evidence={"method": "evil"},
                    nonce="evil_nonce",
                )

                with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                    await handle.execute_update(AvaIntentWorkflow.approve, evil_evidence)

                assert "SUITE_MISMATCH" in str(exc_info.value.__cause__)

    async def test_cross_tenant_approval_workflow_rejected(self) -> None:
        """Tenant B cannot decide on Tenant A's approval workflow."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[ApprovalWorkflow],
                activities=[
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=[], count=0)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    ApprovalWorkflow.run,
                    ApprovalWorkflowInput(
                        suite_id="tenant_A",
                        office_id="office_A",
                        correlation_id="corr_evil_002",
                        approval_id="appr_evil_002",
                        action_type="create_invoice",
                        risk_tier="yellow",
                        payload_hash="hash_target_2",
                        policy_version="1.0.0",
                    ),
                    id="test-evil-approval-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                evil_evidence = ApprovalEvidence(
                    suite_id="tenant_B",
                    office_id="office_B",
                    approval_id="appr_evil_002",
                    approver_id="evil_approver",
                    approved=True,
                    payload_hash="hash_target_2",
                    policy_version="1.0.0",
                    evidence={"method": "evil"},
                    nonce="evil_nonce_2",
                )

                with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                    await handle.execute_update(ApprovalWorkflow.decide, evil_evidence)

                assert "SUITE_MISMATCH" in str(exc_info.value.__cause__)

    def test_workflow_id_not_predictable(self) -> None:
        """Enhancement #5: Workflow IDs contain random suffix."""
        id1 = workflow_id_intent("suite_001", "corr_001")
        id2 = workflow_id_intent("suite_001", "corr_001")

        # Same inputs produce different IDs due to random suffix
        assert id1 != id2

        # Both start with the same prefix
        assert id1.startswith("suite:suite_001:intent:corr_001:")
        assert id2.startswith("suite:suite_001:intent:corr_001:")

        # Suffix is 8 chars
        suffix1 = id1.split(":")[-1]
        suffix2 = id2.split(":")[-1]
        assert len(suffix1) == 8
        assert len(suffix2) == 8
        assert suffix1 != suffix2

    def test_extract_suite_id_from_workflow_id(self) -> None:
        """Suite ID correctly extracted from workflow ID."""
        wf_id = "suite:tenant_123:intent:corr_456:abcd1234"
        assert extract_suite_id_from_workflow_id(wf_id) == "tenant_123"

        # Invalid format returns None
        assert extract_suite_id_from_workflow_id("invalid-id") is None
        assert extract_suite_id_from_workflow_id("") is None

    async def test_cross_tenant_query_rejected(self) -> None:
        """Verify query returns suite_id for API-level validation."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={}, receipts=[],
                requires_approval=True,
                approval_id="appr_query_001",
                approval_payload_hash="hash_q",
                current_agent="quinn",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", yellow_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=[], count=0)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="tenant_A",
                        office_id="office_A",
                        actor_id="actor_A",
                        correlation_id="corr_query_001",
                        thread_id="thread_query_001",
                        initial_state={"message": "test"},
                        risk_tier="yellow",
                    ),
                    id="test-evil-query-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                # Query returns suite_id — API layer should check this
                status = await handle.query(AvaIntentWorkflow.get_status)
                assert status["suite_id"] == "tenant_A"
                # API guard: if requester's suite_id != status["suite_id"], deny


def _mock_activity(name: str, return_value: Any) -> Any:
    from temporalio import activity

    @activity.defn(name=name)
    async def mock_fn(input: Any) -> Any:
        return return_value

    return mock_fn
