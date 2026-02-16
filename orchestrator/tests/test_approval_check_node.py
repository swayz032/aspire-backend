"""Tests for Approval Check Node — LangGraph integration (Law #4, W4-03).

Covers TC-03 (Yellow tier approval flow) and TC-04 (Red tier presence flow):
- GREEN auto-approve
- YELLOW without evidence → pending with payload_hash
- YELLOW with valid evidence → approved
- YELLOW with tampered payload → rejected (approve-then-swap defense)
- RED without evidence → pending
- RED with evidence but no presence → rejected
- RED with evidence + valid presence → approved
- RED with evidence + invalid presence → rejected
- Error propagation from prior nodes
- Receipt emission for every decision path
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aspire_orchestrator.models import (
    ApprovalEvidence,
    ApprovalMethod,
    AspireErrorCode,
    Outcome,
    ReceiptType,
    RiskTier,
)
from aspire_orchestrator.nodes.approval_check import approval_check_node
from aspire_orchestrator.services.approval_service import (
    CURRENT_POLICY_VERSION,
    compute_payload_hash,
)
from aspire_orchestrator.services.presence_service import mint_presence_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUITE_A = "00000000-0000-0000-0000-000000000001"
OFFICE_A = "00000000-0000-0000-0000-000000000011"
ACTOR = "test-user-001"


def _make_state(
    *,
    risk_tier: RiskTier = RiskTier.GREEN,
    task_type: str = "receipts.search",
    approval_evidence: Any = None,
    presence_token: dict | None = None,
    error_code: str | None = None,
    payload: dict | None = None,
) -> dict[str, Any]:
    """Create a minimal OrchestratorState dict for testing the approval_check node."""
    request_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())

    # Build request object as dict (mimics what intake node produces)
    request = {
        "schema_version": "1.0",
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "payload": payload or {"query": "test data"},
    }

    state: dict[str, Any] = {
        "request": request,
        "correlation_id": correlation_id,
        "request_id": request_id,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "actor_type": "user",
        "actor_id": ACTOR,
        "task_type": task_type,
        "risk_tier": risk_tier,
        "pipeline_receipts": [],
    }

    if approval_evidence is not None:
        state["approval_evidence"] = approval_evidence
    if presence_token is not None:
        state["presence_token"] = presence_token
    if error_code is not None:
        state["error_code"] = error_code

    return state


def _make_valid_evidence(
    state: dict[str, Any],
    *,
    payload: dict | None = None,
) -> dict[str, Any]:
    """Create approval evidence that matches the state's execution context."""
    now = datetime.now(timezone.utc)

    # Compute the same payload hash the node will compute
    execution_payload = {
        "task_type": state.get("task_type", "unknown"),
        "parameters": (state.get("request") or {}).get("payload", {}),
        "suite_id": state.get("suite_id", ""),
        "office_id": state.get("office_id", ""),
    }
    if payload:
        execution_payload["parameters"] = payload

    return {
        "approver_id": ACTOR,
        "approval_method": "ui_button",
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=300)).isoformat(),
        "payload_hash": compute_payload_hash(execution_payload),
        "request_id": state["request_id"],
        "policy_version": CURRENT_POLICY_VERSION,
    }


def _make_valid_presence(state: dict[str, Any]) -> dict[str, Any]:
    """Mint a valid presence token matching the state's execution context."""
    execution_payload = {
        "task_type": state.get("task_type", "unknown"),
        "parameters": (state.get("request") or {}).get("payload", {}),
        "suite_id": state.get("suite_id", ""),
        "office_id": state.get("office_id", ""),
    }
    payload_hash = compute_payload_hash(execution_payload)

    token = mint_presence_token(
        suite_id=SUITE_A,
        office_id=OFFICE_A,
        session_id="test-session",
        payload_hash=payload_hash,
    )
    return {
        "token_id": token.token_id,
        "suite_id": token.suite_id,
        "office_id": token.office_id,
        "session_id": token.session_id,
        "nonce": token.nonce,
        "payload_hash": token.payload_hash,
        "issued_at": token.issued_at,
        "expires_at": token.expires_at,
        "signature": token.signature,
    }


# ===========================================================================
# GREEN Tier Tests
# ===========================================================================


