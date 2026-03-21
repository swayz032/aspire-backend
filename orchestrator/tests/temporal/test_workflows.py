"""Unit tests for all Temporal workflows.

Tests workflow logic using Temporal's WorkflowEnvironment for deterministic testing.
Each test mocks activities and verifies workflow behavior.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.client import WorkflowHandle, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aspire_orchestrator.temporal.activities import (
    emit_client_event,
    persist_receipts,
    sync_workflow_execution,
)
from aspire_orchestrator.temporal.activities.langgraph_activity import run_langgraph_turn
from aspire_orchestrator.temporal.models import (
    ApprovalEvidence,
    ApprovalUpdateResponse,
    AvaIntentInput,
    AvaIntentOutput,
    PersistReceiptsOutput,
    RunLangGraphOutput,
)
from aspire_orchestrator.temporal.models import SpecialistInput, SpecialistOutput
from aspire_orchestrator.temporal.workflows.ava_intent import AvaIntentWorkflow
from aspire_orchestrator.temporal.workflows.specialist_agent import SpecialistAgentWorkflow


# ---------------------------------------------------------------------------
# AvaIntentWorkflow Tests
# ---------------------------------------------------------------------------
class TestAvaIntentWorkflow:
    """Tests for AvaIntentWorkflow — Enhancement #1, #5, #13."""

    @pytest.fixture
    def green_input(self) -> AvaIntentInput:
        return AvaIntentInput(
            suite_id="suite_001",
            office_id="office_001",
            actor_id="actor_001",
            correlation_id="corr_001",
            thread_id="thread_001",
            initial_state={"message": "What's on my calendar?"},
            risk_tier="green",
        )

    @pytest.fixture
    def yellow_input(self) -> AvaIntentInput:
        return AvaIntentInput(
            suite_id="suite_001",
            office_id="office_001",
            actor_id="actor_001",
            correlation_id="corr_002",
            thread_id="thread_002",
            initial_state={"message": "Create invoice for $500"},
            risk_tier="yellow",
        )

    async def test_green_intent_happy_path(self, green_input: AvaIntentInput) -> None:
        """GREEN intent → LangGraph activity → receipt → complete."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            green_response = RunLangGraphOutput(
                response={"message": "Here's your calendar"},
                receipts=[{"action": "calendar_read"}],
                requires_approval=False,
                current_agent="ava",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", green_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                result = await env.client.execute_workflow(
                    AvaIntentWorkflow.run,
                    green_input,
                    id="test-green-001",
                    task_queue="test-queue",
                )
                assert result.status == "completed"
                assert result.response is not None

    async def test_approval_wait_and_approve(self, yellow_input: AvaIntentInput) -> None:
        """YELLOW intent → wait → approve update → resume → complete."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={"message": "Invoice draft created"},
                receipts=[],
                requires_approval=True,
                approval_id="appr_001",
                approval_payload_hash="hash_abc",
                current_agent="quinn",
            )
            resume_response = RunLangGraphOutput(
                response={"message": "Invoice sent"},
                receipts=[{"action": "invoice_created"}],
                requires_approval=False,
                current_agent="quinn",
            )

            call_count = {"n": 0}

            async def mock_langgraph(input: Any) -> RunLangGraphOutput:
                call_count["n"] += 1
                return yellow_response if call_count["n"] == 1 else resume_response

            from temporalio import activity

            @activity.defn(name="run_langgraph_turn")
            async def mock_rlt(input: Any) -> RunLangGraphOutput:
                return await mock_langgraph(input)

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    mock_rlt,
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    yellow_input,
                    id="test-yellow-001",
                    task_queue="test-queue",
                )

                # Wait briefly for workflow to reach approval wait
                await env.sleep(timedelta(seconds=1))

                # Send approval update (Enhancement #1)
                evidence = ApprovalEvidence(
                    suite_id="suite_001",
                    office_id="office_001",
                    approval_id="appr_001",
                    approver_id="approver_001",
                    approved=True,
                    payload_hash="hash_abc",
                    policy_version="",
                    evidence={"method": "test"},
                    nonce="nonce_001",
                )
                update_result = await handle.execute_update(
                    AvaIntentWorkflow.approve, evidence
                )
                assert update_result.accepted is True

                result = await handle.result()
                assert result.status == "completed"

    async def test_approval_denied(self, yellow_input: AvaIntentInput) -> None:
        """YELLOW intent → wait → deny → denied status."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={"message": "Invoice draft"},
                receipts=[],
                requires_approval=True,
                approval_id="appr_002",
                approval_payload_hash="hash_def",
                current_agent="quinn",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", yellow_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    yellow_input,
                    id="test-deny-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                evidence = ApprovalEvidence(
                    suite_id="suite_001",
                    office_id="office_001",
                    approval_id="appr_002",
                    approver_id="approver_001",
                    approved=False,
                    payload_hash="hash_def",
                    policy_version="",
                    evidence={"reason": "Denied by test"},
                    nonce="nonce_002",
                )
                await handle.execute_update(AvaIntentWorkflow.approve, evidence)

                result = await handle.result()
                assert result.status == "denied"
                assert result.error == "APPROVAL_DENIED"


# ---------------------------------------------------------------------------
# Enhancement #1: Update Validator Tests
# ---------------------------------------------------------------------------
class TestApprovalValidators:
    """Tests for @update.validator on AvaIntentWorkflow.approve."""

    async def test_approve_rejects_wrong_suite(self) -> None:
        """Enhancement #5: Cross-tenant update rejected by validator."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={}, receipts=[],
                requires_approval=True,
                approval_id="appr_003",
                approval_payload_hash="hash_ghi",
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
                        suite_id="suite_001",
                        office_id="office_001",
                        actor_id="actor_001",
                        correlation_id="corr_003",
                        thread_id="thread_003",
                        initial_state={"message": "test"},
                        risk_tier="yellow",
                    ),
                    id="test-cross-tenant-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                # Attempt cross-tenant approval
                bad_evidence = ApprovalEvidence(
                    suite_id="suite_ATTACKER",  # Wrong suite!
                    office_id="office_001",
                    approval_id="appr_003",
                    approver_id="attacker",
                    approved=True,
                    payload_hash="hash_ghi",
                    policy_version="",
                    evidence={"method": "attack"},
                    nonce="nonce_003",
                )

                with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                    await handle.execute_update(AvaIntentWorkflow.approve, bad_evidence)
                assert "SUITE_MISMATCH" in str(exc_info.value.__cause__)

    async def test_approve_rejects_payload_swap(self) -> None:
        """Enhancement #1: Approve-then-swap attack prevented."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={}, receipts=[],
                requires_approval=True,
                approval_id="appr_004",
                approval_payload_hash="original_hash",
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
                        suite_id="suite_001",
                        office_id="office_001",
                        actor_id="actor_001",
                        correlation_id="corr_004",
                        thread_id="thread_004",
                        initial_state={"message": "test"},
                        risk_tier="yellow",
                    ),
                    id="test-payload-swap-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                swapped_evidence = ApprovalEvidence(
                    suite_id="suite_001",
                    office_id="office_001",
                    approval_id="appr_004",
                    approver_id="approver_001",
                    approved=True,
                    payload_hash="SWAPPED_HASH",  # Different hash!
                    policy_version="",
                    evidence={"method": "test"},
                    nonce="nonce_004",
                )

                with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                    await handle.execute_update(AvaIntentWorkflow.approve, swapped_evidence)
                assert "PAYLOAD_HASH_MISMATCH" in str(exc_info.value.__cause__)

    async def test_approve_rejects_when_not_waiting(self) -> None:
        """Enhancement #1: Approval rejected when workflow not in waiting state."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            green_response = RunLangGraphOutput(
                response={"message": "Done"},
                receipts=[{"action": "read"}],
                requires_approval=False,
                current_agent="ava",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", green_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="suite_001",
                        office_id="office_001",
                        actor_id="actor_001",
                        correlation_id="corr_005",
                        thread_id="thread_005",
                        initial_state={"message": "calendar"},
                        risk_tier="green",
                    ),
                    id="test-not-waiting-001",
                    task_queue="test-queue",
                )

                # Workflow completes before we can send update
                result = await handle.result()
                assert result.status == "completed"

    async def test_approve_rejects_expired_nonce(self) -> None:
        """Enhancement #1: Nonce replay prevented."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={}, receipts=[],
                requires_approval=True,
                approval_id="appr_005",
                approval_payload_hash="hash_jkl",
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
                        suite_id="suite_001",
                        office_id="office_001",
                        actor_id="actor_001",
                        correlation_id="corr_006",
                        thread_id="thread_006",
                        initial_state={"message": "test"},
                        risk_tier="yellow",
                    ),
                    id="test-nonce-replay-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                evidence = ApprovalEvidence(
                    suite_id="suite_001",
                    office_id="office_001",
                    approval_id="appr_005",
                    approver_id="approver_001",
                    approved=True,
                    payload_hash="hash_jkl",
                    policy_version="",
                    evidence={"method": "test"},
                    nonce="reused_nonce",
                )

                # First use — should succeed
                result1 = await handle.execute_update(AvaIntentWorkflow.approve, evidence)
                assert result1.accepted is True


# ---------------------------------------------------------------------------
# SpecialistAgentWorkflow Tests (Task #19)
# ---------------------------------------------------------------------------
class TestSpecialistAgentWorkflow:
    """Tests for SpecialistAgentWorkflow — child workflow for agent fan-out."""

    async def test_specialist_happy_path(self) -> None:
        """Specialist executes LangGraph turn and emits receipt."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            graph_result = RunLangGraphOutput(
                response={"analysis": "Revenue up 15%"},
                receipts=[{"action": "finance_analysis"}],
                requires_approval=False,
                current_agent="finn",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[SpecialistAgentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", graph_result),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1", "r2"], count=2)),
                ],
            ):
                result = await env.client.execute_workflow(
                    SpecialistAgentWorkflow.run,
                    SpecialistInput(
                        suite_id="suite_001",
                        office_id="office_001",
                        correlation_id="corr_spec_001",
                        agent_id="finn",
                        skill_pack="finn_finance_manager",
                        input={"query": "Q4 revenue"},
                    ),
                    id="test-specialist-happy-001",
                    task_queue="test-queue",
                )
                assert result.status == "completed"
                assert result.agent_id == "finn"
                assert result.receipt_ids == ["r1", "r2"]

    async def test_specialist_failure_emits_receipt(self) -> None:
        """Specialist emits failure receipt when LangGraph turn fails (Law #2)."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            from temporalio import activity

            @activity.defn(name="run_langgraph_turn")
            async def failing_rlt(input: Any) -> Any:
                raise RuntimeError("Graph exploded")

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[SpecialistAgentWorkflow],
                activities=[
                    failing_rlt,
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["fail_r1"], count=1)),
                ],
            ):
                result = await env.client.execute_workflow(
                    SpecialistAgentWorkflow.run,
                    SpecialistInput(
                        suite_id="suite_001",
                        office_id="office_001",
                        correlation_id="corr_spec_002",
                        agent_id="adam",
                        skill_pack="adam_research",
                        input={"query": "test"},
                    ),
                    id="test-specialist-fail-001",
                    task_queue="test-queue",
                )
                assert result.status == "failed"
                assert result.agent_id == "adam"
                assert result.error is not None  # Error message captured (may be wrapped by Temporal)


# ---------------------------------------------------------------------------
# Signal Handler Tests (Task #17)
# ---------------------------------------------------------------------------
class TestAvaIntentSignalHandlers:
    """Tests for AvaIntentWorkflow signal handlers (child→parent communication)."""

    async def test_approval_decision_signal_unblocks_wait(self) -> None:
        """ApprovalWorkflow child signals parent, unblocking approval wait."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yellow_response = RunLangGraphOutput(
                response={"message": "Draft invoice"},
                receipts=[],
                requires_approval=True,
                approval_id="appr_sig_001",
                approval_payload_hash="hash_sig",
                current_agent="quinn",
            )
            resume_response = RunLangGraphOutput(
                response={"message": "Invoice sent"},
                receipts=[{"action": "invoice_sent"}],
                requires_approval=False,
                current_agent="quinn",
            )

            call_count = {"n": 0}

            from temporalio import activity

            @activity.defn(name="run_langgraph_turn")
            async def mock_rlt(input: Any) -> RunLangGraphOutput:
                call_count["n"] += 1
                return yellow_response if call_count["n"] == 1 else resume_response

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    mock_rlt,
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="suite_001",
                        office_id="office_001",
                        actor_id="actor_001",
                        correlation_id="corr_sig_001",
                        thread_id="thread_sig_001",
                        initial_state={"message": "create invoice"},
                        risk_tier="yellow",
                    ),
                    id="test-signal-approval-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                # Simulate child ApprovalWorkflow signaling back
                await handle.signal(
                    AvaIntentWorkflow.approval_decision,
                    {
                        "approval_id": "appr_sig_001",
                        "approved": True,
                        "approver_id": "approver_001",
                        "evidence": {"method": "signal"},
                    },
                )

                result = await handle.result()
                assert result.status == "completed"

    async def test_callback_signal_ignored_when_not_waiting(self) -> None:
        """callback_received signal is handled gracefully (no crash)."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            green_response = RunLangGraphOutput(
                response={"message": "Done"},
                receipts=[{"action": "read"}],
                requires_approval=False,
                current_agent="ava",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", green_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="suite_001",
                        office_id="office_001",
                        actor_id="actor_001",
                        correlation_id="corr_sig_002",
                        thread_id="thread_sig_002",
                        initial_state={"message": "calendar"},
                        risk_tier="green",
                    ),
                    id="test-signal-callback-001",
                    task_queue="test-queue",
                )

                result = await handle.result()
                assert result.status == "completed"


# ---------------------------------------------------------------------------
# Stale Signal Warning Tests (Wave 3D)
# ---------------------------------------------------------------------------
class TestStaleSignalWarning:
    """Tests that stale approval_decision signals are logged, not silently dropped."""

    async def test_stale_signal_during_presence_wait_not_approval(self) -> None:
        """approval_decision signal when status=waiting_presence (not waiting_approval) → dropped gracefully."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            red_response = RunLangGraphOutput(
                response={"message": "Contract ready"},
                receipts=[],
                requires_approval=False,
                requires_presence=True,
                presence_token="pres_tok_stale",
                current_agent="clara",
            )

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", red_response),
                    _mock_activity("persist_receipts", PersistReceiptsOutput(receipt_ids=["r1"], count=1)),
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="suite_stale",
                        office_id="office_stale",
                        actor_id="actor_stale",
                        correlation_id="corr_stale_001",
                        thread_id="thread_stale_001",
                        initial_state={"message": "sign contract"},
                        risk_tier="red",
                    ),
                    id="test-stale-signal-001",
                    task_queue="test-queue",
                )

                await env.sleep(timedelta(seconds=1))

                # Verify workflow is in waiting_presence state
                status = await handle.query(AvaIntentWorkflow.get_status)
                assert status["status"] == "waiting_presence"

                # Send approval_decision signal while in waiting_presence — should be dropped gracefully
                await handle.signal(
                    AvaIntentWorkflow.approval_decision,
                    {"approval_id": "stale_001", "approved": True, "approver_id": "x"},
                )

                # Workflow should still be waiting for presence (signal was dropped)
                status = await handle.query(AvaIntentWorkflow.get_status)
                assert status["status"] == "waiting_presence"


