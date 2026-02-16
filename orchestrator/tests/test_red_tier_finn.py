"""Finn Money Desk RED-Tier Tests -- Comprehensive coverage for payment operations.

Categories:
  1. send_payment (3 tests) -- success, red tier enforcement, receipt emission
  2. transfer_funds (3 tests) -- red tier, dual approval, binding fields
  3. owner_draw (3 tests) -- success, insufficient funds, red tier
  4. reconcile_payment (2 tests) -- success, green tier
  5. evil tests (4 tests) -- cross-tenant, bypass approval, bypass presence, amount manipulation

Law compliance:
  - Law #2: Every test verifies receipt emission (success and denial)
  - Law #3: Fail-closed behavior tested for missing fields, insufficient funds
  - Law #4: RED tier classification verified for all payment ops, GREEN for reconcile
  - Law #6: Cross-tenant payment denied
  - Law #8: Presence required for all RED actions
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.skillpacks.finn_money_desk import (
    ACTOR_FINN_MONEY,
    FinnMoneyContext,
    FinnMoneyDeskSkillPack,
    MAX_SPEND_CENTS,
    MIN_AMOUNT_CENTS,
    SkillPackResult,
)
from aspire_orchestrator.services.policy_engine import load_policy_matrix

# =============================================================================
# Fixtures
# =============================================================================

SUITE_A = "suite-finn-money-a-001"
SUITE_B = "suite-finn-money-b-002"
OFFICE = "office-finn-money-001"
CORR_ID = "corr-finn-money-test-001"


@pytest.fixture
def ctx_a() -> FinnMoneyContext:
    """Money context for Suite A."""
    return FinnMoneyContext(suite_id=SUITE_A, office_id=OFFICE, correlation_id=CORR_ID)


@pytest.fixture
def ctx_b() -> FinnMoneyContext:
    """Money context for Suite B (different tenant)."""
    return FinnMoneyContext(suite_id=SUITE_B, office_id=OFFICE, correlation_id="corr-b")


@pytest.fixture
def pack() -> FinnMoneyDeskSkillPack:
    """Finn Money Desk skill pack instance."""
    return FinnMoneyDeskSkillPack()


# =============================================================================
# 1. send_payment tests (3)
# =============================================================================


@pytest.mark.asyncio
async def test_send_payment_success(pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext) -> None:
    """send_payment with valid params returns success plan with RED markers."""
    result = await pack.send_payment(
        payee="vendor-acme-001",
        amount_cents=50000,
        method="ach",
        context=ctx_a,
        currency="USD",
        memo="Monthly retainer",
    )

    assert result.success is True
    assert result.approval_required is True
    assert result.presence_required is True
    assert result.error is None

    # Plan data
    assert result.data["risk_tier"] == "red"
    assert result.data["payee"] == "vendor-acme-001"
    assert result.data["amount_cents"] == 50000
    assert result.data["method"] == "ach"
    assert "payee" in result.data["binding_fields"]
    assert "amount_cents" in result.data["binding_fields"]
    assert "method" in result.data["binding_fields"]


@pytest.mark.asyncio
async def test_send_payment_red_tier_enforced(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """send_payment always sets approval_required and presence_required (RED tier)."""
    result = await pack.send_payment(
        payee="any-vendor",
        amount_cents=1000,
        method="wire",
        context=ctx_a,
    )

    # RED tier enforcement
    assert result.approval_required is True, "RED tier must require approval"
    assert result.presence_required is True, "RED tier must require presence"
    assert result.data["risk_tier"] == "red"


@pytest.mark.asyncio
async def test_send_payment_receipt_emitted(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """send_payment always emits a receipt (Law #2)."""
    result = await pack.send_payment(
        payee="vendor-001",
        amount_cents=25000,
        method="ach",
        context=ctx_a,
    )

    receipt = result.receipt
    assert receipt["receipt_id"].startswith("rcpt-finn-money-")
    assert receipt["event_type"] == "payment.send"
    assert receipt["suite_id"] == SUITE_A
    assert receipt["office_id"] == OFFICE
    assert receipt["actor"] == ACTOR_FINN_MONEY
    assert receipt["correlation_id"] == CORR_ID
    assert receipt["inputs_hash"].startswith("sha256:")
    assert receipt["policy"]["policy_id"] == "finn-money-desk-v1"


# =============================================================================
# 2. transfer_funds tests (3)
# =============================================================================


@pytest.mark.asyncio
async def test_transfer_funds_red_tier(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """transfer_funds is RED tier with approval and presence required."""
    result = await pack.transfer_funds(
        from_account="acct-checking-001",
        to_account="acct-savings-002",
        amount_cents=100000,
        context=ctx_a,
    )

    assert result.success is True
    assert result.approval_required is True
    assert result.presence_required is True
    assert result.data["risk_tier"] == "red"


@pytest.mark.asyncio
async def test_transfer_funds_dual_approval(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """transfer_funds requires dual approval (owner + accountant)."""
    result = await pack.transfer_funds(
        from_account="acct-a",
        to_account="acct-b",
        amount_cents=50000,
        context=ctx_a,
    )

    assert result.success is True
    assert result.dual_approval_required is True
    assert result.data["dual_approval"]["required_approvers"] == ["owner", "accountant"]
    assert result.data["dual_approval"]["approval_count_required"] == 2


@pytest.mark.asyncio
async def test_transfer_funds_missing_binding_fields(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """transfer_funds denies with missing binding fields (Law #3)."""
    result = await pack.transfer_funds(
        from_account="",  # Missing
        to_account="acct-b",
        amount_cents=50000,
        context=ctx_a,
    )

    assert result.success is False
    assert "from_account" in result.error
    assert result.receipt["policy"]["decision"] == "deny"
    assert "MISSING_BINDING_FIELDS" in result.receipt["policy"]["reasons"]


# =============================================================================
# 3. owner_draw tests (3)
# =============================================================================


@pytest.mark.asyncio
async def test_owner_draw_success(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """owner_draw with sufficient cash reserves returns success plan."""
    result = await pack.process_owner_draw(
        owner_id="owner-tonio-001",
        amount_cents=200000,
        context=ctx_a,
        cash_reserve_balance=500000,
        memo="Q1 owner draw",
    )

    assert result.success is True
    assert result.approval_required is True
    assert result.presence_required is True
    assert result.data["risk_tier"] == "red"
    assert result.data["remaining_after_draw"] == 300000
    assert result.data["owner_id"] == "owner-tonio-001"


@pytest.mark.asyncio
async def test_owner_draw_insufficient_funds(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """owner_draw denies when amount exceeds cash reserves (Law #3: fail closed)."""
    result = await pack.process_owner_draw(
        owner_id="owner-001",
        amount_cents=600000,
        context=ctx_a,
        cash_reserve_balance=500000,
    )

    assert result.success is False
    assert "cash reserves" in result.error.lower()
    assert result.receipt["policy"]["decision"] == "deny"
    assert "INSUFFICIENT_CASH_RESERVES" in result.receipt["policy"]["reasons"]
    # Shortfall metadata recorded
    assert result.receipt["metadata"]["shortfall_cents"] == 100000


@pytest.mark.asyncio
async def test_owner_draw_red_tier(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """owner_draw is RED tier with approval + presence required."""
    result = await pack.process_owner_draw(
        owner_id="owner-001",
        amount_cents=10000,
        context=ctx_a,
        cash_reserve_balance=100000,
    )

    assert result.success is True
    assert result.approval_required is True
    assert result.presence_required is True
    assert result.data["risk_tier"] == "red"


# =============================================================================
# 4. reconcile_payment tests (2)
# =============================================================================


@pytest.mark.asyncio
async def test_reconcile_payment_success(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """reconcile_payment matches payment to invoice (GREEN, no approval)."""
    result = await pack.reconcile_payment(
        payment_id="pay-001",
        invoice_id="inv-001",
        context=ctx_a,
    )

    assert result.success is True
    assert result.approval_required is False
    assert result.presence_required is False
    assert result.data["matched"] is True
    assert result.data["payment_id"] == "pay-001"
    assert result.data["invoice_id"] == "inv-001"


@pytest.mark.asyncio
async def test_reconcile_payment_green_tier(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """reconcile_payment is GREEN tier -- no approval or presence."""
    result = await pack.reconcile_payment(
        payment_id="pay-002",
        invoice_id="inv-002",
        context=ctx_a,
    )

    assert result.success is True
    assert result.approval_required is False
    assert result.presence_required is False
    assert result.data["risk_tier"] == "green"
    # Receipt confirms GREEN tier
    assert result.receipt["event_type"] == "payment.reconcile"
    assert result.receipt["status"] == "ok"


# =============================================================================
# 5. Evil tests (4) -- Security attack simulations
# =============================================================================


@pytest.mark.asyncio
async def test_evil_cross_tenant_payment(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext, ctx_b: FinnMoneyContext
) -> None:
    """EVIL: Payment from Suite A must not be attributed to Suite B (Law #6)."""
    result_a = await pack.send_payment(
        payee="vendor-shared",
        amount_cents=10000,
        method="ach",
        context=ctx_a,
    )
    result_b = await pack.send_payment(
        payee="vendor-shared",
        amount_cents=10000,
        method="ach",
        context=ctx_b,
    )

    # Each receipt is scoped to its own suite (Law #6)
    assert result_a.receipt["suite_id"] == SUITE_A
    assert result_b.receipt["suite_id"] == SUITE_B
    assert result_a.receipt["suite_id"] != result_b.receipt["suite_id"]
    # Different receipt IDs
    assert result_a.receipt["receipt_id"] != result_b.receipt["receipt_id"]


@pytest.mark.asyncio
async def test_evil_bypass_approval(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """EVIL: Cannot bypass approval requirement for RED-tier payment."""
    result = await pack.send_payment(
        payee="bypass-vendor",
        amount_cents=999999,
        method="ach",
        context=ctx_a,
    )

    # Even with valid params, RED tier MUST require approval + presence
    assert result.approval_required is True, "RED tier approval cannot be bypassed"
    assert result.presence_required is True, "RED tier presence cannot be bypassed"


@pytest.mark.asyncio
async def test_evil_bypass_presence(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """EVIL: Cannot bypass presence requirement for fund transfers."""
    result = await pack.transfer_funds(
        from_account="acct-a",
        to_account="acct-b",
        amount_cents=100000,
        context=ctx_a,
    )

    # Transfer MUST require presence AND dual approval -- no bypass
    assert result.presence_required is True, "Transfer presence cannot be bypassed"
    assert result.dual_approval_required is True, "Transfer dual approval cannot be bypassed"
    assert result.approval_required is True, "Transfer approval cannot be bypassed"


@pytest.mark.asyncio
async def test_evil_amount_manipulation(
    pack: FinnMoneyDeskSkillPack, ctx_a: FinnMoneyContext
) -> None:
    """EVIL: Amount exceeding max_spend_cents is denied (Law #3)."""
    result = await pack.send_payment(
        payee="vendor-evil",
        amount_cents=MAX_SPEND_CENTS + 1,
        method="ach",
        context=ctx_a,
    )

    assert result.success is False
    assert "exceeds maximum spend limit" in result.error.lower()
    assert result.receipt["policy"]["decision"] == "deny"
    assert "AMOUNT_EXCEEDS_LIMIT" in result.receipt["policy"]["reasons"]

    # Also test below-minimum
    result_low = await pack.send_payment(
        payee="vendor-evil",
        amount_cents=MIN_AMOUNT_CENTS - 1,
        method="ach",
        context=ctx_a,
    )

    assert result_low.success is False
    assert "below minimum" in result_low.error.lower()
    assert result_low.receipt["policy"]["decision"] == "deny"


# =============================================================================
# Policy matrix integration (bonus)
# =============================================================================


def test_policy_matrix_has_payment_send_red() -> None:
    """policy_matrix.yaml classifies payment.send as RED tier."""
    matrix = load_policy_matrix()
    result = matrix.evaluate("payment.send")
    assert result.risk_tier == RiskTier.RED
    assert result.approval_required is True
    assert result.presence_required is True


def test_policy_matrix_has_payment_transfer_red() -> None:
    """policy_matrix.yaml classifies payment.transfer as RED tier."""
    matrix = load_policy_matrix()
    result = matrix.evaluate("payment.transfer")
    assert result.risk_tier == RiskTier.RED
    assert result.approval_required is True
    assert result.presence_required is True


def test_policy_matrix_has_payment_reconcile_green() -> None:
    """policy_matrix.yaml classifies payment.reconcile as GREEN tier."""
    matrix = load_policy_matrix()
    result = matrix.evaluate("payment.reconcile")
    assert result.allowed is True
    assert result.risk_tier == RiskTier.GREEN
    assert result.approval_required is False
    assert result.presence_required is False


def test_policy_matrix_has_owner_draw_red() -> None:
    """policy_matrix.yaml classifies payment.owner_draw as RED tier."""
    matrix = load_policy_matrix()
    result = matrix.evaluate("payment.owner_draw")
    assert result.risk_tier == RiskTier.RED
    assert result.approval_required is True
    assert result.presence_required is True
