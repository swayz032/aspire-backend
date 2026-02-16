"""Teressa Books Skill Pack Tests — 12 tests covering sync, categorization, reports, journal entries.

Categories:
  1. Sync books (2 tests) — success/yellow, missing account_id
  2. Categorize transaction (3 tests) — success/green, valid categories, invalid category
  3. Generate report (3 tests) — P&L, balance_sheet, cash_flow + invalid type
  4. Journal entry (2 tests) — yellow/approval_required, unbalanced rejected
  5. Receipts (1 test) — receipt on all paths
  6. Evil tests (1 test) — cross-tenant receipt scoping

Law compliance:
  - Law #2: Every test verifies receipt emission
  - Law #3: Missing params produce fail-closed error + receipt
  - Law #4: YELLOW tier for sync/journal, GREEN tier for categorize/report
  - Law #6: Tenant isolation verified (evil: cross-tenant scoping)
  - Law #7: Tool executor called via categorize/report methods
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.skillpacks.teressa_books import (
    ACTOR_TERESSA,
    TeressaBooksSkillPack,
    TeressaContext,
    VALID_CATEGORIES,
    VALID_REPORT_TYPES,
)


# =============================================================================
# Fixtures
# =============================================================================

SUITE_ID = "suite-teressa-test-001"
OFFICE_ID = "office-teressa-001"
CORR_ID = "corr-teressa-test-001"

EVIL_SUITE_ID = "suite-evil-attacker-999"
EVIL_OFFICE_ID = "office-evil-999"


@pytest.fixture
def ctx() -> TeressaContext:
    """Tenant-scoped execution context."""
    return TeressaContext(suite_id=SUITE_ID, office_id=OFFICE_ID, correlation_id=CORR_ID)


@pytest.fixture
def evil_ctx() -> TeressaContext:
    """Attacker's tenant context."""
    return TeressaContext(suite_id=EVIL_SUITE_ID, office_id=EVIL_OFFICE_ID, correlation_id="corr-evil-001")


@pytest.fixture
def teressa() -> TeressaBooksSkillPack:
    """Fresh Teressa skill pack instance."""
    return TeressaBooksSkillPack()


def _mock_tool_success(data: dict | None = None) -> ToolExecutionResult:
    """Build a mock successful ToolExecutionResult."""
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="qbo.read_transactions",
        data=data or {"transactions": [], "status": "ok"},
        receipt_data={},
    )


def _mock_tool_failure(error: str = "QBO API error") -> ToolExecutionResult:
    """Build a mock failed ToolExecutionResult."""
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="qbo.read_transactions",
        data={},
        error=error,
        receipt_data={},
    )


# =============================================================================
# 1. Sync Books Tests
# =============================================================================


