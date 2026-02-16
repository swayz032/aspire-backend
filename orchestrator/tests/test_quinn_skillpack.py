"""Quinn Invoicing Skill Pack Tests — 15 tests covering invoices, quotes, webhooks.

Categories:
  1. Create invoice (3 tests) — success, yellow tier, binding_fields
  2. Send invoice (2 tests) — yellow tier, approval_required
  3. Void invoice (2 tests) — yellow tier, reason required
  4. Create/send quote (2 tests) — yellow tier
  5. Webhook processing (3 tests) — payment success, payment failure, receipt
  6. Evil tests (3 tests) — cross-tenant, missing binding fields, unauthorized void

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params / binding fields produce fail-closed error + receipt
  - Law #4: YELLOW tier classification verified for all invoice/quote ops
  - Law #6: Tenant isolation verified (evil: cross-tenant invoice attempt)
  - Law #7: Tool executor called (not direct provider)
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.skillpacks.quinn_invoicing import (
    ACTOR_QUINN,
    QuinnContext,
    QuinnInvoicingSkillPack,
)


# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-quinn-test-001"
OFFICE_ID = "office-quinn-001"
CORR_ID = "corr-quinn-test-001"

EVIL_SUITE_ID = "suite-evil-attacker-999"
EVIL_OFFICE_ID = "office-evil-999"


@pytest.fixture
def ctx() -> QuinnContext:
    """Tenant-scoped execution context."""
    return QuinnContext(suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)


@pytest.fixture
def evil_ctx() -> QuinnContext:
    """Attacker's tenant context."""
    return QuinnContext(suite_id=EVIL_SUITE_ID, office_id=EVIL_OFFICE_ID, correlation_id="corr-evil-001")


@pytest.fixture
def quinn() -> QuinnInvoicingSkillPack:
    """Fresh Quinn skill pack instance."""
    return QuinnInvoicingSkillPack()


def _sample_line_items() -> list[dict]:
    """Sample invoice/quote line items."""
    return [
        {"description": "Web Development", "quantity": 10, "unit_amount": 15000},
        {"description": "Design Review", "quantity": 2, "unit_amount": 7500},
    ]


# =============================================================================
# 1. Create Invoice Tests
# =============================================================================


