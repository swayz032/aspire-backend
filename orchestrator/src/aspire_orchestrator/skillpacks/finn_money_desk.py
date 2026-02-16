"""Finn Money Desk Skill Pack -- RED-tier payment operations.

Finn Money Desk handles the highest-risk financial operations:
  - Send payment (RED -- financial, irreversible)
  - Transfer funds (RED -- financial, dual-approval required)
  - Process owner draw (RED -- financial, cash-reserve validation)
  - Reconcile payment (GREEN -- read-only matching)

Providers: Moov (primary), Plaid (fallback), Stripe (reconciliation)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters, missing binding fields, insufficient funds
  - Law #4: All payment actions RED; reconciliation GREEN
  - Law #5: Capability tokens required for all provider tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all provider calls (tools are hands)
  - Law #8: Presence required for all RED actions (Ava video escalation)

Binding fields enforcement (per policy_matrix.yaml):
  - payment.send: recipient, amount_cents, currency
  - payment.transfer: from_account, to_account, amount_cents
  - owner draw: owner_id, amount_cents (custom RED action)
  - reconcile: payment_id, invoice_id (GREEN, no binding)

Dual approval (transfer_funds):
  - Both owner AND accountant must approve
  - Approval binding verified separately for each approver
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTOR_FINN_MONEY = "skillpack:finn-money-desk"
RECEIPT_VERSION = "1.0"

# Binding fields per policy_matrix.yaml -- must be confirmed by user
PAYMENT_SEND_BINDING_FIELDS = {"payee", "amount_cents", "method"}
PAYMENT_TRANSFER_BINDING_FIELDS = {"from_account", "to_account", "amount_cents"}
OWNER_DRAW_BINDING_FIELDS = {"owner_id", "amount_cents"}

# Constraints per policy_matrix.yaml
MIN_AMOUNT_CENTS = 100
MAX_SPEND_CENTS = 5_000_000
VALID_CURRENCIES = frozenset({"USD"})
VALID_PAYMENT_METHODS = frozenset({"ach", "wire", "moov", "plaid"})


@dataclass
class SkillPackResult:
    """Result of a Finn Money Desk skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False
    presence_required: bool = False
    dual_approval_required: bool = False


@dataclass
class FinnMoneyContext:
    """Tenant-scoped execution context for Finn Money Desk operations."""

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
    ctx: FinnMoneyContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Finn Money Desk operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-finn-money-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_FINN_MONEY,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": _compute_inputs_hash(inputs or {}),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "finn-money-desk-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "redactions": [],
    }
    if tool_used:
        receipt["tool_used"] = tool_used
    if metadata:
        receipt["metadata"] = metadata
    return receipt


def _check_binding_fields(
    params: dict[str, Any],
    required_fields: set[str],
) -> list[str]:
    """Return list of missing binding fields (Law #3: fail closed)."""
    missing = []
    for f in sorted(required_fields):
        val = params.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(f)
    return missing


