"""Temporal test fixtures — shared across all Temporal tests.

Enhancement #4: History capture fixtures for deterministic replay tests.
Enhancement #9: Search attributes disabled in test env (no registration needed).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from aspire_orchestrator.temporal.models import (
    ApprovalEvidence,
    AvaIntentInput,
    RunLangGraphOutput,
)

HISTORY_DIR = Path(__file__).parent / "replay_histories"


# Disable search attributes in the test environment
# (test server doesn't have custom attributes registered)
os.environ.setdefault("TEMPORAL_SEARCH_ATTRS_ENABLED", "false")


@pytest.fixture
def sample_intent_input() -> AvaIntentInput:
    """Standard intent input for tests."""
    return AvaIntentInput(
        suite_id="suite_test_001",
        office_id="office_test_001",
        actor_id="actor_test_001",
        correlation_id="corr_test_001",
        thread_id="thread_test_001",
        initial_state={"message": "Create an invoice for $500"},
        risk_tier="yellow",
        requested_agent="quinn",
    )


@pytest.fixture
def sample_green_intent_input() -> AvaIntentInput:
    """GREEN tier intent (no approval needed)."""
    return AvaIntentInput(
        suite_id="suite_test_001",
        office_id="office_test_001",
        actor_id="actor_test_001",
        correlation_id="corr_test_002",
        thread_id="thread_test_002",
        initial_state={"message": "What is my calendar today?"},
        risk_tier="green",
        requested_agent="ava",
    )


@pytest.fixture
def sample_approval_evidence() -> ApprovalEvidence:
    """Valid approval evidence for tests."""
    return ApprovalEvidence(
        suite_id="suite_test_001",
        office_id="office_test_001",
        approval_id="approval_test_001",
        approver_id="approver_test_001",
        approved=True,
        payload_hash="abc123hash",
        policy_version="1.0.0",
        evidence={"method": "button_click", "timestamp": "2026-03-20T10:00:00Z"},
        nonce="nonce_001",
    )


@pytest.fixture
def mock_langgraph_green_result() -> RunLangGraphOutput:
    """Mock LangGraph result for GREEN intent (no approval)."""
    return RunLangGraphOutput(
        response={"message": "Here is your calendar for today.", "agent": "ava"},
        receipts=[{"action": "calendar_read", "status": "success"}],
        requires_approval=False,
        current_agent="ava",
    )


@pytest.fixture
def mock_langgraph_yellow_result() -> RunLangGraphOutput:
    """Mock LangGraph result for YELLOW intent (approval required)."""
    return RunLangGraphOutput(
        response={"message": "Invoice draft created. Awaiting approval."},
        receipts=[],
        requires_approval=True,
        approval_id="approval_test_001",
        approval_payload_hash="abc123hash",
        current_agent="quinn",
    )


@pytest.fixture
def mock_receipt_output() -> dict[str, Any]:
    """Mock receipt persistence output."""
    return {"receipt_ids": ["rcpt_001"], "count": 1}
