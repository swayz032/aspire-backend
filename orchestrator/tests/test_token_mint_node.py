"""Tests for Token Mint Node — Receipt emission (Law #2, Law #5).

Covers:
- Token mint failure (missing signing key) emits receipt
- Failure receipt has correct outcome and reason_code
- Successful mint returns token fields
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from aspire_orchestrator.models import (
    AspireErrorCode,
    Outcome,
    RiskTier,
)
from aspire_orchestrator.nodes.token_mint import token_mint_node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUITE_A = "00000000-0000-0000-0000-000000000001"
OFFICE_A = "00000000-0000-0000-0000-000000000011"


def _make_state(
    *,
    risk_tier: RiskTier = RiskTier.YELLOW,
    task_type: str = "invoice.create",
    error_code: str | None = None,
    signing_key: str | None = None,
) -> dict[str, Any]:
    """Create a minimal OrchestratorState for token_mint_node."""
    state: dict[str, Any] = {
        "correlation_id": str(uuid.uuid4()),
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "risk_tier": risk_tier,
        "task_type": task_type,
        "allowed_tools": ["stripe.invoice.create"],
        "pipeline_receipts": [],
    }
    if error_code is not None:
        state["error_code"] = error_code
    return state


# ===========================================================================
# Failure Receipt Tests
# ===========================================================================


class TestTokenMintFailureReceipt:
    """Token mint failure emits a receipt (Law #2)."""

    def test_missing_signing_key_emits_receipt(self) -> None:
        """Token mint failure (no signing key) adds receipt to pipeline_receipts."""
        state = _make_state()
        with patch.dict(os.environ, {"ASPIRE_TOKEN_SIGNING_KEY": ""}, clear=False), \
             patch("aspire_orchestrator.nodes.token_mint.settings") as mock_settings:
            mock_settings.token_signing_key = ""
            mock_settings.token_ttl_seconds = 45
            result = token_mint_node(state)

        assert result["outcome"] == Outcome.FAILED
        assert result["error_code"] == AspireErrorCode.CAPABILITY_TOKEN_REQUIRED.value
        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["outcome"] == "failed"

    def test_failure_receipt_has_correct_reason_code(self) -> None:
        """Failure receipt has reason_code=TOKEN_SIGNING_KEY_MISSING."""
        state = _make_state()
        with patch.dict(os.environ, {"ASPIRE_TOKEN_SIGNING_KEY": ""}, clear=False), \
             patch("aspire_orchestrator.nodes.token_mint.settings") as mock_settings:
            mock_settings.token_signing_key = ""
            mock_settings.token_ttl_seconds = 45
            result = token_mint_node(state)

        receipt = result["pipeline_receipts"][0]
        assert receipt["reason_code"] == "TOKEN_SIGNING_KEY_MISSING"
        assert receipt["action_type"] == "token.mint"
        assert receipt["actor_id"] == "orchestrator.token_mint"
        assert receipt["receipt_type"] == "tool_execution"

    def test_failure_receipt_has_tenant_context(self) -> None:
        """Failure receipt includes suite_id and office_id."""
        state = _make_state()
        with patch.dict(os.environ, {"ASPIRE_TOKEN_SIGNING_KEY": ""}, clear=False), \
             patch("aspire_orchestrator.nodes.token_mint.settings") as mock_settings:
            mock_settings.token_signing_key = ""
            mock_settings.token_ttl_seconds = 45
            result = token_mint_node(state)

        receipt = result["pipeline_receipts"][0]
        assert receipt["suite_id"] == SUITE_A
        assert receipt["office_id"] == OFFICE_A

    def test_failure_receipt_has_risk_tier(self) -> None:
        """Failure receipt includes the risk_tier from state."""
        state = _make_state(risk_tier=RiskTier.RED)
        with patch.dict(os.environ, {"ASPIRE_TOKEN_SIGNING_KEY": ""}, clear=False), \
             patch("aspire_orchestrator.nodes.token_mint.settings") as mock_settings:
            mock_settings.token_signing_key = ""
            mock_settings.token_ttl_seconds = 45
            result = token_mint_node(state)

        receipt = result["pipeline_receipts"][0]
        assert receipt["risk_tier"] == "red"


# ===========================================================================
# Error Propagation Tests
# ===========================================================================


class TestTokenMintErrorPropagation:
    def test_prior_error_returns_empty(self) -> None:
        """If prior node set error_code, token_mint returns empty dict."""
        state = _make_state(error_code="SAFETY_BLOCKED")
        result = token_mint_node(state)
        assert result == {}


# ===========================================================================
# Successful Mint Tests
# ===========================================================================


class TestTokenMintSuccess:
    def test_successful_mint_returns_token_fields(self) -> None:
        """Successful mint returns capability_token_id, hash, and token."""
        state = _make_state()
        with patch.dict(os.environ, {"ASPIRE_TOKEN_SIGNING_KEY": "test-signing-key-32chars!!!!!!!!!"}, clear=False):
            result = token_mint_node(state)

        assert "capability_token_id" in result
        assert "capability_token_hash" in result
        assert "capability_token" in result
        assert len(result["capability_token_hash"]) == 64  # SHA-256 hex