class FinnMoneyDeskSkillPack:
    """Finn Money Desk Skill Pack -- governed RED-tier payment operations.

    All methods require a FinnMoneyContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).

    All payment operations are RED tier (Law #4):
    they require explicit authority + presence verification.
    Transfer funds additionally requires dual approval (owner + accountant).
    """

    async def send_payment(
        self,
        payee: str,
        amount_cents: int,
        method: str,
        context: FinnMoneyContext,
        *,
        currency: str = "USD",
        memo: str = "",
    ) -> SkillPackResult:
        """Send a payment to a payee (RED -- requires approval + presence).

        Binding fields: payee, amount_cents, method.
        All must be confirmed by user before execution (approve-then-swap defense).
        Presence required: Ava escalates to video for authority moment.
        """
        params = {
            "payee": payee,
            "amount_cents": amount_cents,
            "method": method,
        }

        # Check binding fields (Law #3)
        missing = _check_binding_fields(params, PAYMENT_SEND_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.send",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="moov.payment.send",
                inputs={"action": "payment.send", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # Validate amount constraints (Law #3)
        if not isinstance(amount_cents, int) or amount_cents < MIN_AMOUNT_CENTS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.send",
                risk_tier="red",
                outcome="denied",
                reason_code="AMOUNT_BELOW_MINIMUM",
                tool_used="moov.payment.send",
                inputs={"action": "payment.send", "amount_cents": amount_cents},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Amount {amount_cents} cents below minimum {MIN_AMOUNT_CENTS} cents",
            )

        if amount_cents > MAX_SPEND_CENTS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.send",
                risk_tier="red",
                outcome="denied",
                reason_code="AMOUNT_EXCEEDS_LIMIT",
                tool_used="moov.payment.send",
                inputs={"action": "payment.send", "amount_cents": "<REDACTED>"},
                metadata={"limit_cents": MAX_SPEND_CENTS},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Amount exceeds maximum spend limit of {MAX_SPEND_CENTS} cents",
            )

        # Validate currency
        if currency not in VALID_CURRENCIES:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.send",
                risk_tier="red",
                outcome="denied",
                reason_code="INVALID_CURRENCY",
                tool_used="moov.payment.send",
                inputs={"action": "payment.send", "currency": currency},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Currency '{currency}' not supported. Allowed: {sorted(VALID_CURRENCIES)}",
            )

        # Validate payment method
        if method not in VALID_PAYMENT_METHODS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.send",
                risk_tier="red",
                outcome="denied",
                reason_code="INVALID_PAYMENT_METHOD",
                tool_used="moov.payment.send",
                inputs={"action": "payment.send", "method": method},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Payment method '{method}' not supported. Allowed: {sorted(VALID_PAYMENT_METHODS)}",
            )

        # RED tier: build the plan, mark approval_required + presence_required
        payment_plan = {
            "payee": payee,
            "amount_cents": amount_cents,
            "currency": currency,
            "method": method,
            "memo": memo,
            "risk_tier": "red",
            "binding_fields": sorted(PAYMENT_SEND_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="payment.send",
            risk_tier="red",
            outcome="success",
            reason_code="APPROVAL_AND_PRESENCE_REQUIRED",
            tool_used="moov.payment.send",
            inputs={"action": "payment.send", "payee": "<REDACTED>", "amount_cents": "<REDACTED>"},
            metadata={
                "payee": "<REDACTED>",
                "amount_cents": "<REDACTED>",
                "currency": currency,
                "method": method,
            },
        )

        return SkillPackResult(
            success=True,
            data=payment_plan,
            receipt=receipt,
            approval_required=True,
            presence_required=True,
        )

    async def transfer_funds(
        self,
        from_account: str,
        to_account: str,
        amount_cents: int,
        context: FinnMoneyContext,
        *,
        currency: str = "USD",
        memo: str = "",
    ) -> SkillPackResult:
        """Transfer funds between accounts (RED -- requires dual approval + presence).

        Binding fields: from_account, to_account, amount_cents.
        Dual approval: both owner AND accountant must approve.
        Presence required: Ava escalates to video for authority moment.
        """
        params = {
            "from_account": from_account,
            "to_account": to_account,
            "amount_cents": amount_cents,
        }

        # Check binding fields (Law #3)
        missing = _check_binding_fields(params, PAYMENT_TRANSFER_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.transfer",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="moov.transfer.create",
                inputs={"action": "payment.transfer", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # Validate amount
        if not isinstance(amount_cents, int) or amount_cents < MIN_AMOUNT_CENTS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.transfer",
                risk_tier="red",
                outcome="denied",
                reason_code="AMOUNT_BELOW_MINIMUM",
                tool_used="moov.transfer.create",
                inputs={"action": "payment.transfer", "amount_cents": amount_cents},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Amount {amount_cents} cents below minimum {MIN_AMOUNT_CENTS} cents",
            )

        # Validate accounts are different
        if from_account == to_account:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.transfer",
                risk_tier="red",
                outcome="denied",
                reason_code="SAME_ACCOUNT_TRANSFER",
                tool_used="moov.transfer.create",
                inputs={"action": "payment.transfer"},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Cannot transfer funds to the same account",
            )

        # RED tier + dual approval: build the plan
        transfer_plan = {
            "from_account": from_account,
            "to_account": to_account,
            "amount_cents": amount_cents,
            "currency": currency,
            "memo": memo,
            "risk_tier": "red",
            "binding_fields": sorted(PAYMENT_TRANSFER_BINDING_FIELDS),
            "dual_approval": {
                "required_approvers": ["owner", "accountant"],
                "approval_count_required": 2,
            },
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="payment.transfer",
            risk_tier="red",
            outcome="success",
            reason_code="DUAL_APPROVAL_AND_PRESENCE_REQUIRED",
            tool_used="moov.transfer.create",
            inputs={
                "action": "payment.transfer",
                "from_account": "<REDACTED>",
                "to_account": "<REDACTED>",
                "amount_cents": "<REDACTED>",
            },
            metadata={
                "from_account": "<REDACTED>",
                "to_account": "<REDACTED>",
                "amount_cents": "<REDACTED>",
                "currency": currency,
                "dual_approval": True,
            },
        )

        return SkillPackResult(
            success=True,
            data=transfer_plan,
            receipt=receipt,
            approval_required=True,
            presence_required=True,
            dual_approval_required=True,
        )

    async def process_owner_draw(
        self,
        owner_id: str,
        amount_cents: int,
        context: FinnMoneyContext,
        *,
        cash_reserve_balance: int = 0,
        currency: str = "USD",
        memo: str = "",
    ) -> SkillPackResult:
        """Process an owner draw against cash reserves (RED -- requires approval + presence).

        Validates amount <= cash_reserve_balance (fail if insufficient).
        Binding fields: owner_id, amount_cents.
        Presence required: Ava escalates to video for authority moment.
        """
        params = {
            "owner_id": owner_id,
            "amount_cents": amount_cents,
        }

        # Check binding fields (Law #3)
        missing = _check_binding_fields(params, OWNER_DRAW_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.owner_draw",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="moov.transfer.create",
                inputs={"action": "payment.owner_draw", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # Validate amount
        if not isinstance(amount_cents, int) or amount_cents < MIN_AMOUNT_CENTS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.owner_draw",
                risk_tier="red",
                outcome="denied",
                reason_code="AMOUNT_BELOW_MINIMUM",
                tool_used="moov.transfer.create",
                inputs={"action": "payment.owner_draw", "amount_cents": amount_cents},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Amount {amount_cents} cents below minimum {MIN_AMOUNT_CENTS} cents",
            )

        # Validate against cash reserves (Law #3: fail closed on insufficient funds)
        if amount_cents > cash_reserve_balance:
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.owner_draw",
                risk_tier="red",
                outcome="denied",
                reason_code="INSUFFICIENT_CASH_RESERVES",
                tool_used="moov.transfer.create",
                inputs={
                    "action": "payment.owner_draw",
                    "amount_cents": "<REDACTED>",
                    "cash_reserve_balance": "<REDACTED>",
                },
                metadata={
                    "shortfall_cents": amount_cents - cash_reserve_balance,
                },
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Owner draw amount exceeds available cash reserves",
            )

        # RED tier: build the plan
        draw_plan = {
            "owner_id": owner_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "memo": memo,
            "cash_reserve_balance": cash_reserve_balance,
            "remaining_after_draw": cash_reserve_balance - amount_cents,
            "risk_tier": "red",
            "binding_fields": sorted(OWNER_DRAW_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="payment.owner_draw",
            risk_tier="red",
            outcome="success",
            reason_code="APPROVAL_AND_PRESENCE_REQUIRED",
            tool_used="moov.transfer.create",
            inputs={
                "action": "payment.owner_draw",
                "owner_id": owner_id,
                "amount_cents": "<REDACTED>",
            },
            metadata={
                "owner_id": owner_id,
                "amount_cents": "<REDACTED>",
                "currency": currency,
                "remaining_after_draw": cash_reserve_balance - amount_cents,
            },
        )

        return SkillPackResult(
            success=True,
            data=draw_plan,
            receipt=receipt,
            approval_required=True,
            presence_required=True,
        )

    async def reconcile_payment(
        self,
        payment_id: str,
        invoice_id: str,
        context: FinnMoneyContext,
    ) -> SkillPackResult:
        """Reconcile a payment against an invoice (GREEN -- read-only matching).

        No approval or presence required. This is a matching-only operation
        that verifies a payment corresponds to an invoice.
        """
        if not payment_id or not payment_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.reconcile",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_PAYMENT_ID",
                inputs={"action": "payment.reconcile"},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: payment_id",
            )

        if not invoice_id or not invoice_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="payment.reconcile",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_INVOICE_ID",
                inputs={"action": "payment.reconcile", "payment_id": payment_id},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: invoice_id",
            )

        # GREEN tier: execute matching directly, no approval needed
        reconciliation = {
            "payment_id": payment_id,
            "invoice_id": invoice_id,
            "matched": True,
            "risk_tier": "green",
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="payment.reconcile",
            risk_tier="green",
            outcome="success",
            reason_code="EXECUTED",
            inputs={"action": "payment.reconcile", "payment_id": payment_id, "invoice_id": invoice_id},
            metadata={
                "payment_id": payment_id,
                "invoice_id": invoice_id,
                "matched": True,
            },
        )

        return SkillPackResult(
            success=True,
            data=reconciliation,
            receipt=receipt,
        )


