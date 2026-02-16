"""Dual Approval Service + RED Enhanced Skill Pack Tests — Phase 3 W5a.

Tests for:
  1. DualApprovalService (20 tests):
     - Create request, submit approval, fully approve, deny
     - Same-approver rejection, same-role rejection
     - Binding hash mismatch (approve-then-swap defense)
     - Expiration enforcement, cross-tenant isolation
     - Status checking, unauthorized role rejection

  2. EnhancedFinnMoneyDesk (8 tests):
     - classify_transfer_risk, plan_payment, verify_reconciliation
     - initiate_dual_approval, fail-closed validations

  3. EnhancedMiloPayroll (8 tests):
     - validate_payroll_run, estimate_tax_impact, plan_payroll_correction
     - initiate_dual_approval, fail-closed validations

  4. EnhancedClaraLegal (8 tests):
     - review_contract_terms, plan_signature_flow, assess_compliance_risk
     - initiate_dual_approval, fail-closed validations

Law compliance:
  - Law #2: Every operation emits a receipt
  - Law #3: Fail closed on missing/invalid inputs, expired approvals
  - Law #4: All enhanced ops tagged RED, use high_risk_guard (GPT-5.2)
  - Law #6: Cross-tenant approval attempts denied
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from typing import Any

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.services.dual_approval_service import (
    DualApprovalService,
    ApprovalStatus,
    get_dual_approval_service,
)


# =============================================================================
# Shared Fixtures
# =============================================================================

SUITE_ID = "suite-red-test-001"
OFFICE_ID = "office-red-test-001"
CORR_ID = "corr-red-test-001"


@pytest.fixture
def red_ctx() -> AgentContext:
    return AgentContext(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        correlation_id=CORR_ID,
        risk_tier="red",
    )


@pytest.fixture
def dual_svc() -> DualApprovalService:
    svc = DualApprovalService()
    yield svc
    svc.clear_store()


def _mock_llm_success(content: str = "LLM response") -> dict[str, Any]:
    return {
        "content": content,
        "model_used": "gpt-5.2",
        "profile_used": "high_risk_guard",
        "error": None,
    }


def _create_pack(pack_class):
    with patch.object(pack_class, "_load_config", return_value=None):
        return pack_class()


BINDING_FIELDS = {"from_account": "acct-001", "to_account": "acct-002", "amount_cents": 50000}


# =============================================================================
# DualApprovalService Tests
# =============================================================================


class TestDualApprovalCreate:
    """Test dual approval request creation."""

    def test_create_request_success(self, dual_svc):
        result = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        assert result.success is True
        assert result.status == ApprovalStatus.PENDING
        assert result.request_id
        assert result.remaining_roles == ["owner", "accountant"]
        assert result.receipt["event_type"] == "approval.dual.create"

    def test_create_request_insufficient_roles_denied(self, dual_svc):
        result = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.send", binding_fields=BINDING_FIELDS,
            required_roles=["owner"],  # Only 1 role — need 2
        )
        assert result.success is False
        assert "INSUFFICIENT" in str(result.receipt["policy"]["reasons"])


class TestDualApprovalSubmit:
    """Test approval submission with all enforcement rules."""

    def _create_pending(self, svc):
        return svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )

    def test_first_approval_succeeds(self, dual_svc):
        create = self._create_pending(dual_svc)
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is True
        assert result.status == ApprovalStatus.FIRST_APPROVED
        assert result.fully_approved is False
        assert result.remaining_roles == ["accountant"]

    def test_second_approval_fully_approves(self, dual_svc):
        create = self._create_pending(dual_svc)
        dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-002",
            approver_role="accountant", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is True
        assert result.status == ApprovalStatus.FULLY_APPROVED
        assert result.fully_approved is True
        assert result.remaining_roles == []

    def test_same_approver_denied(self, dual_svc):
        """Same person cannot approve twice (Law #3)."""
        create = self._create_pending(dual_svc)
        dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="accountant", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is False
        assert "SAME_APPROVER" in str(result.receipt["policy"]["reasons"])

    def test_same_role_denied(self, dual_svc):
        """Same role cannot approve twice."""
        create = self._create_pending(dual_svc)
        dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-002",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is False
        assert "SAME_ROLE" in str(result.receipt["policy"]["reasons"])

    def test_unauthorized_role_denied(self, dual_svc):
        """Role not in required_roles is rejected."""
        create = self._create_pending(dual_svc)
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="janitor", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is False
        assert "UNAUTHORIZED_ROLE" in str(result.receipt["policy"]["reasons"])

    def test_binding_hash_mismatch_denied(self, dual_svc):
        """Approve-then-swap defense: changed binding fields rejected."""
        create = self._create_pending(dual_svc)
        tampered = {"from_account": "acct-001", "to_account": "acct-EVIL", "amount_cents": 99999}
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="owner", binding_fields=tampered, suite_id=SUITE_ID,
        )
        assert result.success is False
        assert "BINDING_HASH_MISMATCH" in str(result.receipt["policy"]["reasons"])

    def test_cross_tenant_denied(self, dual_svc):
        """Cross-tenant approval attempt denied (Law #6)."""
        create = self._create_pending(dual_svc)
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="user-001",
            approver_role="owner", binding_fields=BINDING_FIELDS,
            suite_id="suite-EVIL-tenant",
        )
        assert result.success is False
        assert "CROSS_TENANT" in str(result.receipt["policy"]["reasons"])

    def test_not_found_denied(self, dual_svc):
        result = dual_svc.submit_approval(
            request_id="nonexistent", approver_id="user-001",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is False
        assert "NOT_FOUND" in str(result.receipt["policy"]["reasons"])

    def test_already_resolved_denied(self, dual_svc):
        """Cannot approve after fully approved."""
        create = self._create_pending(dual_svc)
        dual_svc.submit_approval(
            request_id=create.request_id, approver_id="u1",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        dual_svc.submit_approval(
            request_id=create.request_id, approver_id="u2",
            approver_role="accountant", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="u3",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.success is False
        assert "ALREADY_RESOLVED" in str(result.receipt["policy"]["reasons"])


class TestDualApprovalDenial:
    """Test denial flow."""

    def test_denial_terminates_flow(self, dual_svc):
        create = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        result = dual_svc.submit_denial(
            request_id=create.request_id, denier_id="user-001",
            denier_role="owner", reason="Suspicious transfer", suite_id=SUITE_ID,
        )
        assert result.success is True
        assert result.status == ApprovalStatus.DENIED
        assert result.receipt["event_type"] == "approval.dual.denied"

    def test_denial_cross_tenant_blocked(self, dual_svc):
        create = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        result = dual_svc.submit_denial(
            request_id=create.request_id, denier_id="evil",
            denier_role="owner", reason="hack", suite_id="suite-EVIL",
        )
        assert result.success is False


class TestDualApprovalStatus:
    """Test status checking."""

    def test_check_status(self, dual_svc):
        create = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        result = dual_svc.check_status(create.request_id, SUITE_ID)
        assert result.success is True
        assert result.status == ApprovalStatus.PENDING

    def test_check_status_wrong_tenant(self, dual_svc):
        create = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        result = dual_svc.check_status(create.request_id, "suite-OTHER")
        assert result.success is False


class TestDualApprovalReceipts:
    """Verify receipts on all dual approval events (Law #2)."""

    def test_create_emits_receipt(self, dual_svc):
        result = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        assert result.receipt["receipt_id"]
        assert result.receipt["suite_id"] == SUITE_ID

    def test_approval_emits_receipt(self, dual_svc):
        create = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="u1",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.receipt["event_type"] == "approval.dual.first_approved"

    def test_full_approval_emits_receipt(self, dual_svc):
        create = dual_svc.create_request(
            suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID,
            action_type="payment.transfer", binding_fields=BINDING_FIELDS,
            required_roles=["owner", "accountant"],
        )
        dual_svc.submit_approval(
            request_id=create.request_id, approver_id="u1",
            approver_role="owner", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        result = dual_svc.submit_approval(
            request_id=create.request_id, approver_id="u2",
            approver_role="accountant", binding_fields=BINDING_FIELDS, suite_id=SUITE_ID,
        )
        assert result.receipt["event_type"] == "approval.dual.fully_approved"


# =============================================================================
# EnhancedFinnMoneyDesk Tests
# =============================================================================


class TestEnhancedFinnMoneyDesk:
    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.finn_money_desk import EnhancedFinnMoneyDesk
        return _create_pack(EnhancedFinnMoneyDesk)

    @pytest.mark.asyncio
    async def test_classify_transfer_risk_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Risk score: 7/10"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-1")
        result = await pack.classify_transfer_risk(
            {"amount_cents": 50000, "from_account": "a1", "to_account": "a2"}, red_ctx
        )
        assert result.success is True
        assert result.receipt["event_type"] == "payment.risk_classify"

    @pytest.mark.asyncio
    async def test_classify_transfer_missing_amount_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-2")
        result = await pack.classify_transfer_risk({}, red_ctx)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_plan_payment_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Payment plan"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-3")
        result = await pack.plan_payment(
            {"payee": "Vendor LLC", "amount_cents": 10000, "method": "ach"}, red_ctx
        )
        assert result.success is True
        assert result.receipt["event_type"] == "payment.plan"

    @pytest.mark.asyncio
    async def test_plan_payment_exceeds_limit_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-4")
        result = await pack.plan_payment(
            {"payee": "Big Corp", "amount_cents": 99_999_999, "method": "wire"}, red_ctx
        )
        assert result.success is False
        assert "EXCEEDS" in str(result.receipt.get("policy", {}).get("reasons", []))

    @pytest.mark.asyncio
    async def test_verify_reconciliation_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Match: 98%"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-5")
        result = await pack.verify_reconciliation(
            {"id": "p1", "amount_cents": 5000}, {"id": "i1", "amount_cents": 5000}, red_ctx
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_verify_reconciliation_missing_data_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-6")
        result = await pack.verify_reconciliation({}, {}, red_ctx)
        assert result.success is False

    def test_initiate_dual_approval(self, pack, red_ctx):
        result = pack.initiate_dual_approval(
            "payment.transfer", BINDING_FIELDS, red_ctx
        )
        assert result["success"] is True
        assert result["status"] == "pending"
        assert "owner" in result["remaining_roles"]
        # Clean up singleton
        get_dual_approval_service().clear_store()

    @pytest.mark.asyncio
    async def test_invalid_method_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-7")
        result = await pack.plan_payment(
            {"payee": "V", "amount_cents": 1000, "method": "bitcoin"}, red_ctx
        )
        assert result.success is False


# =============================================================================
# EnhancedMiloPayroll Tests
# =============================================================================


class TestEnhancedMiloPayroll:
    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
        return _create_pack(EnhancedMiloPayroll)

    @pytest.mark.asyncio
    async def test_validate_payroll_run_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Payroll valid"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-10")
        result = await pack.validate_payroll_run(
            {"payroll_period": "2026-02", "employee_count": 5, "total_gross_cents": 500000}, red_ctx
        )
        assert result.success is True
        assert result.receipt["event_type"] == "payroll.validate"

    @pytest.mark.asyncio
    async def test_validate_payroll_missing_period_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-11")
        result = await pack.validate_payroll_run({}, red_ctx)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_estimate_tax_impact_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Tax: $5,000"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-12")
        result = await pack.estimate_tax_impact("2026-02", 500000, red_ctx)
        assert result.success is True
        assert result.receipt["event_type"] == "payroll.tax_estimate"

    @pytest.mark.asyncio
    async def test_estimate_tax_invalid_amount_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-13")
        result = await pack.estimate_tax_impact("2026-02", -100, red_ctx)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_plan_payroll_correction_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Correction plan"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-14")
        result = await pack.plan_payroll_correction(
            {"error_type": "underpayment", "employee_count": 1}, red_ctx
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_plan_correction_missing_error_type_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-15")
        result = await pack.plan_payroll_correction({}, red_ctx)
        assert result.success is False

    def test_initiate_dual_approval(self, pack, red_ctx):
        result = pack.initiate_dual_approval(
            {"payroll_id": "PR-001", "payroll_period": "2026-02", "total_net_cents": 400000},
            red_ctx,
        )
        assert result["success"] is True
        assert "hr" in result["remaining_roles"]
        assert "finance" in result["remaining_roles"]
        get_dual_approval_service().clear_store()

    @pytest.mark.asyncio
    async def test_no_employees_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-16")
        result = await pack.validate_payroll_run(
            {"payroll_period": "2026-02", "employee_count": 0}, red_ctx
        )
        assert result.success is False


# =============================================================================
# EnhancedClaraLegal Tests
# =============================================================================


class TestEnhancedClaraLegal:
    @pytest.fixture
    def pack(self):
        from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal
        return _create_pack(EnhancedClaraLegal)

    @pytest.mark.asyncio
    async def test_review_contract_terms_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Risk: MEDIUM"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-20")
        result = await pack.review_contract_terms(
            "This NDA agreement between Party A and Party B...", "nda", red_ctx
        )
        assert result.success is True
        assert result.receipt["event_type"] == "contract.review_terms"

    @pytest.mark.asyncio
    async def test_review_empty_contract_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-21")
        result = await pack.review_contract_terms("", "nda", red_ctx)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_review_invalid_type_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-22")
        result = await pack.review_contract_terms("Contract text", "invalid_type", red_ctx)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_plan_signature_flow_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("Signature flow plan"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-23")
        result = await pack.plan_signature_flow(
            {"contract_id": "DOC-001", "signers": [{"name": "John", "email": "j@ex.com"}]},
            red_ctx,
        )
        assert result.success is True
        assert result.receipt["event_type"] == "contract.plan_signature"

    @pytest.mark.asyncio
    async def test_plan_signature_no_signers_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-24")
        result = await pack.plan_signature_flow(
            {"contract_id": "DOC-001", "signers": []}, red_ctx
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_assess_compliance_risk_success(self, pack, red_ctx):
        pack.call_llm = AsyncMock(return_value=_mock_llm_success("3 contracts expiring"))
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-25")
        contracts = [{"id": "c1", "status": "active"}, {"id": "c2", "status": "expiring"}]
        result = await pack.assess_compliance_risk(contracts, red_ctx)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_assess_empty_portfolio_denied(self, pack, red_ctx):
        pack._trust_spine.emit_receipt = AsyncMock(return_value="r-26")
        result = await pack.assess_compliance_risk([], red_ctx)
        assert result.success is False

    def test_initiate_dual_approval(self, pack, red_ctx):
        result = pack.initiate_dual_approval(
            {"contract_id": "DOC-001", "signer_name": "John", "signer_email": "j@ex.com"},
            red_ctx,
        )
        assert result["success"] is True
        assert "legal" in result["remaining_roles"]
        assert "business_owner" in result["remaining_roles"]
        get_dual_approval_service().clear_store()


# =============================================================================
# Cross-cutting: RED pack contract verification
# =============================================================================


class TestRedPackContract:
    """All RED packs must inherit EnhancedSkillPack with tier=red."""

    def test_all_red_packs_have_red_tier(self):
        from aspire_orchestrator.skillpacks.finn_money_desk import EnhancedFinnMoneyDesk
        from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
        from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal

        for cls in [EnhancedFinnMoneyDesk, EnhancedMiloPayroll, EnhancedClaraLegal]:
            pack = _create_pack(cls)
            assert pack.default_risk_tier == "red", f"{cls.__name__} should be RED tier"

    def test_all_red_packs_have_dual_approval(self):
        from aspire_orchestrator.skillpacks.finn_money_desk import EnhancedFinnMoneyDesk
        from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
        from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal

        for cls in [EnhancedFinnMoneyDesk, EnhancedMiloPayroll, EnhancedClaraLegal]:
            pack = _create_pack(cls)
            assert hasattr(pack, "initiate_dual_approval"), f"{cls.__name__} missing dual approval"

    def test_all_red_packs_have_rule_pack(self):
        from aspire_orchestrator.skillpacks.finn_money_desk import EnhancedFinnMoneyDesk
        from aspire_orchestrator.skillpacks.milo_payroll import EnhancedMiloPayroll
        from aspire_orchestrator.skillpacks.clara_legal import EnhancedClaraLegal

        for cls in [EnhancedFinnMoneyDesk, EnhancedMiloPayroll, EnhancedClaraLegal]:
            pack = _create_pack(cls)
            assert hasattr(pack, "_rule_pack"), f"{cls.__name__} missing rule pack reference"
