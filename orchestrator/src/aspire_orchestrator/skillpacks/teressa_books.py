"""Teressa Books Skill Pack — Bookkeeping, QuickBooks sync, categorization, reports, journal entries.

Teressa is the Books desk. She handles:
  - Book syncing (YELLOW — external data pull + state mutation)
  - Transaction categorization (GREEN — AI-powered read-only classification)
  - Financial report generation (GREEN — read-only aggregation)
  - Journal entry creation (YELLOW — state-changing financial write)

Provider: QuickBooks Online (via OAuth2 connected accounts per-suite)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters
  - Law #4: Sync + journal entry = YELLOW; categorize + report = GREEN
  - Law #5: Capability tokens required for all QBO tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all QBO calls (tools are hands)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_executor import execute_tool
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

ACTOR_TERESSA = "skillpack:teressa-books"
RECEIPT_VERSION = "1.0"

# Valid transaction categories for AI-powered categorization
VALID_CATEGORIES = frozenset({
    "revenue",
    "cogs",
    "operating_expense",
    "payroll",
    "tax",
    "transfer",
    "owner_draw",
    "uncategorized",
})

# Valid report types for financial reporting
VALID_REPORT_TYPES = frozenset({
    "profit_and_loss",
    "balance_sheet",
    "cash_flow",
    "trial_balance",
})


@dataclass
class SkillPackResult:
    """Result of a Teressa Books skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False


