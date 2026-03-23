"""Tests for Policy Engine — YAML-driven deterministic evaluation (Law #4, W4-06/07).

Covers:
- YAML loading and parsing
- 9-step evaluation correctness
- GREEN/YELLOW/RED tier classification
- Fail-closed for unknown actions
- Approval requirements by tier
- Presence requirements for RED only
- Tool set computation
- Capability scope derivation
- Redaction field propagation
- Policy matrix introspection
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.policy_engine import (
    PolicyAction,
    PolicyEvalResult,
    PolicyMatrix,
    load_policy_matrix,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def matrix() -> PolicyMatrix:
    """Load the production policy matrix."""
    return load_policy_matrix()


# ===========================================================================
# YAML Loading Tests
# ===========================================================================


class TestYAMLLoading:
    def test_loads_successfully(self, matrix: PolicyMatrix) -> None:
        assert matrix.version == "1.0.0"
        assert len(matrix.actions) > 0

    def test_has_green_actions(self, matrix: PolicyMatrix) -> None:
        green = matrix.list_actions(RiskTier.GREEN)
        assert len(green) >= 10  # 10 GREEN actions defined

    def test_has_yellow_actions(self, matrix: PolicyMatrix) -> None:
        yellow = matrix.list_actions(RiskTier.YELLOW)
        assert len(yellow) >= 10  # 10+ YELLOW actions defined

    def test_has_red_actions(self, matrix: PolicyMatrix) -> None:
        red = matrix.list_actions(RiskTier.RED)
        assert len(red) >= 6  # 6 RED actions defined (payment.* removed)

    def test_total_actions_count(self, matrix: PolicyMatrix) -> None:
        all_actions = matrix.list_actions()
        assert len(all_actions) >= 28  # At least 28 actions total

    def test_defaults_deny_by_default(self, matrix: PolicyMatrix) -> None:
        assert matrix.deny_by_default is True

    def test_defaults_capability_ttl(self, matrix: PolicyMatrix) -> None:
        assert matrix.capability_max_ttl_seconds == 60

    def test_defaults_fail_closed_dlp(self, matrix: PolicyMatrix) -> None:
        assert matrix.fail_closed_on_dlp_error is True

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_policy_matrix(path="/nonexistent/path.yaml")


# ===========================================================================
# GREEN Tier Evaluation Tests
# ===========================================================================


class TestGreenTierEvaluation:
    def test_calendar_read_is_green(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("calendar.read")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.GREEN
        assert result.approval_required is False
        assert result.presence_required is False

    def test_receipts_search_is_green(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("receipts.search")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.GREEN

    def test_green_has_tools(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("calendar.read")
        assert len(result.tools) > 0
        assert "calendar.event.list" in result.tools

    def test_green_has_capability_scope(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("calendar.read")
        assert result.capability_scope == "calendar:read"

    def test_contacts_read_redacts_pii(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("contacts.read")
        assert "email" in result.redact_fields
        assert "phone" in result.redact_fields


# ===========================================================================
# YELLOW Tier Evaluation Tests
# ===========================================================================


class TestYellowTierEvaluation:
    def test_invoice_create_is_green(self, matrix: PolicyMatrix) -> None:
        """invoice.create is GREEN (draft creation, no money moves)."""
        result = matrix.evaluate("invoice.create")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.GREEN
        assert result.approval_required is False
        assert result.presence_required is False

    def test_email_send_is_yellow(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("email.send")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.YELLOW
        assert result.approval_required is True

    def test_yellow_has_binding_fields(self, matrix: PolicyMatrix) -> None:
        action = matrix.get_action("email.send")
        assert action is not None
        assert len(action.approval_binding_fields) > 0
        assert "to" in action.approval_binding_fields

    def test_email_redacts_pii(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("email.send")
        assert "to" in result.redact_fields
        assert "body" in result.redact_fields

    def test_invoice_has_required_params(self, matrix: PolicyMatrix) -> None:
        action = matrix.get_action("invoice.create")
        assert action is not None
        assert "customer_email" in action.params_required
        assert "amount" in action.params_required


# ===========================================================================
# RED Tier Evaluation Tests
# ===========================================================================


class TestRedTierEvaluation:
    def test_contract_sign_is_red(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("contract.sign")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.RED
        assert result.approval_required is True
        assert result.presence_required is True

    def test_contract_sign_is_red_alternative(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("contract.sign")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.RED
        assert result.presence_required is True

    def test_payroll_run_is_red(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("payroll.run")
        assert result.allowed is True
        assert result.risk_tier == RiskTier.RED

    def test_red_has_spend_fields(self, matrix: PolicyMatrix) -> None:
        action = matrix.get_action("payroll.run")
        assert action is not None
        assert action.approval_type == "spend"
        assert "payroll_id" in action.approval_binding_fields
        assert "total_amount" in action.approval_binding_fields

    def test_payroll_redacts_sensitive(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("payroll.run")
        assert "ssn" in result.redact_fields
        assert "bank_routing" in result.redact_fields


# ===========================================================================
# Fail-Closed Tests
# ===========================================================================


class TestFailClosed:
    def test_unknown_action_denied(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("hack.system")
        assert result.allowed is False
        assert result.deny_reason is not None
        assert "Unknown" in result.deny_reason

    def test_unknown_action_defaults_to_yellow(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("not.a.real.action")
        assert result.risk_tier == RiskTier.YELLOW  # Per spec: default unknown to YELLOW

    def test_unknown_action_no_tools(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("fake.action")
        assert result.tools == []

    def test_unknown_action_no_approval(self, matrix: PolicyMatrix) -> None:
        """Unknown actions are denied outright — no approval flow."""
        result = matrix.evaluate("fake.action")
        assert result.approval_required is False


# ===========================================================================
# Introspection Tests
# ===========================================================================


class TestIntrospection:
    def test_list_all_actions(self, matrix: PolicyMatrix) -> None:
        all_actions = matrix.list_actions()
        assert "calendar.read" in all_actions
        assert "contract.sign" in all_actions
        assert "invoice.create" in all_actions

    def test_list_green_only(self, matrix: PolicyMatrix) -> None:
        green = matrix.list_actions(RiskTier.GREEN)
        for action_name in green:
            action = matrix.get_action(action_name)
            assert action is not None
            assert action.risk_tier == RiskTier.GREEN

    def test_get_action_returns_none_for_unknown(self, matrix: PolicyMatrix) -> None:
        assert matrix.get_action("nonexistent") is None

    def test_action_has_category(self, matrix: PolicyMatrix) -> None:
        action = matrix.get_action("contract.sign")
        assert action is not None
        assert action.category == "contracts"

    def test_action_has_capability_scope(self, matrix: PolicyMatrix) -> None:
        action = matrix.get_action("contract.sign")
        assert action is not None
        assert action.capability_scope == "contracts:sign"


# ===========================================================================
# Domain-Specific Action Tests (Mail + Domain from Phase 0C)
# ===========================================================================


class TestMailDomainActions:
    def test_domain_check_is_green(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("domain.check")
        assert result.risk_tier == RiskTier.GREEN

    def test_domain_dns_create_is_yellow(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("domain.dns.create")
        assert result.risk_tier == RiskTier.YELLOW
        assert result.approval_required is True

    def test_domain_purchase_is_red(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("domain.purchase")
        assert result.risk_tier == RiskTier.RED
        assert result.presence_required is True

    def test_mail_account_read_is_green(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("mail.account.read")
        assert result.risk_tier == RiskTier.GREEN

    def test_mail_account_create_is_yellow(self, matrix: PolicyMatrix) -> None:
        result = matrix.evaluate("mail.account.create")
        assert result.risk_tier == RiskTier.YELLOW