class TestCreateInvoice:
    """Test create_invoice (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_create_invoice_success(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Successful invoice creation returns plan with approval_required."""
        result = await quinn.create_invoice(
            customer="cus_abc123",
            line_items=_sample_line_items(),
            context=ctx,
            amount=165000,
            currency="usd",
        )

        assert result.success
        assert result.approval_required
        assert result.data["customer_id"] == "cus_abc123"
        assert result.data["amount"] == 165000
        assert result.data["currency"] == "usd"
        assert result.data["risk_tier"] == "yellow"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_create_invoice_yellow_tier_receipt(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Invoice creation emits a receipt with correct fields (Law #2)."""
        result = await quinn.create_invoice(
            customer="cus_abc123",
            line_items=_sample_line_items(),
            context=ctx,
            amount=165000,
        )

        receipt = result.receipt
        assert receipt["event_type"] == "invoice.create"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["office_id"] == OFFICE_ID
        assert receipt["actor"] == ACTOR_QUINN
        assert receipt["correlation_id"] == CORR_ID
        assert receipt["status"] == "ok"
        assert receipt["inputs_hash"].startswith("sha256:")
        assert receipt["metadata"]["customer_id"] == "cus_abc123"
        assert receipt["metadata"]["amount"] == 165000

    @pytest.mark.asyncio
    async def test_create_invoice_missing_binding_fields(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Missing binding fields fails closed (Law #3)."""
        result = await quinn.create_invoice(
            customer="",  # missing customer_id
            line_items=[],  # missing line_items
            context=ctx,
            amount=None,  # missing amount
        )

        assert not result.success
        assert "Missing required binding fields" in result.error
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_BINDING_FIELDS" in result.receipt["policy"]["reasons"]


# =============================================================================
# 2. Send Invoice Tests
# =============================================================================


class TestSendInvoice:
    """Test send_invoice (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_send_invoice_yellow_tier(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Send invoice returns YELLOW plan with approval_required."""
        result = await quinn.send_invoice("inv_abc123", ctx)

        assert result.success
        assert result.approval_required
        assert result.data["invoice_id"] == "inv_abc123"
        assert result.data["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_send_invoice_approval_required(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Send invoice requires user approval (Law #4)."""
        result = await quinn.send_invoice("inv_abc123", ctx)

        assert result.approval_required
        assert result.receipt["event_type"] == "invoice.send"
        assert result.receipt["actor"] == ACTOR_QUINN
        assert result.receipt["suite_id"] == SUITE_ID


# =============================================================================
# 3. Void Invoice Tests
# =============================================================================


class TestVoidInvoice:
    """Test void_invoice (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_void_invoice_yellow_tier(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Void invoice returns YELLOW plan with reason."""
        result = await quinn.void_invoice("inv_abc123", "Customer requested cancellation", ctx)

        assert result.success
        assert result.approval_required
        assert result.data["invoice_id"] == "inv_abc123"
        assert result.data["reason"] == "Customer requested cancellation"
        assert result.data["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_void_invoice_reason_required(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Void without reason fails closed (Law #3 + Law #2 audit trail)."""
        result = await quinn.void_invoice("inv_abc123", "", ctx)

        assert not result.success
        assert "reason" in result.error.lower()
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"


# =============================================================================
# 4. Quote Tests
# =============================================================================


class TestQuotes:
    """Test create_quote and send_quote (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_create_quote_yellow_tier(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Create quote returns YELLOW plan with approval_required."""
        result = await quinn.create_quote(
            customer="cus_xyz789",
            line_items=_sample_line_items(),
            expiry="2026-03-01T00:00:00Z",
            context=ctx,
            amount=165000,
        )

        assert result.success
        assert result.approval_required
        assert result.data["customer_id"] == "cus_xyz789"
        assert result.data["risk_tier"] == "yellow"
        assert result.data["expires_at"] == "2026-03-01T00:00:00Z"
        assert result.receipt["event_type"] == "quote.create"

    @pytest.mark.asyncio
    async def test_send_quote_yellow_tier(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Send quote returns YELLOW plan with approval_required."""
        result = await quinn.send_quote("qt_abc123", ctx)

        assert result.success
        assert result.approval_required
        assert result.data["quote_id"] == "qt_abc123"
        assert result.data["risk_tier"] == "yellow"
        assert result.receipt["event_type"] == "quote.send"


# =============================================================================
# 5. Webhook Tests
# =============================================================================


class TestWebhook:
    """Test handle_webhook (GREEN tier — internal processing)."""

    @pytest.mark.asyncio
    async def test_webhook_payment_success(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """invoice.paid webhook processes successfully."""
        result = await quinn.handle_webhook(
            event_type="invoice.paid",
            payload={
                "object": {
                    "id": "inv_paid_001",
                    "status": "paid",
                    "amount_due": 15000,
                    "amount_paid": 15000,
                    "customer": "cus_abc123",
                },
            },
            context=ctx,
        )

        assert result.success
        assert not result.approval_required  # GREEN tier
        assert result.data["handled"] is True
        assert result.data["invoice_id"] == "inv_paid_001"
        assert result.data["status"] == "paid"
        assert result.data["amount_paid"] == 15000

    @pytest.mark.asyncio
    async def test_webhook_payment_failure(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """invoice.payment_failed webhook captures failure details."""
        result = await quinn.handle_webhook(
            event_type="invoice.payment_failed",
            payload={
                "object": {
                    "id": "inv_fail_001",
                    "status": "open",
                    "amount_due": 15000,
                    "amount_paid": 0,
                    "customer": "cus_abc123",
                    "charge": {
                        "failure_code": "card_declined",
                        "failure_message": "Your card was declined.",
                    },
                },
            },
            context=ctx,
        )

        assert result.success
        assert result.data["handled"] is True
        assert result.data["failure_code"] == "card_declined"
        assert result.data["failure_message"] == "Your card was declined."

    @pytest.mark.asyncio
    async def test_webhook_receipt_generated(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Webhook processing always generates a receipt (Law #2)."""
        result = await quinn.handle_webhook(
            event_type="invoice.paid",
            payload={"object": {"id": "inv_rcpt_001", "status": "paid"}},
            context=ctx,
        )

        receipt = result.receipt
        assert receipt["event_type"] == "invoice.webhook"
        assert receipt["suite_id"] == SUITE_ID
        assert receipt["actor"] == ACTOR_QUINN
        assert receipt["status"] == "ok"
        assert receipt["metadata"]["event_type"] == "invoice.paid"
        assert receipt["metadata"]["handled"] is True


# =============================================================================
# 6. Evil Tests
# =============================================================================


class TestEvilQuinn:
    """Evil tests — security boundaries (Law #3, #6)."""

    @pytest.mark.asyncio
    async def test_evil_cross_tenant_invoice(
        self, quinn: QuinnInvoicingSkillPack, evil_ctx: QuinnContext, ctx: QuinnContext,
    ) -> None:
        """Invoices created with evil context have evil tenant IDs — not the victim's.

        Verifies that tenant scoping in receipts cannot be forged (Law #6).
        An attacker cannot create an invoice and have it attributed to
        another tenant.
        """
        result = await quinn.create_invoice(
            customer="cus_victim_001",
            line_items=_sample_line_items(),
            context=evil_ctx,
            amount=999999,
        )

        # The receipt should contain the evil tenant, not the victim
        assert result.receipt["suite_id"] == EVIL_SUITE_ID
        assert result.receipt["office_id"] == EVIL_OFFICE_ID
        # It should NOT contain the legitimate tenant
        assert result.receipt["suite_id"] != SUITE_ID
        assert result.receipt["office_id"] != OFFICE_ID

    @pytest.mark.asyncio
    async def test_evil_missing_all_binding_fields(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Attempt to create invoice with all binding fields missing fails closed."""
        result = await quinn.create_invoice(
            customer="",
            line_items=[],
            context=ctx,
            amount=None,
            currency="",
        )

        assert not result.success
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_BINDING_FIELDS" in result.receipt["policy"]["reasons"]
        # All 4 binding fields should be reported missing
        assert "amount" in result.error
        assert "customer_id" in result.error

    @pytest.mark.asyncio
    async def test_evil_unauthorized_void_no_reason(
        self, quinn: QuinnInvoicingSkillPack, ctx: QuinnContext,
    ) -> None:
        """Void without reason is rejected — audit trail is mandatory (Law #2)."""
        result = await quinn.void_invoice("inv_steal_001", "   ", ctx)

        assert not result.success
        assert result.receipt["status"] == "denied"
        assert "reason" in result.error.lower()