class TestGreenTier:
    def test_auto_approve(self) -> None:
        """GREEN tier auto-approves without any evidence."""
        state = _make_state(risk_tier=RiskTier.GREEN)
        result = approval_check_node(state)
        assert result["approval_status"] == "approved"
        assert result.get("approval_evidence") is None

    def test_green_auto_approve_emits_receipt(self) -> None:
        """GREEN tier emits approval_granted receipt (Law #2)."""
        state = _make_state(risk_tier=RiskTier.GREEN)
        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["receipt_type"] == ReceiptType.APPROVAL_GRANTED.value
        assert receipts[0]["outcome"] == Outcome.SUCCESS.value

    def test_green_auto_approve_receipt_has_correct_reason_code(self) -> None:
        """GREEN auto-approve receipt has reason_code=GREEN_AUTO_APPROVED."""
        state = _make_state(risk_tier=RiskTier.GREEN)
        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert receipts[0]["reason_code"] == "GREEN_AUTO_APPROVED"
        assert receipts[0]["action_type"] == "approval.auto_approve"
        assert receipts[0]["risk_tier"] == "green"


# ===========================================================================
# YELLOW Tier Tests (TC-03)
# ===========================================================================


class TestYellowTier:
    def test_no_evidence_returns_pending(self) -> None:
        """TC-03: Yellow tier without evidence returns APPROVAL_REQUIRED."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        result = approval_check_node(state)

        assert result["approval_status"] == "pending"
        assert result["error_code"] == AspireErrorCode.APPROVAL_REQUIRED.value
        assert "approval_payload_hash" in result
        assert len(result["approval_payload_hash"]) == 64  # SHA-256 hex

    def test_pending_emits_receipt(self) -> None:
        """Pending approval emits an APPROVAL_REQUESTED receipt."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        result = approval_check_node(state)

        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["receipt_type"] == ReceiptType.APPROVAL_REQUESTED.value
        assert receipts[0]["outcome"] == Outcome.PENDING.value

    def test_valid_evidence_approves(self) -> None:
        """YELLOW tier with valid evidence approves the action."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        evidence = _make_valid_evidence(state)
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        assert result["approval_status"] == "approved"
        assert "approval_payload_hash" in result

    def test_approved_emits_receipt(self) -> None:
        """Approved action emits an APPROVAL_GRANTED receipt."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        evidence = _make_valid_evidence(state)
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["receipt_type"] == ReceiptType.APPROVAL_GRANTED.value
        assert receipts[0]["outcome"] == Outcome.SUCCESS.value

    def test_swapped_payload_rejected(self) -> None:
        """Approve-then-swap: evidence for payload A, execute with payload B."""
        state = _make_state(
            risk_tier=RiskTier.YELLOW,
            task_type="invoice.create",
            payload={"amount": 500},
        )
        # Create evidence for the original payload
        evidence = _make_valid_evidence(state)
        # Now swap the payload
        state["request"]["payload"] = {"amount": 50000}  # Swapped!
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        assert result["approval_status"] == "rejected"
        assert result["error_code"] == AspireErrorCode.APPROVAL_BINDING_FAILED.value

    def test_expired_evidence_rejected(self) -> None:
        """Expired approval evidence is rejected."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        evidence = _make_valid_evidence(state)
        # Set expires_at to past
        past = datetime.now(timezone.utc) - timedelta(seconds=600)
        evidence["expires_at"] = past.isoformat()
        evidence["approved_at"] = (past - timedelta(seconds=300)).isoformat()
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        assert result["approval_status"] == "rejected"
        assert result["error_code"] == AspireErrorCode.APPROVAL_EXPIRED.value

    def test_pydantic_evidence_works(self) -> None:
        """ApprovalEvidence Pydantic model works (backward compatibility)."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        evidence = ApprovalEvidence(
            approver_id="test_user",
            approval_method=ApprovalMethod.UI_BUTTON,
            approved_at=datetime.now(timezone.utc),
        )
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        # Should approve because Pydantic model defaults match current context
        assert result["approval_status"] == "approved"


# ===========================================================================
# RED Tier Tests (TC-04)
# ===========================================================================


