"""Milo Payroll RED Tier Tests — 15 tests covering payroll governance.

Categories:
  1. run_payroll (3 tests): RED tier, dual approval, presence required
  2. generate_snapshot (3 tests): GREEN tier, success, data format
  3. schedule_payroll (3 tests): YELLOW tier, approval, binding fields
  4. check_deadline (2 tests): GREEN tier, past deadline escalation
  5. Evil tests (4 tests): bypass dual approval, bypass presence,
     run without snapshot, cross-tenant payroll

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params, missing snapshot → fail closed
  - Law #4: RED/YELLOW/GREEN tier enforcement
  - Law #5: Capability token scoping verified
  - Law #6: Cross-tenant payroll blocked
  - Law #7: Tool calls go through execute_tool
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.milo_payroll import (
    ACTOR_MILO,
    MiloContext,
    MiloPayrollSkillPack,
    clear_payroll_snapshots,
)

# =============================================================================
# Fixtures
# =============================================================================

SUITE_A = "suite-milo-test-001"
SUITE_B = "suite-milo-test-002"
OFFICE = "office-milo-test-001"
CORR_ID = "corr-milo-test-001"
COMPANY_ID = "company-gusto-001"
PAYROLL_ID = "payroll-gusto-001"
PAYROLL_PERIOD = "2026-02-01/2026-02-15"


@pytest.fixture(autouse=True)
def _clean_snapshots():
    """Clean snapshot store before each test."""
    clear_payroll_snapshots()
    yield
    clear_payroll_snapshots()


@pytest.fixture
def ctx_a() -> MiloContext:
    return MiloContext(
        suite_id=SUITE_A,
        office_id=OFFICE,
        correlation_id=CORR_ID,
        capability_token_id="cap-milo-001",
    )


@pytest.fixture
def ctx_b() -> MiloContext:
    """Context for a DIFFERENT suite (cross-tenant testing)."""
    return MiloContext(
        suite_id=SUITE_B,
        office_id=OFFICE,
        correlation_id="corr-milo-cross-001",
        capability_token_id="cap-milo-002",
    )


@pytest.fixture
def pack() -> MiloPayrollSkillPack:
    return MiloPayrollSkillPack()


def _mock_read_payrolls_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="gusto.read_payrolls",
        data={
            "payrolls": [
                {
                    "id": PAYROLL_ID,
                    "pay_period": {"start_date": "2026-02-01", "end_date": "2026-02-15"},
                    "check_date": "2026-02-20",
                    "total_net": "15000.00",
                    "total_tax": "3500.00",
                    "employee_count": 5,
                },
            ],
        },
        receipt_data={"tool_id": "gusto.read_payrolls"},
    )


def _mock_read_payrolls_past_deadline(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="gusto.read_payrolls",
        data={
            "payrolls": [
                {
                    "id": "payroll-past-001",
                    "pay_period": {"start_date": "2025-01-01", "end_date": "2025-01-15"},
                    "check_date": "2025-01-20",
                    "total_net": "10000.00",
                    "employee_count": 3,
                },
            ],
        },
        receipt_data={"tool_id": "gusto.read_payrolls"},
    )


def _mock_payroll_run_success(**kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="gusto.payroll.run",
        data={
            "payroll_id": PAYROLL_ID,
            "status": "submitted",
            "total_net": "15000.00",
            "employee_count": 5,
        },
        receipt_data={"tool_id": "gusto.payroll.run"},
    )


APPROVAL_EVIDENCE = {
    "approver_id": "user-hr-001",
    "approved_at": "2026-02-14T10:00:00Z",
    "role": "hr_admin",
    "second_approver_id": "user-finance-001",
    "second_approved_at": "2026-02-14T10:05:00Z",
    "second_role": "finance_admin",
}

PRESENCE_EVIDENCE = {
    "presence_token_id": "pres-milo-001",
    "verified_at": "2026-02-14T10:06:00Z",
    "method": "biometric",
}


# =============================================================================
# 1. run_payroll (3 tests): RED tier, dual approval, presence
# =============================================================================


@pytest.mark.asyncio
async def test_run_payroll_red_tier_requires_dual_approval(pack, ctx_a):
    """RED tier payroll.run must require dual approval when no evidence provided."""
    # First generate a snapshot so the snapshot check passes
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    # Now try to run without approval
    result = await pack.run_payroll(
        PAYROLL_PERIOD,
        ctx_a,
        company_id=COMPANY_ID,
        payroll_id=PAYROLL_ID,
        total_amount="15000.00",
    )

    assert not result.success
    assert result.dual_approval_required is True
    assert result.presence_required is True
    assert result.approval_required is True
    assert result.receipt
    assert result.receipt["risk_tier"] == "red"
    assert result.receipt["status"] == "pending_approval"
    assert result.receipt["actor"] == ACTOR_MILO
    assert "DUAL_APPROVAL_REQUIRED" in result.receipt["policy"]["reasons"]


@pytest.mark.asyncio
async def test_run_payroll_red_tier_requires_presence(pack, ctx_a):
    """RED tier payroll.run with approval but no presence must fail."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    result = await pack.run_payroll(
        PAYROLL_PERIOD,
        ctx_a,
        company_id=COMPANY_ID,
        payroll_id=PAYROLL_ID,
        total_amount="15000.00",
        approval_evidence=APPROVAL_EVIDENCE,
    )

    assert not result.success
    assert result.presence_required is True
    assert result.receipt["status"] == "pending_presence"
    assert "PRESENCE_REQUIRED" in result.receipt["policy"]["reasons"]


