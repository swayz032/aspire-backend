"""Quinn Invoicing Skill Pack — Invoice creation, sending, voiding, quotes, and webhooks.

Quinn is the Invoicing desk. She handles:
  - Invoice creation (YELLOW — financial + external communication)
  - Invoice sending (YELLOW — external delivery to customer)
  - Invoice voiding (YELLOW — reversal of financial document)
  - Quote creation (YELLOW — financial proposal to customer)
  - Quote sending (YELLOW — external delivery of financial proposal)
  - Webhook processing (GREEN — internal event processing)

Provider: Stripe (via Stripe Connect — per-suite connected accounts)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters, missing binding fields
  - Law #4: All invoice/quote actions YELLOW; webhook handler GREEN
  - Law #5: Capability tokens required for all Stripe tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all Stripe calls (tools are hands)

Binding fields enforcement (per policy_matrix.yaml):
  - invoice.create: customer_id, amount, currency, line_items
  - invoice.send: invoice_id
  - invoice.void: invoice_id
  - quote.create: customer_id, amount, currency, line_items
  - quote.send: quote_id
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

ACTOR_QUINN = "skillpack:quinn-invoicing"
RECEIPT_VERSION = "1.0"

# Binding fields per policy_matrix.yaml — must be confirmed by user
INVOICE_CREATE_BINDING_FIELDS = {"customer_id", "amount", "currency", "line_items"}
INVOICE_SEND_BINDING_FIELDS = {"invoice_id"}
INVOICE_VOID_BINDING_FIELDS = {"invoice_id"}
QUOTE_CREATE_BINDING_FIELDS = {"customer_id", "amount", "currency", "line_items"}
QUOTE_SEND_BINDING_FIELDS = {"quote_id"}

# Stripe webhook events we process
HANDLED_WEBHOOK_EVENTS = frozenset({
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.finalized",
    "invoice.voided",
    "invoice.updated",
    "quote.accepted",
    "quote.canceled",
})


@dataclass
class SkillPackResult:
    """Result of a Quinn Invoicing skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False


@dataclass
class QuinnContext:
    """Tenant-scoped execution context for Quinn operations."""

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
    ctx: QuinnContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Quinn invoicing operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-quinn-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_QUINN,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": _compute_inputs_hash(inputs or {}),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "quinn-invoicing-v1",
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
        elif isinstance(val, list) and len(val) == 0:
            missing.append(f)
    return missing


