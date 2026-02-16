"""Tests for Kill Switch Service — Phase 2.5 Wave 4."""

import os
import pytest

from aspire_orchestrator.services.kill_switch import (
    KillSwitchMode,
    KillSwitchResult,
    check_kill_switch,
    get_kill_switch_mode,
    reset_kill_switch,
    set_kill_switch_mode,
)
from aspire_orchestrator.services.receipt_store import clear_store, query_receipts


@pytest.fixture(autouse=True)
def _reset():
    """Reset kill switch state between tests."""
    reset_kill_switch()
    clear_store()
    # Clear env var if set
    os.environ.pop("ASPIRE_KILL_SWITCH", None)
    yield
    reset_kill_switch()
    clear_store()
    os.environ.pop("ASPIRE_KILL_SWITCH", None)


class TestKillSwitchMode:
    """Test kill switch mode resolution."""

    def test_default_mode_is_enabled(self):
        assert get_kill_switch_mode() == KillSwitchMode.ENABLED

    def test_env_var_disabled(self):
        os.environ["ASPIRE_KILL_SWITCH"] = "DISABLED"
        assert get_kill_switch_mode() == KillSwitchMode.DISABLED

    def test_env_var_approval_only(self):
        os.environ["ASPIRE_KILL_SWITCH"] = "APPROVAL_ONLY"
        assert get_kill_switch_mode() == KillSwitchMode.APPROVAL_ONLY

    def test_invalid_env_var_defaults_to_enabled(self):
        os.environ["ASPIRE_KILL_SWITCH"] = "INVALID"
        assert get_kill_switch_mode() == KillSwitchMode.ENABLED

    def test_runtime_override_takes_precedence(self):
        os.environ["ASPIRE_KILL_SWITCH"] = "ENABLED"
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        assert get_kill_switch_mode() == KillSwitchMode.DISABLED


class TestKillSwitchModeChange:
    """Test kill switch mode changes produce receipts."""

    def test_mode_change_returns_receipt(self):
        receipt = set_kill_switch_mode(KillSwitchMode.DISABLED)
        assert receipt["receipt_type"] == "kill_switch.mode_changed"
        assert receipt["outcome"] == "success"
        assert receipt["details"]["old_mode"] == "ENABLED"
        assert receipt["details"]["new_mode"] == "DISABLED"

    def test_mode_change_has_required_fields(self):
        receipt = set_kill_switch_mode(KillSwitchMode.APPROVAL_ONLY)
        assert receipt["id"]
        assert receipt["correlation_id"]
        assert receipt["created_at"]
        assert receipt["actor_id"] == "kill_switch"
        assert receipt["risk_tier"] == "red"


class TestKillSwitchEnabled:
    """Test ENABLED mode — everything passes."""

    def test_green_allowed(self):
        result = check_kill_switch(action_type="calendar.read", risk_tier="green")
        assert result.allowed is True
        assert result.mode == KillSwitchMode.ENABLED

    def test_yellow_allowed(self):
        result = check_kill_switch(action_type="email.send", risk_tier="yellow")
        assert result.allowed is True

    def test_red_allowed(self):
        result = check_kill_switch(action_type="money.transfer", risk_tier="red")
        assert result.allowed is True

    def test_no_receipt_when_allowed(self):
        result = check_kill_switch(action_type="calendar.read", risk_tier="green")
        assert result.receipt is None


class TestKillSwitchDisabled:
    """Test DISABLED mode — block all YELLOW/RED."""

    def test_green_still_allowed(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        result = check_kill_switch(action_type="calendar.read", risk_tier="green")
        assert result.allowed is True

    def test_yellow_blocked(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        result = check_kill_switch(
            action_type="email.send",
            risk_tier="yellow",
            suite_id="test-suite",
            correlation_id="test-corr",
        )
        assert result.allowed is False
        assert result.mode == KillSwitchMode.DISABLED
        assert "DISABLED" in result.reason

    def test_red_blocked(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        result = check_kill_switch(
            action_type="money.transfer",
            risk_tier="red",
            suite_id="test-suite",
        )
        assert result.allowed is False

    def test_blocked_produces_receipt(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        result = check_kill_switch(
            action_type="email.send",
            risk_tier="yellow",
            suite_id="test-suite",
            office_id="test-office",
            correlation_id="test-corr",
        )
        assert result.receipt is not None
        receipt = result.receipt
        assert receipt["receipt_type"] == "kill_switch.activated"
        assert receipt["outcome"] == "denied"
        assert receipt["suite_id"] == "test-suite"
        assert receipt["office_id"] == "test-office"
        assert receipt["details"]["kill_switch_mode"] == "DISABLED"


class TestKillSwitchApprovalOnly:
    """Test APPROVAL_ONLY mode — block new YELLOW/RED."""

    def test_green_allowed(self):
        set_kill_switch_mode(KillSwitchMode.APPROVAL_ONLY)
        result = check_kill_switch(action_type="calendar.read", risk_tier="green")
        assert result.allowed is True

    def test_yellow_blocked(self):
        set_kill_switch_mode(KillSwitchMode.APPROVAL_ONLY)
        result = check_kill_switch(
            action_type="invoice.create",
            risk_tier="yellow",
        )
        assert result.allowed is False
        assert "APPROVAL_ONLY" in result.reason

    def test_red_blocked(self):
        set_kill_switch_mode(KillSwitchMode.APPROVAL_ONLY)
        result = check_kill_switch(
            action_type="payroll.run",
            risk_tier="red",
        )
        assert result.allowed is False

    def test_blocked_receipt_has_correct_reason(self):
        set_kill_switch_mode(KillSwitchMode.APPROVAL_ONLY)
        result = check_kill_switch(
            action_type="invoice.create",
            risk_tier="yellow",
        )
        assert result.receipt["reason_code"] == "kill_switch_approval_only"


class TestKillSwitchReceiptPersistence:
    """Law #2: Kill switch receipts must be persisted in receipt store."""

    def test_mode_change_persists_receipt(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        receipts = query_receipts(suite_id="system")
        assert len(receipts) >= 1
        mode_receipts = [r for r in receipts if r.get("receipt_type") == "kill_switch.mode_changed"]
        assert len(mode_receipts) == 1
        assert mode_receipts[0]["outcome"] == "success"

    def test_disabled_block_persists_receipt(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        check_kill_switch(
            action_type="email.send",
            risk_tier="yellow",
            suite_id="test-suite",
            correlation_id="test-corr",
        )
        receipts = query_receipts(suite_id="test-suite")
        block_receipts = [r for r in receipts if r.get("receipt_type") == "kill_switch.activated"]
        assert len(block_receipts) == 1
        assert block_receipts[0]["outcome"] == "denied"

    def test_approval_only_block_persists_receipt(self):
        set_kill_switch_mode(KillSwitchMode.APPROVAL_ONLY)
        check_kill_switch(
            action_type="invoice.create",
            risk_tier="yellow",
            suite_id="test-suite-2",
            correlation_id="test-corr-2",
        )
        receipts = query_receipts(suite_id="test-suite-2")
        block_receipts = [r for r in receipts if r.get("receipt_type") == "kill_switch.activated"]
        assert len(block_receipts) == 1
        assert block_receipts[0]["reason_code"] == "kill_switch_approval_only"


class TestKillSwitchReset:
    """Test reset functionality."""

    def test_reset_clears_override(self):
        set_kill_switch_mode(KillSwitchMode.DISABLED)
        assert get_kill_switch_mode() == KillSwitchMode.DISABLED
        reset_kill_switch()
        assert get_kill_switch_mode() == KillSwitchMode.ENABLED
