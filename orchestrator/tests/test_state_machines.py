"""Tests for Phase 2 Wave 6 state machines.

~35 tests covering all 5 state machines:
  - inbox_send_state_machine (7 tests)
  - invoice_state_machine (7 tests)
  - contract_state_machine (7 tests)
  - payroll_state_machine (7 tests)
  - payment_state_machine (7 tests)

Each state machine has:
  1. Happy path through all states
  2. Invalid transition blocked with denial receipt
  3. Approval required (YELLOW/RED) — denied without evidence
  4. Receipt emitted on every transition
  5. Terminal state blocks further transitions
  6. Tenant isolation (suite_id/office_id mismatch denied)
  7. Domain-specific rule (binding fields, dual approval, presence, etc.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# --- InboxSend ---
from aspire_orchestrator.services.inbox_send_state_machine import (
    InboxSendStateMachine,
    InvalidTransitionError as InboxSendInvalidTransition,
)

# --- Invoice ---
from aspire_orchestrator.services.invoice_state_machine import (
    InvoiceStateMachine,
    InvalidTransitionError as InvoiceInvalidTransition,
)

# --- Contract ---
from aspire_orchestrator.services.contract_state_machine import (
    ContractStateMachine,
    InvalidTransitionError as ContractInvalidTransition,
)

# --- Payroll ---
from aspire_orchestrator.services.payroll_state_machine import (
    PayrollStateMachine,
    InvalidTransitionError as PayrollInvalidTransition,
)

# --- Payment ---
from aspire_orchestrator.services.payment_state_machine import (
    PaymentStateMachine,
    InvalidTransitionError as PaymentInvalidTransition,
)


# ============================================================================
# Shared test fixtures
# ============================================================================

SUITE = "suite-001"
OFFICE = "office-001"
CORR = "corr-001"
ACTOR = "user-tonio"
YELLOW_APPROVAL = {"approver": "tonio", "reason": "approved"}


# ============================================================================
# 1. InboxSendStateMachine (7 tests)
# ============================================================================


class TestInboxSendStateMachine:
    """Tests for the 5-state inbox send lifecycle."""

    def test_happy_path_draft_to_sent(self) -> None:
        """Happy path: DRAFT -> APPROVED -> SENDING -> SENT."""
        sm = InboxSendStateMachine("email-1", SUITE, OFFICE)
        assert sm.current_state == "draft"

        # DRAFT -> APPROVED (YELLOW, needs approval)
        r1 = sm.transition(
            "email-1", "draft", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence=YELLOW_APPROVAL,
        )
        assert r1.outcome == "success"
        assert r1.from_state == "draft"
        assert r1.to_state == "approved"
        assert r1.risk_tier == "yellow"

        # APPROVED -> SENDING
        r2 = sm.transition(
            "email-1", "approved", "sending",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r2.outcome == "success"

        # SENDING -> SENT
        r3 = sm.transition(
            "email-1", "sending", "sent",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r3.outcome == "success"
        assert sm.current_state == "sent"
        assert sm.is_terminal
        assert len(sm.history) == 3

    def test_sending_to_failed(self) -> None:
        """SENDING -> FAILED is a valid terminal path."""
        sm = InboxSendStateMachine("email-2", SUITE, OFFICE)
        sm.transition(
            "email-2", "draft", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence=YELLOW_APPROVAL,
        )
        sm.transition(
            "email-2", "approved", "sending",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        r = sm.transition(
            "email-2", "sending", "failed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r.outcome == "success"
        assert sm.current_state == "failed"
        assert sm.is_terminal

    def test_invalid_transition_blocked(self) -> None:
        """DRAFT -> SENT is not a valid transition — denial receipt emitted."""
        sm = InboxSendStateMachine("email-3", SUITE, OFFICE)
        with pytest.raises(InboxSendInvalidTransition) as exc_info:
            sm.transition(
                "email-3", "draft", "sent",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
            )
        denial = exc_info.value.denial_receipt
        assert denial.outcome == "denied"
        assert denial.reason_code == "invalid_transition"
        # Denial receipt is also in history (Law #2)
        assert len(sm.history) == 1
        assert sm.history[0].outcome == "denied"

    def test_approval_required_for_yellow(self) -> None:
        """DRAFT -> APPROVED requires approval_evidence (YELLOW tier)."""
        sm = InboxSendStateMachine("email-4", SUITE, OFFICE)
        with pytest.raises(InboxSendInvalidTransition) as exc_info:
            sm.transition(
                "email-4", "draft", "approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                # No approval_evidence!
            )
        denial = exc_info.value.denial_receipt
        assert denial.outcome == "denied"
        assert denial.reason_code == "missing_approval_evidence"

    def test_receipt_emitted_on_every_transition(self) -> None:
        """Every transition produces a receipt — check IDs are unique."""
        sm = InboxSendStateMachine("email-5", SUITE, OFFICE)
        r1 = sm.transition(
            "email-5", "draft", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence=YELLOW_APPROVAL,
        )
        r2 = sm.transition(
            "email-5", "approved", "sending",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r1.id != r2.id
        assert r1.entity_type == "inbox_send"
        assert r2.entity_type == "inbox_send"
        assert len(sm.history) == 2

    def test_terminal_state_blocks_transition(self) -> None:
        """Cannot transition from terminal state SENT."""
        sm = InboxSendStateMachine("email-6", SUITE, OFFICE, initial_state="sent")
        assert sm.is_terminal
        with pytest.raises(InboxSendInvalidTransition) as exc_info:
            sm.transition(
                "email-6", "sent", "draft",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"

    def test_tenant_isolation_suite_mismatch(self) -> None:
        """Suite ID mismatch is denied (Law #6)."""
        sm = InboxSendStateMachine("email-7", SUITE, OFFICE)
        with pytest.raises(InboxSendInvalidTransition) as exc_info:
            sm.transition(
                "email-7", "draft", "approved",
                suite_id="wrong-suite", office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence=YELLOW_APPROVAL,
            )
        assert exc_info.value.denial_receipt.reason_code == "suite_id_mismatch"


# ============================================================================
# 2. InvoiceStateMachine (7 tests)
# ============================================================================


class TestInvoiceStateMachine:
    """Tests for the 7-state invoice lifecycle."""

    def test_happy_path_draft_to_refunded(self) -> None:
        """Happy path: DRAFT -> APPROVED -> SENT -> VIEWED -> PAID -> REFUNDED."""
        sm = InvoiceStateMachine("inv-1", SUITE, OFFICE)
        evidence = {
            "approver": "tonio",
            "recipient": "client@example.com",
            "amount": 1500.00,
            "line_items": [{"desc": "Consulting", "qty": 1, "price": 1500}],
        }
        sm.transition(
            "inv-1", "draft", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence=evidence,
        )
        sm.transition(
            "inv-1", "approved", "sent",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence={"approver": "tonio"},
        )
        sm.transition(
            "inv-1", "sent", "viewed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        sm.transition(
            "inv-1", "viewed", "paid",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        r = sm.transition(
            "inv-1", "paid", "refunded",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence={"approver": "tonio", "reason": "refund requested"},
            presence_token="presence-xyz",
        )
        assert r.outcome == "success"
        assert r.risk_tier == "red"
        assert sm.current_state == "refunded"
        assert sm.is_terminal
        assert len(sm.history) == 5

    def test_void_from_any_non_terminal(self) -> None:
        """VOID is reachable from DRAFT, APPROVED, SENT, VIEWED, PAID."""
        for start_state in ("draft", "approved", "sent", "viewed", "paid"):
            sm = InvoiceStateMachine(f"inv-void-{start_state}", SUITE, OFFICE, initial_state=start_state)
            r = sm.transition(
                f"inv-void-{start_state}", start_state, "void",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={"approver": "tonio", "reason": "voided"},
            )
            assert r.outcome == "success"
            assert sm.current_state == "void"
            assert sm.is_terminal

    def test_invalid_transition_blocked(self) -> None:
        """DRAFT -> PAID is not valid — denial receipt emitted."""
        sm = InvoiceStateMachine("inv-bad", SUITE, OFFICE)
        with pytest.raises(InvoiceInvalidTransition) as exc_info:
            sm.transition(
                "inv-bad", "draft", "paid",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"

    def test_approval_binding_fields_required(self) -> None:
        """DRAFT -> APPROVED without binding fields (recipient, amount, line_items) is denied."""
        sm = InvoiceStateMachine("inv-bind", SUITE, OFFICE)
        with pytest.raises(InvoiceInvalidTransition) as exc_info:
            sm.transition(
                "inv-bind", "draft", "approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={"approver": "tonio"},  # missing binding fields
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_binding_fields"

    def test_refunded_requires_presence_token(self) -> None:
        """PAID -> REFUNDED is RED tier and requires presence_token."""
        sm = InvoiceStateMachine("inv-ref", SUITE, OFFICE, initial_state="paid")
        with pytest.raises(InvoiceInvalidTransition) as exc_info:
            sm.transition(
                "inv-ref", "paid", "refunded",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={"approver": "tonio"},
                # No presence_token!
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_presence_token"

    def test_receipt_has_correct_entity_type(self) -> None:
        """All receipts have entity_type='invoice'."""
        sm = InvoiceStateMachine("inv-type", SUITE, OFFICE)
        evidence = {
            "approver": "tonio",
            "recipient": "x@y.com",
            "amount": 100,
            "line_items": [{"a": 1}],
        }
        r = sm.transition(
            "inv-type", "draft", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence=evidence,
        )
        assert r.entity_type == "invoice"

    def test_refunded_not_reachable_from_void(self) -> None:
        """VOID is terminal — cannot transition to REFUNDED."""
        sm = InvoiceStateMachine("inv-void-term", SUITE, OFFICE, initial_state="void")
        assert sm.is_terminal
        with pytest.raises(InvoiceInvalidTransition) as exc_info:
            sm.transition(
                "inv-void-term", "void", "refunded",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={"approver": "tonio"},
                presence_token="p-tok",
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"


# ============================================================================
# 3. ContractStateMachine (7 tests)
# ============================================================================


class TestContractStateMachine:
    """Tests for the 6-state contract lifecycle."""

    def test_happy_path_draft_to_archived(self) -> None:
        """Happy path: DRAFT -> REVIEWED -> SENT -> SIGNED -> ARCHIVED."""
        sm = ContractStateMachine("ctr-1", SUITE, OFFICE)

        # DRAFT -> REVIEWED (GREEN)
        r1 = sm.transition(
            "ctr-1", "draft", "reviewed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r1.risk_tier == "green"

        # REVIEWED -> SENT (YELLOW)
        r2 = sm.transition(
            "ctr-1", "reviewed", "sent",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence={"approver": "tonio"},
        )
        assert r2.risk_tier == "yellow"

        # SENT -> SIGNED (RED + presence)
        r3 = sm.transition(
            "ctr-1", "sent", "signed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence={"approver": "tonio", "legal_review": True},
            presence_token="presence-abc",
        )
        assert r3.risk_tier == "red"

        # SIGNED -> ARCHIVED (GREEN)
        r4 = sm.transition(
            "ctr-1", "signed", "archived",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r4.risk_tier == "green"
        assert sm.current_state == "archived"
        assert sm.is_terminal
        assert len(sm.history) == 4

    def test_signed_requires_presence(self) -> None:
        """SENT -> SIGNED requires presence_token (RED + binding legal action)."""
        sm = ContractStateMachine("ctr-pres", SUITE, OFFICE, initial_state="sent")
        with pytest.raises(ContractInvalidTransition) as exc_info:
            sm.transition(
                "ctr-pres", "sent", "signed",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={"approver": "tonio"},
                # No presence_token!
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_presence_token"

    def test_sent_requires_yellow_approval(self) -> None:
        """REVIEWED -> SENT requires approval evidence (YELLOW tier)."""
        sm = ContractStateMachine("ctr-yel", SUITE, OFFICE, initial_state="reviewed")
        with pytest.raises(ContractInvalidTransition) as exc_info:
            sm.transition(
                "ctr-yel", "reviewed", "sent",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_approval_evidence"

    def test_expired_from_sent(self) -> None:
        """SENT -> EXPIRED is valid and emits warning receipt."""
        sm = ContractStateMachine("ctr-exp", SUITE, OFFICE, initial_state="sent")
        r = sm.transition(
            "ctr-exp", "sent", "expired",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="system",
        )
        assert r.outcome == "success"
        assert r.warning is not None
        assert "expired" in r.warning.lower()
        assert sm.is_terminal

    def test_expired_from_signed(self) -> None:
        """SIGNED -> EXPIRED is valid (time-based)."""
        sm = ContractStateMachine("ctr-exp2", SUITE, OFFICE, initial_state="signed")
        r = sm.transition(
            "ctr-exp2", "signed", "expired",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="system",
        )
        assert r.outcome == "success"
        assert r.warning is not None

    def test_invalid_transition_draft_to_signed(self) -> None:
        """DRAFT -> SIGNED is not valid — must go through REVIEWED and SENT first."""
        sm = ContractStateMachine("ctr-inv", SUITE, OFFICE)
        with pytest.raises(ContractInvalidTransition) as exc_info:
            sm.transition(
                "ctr-inv", "draft", "signed",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={"approver": "tonio"},
                presence_token="p-tok",
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"

    def test_office_id_mismatch_denied(self) -> None:
        """Office ID mismatch is denied (Law #6)."""
        sm = ContractStateMachine("ctr-iso", SUITE, OFFICE)
        with pytest.raises(ContractInvalidTransition) as exc_info:
            sm.transition(
                "ctr-iso", "draft", "reviewed",
                suite_id=SUITE, office_id="wrong-office",
                correlation_id=CORR, actor_id=ACTOR,
            )
        assert exc_info.value.denial_receipt.reason_code == "office_id_mismatch"


# ============================================================================
# 4. PayrollStateMachine (7 tests)
# ============================================================================


class TestPayrollStateMachine:
    """Tests for the 6-state payroll lifecycle."""

    def _make_dual_approval(self) -> dict:
        """Helper: valid dual-approval evidence (HR + Finance)."""
        return {
            "approvals": [
                {"role": "hr", "approver_id": "hr-user-1", "approved_at": "2026-02-14T12:00:00Z"},
                {"role": "finance", "approver_id": "fin-user-1", "approved_at": "2026-02-14T12:01:00Z"},
            ],
        }

    def test_happy_path_snapshot_to_confirmed(self) -> None:
        """Happy path: SNAPSHOT -> REVIEWED -> APPROVED -> PROCESSING -> CONFIRMED."""
        sm = PayrollStateMachine("pr-1", SUITE, OFFICE)
        assert sm.current_state == "snapshot"

        sm.transition(
            "pr-1", "snapshot", "reviewed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        sm.transition(
            "pr-1", "reviewed", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
            approval_evidence=self._make_dual_approval(),
            presence_token="presence-pr",
        )
        sm.transition(
            "pr-1", "approved", "processing",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        r = sm.transition(
            "pr-1", "processing", "confirmed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r.outcome == "success"
        assert sm.current_state == "confirmed"
        assert sm.is_terminal
        assert len(sm.history) == 4

    def test_processing_to_failed(self) -> None:
        """PROCESSING -> FAILED is valid terminal."""
        sm = PayrollStateMachine("pr-fail", SUITE, OFFICE, initial_state="processing")
        r = sm.transition(
            "pr-fail", "processing", "failed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r.outcome == "success"
        assert sm.is_terminal

    def test_dual_approval_required(self) -> None:
        """REVIEWED -> APPROVED requires both HR and Finance approvals."""
        sm = PayrollStateMachine("pr-dual", SUITE, OFFICE, initial_state="reviewed")
        # Only HR, missing Finance
        with pytest.raises(PayrollInvalidTransition) as exc_info:
            sm.transition(
                "pr-dual", "reviewed", "approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence={
                    "approvals": [
                        {"role": "hr", "approver_id": "hr-user"},
                    ],
                },
                presence_token="pres-tok",
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_dual_approval"

    def test_red_tier_requires_presence(self) -> None:
        """REVIEWED -> APPROVED (RED) requires presence_token."""
        sm = PayrollStateMachine("pr-pres", SUITE, OFFICE, initial_state="reviewed")
        with pytest.raises(PayrollInvalidTransition) as exc_info:
            sm.transition(
                "pr-pres", "reviewed", "approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                approval_evidence=self._make_dual_approval(),
                # No presence_token!
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_presence_token"

    def test_red_tier_requires_approval_evidence(self) -> None:
        """REVIEWED -> APPROVED (RED) without approval_evidence is denied."""
        sm = PayrollStateMachine("pr-noev", SUITE, OFFICE, initial_state="reviewed")
        with pytest.raises(PayrollInvalidTransition) as exc_info:
            sm.transition(
                "pr-noev", "reviewed", "approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
                presence_token="pres-tok",
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_approval_evidence"

    def test_deadline_warning_receipt(self) -> None:
        """Past-deadline transitions emit warning in receipt."""
        past_deadline = datetime.now(timezone.utc) - timedelta(hours=1)
        sm = PayrollStateMachine("pr-dl", SUITE, OFFICE, deadline=past_deadline)
        r = sm.transition(
            "pr-dl", "snapshot", "reviewed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id=ACTOR,
        )
        assert r.outcome == "success"
        assert r.warning is not None
        assert "past deadline" in r.warning.lower()

    def test_terminal_state_blocked(self) -> None:
        """Cannot transition from CONFIRMED (terminal)."""
        sm = PayrollStateMachine("pr-term", SUITE, OFFICE, initial_state="confirmed")
        assert sm.is_terminal
        with pytest.raises(PayrollInvalidTransition) as exc_info:
            sm.transition(
                "pr-term", "confirmed", "snapshot",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"


# ============================================================================
# 5. PaymentStateMachine (7 tests)
# ============================================================================


class TestPaymentStateMachine:
    """Tests for the 6-state payment lifecycle with dual sequential approval."""

    def test_happy_path_draft_to_reconciled(self) -> None:
        """Happy path: DRAFT -> OWNER_APPROVED -> ACCOUNTANT_APPROVED -> EXECUTING -> RECONCILED."""
        sm = PaymentStateMachine("pay-1", SUITE, OFFICE)

        # DRAFT -> OWNER_APPROVED (RED + presence)
        r1 = sm.transition(
            "pay-1", "draft", "owner_approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="owner-tonio",
            approval_evidence={"approver_role": "owner", "approver_id": "owner-tonio"},
            presence_token="presence-owner",
        )
        assert r1.risk_tier == "red"

        # OWNER_APPROVED -> ACCOUNTANT_APPROVED (RED)
        r2 = sm.transition(
            "pay-1", "owner_approved", "accountant_approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="accountant-jane",
            approval_evidence={"approver_role": "accountant", "approver_id": "accountant-jane"},
        )
        assert r2.risk_tier == "red"

        # ACCOUNTANT_APPROVED -> EXECUTING
        r3 = sm.transition(
            "pay-1", "accountant_approved", "executing",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="system",
        )
        assert r3.outcome == "success"

        # EXECUTING -> RECONCILED
        r4 = sm.transition(
            "pay-1", "executing", "reconciled",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="system",
        )
        assert r4.outcome == "success"
        assert sm.current_state == "reconciled"
        assert sm.is_terminal
        assert len(sm.history) == 4

    def test_executing_to_failed(self) -> None:
        """EXECUTING -> FAILED is valid terminal."""
        sm = PaymentStateMachine("pay-fail", SUITE, OFFICE, initial_state="executing")
        r = sm.transition(
            "pay-fail", "executing", "failed",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="system",
        )
        assert r.outcome == "success"
        assert sm.is_terminal

    def test_owner_approval_requires_presence(self) -> None:
        """DRAFT -> OWNER_APPROVED requires presence_token (owner must be present)."""
        sm = PaymentStateMachine("pay-pres", SUITE, OFFICE)
        with pytest.raises(PaymentInvalidTransition) as exc_info:
            sm.transition(
                "pay-pres", "draft", "owner_approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id="owner-tonio",
                approval_evidence={"approver_role": "owner"},
                # No presence_token!
            )
        assert exc_info.value.denial_receipt.reason_code == "missing_presence_token"

    def test_owner_role_validated(self) -> None:
        """Owner approval step rejects wrong approver_role."""
        sm = PaymentStateMachine("pay-role", SUITE, OFFICE)
        with pytest.raises(PaymentInvalidTransition) as exc_info:
            sm.transition(
                "pay-role", "draft", "owner_approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id="someone",
                approval_evidence={"approver_role": "accountant"},  # wrong role
                presence_token="pres-tok",
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_approver_role"

    def test_accountant_role_validated(self) -> None:
        """Accountant approval step rejects wrong approver_role."""
        sm = PaymentStateMachine("pay-acct", SUITE, OFFICE, initial_state="owner_approved")
        with pytest.raises(PaymentInvalidTransition) as exc_info:
            sm.transition(
                "pay-acct", "owner_approved", "accountant_approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id="someone",
                approval_evidence={"approver_role": "owner"},  # wrong role
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_approver_role"

    def test_sequential_order_enforced(self) -> None:
        """Cannot skip owner approval — DRAFT -> ACCOUNTANT_APPROVED is invalid."""
        sm = PaymentStateMachine("pay-skip", SUITE, OFFICE)
        with pytest.raises(PaymentInvalidTransition) as exc_info:
            sm.transition(
                "pay-skip", "draft", "accountant_approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id="accountant",
                approval_evidence={"approver_role": "accountant"},
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"

    def test_terminal_state_blocked(self) -> None:
        """Cannot transition from RECONCILED (terminal)."""
        sm = PaymentStateMachine("pay-term", SUITE, OFFICE, initial_state="reconciled")
        assert sm.is_terminal
        with pytest.raises(PaymentInvalidTransition) as exc_info:
            sm.transition(
                "pay-term", "reconciled", "draft",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id=ACTOR,
            )
        assert exc_info.value.denial_receipt.reason_code == "invalid_transition"


# =============================================================================
# Evil Tests: Dual-Approval Same-Approver Bypass (Law #4 + Gate 5)
# =============================================================================


class TestPayrollDualApprovalSameApproverEvil:
    """Evil tests: Payroll dual approval bypass by same person as HR + Finance."""

    def test_same_approver_id_for_both_roles_is_rejected(self) -> None:
        """Same person cannot approve as both HR and Finance (collusion bypass)."""
        sm = PayrollStateMachine("pr-evil-1", SUITE, OFFICE, initial_state="reviewed")
        with pytest.raises(PayrollInvalidTransition) as exc_info:
            sm.transition(
                "pr-evil-1", "reviewed", "approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id="evil-cfo",
                approval_evidence={
                    "approvals": [
                        {"role": "hr", "approver_id": "same-person-123"},
                        {"role": "finance", "approver_id": "same-person-123"},
                    ],
                },
                presence_token="present",
            )
        assert exc_info.value.denial_receipt.reason_code == "dual_approval_same_approver"
        assert exc_info.value.denial_receipt.outcome == "denied"

    def test_distinct_approvers_succeed(self) -> None:
        """Different approver_ids for HR and Finance proceed normally."""
        sm = PayrollStateMachine("pr-good-1", SUITE, OFFICE, initial_state="reviewed")
        receipt = sm.transition(
            "pr-good-1", "reviewed", "approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="cfo",
            approval_evidence={
                "approvals": [
                    {"role": "hr", "approver_id": "hr-person-1"},
                    {"role": "finance", "approver_id": "finance-person-2"},
                ],
            },
            presence_token="present",
        )
        assert receipt.outcome == "success"
        assert receipt.to_state == "approved"


class TestPaymentDualApprovalSameApproverEvil:
    """Evil tests: Payment sequential dual approval bypass by same person."""

    def test_same_person_as_owner_and_accountant_rejected(self) -> None:
        """Same person cannot approve as owner then accountant (collusion bypass)."""
        sm = PaymentStateMachine("pay-evil-1", SUITE, OFFICE)
        # Step 1: Owner approves (succeeds)
        sm.transition(
            "pay-evil-1", "draft", "owner_approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="colluding-person",
            approval_evidence={"approver_role": "owner", "approver_id": "user-X"},
            presence_token="present",
        )
        # Step 2: Same person tries accountant approval (must be rejected)
        with pytest.raises(PaymentInvalidTransition) as exc_info:
            sm.transition(
                "pay-evil-1", "owner_approved", "accountant_approved",
                suite_id=SUITE, office_id=OFFICE,
                correlation_id=CORR, actor_id="colluding-person",
                approval_evidence={"approver_role": "accountant", "approver_id": "user-X"},
            )
        assert exc_info.value.denial_receipt.reason_code == "dual_approval_same_approver"
        assert exc_info.value.denial_receipt.outcome == "denied"

    def test_distinct_owner_and_accountant_succeed(self) -> None:
        """Different people for owner and accountant proceed normally."""
        sm = PaymentStateMachine("pay-good-1", SUITE, OFFICE)
        # Step 1: Owner approves
        sm.transition(
            "pay-good-1", "draft", "owner_approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="owner",
            approval_evidence={"approver_role": "owner", "approver_id": "owner-A"},
            presence_token="present",
        )
        # Step 2: Different accountant approves
        receipt = sm.transition(
            "pay-good-1", "owner_approved", "accountant_approved",
            suite_id=SUITE, office_id=OFFICE,
            correlation_id=CORR, actor_id="accountant",
            approval_evidence={"approver_role": "accountant", "approver_id": "accountant-B"},
        )
        assert receipt.outcome == "success"
        assert receipt.to_state == "accountant_approved"