class QuinnInvoicingSkillPack:
    async def invoice_create(
        self,
        customer: str,
        line_items: list[dict[str, Any]],
        context: QuinnContext,
        *,
        amount: int | None = None,
        currency: str = "usd",
        description: str = "",
        due_days: int = 30,
    ) -> SkillPackResult:
        return await self.create_invoice(customer=customer, line_items=line_items, context=context, amount=amount, currency=currency, description=description, due_days=due_days)

    async def invoice_send(
        self,
        invoice_id: str,
        context: QuinnContext,
    ) -> SkillPackResult:
        return await self.send_invoice(invoice_id=invoice_id, context=context)

    async def invoice_void(
        self,
        invoice_id: str,
        reason: str,
        context: QuinnContext,
    ) -> SkillPackResult:
        return await self.void_invoice(invoice_id=invoice_id, reason=reason, context=context)

    async def quote_create(
        self,
        customer: str,
        line_items: list[dict[str, Any]],
        expiry: str | None,
        context: QuinnContext,
        *,
        amount: int | None = None,
        currency: str = "usd",
    ) -> SkillPackResult:
        return await self.create_quote(customer=customer, line_items=line_items, expiry=expiry, context=context, amount=amount, currency=currency)

    async def quote_send(
        self,
        quote_id: str,
        context: QuinnContext,
    ) -> SkillPackResult:
        return await self.send_quote(quote_id=quote_id, context=context)

    """Quinn Invoicing Skill Pack — governed invoice and quote operations.

    All methods require a QuinnContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).

    All invoice/quote operations are YELLOW tier (Law #4):
    they require explicit user confirmation before execution.
    """

    async def create_invoice(
        self,
        customer: str,
        line_items: list[dict[str, Any]],
        context: QuinnContext,
        *,
        amount: int | None = None,
        currency: str = "usd",
        description: str = "",
        due_days: int = 30,
    ) -> SkillPackResult:
        """Create a draft invoice via Stripe (YELLOW — requires user approval).

        Binding fields: customer_id, amount, currency, line_items.
        All must be confirmed by user before execution (approve-then-swap defense).
        """
        params = {
            "customer_id": customer,
            "amount": amount,
            "currency": currency,
            "line_items": line_items,
        }

        missing = _check_binding_fields(params, INVOICE_CREATE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="invoice.create",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="stripe.invoice.create",
                inputs={"action": "invoice.create", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # YELLOW tier: build the plan, mark approval_required
        invoice_plan = {
            "customer_id": customer,
            "amount": amount,
            "currency": currency,
            "line_items": line_items,
            "description": description,
            "due_days": due_days,
            "risk_tier": "yellow",
            "binding_fields": sorted(INVOICE_CREATE_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="invoice.create",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="stripe.invoice.create",
            inputs={"action": "invoice.create", "customer_id": customer, "amount": amount},
            metadata={
                "customer_id": customer,
                "amount": amount,
                "currency": currency,
                "line_item_count": len(line_items),
            },
        )

        return SkillPackResult(
            success=True,
            data=invoice_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def send_invoice(
        self,
        invoice_id: str,
        context: QuinnContext,
    ) -> SkillPackResult:
        """Finalize and send an invoice via Stripe (YELLOW — requires user approval).

        Binding fields: invoice_id.
        """
        params = {"invoice_id": invoice_id}
        missing = _check_binding_fields(params, INVOICE_SEND_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="invoice.send",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="stripe.invoice.send",
                inputs={"action": "invoice.send", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        send_plan = {
            "invoice_id": invoice_id,
            "risk_tier": "yellow",
            "binding_fields": sorted(INVOICE_SEND_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="invoice.send",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="stripe.invoice.send",
            inputs={"action": "invoice.send", "invoice_id": invoice_id},
            metadata={"invoice_id": invoice_id},
        )

        return SkillPackResult(
            success=True,
            data=send_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def void_invoice(
        self,
        invoice_id: str,
        reason: str,
        context: QuinnContext,
    ) -> SkillPackResult:
        """Void an open invoice via Stripe (YELLOW — requires user approval).

        Binding fields: invoice_id.
        Reason is required for audit trail (Law #2).
        """
        if not reason or not reason.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="invoice.void",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_VOID_REASON",
                tool_used="stripe.invoice.void",
                inputs={"action": "invoice.void", "invoice_id": invoice_id},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: reason (void reason is mandatory for audit trail)",
            )

        params = {"invoice_id": invoice_id}
        missing = _check_binding_fields(params, INVOICE_VOID_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="invoice.void",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="stripe.invoice.void",
                inputs={"action": "invoice.void", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        void_plan = {
            "invoice_id": invoice_id,
            "reason": reason.strip(),
            "risk_tier": "yellow",
            "binding_fields": sorted(INVOICE_VOID_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="invoice.void",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="stripe.invoice.void",
            inputs={"action": "invoice.void", "invoice_id": invoice_id, "reason": reason.strip()},
            metadata={"invoice_id": invoice_id, "void_reason": reason.strip()},
        )

        return SkillPackResult(
            success=True,
            data=void_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def create_quote(
        self,
        customer: str,
        line_items: list[dict[str, Any]],
        expiry: str | None,
        context: QuinnContext,
        *,
        amount: int | None = None,
        currency: str = "usd",
    ) -> SkillPackResult:
        """Create a quote via Stripe (YELLOW — requires user approval).

        Binding fields: customer_id, amount, currency, line_items.
        """
        params = {
            "customer_id": customer,
            "amount": amount,
            "currency": currency,
            "line_items": line_items,
        }

        missing = _check_binding_fields(params, QUOTE_CREATE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="quote.create",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="stripe.quote.create",
                inputs={"action": "quote.create", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        quote_plan: dict[str, Any] = {
            "customer_id": customer,
            "amount": amount,
            "currency": currency,
            "line_items": line_items,
            "risk_tier": "yellow",
            "binding_fields": sorted(QUOTE_CREATE_BINDING_FIELDS),
        }
        if expiry:
            quote_plan["expires_at"] = expiry

        receipt = _make_receipt(
            ctx=context,
            action_type="quote.create",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="stripe.quote.create",
            inputs={"action": "quote.create", "customer_id": customer, "amount": amount},
            metadata={
                "customer_id": customer,
                "amount": amount,
                "currency": currency,
                "line_item_count": len(line_items),
            },
        )

        return SkillPackResult(
            success=True,
            data=quote_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def send_quote(
        self,
        quote_id: str,
        context: QuinnContext,
    ) -> SkillPackResult:
        """Finalize and send a quote via Stripe (YELLOW — requires user approval).

        Binding fields: quote_id.
        """
        params = {"quote_id": quote_id}
        missing = _check_binding_fields(params, QUOTE_SEND_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="quote.send",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="stripe.quote.send",
                inputs={"action": "quote.send", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        send_plan = {
            "quote_id": quote_id,
            "risk_tier": "yellow",
            "binding_fields": sorted(QUOTE_SEND_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="quote.send",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="stripe.quote.send",
            inputs={"action": "quote.send", "quote_id": quote_id},
            metadata={"quote_id": quote_id},
        )

        return SkillPackResult(
            success=True,
            data=send_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def handle_webhook(
        self,
        event_type: str,
        payload: dict[str, Any],
        context: QuinnContext,
    ) -> SkillPackResult:
        """Process a Stripe webhook event (GREEN — internal event processing).

        This is an internal operation: Stripe sends webhook events when
        invoices are paid, fail payment, or quotes are accepted.
        No user approval needed (GREEN tier).

        Args:
            event_type: Stripe event type (e.g. "invoice.paid")
            payload: Stripe event data object
            context: Tenant-scoped execution context
        """
        if not event_type:
            receipt = _make_receipt(
                ctx=context,
                action_type="invoice.webhook",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_EVENT_TYPE",
                inputs={"action": "invoice.webhook"},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: event_type",
            )

        if event_type not in HANDLED_WEBHOOK_EVENTS:
            receipt = _make_receipt(
                ctx=context,
                action_type="invoice.webhook",
                risk_tier="green",
                outcome="success",
                reason_code="UNHANDLED_EVENT",
                inputs={"action": "invoice.webhook", "event_type": event_type},
                metadata={"event_type": event_type, "handled": False},
            )
            return SkillPackResult(
                success=True,
                data={"event_type": event_type, "handled": False, "reason": "unrecognized_event"},
                receipt=receipt,
            )

        # Extract key fields from the Stripe event payload
        obj = payload.get("object", payload)
        event_data = _extract_webhook_data(event_type, obj)

        receipt = _make_receipt(
            ctx=context,
            action_type="invoice.webhook",
            risk_tier="green",
            outcome="success",
            reason_code="EXECUTED",
            inputs={"action": "invoice.webhook", "event_type": event_type},
            metadata={
                "event_type": event_type,
                "handled": True,
                **event_data,
            },
        )

        return SkillPackResult(
            success=True,
            data={
                "event_type": event_type,
                "handled": True,
                **event_data,
            },
            receipt=receipt,
        )


def _extract_webhook_data(event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
    """Extract relevant fields from a Stripe webhook event object."""
    data: dict[str, Any] = {}

    if event_type.startswith("invoice."):
        data["invoice_id"] = obj.get("id", "")
        data["status"] = obj.get("status", "")
        data["amount_due"] = obj.get("amount_due", 0)
        data["amount_paid"] = obj.get("amount_paid", 0)
        data["customer"] = obj.get("customer", "")

        if event_type == "invoice.payment_failed":
            charge = obj.get("charge", {})
            if isinstance(charge, dict):
                data["failure_code"] = charge.get("failure_code", "")
                data["failure_message"] = charge.get("failure_message", "")

    elif event_type.startswith("quote."):
        data["quote_id"] = obj.get("id", "")
        data["status"] = obj.get("status", "")
        data["amount_total"] = obj.get("amount_total", 0)
        data["customer"] = obj.get("customer", "")

    return data


# =============================================================================
# Phase 3 W4: Enhanced Quinn Invoicing with LLM reasoning
# =============================================================================

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedQuinnInvoicing(AgenticSkillPack):
    """LLM-enhanced Quinn Invoicing — intelligent invoice parsing, customer matching.

    Extends QuinnInvoicingSkillPack with:
    - parse_invoice_intent: LLM extracts line items, customer, amounts from natural language
    - match_customer: LLM fuzzy-matches customer name to existing Stripe customers
    - draft_invoice_plan: LLM builds a complete invoice plan for user approval

    YELLOW tier — all operations require user confirmation before execution.
    Binding fields enforced: amount, customer_id, currency (approve-then-swap defense).
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="quinn-invoicing",
            agent_name="Quinn Invoicing",
            default_risk_tier="yellow",
            memory_enabled=True,
        )
        self._rule_pack = QuinnInvoicingSkillPack()

    async def parse_invoice_intent(
        self,
        user_request: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Parse natural language into structured invoice data using LLM.

        Uses primary_reasoner (GPT-5.2) for complex invoices with:
        - Multiple line items
        - Tax calculations
        - Discount application
        - Payment terms extraction

        YELLOW tier — returns structured plan, requires approval to execute.
        """
        if not user_request or not user_request.strip():
            receipt = self.build_receipt(
                ctx=ctx, event_type="invoice.parse_intent",
                status="failed", inputs={"user_request": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUEST"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing user_request")

        return await self.execute_with_llm(
            prompt=(
                f"You are Quinn, the invoicing specialist for a small business.\n"
                f"Parse this invoice request into structured data.\n\n"
                f"Request: {user_request}\n\n"
                f"Extract:\n"
                f"1. Customer name/identifier\n"
                f"2. Line items (description, quantity, unit_price)\n"
                f"3. Currency (default: USD)\n"
                f"4. Due date or payment terms (net_30, net_60, etc.)\n"
                f"5. Any discounts or special terms\n"
                f"6. Tax rate if mentioned\n\n"
                f"Return structured JSON. Mark uncertain fields as [CONFIRM_WITH_USER].\n"
                f"BINDING FIELDS (cannot change after approval): amount, customer_id, currency."
            ),
            ctx=ctx,
            event_type="invoice.parse_intent",
            step_type="extract",
            inputs={"action": "invoice.parse_intent", "request_length": len(user_request)},
        )

    async def match_customer(
        self,
        customer_name: str,
        known_customers: list[dict[str, Any]],
        ctx: AgentContext,
    ) -> AgentResult:
        """Fuzzy-match customer name to existing Stripe customers using LLM.

        Uses cheap_classifier (GPT-5-mini) for fast matching.
        GREEN tier operation within YELLOW pack — matching is read-only.
        """
        if not customer_name:
            receipt = self.build_receipt(
                ctx=ctx, event_type="invoice.match_customer",
                status="failed", inputs={"customer_name": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_CUSTOMER_NAME"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing customer_name")

        # Truncate customer list for LLM context
        customer_preview = [
            {"id": c.get("id", ""), "name": c.get("name", ""), "email": c.get("email", "")}
            for c in known_customers[:50]
        ]

        return await self.execute_with_llm(
            prompt=(
                f"Match this customer name to the closest existing customer.\n\n"
                f"Input: \"{customer_name}\"\n"
                f"Known customers: {customer_preview}\n\n"
                f"Return the best match with:\n"
                f"1. Matched customer ID and name\n"
                f"2. Confidence score (0.0-1.0)\n"
                f"3. If confidence < 0.7, suggest creating a new customer\n"
                f"4. Alternative matches if any"
            ),
            ctx=ctx,
            event_type="invoice.match_customer",
            step_type="classify",
            inputs={"action": "invoice.match_customer", "customer_name": customer_name},
        )

    async def draft_invoice_plan(
        self,
        parsed_data: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult:
        """Build a complete invoice plan for user approval.

        Uses fast_general (GPT-5) to validate and enrich the parsed data.
        YELLOW tier — requires user approval before Stripe API call.
        """
        # Law #3: Fail-closed on empty input
        if not parsed_data:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="invoice.draft_plan",
                status="denied",
                inputs={"action": "invoice.draft_plan"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["empty_parsed_data"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, data={}, receipt=receipt)

        return await self.execute_with_llm(
            prompt=(
                f"You are Quinn. Build a complete invoice plan from parsed data.\n\n"
                f"Parsed data: {parsed_data}\n\n"
                f"Produce a plan with:\n"
                f"1. Validated line items with subtotals\n"
                f"2. Tax calculation\n"
                f"3. Total amount due\n"
                f"4. Payment terms and due date\n"
                f"5. Stripe API parameters (ready for execution)\n"
                f"6. Binding fields summary for approval UX\n\n"
                f"This plan goes to the user for approval. After approval,\n"
                f"the orchestrator mints a capability token for stripe.invoice.create."
            ),
            ctx=ctx,
            event_type="invoice.draft_plan",
            step_type="plan",
            inputs={
                "action": "invoice.draft_plan",
                "line_item_count": len(parsed_data.get("line_items", [])),
                "customer": parsed_data.get("customer_name", ""),
            },
        )