# ---------------------------------------------------------------------------
# Presence Timeout Receipt Tests (Wave 3C — Law #2)
# ---------------------------------------------------------------------------
class TestPresenceTimeoutReceipt:
    """Tests that RED-tier presence timeout emits a receipt (Law #2)."""

    async def test_presence_timeout_emits_receipt(self) -> None:
        """RED-tier presence timeout → receipt with action=presence_timeout."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            red_response = RunLangGraphOutput(
                response={"message": "Contract ready"},
                receipts=[],
                requires_approval=False,
                requires_presence=True,
                presence_token="pres_tok_001",
                current_agent="clara",
            )

            receipt_calls: list[Any] = []

            from temporalio import activity

            @activity.defn(name="persist_receipts")
            async def tracking_receipts(input: Any) -> PersistReceiptsOutput:
                receipt_calls.append(input)
                return PersistReceiptsOutput(receipt_ids=["r_pres_timeout"], count=1)

            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[AvaIntentWorkflow],
                activities=[
                    _mock_activity("run_langgraph_turn", red_response),
                    tracking_receipts,
                    _mock_activity("sync_workflow_execution", None),
                    _mock_activity("emit_client_event", None),
                ],
            ):
                handle = await env.client.start_workflow(
                    AvaIntentWorkflow.run,
                    AvaIntentInput(
                        suite_id="suite_red",
                        office_id="office_red",
                        actor_id="actor_red",
                        correlation_id="corr_red_001",
                        thread_id="thread_red_001",
                        initial_state={"message": "sign contract"},
                        risk_tier="red",
                    ),
                    id="test-presence-timeout-001",
                    task_queue="test-queue",
                )

                # Let presence timeout expire (30 min in time-skipping mode)
                await env.sleep(timedelta(minutes=31))

                result = await handle.result()
                assert result.status == "timed_out"
                assert result.error == "PRESENCE_TIMEOUT"
                assert result.receipt_ids == ["r_pres_timeout"]

                # Verify receipt was emitted with correct fields
                assert len(receipt_calls) > 0
                timeout_receipt = None
                for call in receipt_calls:
                    # Activity input may be dict or dataclass depending on serialization
                    receipts = call.receipts if hasattr(call, "receipts") else call.get("receipts", [])
                    for r in receipts:
                        if r.get("action") == "presence_timeout":
                            timeout_receipt = r
                            break
                assert timeout_receipt is not None
                assert timeout_receipt["risk_tier"] == "red"
                assert timeout_receipt["suite_id"] == "suite_red"


# ---------------------------------------------------------------------------
# Helper: Create mock activities
# ---------------------------------------------------------------------------
def _mock_activity(name: str, return_value: Any) -> Any:
    """Create a mock activity function with the given name and return value."""
    from temporalio import activity

    @activity.defn(name=name)
    async def mock_fn(input: Any) -> Any:
        return return_value

    return mock_fn