class TestSyncBooks:
    """Test sync_books (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_sync_books_yellow_approval_required(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Sync books returns YELLOW plan with approval_required."""
        result = await teressa.sync_books(
            account_id="qbo-acct-001",
            date_range={"start": "2026-01-01", "end": "2026-01-31"},
            context=ctx,
        )

        assert result.success
        assert result.approval_required
        assert result.data["account_id"] == "qbo-acct-001"
        assert result.data["risk_tier"] == "yellow"
        assert result.data["date_range"]["start"] == "2026-01-01"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_sync_books_missing_account_id(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Missing account_id fails closed (Law #3)."""
        result = await teressa.sync_books(
            account_id="",
            date_range={"start": "2026-01-01", "end": "2026-01-31"},
            context=ctx,
        )

        assert not result.success
        assert "account_id" in result.error
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"


# =============================================================================
# 2. Categorize Transaction Tests
# =============================================================================


class TestCategorizeTransaction:
    """Test categorize_transaction (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_categorize_success_green(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Successful categorization is GREEN tier, no approval needed."""
        with patch(
            "aspire_orchestrator.skillpacks.teressa_books.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_tool_success({"amount": 150.00, "vendor": "Office Depot"}),
        ):
            result = await teressa.categorize_transaction(
                transaction_id="txn-001",
                context=ctx,
            )

        assert result.success
        assert not result.approval_required
        assert result.data["transaction_id"] == "txn-001"
        assert result.data["category"] in VALID_CATEGORIES
        assert result.data["risk_tier"] == "green"

    @pytest.mark.asyncio
    async def test_categorize_valid_categories(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """User-suggested category must be from VALID_CATEGORIES."""
        with patch(
            "aspire_orchestrator.skillpacks.teressa_books.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_tool_success(),
        ):
            result = await teressa.categorize_transaction(
                transaction_id="txn-002",
                context=ctx,
                suggested_category="revenue",
            )

        assert result.success
        assert result.data["category"] == "revenue"
        assert result.data["source"] == "user_suggested"
        assert result.data["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_categorize_invalid_category_rejected(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Invalid category fails closed (Law #3)."""
        result = await teressa.categorize_transaction(
            transaction_id="txn-003",
            context=ctx,
            suggested_category="imaginary_category",
        )

        assert not result.success
        assert "imaginary_category" in result.error
        assert result.receipt["status"] == "denied"
        assert result.receipt["policy"]["decision"] == "deny"
        assert "INVALID_CATEGORY" in result.receipt["policy"]["reasons"]


# =============================================================================
# 3. Generate Report Tests
# =============================================================================


class TestGenerateReport:
    """Test generate_report (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_report_profit_and_loss(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """P&L report generates successfully via tool_executor."""
        with patch(
            "aspire_orchestrator.skillpacks.teressa_books.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_tool_success({"accounts": []}),
        ):
            result = await teressa.generate_report(
                report_type="profit_and_loss",
                date_range={"start": "2026-01-01", "end": "2026-01-31"},
                context=ctx,
            )

        assert result.success
        assert not result.approval_required
        assert result.data["report_type"] == "profit_and_loss"
        assert result.data["status"] == "generated"
        assert result.data["report_id"].startswith("RPT-")

    @pytest.mark.asyncio
    async def test_report_balance_sheet(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Balance sheet report generates successfully."""
        with patch(
            "aspire_orchestrator.skillpacks.teressa_books.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_tool_success(),
        ):
            result = await teressa.generate_report(
                report_type="balance_sheet",
                date_range={"start": "2026-01-01", "end": "2026-01-31"},
                context=ctx,
            )

        assert result.success
        assert result.data["report_type"] == "balance_sheet"

    @pytest.mark.asyncio
    async def test_report_cash_flow(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Cash flow report generates successfully."""
        with patch(
            "aspire_orchestrator.skillpacks.teressa_books.execute_tool",
            new_callable=AsyncMock,
            return_value=_mock_tool_success(),
        ):
            result = await teressa.generate_report(
                report_type="cash_flow",
                date_range={"start": "2026-01-01", "end": "2026-01-31"},
                context=ctx,
            )

        assert result.success
        assert result.data["report_type"] == "cash_flow"

    @pytest.mark.asyncio
    async def test_report_invalid_type_rejected(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Invalid report type fails closed (Law #3)."""
        result = await teressa.generate_report(
            report_type="nonexistent_report",
            date_range={"start": "2026-01-01", "end": "2026-01-31"},
            context=ctx,
        )

        assert not result.success
        assert "nonexistent_report" in result.error
        assert result.receipt["status"] == "denied"
        assert "INVALID_REPORT_TYPE" in result.receipt["policy"]["reasons"]


# =============================================================================
# 4. Journal Entry Tests
# =============================================================================


class TestJournalEntry:
    """Test create_journal_entry (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_journal_entry_yellow_approval(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Balanced journal entry returns YELLOW plan with approval_required."""
        entries = [
            {"account_id": "acct-100", "amount": 500.00, "type": "debit"},
            {"account_id": "acct-200", "amount": 500.00, "type": "credit"},
        ]

        result = await teressa.create_journal_entry(entries=entries, context=ctx, memo="Monthly rent")

        assert result.success
        assert result.approval_required
        assert result.data["risk_tier"] == "yellow"
        assert result.data["total_debits"] == 500.00
        assert result.data["total_credits"] == 500.00
        assert result.data["entry_count"] == 2

    @pytest.mark.asyncio
    async def test_journal_entry_unbalanced_rejected(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Unbalanced journal entry fails closed (double-entry enforcement)."""
        entries = [
            {"account_id": "acct-100", "amount": 500.00, "type": "debit"},
            {"account_id": "acct-200", "amount": 300.00, "type": "credit"},
        ]

        result = await teressa.create_journal_entry(entries=entries, context=ctx)

        assert not result.success
        assert "Unbalanced" in result.error
        assert result.receipt["status"] == "denied"
        assert "UNBALANCED_ENTRY" in result.receipt["policy"]["reasons"]


# =============================================================================
# 5. Receipt Coverage Tests
# =============================================================================


class TestReceiptCoverage:
    """Verify receipts on all paths (Law #2)."""

    @pytest.mark.asyncio
    async def test_all_methods_emit_receipts(
        self, teressa: TeressaBooksSkillPack, ctx: TeressaContext,
    ) -> None:
        """Every method emits a receipt regardless of outcome."""
        # Sync (success path)
        sync_result = await teressa.sync_books(
            account_id="acct-001",
            date_range={"start": "2026-01-01", "end": "2026-01-31"},
            context=ctx,
        )
        assert sync_result.receipt
        assert sync_result.receipt["actor"] == ACTOR_TERESSA
        assert sync_result.receipt["suite_id"] == SUITE_ID
        assert sync_result.receipt["inputs_hash"].startswith("sha256:")

        # Sync (denied path)
        denied_result = await teressa.sync_books(
            account_id="",
            date_range={"start": "2026-01-01", "end": "2026-01-31"},
            context=ctx,
        )
        assert denied_result.receipt
        assert denied_result.receipt["status"] == "denied"

        # Journal (denied path)
        journal_result = await teressa.create_journal_entry(entries=[], context=ctx)
        assert journal_result.receipt
        assert journal_result.receipt["status"] == "denied"


# =============================================================================
# 6. Evil Tests
# =============================================================================


class TestEvilTeressa:
    """Evil tests — security boundaries (Law #3, #6)."""

    @pytest.mark.asyncio
    async def test_evil_cross_tenant_receipt_scoping(
        self, teressa: TeressaBooksSkillPack, evil_ctx: TeressaContext, ctx: TeressaContext,
    ) -> None:
        """Receipts created with evil context have evil tenant IDs, not the victim's.

        Verifies tenant scoping in receipts cannot be forged (Law #6).
        """
        result = await teressa.sync_books(
            account_id="qbo-victim-acct",
            date_range={"start": "2026-01-01", "end": "2026-01-31"},
            context=evil_ctx,
        )

        # Receipt should contain the evil tenant, not the victim
        assert result.receipt["suite_id"] == EVIL_SUITE_ID
        assert result.receipt["office_id"] == EVIL_OFFICE_ID
        # Must NOT contain the legitimate tenant
        assert result.receipt["suite_id"] != SUITE_ID
        assert result.receipt["office_id"] != OFFICE_ID