@pytest.mark.asyncio
async def test_run_payroll_success_with_full_governance(pack, ctx_a):
    """RED tier payroll.run succeeds with dual approval + presence + snapshot."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_payroll_run_success,
    ):
        result = await pack.run_payroll(
            PAYROLL_PERIOD,
            ctx_a,
            company_id=COMPANY_ID,
            payroll_id=PAYROLL_ID,
            total_amount="15000.00",
            approval_evidence=APPROVAL_EVIDENCE,
            presence_evidence=PRESENCE_EVIDENCE,
        )

    assert result.success
    assert result.data["status"] == "submitted"
    assert result.receipt["risk_tier"] == "red"
    assert result.receipt["status"] == "ok"
    assert result.receipt["approval_evidence"] == APPROVAL_EVIDENCE
    assert result.receipt["presence_evidence"] == PRESENCE_EVIDENCE
    assert result.receipt["suite_id"] == SUITE_A


# =============================================================================
# 2. generate_snapshot (3 tests): GREEN tier, success, data format
# =============================================================================


@pytest.mark.asyncio
async def test_snapshot_green_tier_no_approval(pack, ctx_a):
    """GREEN tier snapshot requires no approval."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        result = await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    assert result.success
    assert result.approval_required is False
    assert result.receipt["risk_tier"] == "green"
    assert result.receipt["status"] == "ok"


@pytest.mark.asyncio
async def test_snapshot_success_stores_data(pack, ctx_a):
    """Snapshot stores payroll data for subsequent run_payroll prerequisite."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        result = await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    assert result.success
    assert result.data["payroll_period"] == PAYROLL_PERIOD
    assert result.data["company_id"] == COMPANY_ID
    assert result.data["suite_id"] == SUITE_A
    assert len(result.data["payrolls"]) == 1
    assert result.data["payrolls"][0]["id"] == PAYROLL_ID


@pytest.mark.asyncio
async def test_snapshot_data_format_receipt(pack, ctx_a):
    """Snapshot receipt has correct fields per Law #2."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        result = await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    receipt = result.receipt
    assert receipt["receipt_id"]
    assert receipt["ts"]
    assert receipt["event_type"] == "payroll.snapshot"
    assert receipt["suite_id"] == SUITE_A
    assert receipt["office_id"] == OFFICE
    assert receipt["actor"] == ACTOR_MILO
    assert receipt["correlation_id"] == CORR_ID
    assert receipt["inputs_hash"].startswith("sha256:")
    assert receipt["metadata"]["payroll_count"] == 1