@dataclass
class TeressaContext:
    """Tenant-scoped execution context for Teressa operations."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _make_receipt(
    *,
    ctx: TeressaContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Teressa Books operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-teressa-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_TERESSA,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": _compute_inputs_hash(inputs or {}),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "teressa-books-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "redactions": [],
    }
    if tool_used:
        receipt["tool_used"] = tool_used
    if metadata:
        receipt["metadata"] = metadata
    return receipt


class TeressaBooksSkillPack:
    """Teressa Books Skill Pack — governed bookkeeping operations.

    All methods require a TeressaContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).

    YELLOW tier: sync_books, create_journal_entry
    GREEN tier: categorize_transaction, generate_report
    """

    async def sync_books(
        self,
        account_id: str,
        date_range: dict[str, str],
        context: TeressaContext,
    ) -> SkillPackResult:
        """Sync QuickBooks data for a given account (YELLOW — requires user approval).

        Pulls company info, transactions, and accounts from QBO.
        This is YELLOW because it triggers external API calls and may
        update local state with financial data.

        Args:
            account_id: QuickBooks connected account ID
            date_range: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
            context: Tenant-scoped execution context
        """
        if not account_id or not account_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="books.sync",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_ACCOUNT_ID",
                tool_used="quickbooks.sync",
                inputs={"action": "books.sync", "account_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: account_id",
            )

        start_date = date_range.get("start", "")
        end_date = date_range.get("end", "")
        if not start_date or not end_date:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.sync",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_DATE_RANGE",
                tool_used="quickbooks.sync",
                inputs={"action": "books.sync", "account_id": account_id},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: date_range (start and end required)",
            )

        # YELLOW tier: build sync plan, mark approval_required
        sync_plan = {
            "account_id": account_id.strip(),
            "date_range": {"start": start_date, "end": end_date},
            "risk_tier": "yellow",
            "tools": ["qbo.read_company", "qbo.read_transactions", "qbo.read_accounts"],
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="books.sync",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="quickbooks.sync",
            inputs={
                "action": "books.sync",
                "account_id": account_id.strip(),
                "start": start_date,
                "end": end_date,
            },
            metadata={
                "account_id": account_id.strip(),
                "date_range_start": start_date,
                "date_range_end": end_date,
            },
        )

        return SkillPackResult(
            success=True,
            data=sync_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def categorize_transaction(
        self,
        transaction_id: str,
        context: TeressaContext,
        *,
        suggested_category: str | None = None,
    ) -> SkillPackResult:
        """Categorize a transaction using AI-powered rules (GREEN — no approval needed).

        Reads transaction data from QBO, then applies categorization logic.
        If a suggested_category is provided, it must be one of VALID_CATEGORIES.

        Args:
            transaction_id: QuickBooks transaction ID
            context: Tenant-scoped execution context
            suggested_category: Optional pre-suggested category to validate
        """
        if not transaction_id or not transaction_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="books.categorize",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_TRANSACTION_ID",
                tool_used="qbo.read_transactions",
                inputs={"action": "books.categorize", "transaction_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: transaction_id",
            )

        if suggested_category and suggested_category not in VALID_CATEGORIES:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.categorize",
                risk_tier="green",
                outcome="denied",
                reason_code="INVALID_CATEGORY",
                tool_used="qbo.read_transactions",
                inputs={
                    "action": "books.categorize",
                    "transaction_id": transaction_id.strip(),
                    "suggested_category": suggested_category,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Invalid category: {suggested_category}. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}",
            )

        # GREEN tier: read transaction via tool_executor, apply categorization
        result: ToolExecutionResult = await execute_tool(
            tool_id="qbo.read_transactions",
            payload={
                "transaction_id": transaction_id.strip(),
                "suite_id": context.suite_id,
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        if result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.categorize",
                risk_tier="green",
                outcome="failed",
                reason_code="TOOL_EXECUTION_FAILED",
                tool_used="qbo.read_transactions",
                inputs={
                    "action": "books.categorize",
                    "transaction_id": transaction_id.strip(),
                },
                metadata={"tool_error": result.error or "unknown"},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=result.error or "Failed to read transaction from QuickBooks",
            )

        # Apply categorization (rule-based or use suggested)
        category = suggested_category or _auto_categorize(result.data)

        categorization_data = {
            "transaction_id": transaction_id.strip(),
            "category": category,
            "confidence": 1.0 if suggested_category else 0.8,
            "source": "user_suggested" if suggested_category else "auto_rule",
            "risk_tier": "green",
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="books.categorize",
            risk_tier="green",
            outcome="success",
            reason_code="EXECUTED",
            tool_used="qbo.read_transactions",
            inputs={
                "action": "books.categorize",
                "transaction_id": transaction_id.strip(),
            },
            metadata={
                "transaction_id": transaction_id.strip(),
                "category": category,
                "source": categorization_data["source"],
            },
        )

        return SkillPackResult(
            success=True,
            data=categorization_data,
            receipt=receipt,
        )

    async def generate_report(
        self,
        report_type: str,
        date_range: dict[str, str],
        context: TeressaContext,
    ) -> SkillPackResult:
        """Generate a financial report (GREEN — read-only aggregation, no approval).

        Valid report types: profit_and_loss, balance_sheet, cash_flow, trial_balance.

        Args:
            report_type: Type of report to generate
            date_range: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
            context: Tenant-scoped execution context
        """
        if not report_type or report_type not in VALID_REPORT_TYPES:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.report",
                risk_tier="green",
                outcome="denied",
                reason_code="INVALID_REPORT_TYPE",
                tool_used="qbo.read_accounts",
                inputs={
                    "action": "books.report",
                    "report_type": report_type or "",
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Invalid report_type: {report_type}. Must be one of: {', '.join(sorted(VALID_REPORT_TYPES))}",
            )

        start_date = date_range.get("start", "")
        end_date = date_range.get("end", "")
        if not start_date or not end_date:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.report",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_DATE_RANGE",
                tool_used="qbo.read_accounts",
                inputs={
                    "action": "books.report",
                    "report_type": report_type,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: date_range (start and end required)",
            )

        # GREEN tier: read accounts + transactions via tool_executor
        accounts_result: ToolExecutionResult = await execute_tool(
            tool_id="qbo.read_accounts",
            payload={
                "suite_id": context.suite_id,
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        txn_result: ToolExecutionResult = await execute_tool(
            tool_id="qbo.read_transactions",
            payload={
                "suite_id": context.suite_id,
                "start_date": start_date,
                "end_date": end_date,
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        if accounts_result.outcome != Outcome.SUCCESS or txn_result.outcome != Outcome.SUCCESS:
            failed_tool = (
                "qbo.read_accounts" if accounts_result.outcome != Outcome.SUCCESS
                else "qbo.read_transactions"
            )
            error_msg = (
                accounts_result.error if accounts_result.outcome != Outcome.SUCCESS
                else txn_result.error
            ) or "unknown"

            receipt = _make_receipt(
                ctx=context,
                action_type="books.report",
                risk_tier="green",
                outcome="failed",
                reason_code="TOOL_EXECUTION_FAILED",
                tool_used=failed_tool,
                inputs={
                    "action": "books.report",
                    "report_type": report_type,
                    "start": start_date,
                    "end": end_date,
                },
                metadata={"tool_error": error_msg, "failed_tool": failed_tool},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Failed to fetch data from QuickBooks: {error_msg}",
            )

        # Build report from fetched data
        report_data = _build_report(
            report_type=report_type,
            accounts_data=accounts_result.data,
            txn_data=txn_result.data,
            start_date=start_date,
            end_date=end_date,
        )

        receipt = _make_receipt(
            ctx=context,
            action_type="books.report",
            risk_tier="green",
            outcome="success",
            reason_code="EXECUTED",
            tool_used="qbo.read_accounts",
            inputs={
                "action": "books.report",
                "report_type": report_type,
                "start": start_date,
                "end": end_date,
            },
            metadata={
                "report_type": report_type,
                "date_range_start": start_date,
                "date_range_end": end_date,
                "report_id": report_data["report_id"],
            },
        )

        return SkillPackResult(
            success=True,
            data=report_data,
            receipt=receipt,
        )

    async def create_journal_entry(
        self,
        entries: list[dict[str, Any]],
        context: TeressaContext,
        *,
        memo: str = "",
    ) -> SkillPackResult:
        """Create a manual journal entry in QuickBooks (YELLOW — requires user approval).

        Journal entries are state-changing financial writes that affect the
        general ledger. Requires explicit user confirmation.

        Each entry must have: account_id, amount, type (debit/credit).

        Args:
            entries: List of journal entry line items
            context: Tenant-scoped execution context
            memo: Optional memo/description for the journal entry
        """
        if not entries:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.journal_entry",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_ENTRIES",
                tool_used="qbo.journal_entry.create",
                inputs={"action": "books.journal_entry", "entry_count": 0},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: entries (at least one line item required)",
            )

        # Validate entry structure
        validation_errors = _validate_journal_entries(entries)
        if validation_errors:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.journal_entry",
                risk_tier="yellow",
                outcome="denied",
                reason_code="INVALID_ENTRIES",
                tool_used="qbo.journal_entry.create",
                inputs={
                    "action": "books.journal_entry",
                    "entry_count": len(entries),
                    "errors": validation_errors,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Invalid journal entries: {'; '.join(validation_errors)}",
            )

        # Verify debits = credits (double-entry bookkeeping)
        total_debits = sum(
            e.get("amount", 0) for e in entries if e.get("type") == "debit"
        )
        total_credits = sum(
            e.get("amount", 0) for e in entries if e.get("type") == "credit"
        )
        if abs(total_debits - total_credits) > 0.001:
            receipt = _make_receipt(
                ctx=context,
                action_type="books.journal_entry",
                risk_tier="yellow",
                outcome="denied",
                reason_code="UNBALANCED_ENTRY",
                tool_used="qbo.journal_entry.create",
                inputs={
                    "action": "books.journal_entry",
                    "total_debits": total_debits,
                    "total_credits": total_credits,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Unbalanced journal entry: debits ({total_debits}) != credits ({total_credits})",
            )

        # YELLOW tier: build the plan, mark approval_required
        journal_plan = {
            "entries": entries,
            "memo": memo,
            "total_debits": total_debits,
            "total_credits": total_credits,
            "entry_count": len(entries),
            "risk_tier": "yellow",
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="books.journal_entry",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="qbo.journal_entry.create",
            inputs={
                "action": "books.journal_entry",
                "entry_count": len(entries),
                "total_debits": total_debits,
            },
            metadata={
                "entry_count": len(entries),
                "total_debits": total_debits,
                "total_credits": total_credits,
                "memo": memo,
            },
        )

        return SkillPackResult(
            success=True,
            data=journal_plan,
            receipt=receipt,
            approval_required=True,
        )


def _auto_categorize(transaction_data: dict[str, Any]) -> str:
    """Rule-based auto-categorization for transactions.

    Uses simple heuristics based on transaction metadata.
    Returns 'uncategorized' when confidence is low.
    """
    # Stub implementation: real version would use ML/rules engine
    # For now, return uncategorized — the orchestrator can enhance later
    return "uncategorized"


def _validate_journal_entries(entries: list[dict[str, Any]]) -> list[str]:
    """Validate journal entry line items.

    Each entry must have: account_id (non-empty), amount (positive number),
    type ('debit' or 'credit').
    """
    errors: list[str] = []
    for i, entry in enumerate(entries):
        if not entry.get("account_id"):
            errors.append(f"Entry {i}: missing account_id")
        amount = entry.get("amount")
        if amount is None or (isinstance(amount, (int, float)) and amount <= 0):
            errors.append(f"Entry {i}: amount must be a positive number")
        entry_type = entry.get("type", "")
        if entry_type not in ("debit", "credit"):
            errors.append(f"Entry {i}: type must be 'debit' or 'credit'")
    return errors


def _build_report(
    *,
    report_type: str,
    accounts_data: dict[str, Any],
    txn_data: dict[str, Any],
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Build a structured financial report from QBO data.

    Returns a report skeleton with the data from QuickBooks.
    Real aggregation happens in the orchestrator or downstream consumers.
    """
    report_id = f"RPT-{uuid.uuid4().hex[:8].upper()}"

    return {
        "report_id": report_id,
        "report_type": report_type,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date_range": {"start": start_date, "end": end_date},
        "accounts": accounts_data,
        "transactions": txn_data,
        "status": "generated",
    }