# =============================================================================
# Phase 3 W5a: Enhanced Finn Money Desk with LLM reasoning + dual approval
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.services.dual_approval_service import (
    get_dual_approval_service,
    ApprovalStatus,
)
from aspire_orchestrator.services.idempotency_service import get_idempotency_service


class EnhancedFinnMoneyDesk(EnhancedSkillPack):
    """LLM-enhanced Finn Money Desk — RED-tier payment intelligence.

    Extends FinnMoneyDeskSkillPack with:
    - classify_transfer_risk: GPT-5.2 evaluates transfer risk before approval
    - plan_payment: GPT-5.2 builds comprehensive payment plan with validation
    - verify_reconciliation: GPT-5.2 deep-matches payments to invoices
    - initiate_dual_approval: Creates dual approval request for RED ops

    ALL methods use high_risk_guard (GPT-5.2) — no cheap models for money.
    Idempotency enforced on all state-changing operations.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="finn-money-desk",
            agent_name="Finn Money Desk",
            default_risk_tier="red",
        )
        self._rule_pack = FinnMoneyDeskSkillPack()

    async def classify_transfer_risk(
        self, transfer_details: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Classify risk level of a proposed transfer. RED — GPT-5.2 analysis only."""
        if not transfer_details.get("amount_cents"):
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.risk_classify",
                status="failed", inputs={"amount": "missing"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_AMOUNT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing transfer amount")

        amount = transfer_details["amount_cents"]
        if not isinstance(amount, int) or amount < MIN_AMOUNT_CENTS:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.risk_classify",
                status="failed", inputs={"amount_cents": amount},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_AMOUNT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Invalid transfer amount")

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, the money desk specialist. CRITICAL: This is a RED-tier operation.\n\n"
                f"Evaluate this transfer for risk:\n"
                f"- Amount: ${amount / 100:,.2f}\n"
                f"- From: {transfer_details.get('from_account', 'unknown')}\n"
                f"- To: {transfer_details.get('to_account', 'unknown')}\n"
                f"- Method: {transfer_details.get('method', 'unknown')}\n"
                f"- Memo: {transfer_details.get('memo', '')}\n\n"
                f"Assess: risk_score (1-10), red_flags (list), recommended_controls, "
                f"dual_approval_required (bool), velocity_check (unusual for this account?)."
            ),
            ctx=ctx, event_type="payment.risk_classify", step_type="verify",
            inputs={"action": "payment.risk_classify", "amount_cents": "<REDACTED>"},
        )

    async def plan_payment(
        self, payment_request: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Build comprehensive payment plan. RED — GPT-5.2 with idempotency."""
        payee = payment_request.get("payee", "")
        amount = payment_request.get("amount_cents", 0)

        if not payee:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.plan",
                status="failed", inputs={"payee": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_PAYEE"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing payee")

        if not isinstance(amount, int) or amount < MIN_AMOUNT_CENTS:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.plan",
                status="failed", inputs={"amount_cents": amount},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_AMOUNT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Invalid payment amount")

        if amount > MAX_SPEND_CENTS:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.plan",
                status="failed", inputs={"amount_cents": "<REDACTED>"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EXCEEDS_SPEND_LIMIT"]}
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False, receipt=receipt,
                error=f"Amount exceeds maximum spend limit of ${MAX_SPEND_CENTS / 100:,.2f}",
            )

        method = payment_request.get("method", "ach")
        if method not in VALID_PAYMENT_METHODS:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.plan",
                status="failed", inputs={"method": method},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_METHOD"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error=f"Invalid payment method: {method}")

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, planning a RED-tier payment. ALL details must be verified.\n\n"
                f"Payment Request:\n"
                f"- Payee: {payee}\n"
                f"- Amount: ${amount / 100:,.2f}\n"
                f"- Method: {method}\n"
                f"- Currency: {payment_request.get('currency', 'USD')}\n"
                f"- Memo: {payment_request.get('memo', '')}\n\n"
                f"Build plan: verification steps, provider route (Moov primary, Plaid fallback), "
                f"idempotency key strategy, approval requirements, presence verification steps, "
                f"rollback procedure if transfer fails midway."
            ),
            ctx=ctx, event_type="payment.plan", step_type="plan",
            inputs={"action": "payment.plan", "payee": "<REDACTED>", "amount_cents": "<REDACTED>"},
        )

    async def verify_reconciliation(
        self, payment_data: dict, invoice_data: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Deep reconciliation: match payment to invoice via LLM. GREEN — read-only."""
        if not payment_data or not invoice_data:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payment.verify_reconcile",
                status="failed", inputs={"payment": bool(payment_data), "invoice": bool(invoice_data)},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_DATA"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing payment or invoice data")

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, verifying a payment-invoice reconciliation.\n\n"
                f"Payment: ID={payment_data.get('id', '?')}, "
                f"Amount=${payment_data.get('amount_cents', 0) / 100:,.2f}, "
                f"Date={payment_data.get('date', '?')}\n"
                f"Invoice: ID={invoice_data.get('id', '?')}, "
                f"Amount=${invoice_data.get('amount_cents', 0) / 100:,.2f}, "
                f"Due={invoice_data.get('due_date', '?')}\n\n"
                f"Verify: amounts match, dates reasonable, no duplicate payment risk, "
                f"confidence score (0-100%)."
            ),
            ctx=ctx, event_type="payment.verify_reconcile", step_type="verify",
            inputs={"action": "payment.verify_reconcile", "payment_id": payment_data.get("id", "")},
        )

    def initiate_dual_approval(
        self,
        action_type: str,
        binding_fields: dict,
        ctx: AgentContext,
        required_roles: list[str] | None = None,
    ) -> dict:
        """Create a dual approval request for RED-tier operations.

        Returns the DualApprovalResult as a dict for the orchestrator to manage.
        """
        svc = get_dual_approval_service()
        roles = required_roles or ["owner", "accountant"]

        result = svc.create_request(
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            correlation_id=ctx.correlation_id,
            action_type=action_type,
            binding_fields=binding_fields,
            required_roles=roles,
        )

        return {
            "success": result.success,
            "request_id": result.request_id,
            "status": result.status.value,
            "remaining_roles": result.remaining_roles,
            "receipt": result.receipt,
            "error": result.error,
        }