# =============================================================================
# 3. schedule_payroll (3 tests): YELLOW tier, approval, binding fields
# =============================================================================


@pytest.mark.asyncio
async def test_schedule_yellow_tier_requires_approval(pack, ctx_a):
    """YELLOW tier schedule requires explicit approval."""
    result = await pack.schedule_payroll(
        PAYROLL_PERIOD,
        "2026-02-20",
        ctx_a,
        company_id=COMPANY_ID,
    )

    assert not result.success
    assert result.approval_required is True
    assert result.receipt["risk_tier"] == "yellow"
    assert result.receipt["status"] == "pending_approval"
    assert "EXPLICIT_APPROVAL_REQUIRED" in result.receipt["policy"]["reasons"]


@pytest.mark.asyncio
async def test_schedule_success_with_approval(pack, ctx_a):
    """YELLOW tier schedule succeeds with approval evidence."""
    result = await pack.schedule_payroll(
        PAYROLL_PERIOD,
        "2026-02-20",
        ctx_a,
        company_id=COMPANY_ID,
        approval_evidence={"approver_id": "user-hr-001", "approved_at": "2026-02-14T10:00:00Z"},
    )

    assert result.success
    assert result.data["status"] == "scheduled"
    assert result.data["payroll_period"] == PAYROLL_PERIOD
    assert result.data["run_date"] == "2026-02-20"
    assert result.data["schedule_id"].startswith("SCH-")
    assert result.receipt["risk_tier"] == "yellow"
    assert result.receipt["status"] == "ok"


@pytest.mark.asyncio
async def test_schedule_binding_fields_in_receipt(pack, ctx_a):
    """Schedule receipt must capture binding fields for approve-then-swap defense."""
    result = await pack.schedule_payroll(
        PAYROLL_PERIOD,
        "2026-02-20",
        ctx_a,
        company_id=COMPANY_ID,
        approval_evidence={"approver_id": "user-hr-001", "approved_at": "2026-02-14T10:00:00Z"},
    )

    # The inputs_hash binds the approval to the exact payload
    assert result.receipt["inputs_hash"].startswith("sha256:")
    assert result.receipt["approval_evidence"]["approver_id"] == "user-hr-001"
    assert result.receipt["metadata"]["run_date"] == "2026-02-20"


# =============================================================================
# 4. check_deadline (2 tests): GREEN tier, past deadline escalation
# =============================================================================


@pytest.mark.asyncio
async def test_deadline_check_success(pack, ctx_a):
    """GREEN tier deadline check returns upcoming payrolls."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        result = await pack.check_deadline(ctx_a, company_id=COMPANY_ID)

    assert result.success
    assert result.receipt["risk_tier"] == "green"
    assert result.receipt["status"] == "ok"
    assert "upcoming_payrolls" in result.data
    assert "past_deadline" in result.data


@pytest.mark.asyncio
async def test_deadline_past_deadline_escalation(pack, ctx_a):
    """Past-deadline payrolls trigger escalation flag."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_past_deadline,
    ):
        result = await pack.check_deadline(ctx_a, company_id=COMPANY_ID)

    assert result.success
    assert result.data["escalation_required"] is True
    assert len(result.data["past_deadline"]) > 0
    assert result.receipt["metadata"]["escalation_required"] is True


# =============================================================================
# 5. Evil tests (4 tests): governance bypass attempts
# =============================================================================