# =============================================================================
# Phase 3 W4: Enhanced Teressa Books with LLM reasoning
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedTeressaBooks(EnhancedSkillPack):
    """LLM-enhanced Teressa Books — intelligent categorization, reconciliation planning.

    Extends TeressaBooksSkillPack with:
    - categorize_transaction: LLM classifies transactions by account/category
    - plan_reconciliation: LLM compares Stripe vs QBO and flags mismatches
    - analyze_financials: LLM provides financial insights from QBO data

    YELLOW tier for sync/categorization, GREEN for analysis.
    Desk router: teressa_booksdesk_router.yaml
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="teressa-books",
            agent_name="Teressa Books",
            default_risk_tier="yellow",
        )
        self._rule_pack = TeressaBooksSkillPack()

    async def categorize_transaction(
        self, transaction: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Classify a transaction into QBO categories using LLM. YELLOW — changes QBO data."""
        description = transaction.get("description", transaction.get("memo", ""))
        amount = transaction.get("amount", 0)
        if not description and not amount:
            receipt = self.build_receipt(
                ctx=ctx, event_type="books.categorize",
                status="failed", inputs={"description": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_TRANSACTION_DATA"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing transaction data")

        return await self.execute_with_llm(
            prompt=(
                f"You are Teressa, the bookkeeper. Categorize this transaction.\n\n"
                f"Description: {description}\nAmount: ${amount}\n"
                f"Date: {transaction.get('date', 'unknown')}\n"
                f"Vendor: {transaction.get('vendor', 'unknown')}\n\n"
                f"Classify into QBO category (Expenses, Revenue, COGS, Assets, Liabilities).\n"
                f"Suggest account name and tax treatment. YELLOW — user confirms before QBO sync."
            ),
            ctx=ctx, event_type="books.categorize", step_type="classify",
            inputs={"action": "books.categorize", "amount": amount},
        )

    async def plan_reconciliation(
        self, stripe_data: dict, qbo_data: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Compare Stripe transactions with QBO entries. GREEN — analysis only."""
        return await self.execute_with_llm(
            prompt=(
                f"You are Teressa. Compare Stripe and QuickBooks data for discrepancies.\n\n"
                f"Stripe summary: {len(stripe_data.get('transactions', []))} transactions, "
                f"total: ${stripe_data.get('total', 0)}\n"
                f"QBO summary: {len(qbo_data.get('transactions', []))} entries, "
                f"total: ${qbo_data.get('total', 0)}\n\n"
                f"Identify: missing entries, amount mismatches, date discrepancies, "
                f"duplicate entries. Flag items needing manual review."
            ),
            ctx=ctx, event_type="books.reconcile_plan", step_type="verify",
            inputs={
                "action": "books.reconcile_plan",
                "stripe_count": len(stripe_data.get("transactions", [])),
                "qbo_count": len(qbo_data.get("transactions", [])),
            },
        )

    async def analyze_financials(
        self, period: str, financial_data: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Provide financial insights from QBO data. GREEN — read-only analysis."""
        return await self.execute_with_llm(
            prompt=(
                f"You are Teressa, providing financial insights.\n\n"
                f"Period: {period}\nRevenue: ${financial_data.get('revenue', 0)}\n"
                f"Expenses: ${financial_data.get('expenses', 0)}\n"
                f"Categories: {financial_data.get('categories', {})}\n\n"
                f"Provide: revenue trends, expense anomalies, cash flow observations, "
                f"tax planning suggestions. Keep it practical for a small business owner."
            ),
            ctx=ctx, event_type="books.analyze", step_type="summarize",
            inputs={"action": "books.analyze", "period": period},
        )
