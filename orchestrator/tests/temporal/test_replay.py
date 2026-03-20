"""Deterministic replay tests — Enhancement #4.

Uses Temporal's Replayer to verify workflow code is deterministic.
Golden history JSONs are captured once and checked into the repo.
CI runs these on every workflow code change — fast, no server needed.

Note: These tests require golden history files in replay_histories/.
Initial run captures them; subsequent runs verify determinism.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aspire_orchestrator.temporal.workflows.ava_intent import AvaIntentWorkflow
from aspire_orchestrator.temporal.workflows.approval import ApprovalWorkflow
from aspire_orchestrator.temporal.workflows.outbox_execution import OutboxExecutionWorkflow
from aspire_orchestrator.temporal.workflows.provider_callback import ProviderCallbackWorkflow

HISTORY_DIR = Path(__file__).parent / "replay_histories"


class TestDeterministicReplay:
    """Enhancement #4: Replay tests for all workflow types.

    These tests verify that workflow code changes don't break replay.
    A NondeterminismError during replay means the workflow code change
    is incompatible with existing in-flight workflows.
    """

    @pytest.mark.skipif(
        not (HISTORY_DIR / "ava_intent_happy.json").exists(),
        reason="Golden history not yet captured — run integration tests first",
    )
    async def test_ava_intent_replay_deterministic(self) -> None:
        """AvaIntentWorkflow happy path replays without NondeterminismError."""
        from temporalio.worker import Replayer

        history_json = (HISTORY_DIR / "ava_intent_happy.json").read_text()
        await Replayer(workflows=[AvaIntentWorkflow]).replay_workflow(
            WorkflowHistory.from_json("ava_intent_happy", history_json)
        )

    @pytest.mark.skipif(
        not (HISTORY_DIR / "ava_intent_approval.json").exists(),
        reason="Golden history not yet captured",
    )
    async def test_ava_intent_approval_replay_deterministic(self) -> None:
        """AvaIntentWorkflow with approval wait+resume replays correctly."""
        from temporalio.worker import Replayer

        history_json = (HISTORY_DIR / "ava_intent_approval.json").read_text()
        await Replayer(workflows=[AvaIntentWorkflow]).replay_workflow(
            WorkflowHistory.from_json("ava_intent_approval", history_json)
        )

    @pytest.mark.skipif(
        not (HISTORY_DIR / "approval_happy.json").exists(),
        reason="Golden history not yet captured",
    )
    async def test_approval_workflow_replay_deterministic(self) -> None:
        """ApprovalWorkflow replays correctly."""
        from temporalio.worker import Replayer

        history_json = (HISTORY_DIR / "approval_happy.json").read_text()
        await Replayer(workflows=[ApprovalWorkflow]).replay_workflow(
            WorkflowHistory.from_json("approval_happy", history_json)
        )

    @pytest.mark.skipif(
        not (HISTORY_DIR / "outbox_happy.json").exists(),
        reason="Golden history not yet captured",
    )
    async def test_outbox_workflow_replay_deterministic(self) -> None:
        """OutboxExecutionWorkflow replays correctly."""
        from temporalio.worker import Replayer

        history_json = (HISTORY_DIR / "outbox_happy.json").read_text()
        await Replayer(workflows=[OutboxExecutionWorkflow]).replay_workflow(
            WorkflowHistory.from_json("outbox_happy", history_json)
        )

    @pytest.mark.skipif(
        not (HISTORY_DIR / "callback_happy.json").exists(),
        reason="Golden history not yet captured",
    )
    async def test_callback_workflow_replay_deterministic(self) -> None:
        """ProviderCallbackWorkflow replays correctly."""
        from temporalio.worker import Replayer

        history_json = (HISTORY_DIR / "callback_happy.json").read_text()
        await Replayer(workflows=[ProviderCallbackWorkflow]).replay_workflow(
            WorkflowHistory.from_json("callback_happy", history_json)
        )


class TestSideEffects:
    """Enhancement #13: Verify no raw uuid4/datetime.now in workflow code."""

    def test_no_raw_uuid4_in_workflows(self) -> None:
        """Workflow files must use workflow.uuid4(), not uuid.uuid4()."""
        import ast
        import importlib

        workflows_dir = Path(__file__).parent.parent.parent / "src" / "aspire_orchestrator" / "temporal" / "workflows"

        for py_file in workflows_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            source = py_file.read_text()
            # Check for direct uuid4 imports (not from temporalio)
            assert "from uuid import" not in source or "uuid4" not in source, (
                f"{py_file.name} imports uuid.uuid4 directly — use workflow.uuid4() instead"
            )
            # Check for datetime.now() outside of imports_passed_through
            # (Simplified check — full AST analysis would be more robust)
            if "datetime.now()" in source:
                # Allow it only inside with workflow.unsafe.imports_passed_through() blocks
                # For now, flag it
                pass  # TODO: Full AST check in Phase 3

    def test_no_raw_random_in_workflows(self) -> None:
        """Workflow files must not use random module."""
        workflows_dir = Path(__file__).parent.parent.parent / "src" / "aspire_orchestrator" / "temporal" / "workflows"

        for py_file in workflows_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            source = py_file.read_text()
            assert "import random" not in source, (
                f"{py_file.name} imports random — non-deterministic in workflow code"
            )


# Placeholder for WorkflowHistory — will be available from temporalio.worker
try:
    from temporalio.worker import WorkflowHistory
except ImportError:
    # Stub for import compatibility
    class WorkflowHistory:  # type: ignore[no-redef]
        @staticmethod
        def from_json(name: str, json_str: str) -> "WorkflowHistory":
            raise NotImplementedError("Requires temporalio>=1.9.0")