class TestRedTier:
    def test_no_evidence_returns_pending(self) -> None:
        """RED tier without evidence returns PRESENCE_REQUIRED (video escalation)."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        result = approval_check_node(state)

        assert result["approval_status"] == "pending"
        assert result["error_code"] == AspireErrorCode.PRESENCE_REQUIRED.value
        assert result["presence_required"] is True

    def test_evidence_without_presence_rejected(self) -> None:
        """RED tier with evidence but NO presence token is rejected."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        evidence = _make_valid_evidence(state)
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        assert result["approval_status"] == "rejected"
        assert result["error_code"] == AspireErrorCode.PRESENCE_REQUIRED.value

    def test_evidence_with_presence_missing_emits_receipt(self) -> None:
        """Missing presence emits PRESENCE_MISSING receipt."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        evidence = _make_valid_evidence(state)
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert any(r["receipt_type"] == ReceiptType.PRESENCE_MISSING.value for r in receipts)

    def test_evidence_with_valid_presence_approves(self) -> None:
        """TC-04: RED tier with valid evidence + valid presence → approved."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        evidence = _make_valid_evidence(state)
        presence = _make_valid_presence(state)
        state["approval_evidence"] = evidence
        state["presence_token"] = presence

        result = approval_check_node(state)
        assert result["approval_status"] == "approved"
        assert "approval_payload_hash" in result

    def test_approved_red_emits_receipt(self) -> None:
        """Approved RED action emits APPROVAL_GRANTED receipt."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        evidence = _make_valid_evidence(state)
        presence = _make_valid_presence(state)
        state["approval_evidence"] = evidence
        state["presence_token"] = presence

        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert any(r["receipt_type"] == ReceiptType.APPROVAL_GRANTED.value for r in receipts)

    def test_invalid_presence_rejected(self) -> None:
        """RED tier with evidence + invalid presence (wrong suite) → rejected."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        evidence = _make_valid_evidence(state)
        # Presence for wrong suite
        presence = _make_valid_presence(state)
        presence["suite_id"] = "wrong-suite"  # Tamper!
        state["approval_evidence"] = evidence
        state["presence_token"] = presence

        result = approval_check_node(state)
        assert result["approval_status"] == "rejected"
        # Should fail at signature check (tampered field changes signature verification)

    def test_expired_presence_rejected(self) -> None:
        """RED tier with expired presence token → rejected."""
        state = _make_state(risk_tier=RiskTier.RED, task_type="payment.send")
        evidence = _make_valid_evidence(state)
        # Create presence, then expire it by setting verification time in the future
        presence = _make_valid_presence(state)
        state["approval_evidence"] = evidence
        state["presence_token"] = presence

        # The presence token has default 180s TTL. We can't easily time-travel
        # the node (it uses datetime.now). Instead, test with a presence token
        # that has already expired by minting with 1s TTL.
        from aspire_orchestrator.services.presence_service import (
            mint_presence_token as _mint,
        )

        execution_payload = {
            "task_type": state.get("task_type", "unknown"),
            "parameters": (state.get("request") or {}).get("payload", {}),
            "suite_id": state.get("suite_id", ""),
            "office_id": state.get("office_id", ""),
        }
        payload_hash = compute_payload_hash(execution_payload)

        # Mint with very short TTL and sleep long enough to guarantee expiry
        import time

        token = _mint(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id="test-session",
            payload_hash=payload_hash,
            ttl_seconds=1,
        )
        time.sleep(2.0)  # 2x TTL to avoid timing flakes under load

        state["presence_token"] = {
            "token_id": token.token_id,
            "suite_id": token.suite_id,
            "office_id": token.office_id,
            "session_id": token.session_id,
            "nonce": token.nonce,
            "payload_hash": token.payload_hash,
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
            "signature": token.signature,
        }

        result = approval_check_node(state)
        assert result["approval_status"] == "rejected"


# ===========================================================================
# Error Propagation Tests
# ===========================================================================


class TestErrorPropagation:
    def test_prior_error_returns_denied(self) -> None:
        """If a prior node set error_code, approval_check returns denied."""
        state = _make_state(
            risk_tier=RiskTier.YELLOW,
            error_code="SAFETY_BLOCKED",
        )
        result = approval_check_node(state)
        assert result["approval_status"] == "denied"


# ===========================================================================
# Receipt Coverage Tests
# ===========================================================================


class TestReceiptCoverage:
    def test_pending_receipt_has_payload_hash(self) -> None:
        """Pending receipt includes the payload_hash for client binding."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        result = approval_check_node(state)

        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert "details" in receipts[0]
        assert "payload_hash" in receipts[0]["details"]
        assert len(receipts[0]["details"]["payload_hash"]) == 64

    def test_approved_receipt_has_policy_version(self) -> None:
        """Approved receipt includes the policy_version for audit trail."""
        state = _make_state(risk_tier=RiskTier.YELLOW, task_type="invoice.create")
        evidence = _make_valid_evidence(state)
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["details"]["policy_version"] == CURRENT_POLICY_VERSION

    def test_denied_receipt_has_error_details(self) -> None:
        """Denied receipt includes binding error details for diagnostics."""
        state = _make_state(
            risk_tier=RiskTier.YELLOW,
            task_type="invoice.create",
            payload={"amount": 500},
        )
        evidence = _make_valid_evidence(state)
        state["request"]["payload"] = {"amount": 50000}  # Swap!
        state["approval_evidence"] = evidence

        result = approval_check_node(state)
        receipts = result["pipeline_receipts"]
        assert len(receipts) == 1
        assert receipts[0]["receipt_type"] == ReceiptType.APPROVAL_DENIED.value
        assert "binding_error" in receipts[0]["details"]