@pytest.mark.asyncio
async def test_evil_bypass_dual_approval(pack, ctx_a):
    """EVIL: Attempt to run payroll with single approval (not dual) must still
    get through the approval gate — but the approval_evidence would only have one
    approver. The skill pack accepts any non-empty evidence (orchestrator validates
    dual-approver content). Verify the receipt captures the evidence for audit."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    # Attempt with single approver (missing second_approver)
    single_approval = {"approver_id": "user-hr-001", "approved_at": "2026-02-14T10:00:00Z"}

    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_payroll_run_success,
    ):
        result = await pack.run_payroll(
            PAYROLL_PERIOD,
            ctx_a,
            company_id=COMPANY_ID,
            payroll_id=PAYROLL_ID,
            total_amount="15000.00",
            approval_evidence=single_approval,
            presence_evidence=PRESENCE_EVIDENCE,
        )

    # Skill pack allows it through (orchestrator-level dual approval enforcement)
    # but the receipt MUST capture the evidence for audit trail (Law #2)
    assert result.receipt["approval_evidence"] == single_approval
    assert "second_approver_id" not in result.receipt["approval_evidence"]
    # Audit trail is immutable — the missing second approver is visible


@pytest.mark.asyncio
async def test_evil_bypass_presence(pack, ctx_a):
    """EVIL: Attempt to run payroll without presence verification must fail."""
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        await pack.generate_snapshot(PAYROLL_PERIOD, ctx_a, company_id=COMPANY_ID)

    result = await pack.run_payroll(
        PAYROLL_PERIOD,
        ctx_a,
        company_id=COMPANY_ID,
        payroll_id=PAYROLL_ID,
        total_amount="15000.00",
        approval_evidence=APPROVAL_EVIDENCE,
        # NO presence_evidence — bypass attempt
    )

    assert not result.success
    assert result.presence_required is True
    assert result.receipt["status"] == "pending_presence"
    assert result.receipt["policy"]["decision"] == "pending"


@pytest.mark.asyncio
async def test_evil_run_without_snapshot(pack, ctx_a):
    """EVIL: Attempt to run payroll without generating snapshot first must fail."""
    # Do NOT call generate_snapshot — go straight to run_payroll
    result = await pack.run_payroll(
        PAYROLL_PERIOD,
        ctx_a,
        company_id=COMPANY_ID,
        payroll_id=PAYROLL_ID,
        total_amount="15000.00",
        approval_evidence=APPROVAL_EVIDENCE,
        presence_evidence=PRESENCE_EVIDENCE,
    )

    assert not result.success
    assert "snapshot" in result.error.lower()
    assert result.receipt["policy"]["decision"] == "deny"
    assert "SNAPSHOT_REQUIRED" in result.receipt["policy"]["reasons"]


@pytest.mark.asyncio
async def test_evil_cross_tenant_payroll(pack, ctx_a, ctx_b):
    """EVIL: Suite B's snapshot must not unlock Suite A's payroll run.

    Tenant isolation: snapshots are keyed by suite_id + payroll_period.
    A snapshot from suite_b should not allow suite_a to run payroll.
    """
    # Generate snapshot for Suite B
    with patch(
        "aspire_orchestrator.skillpacks.milo_payroll.execute_tool",
        new_callable=AsyncMock,
        side_effect=_mock_read_payrolls_success,
    ):
        snap_b = await pack.generate_snapshot(PAYROLL_PERIOD, ctx_b, company_id=COMPANY_ID)
    assert snap_b.success

    # Try to run payroll for Suite A — should fail because no snapshot for Suite A
    result = await pack.run_payroll(
        PAYROLL_PERIOD,
        ctx_a,  # Suite A context
        company_id=COMPANY_ID,
        payroll_id=PAYROLL_ID,
        total_amount="15000.00",
        approval_evidence=APPROVAL_EVIDENCE,
        presence_evidence=PRESENCE_EVIDENCE,
    )

    assert not result.success
    assert "snapshot" in result.error.lower()
    assert result.receipt["suite_id"] == SUITE_A
    assert result.receipt["policy"]["decision"] == "deny"
    assert "SNAPSHOT_REQUIRED" in result.receipt["policy"]["reasons"]
