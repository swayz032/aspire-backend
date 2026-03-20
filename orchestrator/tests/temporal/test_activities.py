"""Unit tests for Temporal activities."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.temporal.models import (
    EmitClientEventInput,
    PersistReceiptsInput,
    ProviderCallInput,
    RunLangGraphInput,
)


class TestRunLangGraphTurn:
    """Tests for the LangGraph activity wrapper."""

    @patch("aspire_orchestrator.services.orchestrator_runtime.invoke_orchestrator_graph")
    @patch("temporalio.activity.heartbeat")
    async def test_heartbeat_before_and_after(
        self, mock_heartbeat: MagicMock, mock_invoke: AsyncMock
    ) -> None:
        """Enhancement #3: Heartbeat called before and after graph invocation."""
        from aspire_orchestrator.temporal.activities.langgraph_activity import run_langgraph_turn

        mock_invoke.return_value = {"message": "Done", "_receipts": [], "_requires_approval": False}

        input_data = RunLangGraphInput(
            suite_id="s1",
            office_id="o1",
            actor_id="a1",
            thread_id="t1",
            correlation_id="c1",
            initial_state={"message": "test"},
        )

        result = await run_langgraph_turn(input_data)

        assert result.response == {"message": "Done"}
        # Heartbeat called at least twice (init + complete)
        assert mock_heartbeat.call_count >= 2
        phases = [call.args[0]["phase"] for call in mock_heartbeat.call_args_list]
        assert "graph_init" in phases
        assert "graph_complete" in phases

    @patch("aspire_orchestrator.services.orchestrator_runtime.invoke_orchestrator_graph")
    @patch("temporalio.activity.heartbeat")
    async def test_heartbeat_on_error(
        self, mock_heartbeat: MagicMock, mock_invoke: AsyncMock
    ) -> None:
        """Enhancement #3: Heartbeat with error phase on failure."""
        from aspire_orchestrator.temporal.activities.langgraph_activity import run_langgraph_turn

        mock_invoke.side_effect = RuntimeError("Graph crashed")

        input_data = RunLangGraphInput(
            suite_id="s1", office_id="o1", actor_id="a1",
            thread_id="t1", correlation_id="c1",
            initial_state={"message": "test"},
        )

        with pytest.raises(RuntimeError, match="Graph crashed"):
            await run_langgraph_turn(input_data)

        phases = [call.args[0]["phase"] for call in mock_heartbeat.call_args_list]
        assert "graph_error" in phases


class TestPersistReceipts:
    """Tests for receipt persistence activity."""

    @patch("aspire_orchestrator.services.receipt_store.store_receipts", new_callable=AsyncMock)
    async def test_persist_receipts_success(self, mock_store: AsyncMock) -> None:
        from aspire_orchestrator.temporal.activities.receipt_activity import persist_receipts

        mock_store.return_value = ["rcpt_001", "rcpt_002"]

        input_data = PersistReceiptsInput(
            receipts=[{"action": "test1"}, {"action": "test2"}],
            suite_id="s1",
            correlation_id="c1",
        )

        result = await persist_receipts(input_data)
        assert result.count == 2
        assert result.receipt_ids == ["rcpt_001", "rcpt_002"]

    async def test_persist_empty_receipts(self) -> None:
        """Empty receipts list returns immediately without calling store."""
        from aspire_orchestrator.temporal.activities.receipt_activity import persist_receipts

        input_data = PersistReceiptsInput(receipts=[], suite_id="s1", correlation_id="c1")

        result = await persist_receipts(input_data)
        assert result.count == 0
        assert result.receipt_ids == []


class TestProviderActivity:
    """Tests for provider call activities."""

    @patch("aspire_orchestrator.services.tool_executor.execute_tool")
    @patch("temporalio.activity.heartbeat")
    async def test_heartbeat_around_provider_call(
        self, mock_heartbeat: MagicMock, mock_execute: AsyncMock
    ) -> None:
        """Enhancement #3: Heartbeat before and after provider call."""
        from aspire_orchestrator.temporal.activities.provider_activity import execute_provider_call

        mock_execute.return_value = {"invoice_id": "inv_001"}

        input_data = ProviderCallInput(
            suite_id="s1", office_id="o1", correlation_id="c1",
            provider="stripe", action="create_invoice",
            payload={"amount": 500},
        )

        result = await execute_provider_call(input_data)
        assert result.success is True
        assert result.provider == "stripe"

        phases = [call.args[0]["phase"] for call in mock_heartbeat.call_args_list]
        assert "provider_call_start" in phases
        assert "provider_call_complete" in phases
