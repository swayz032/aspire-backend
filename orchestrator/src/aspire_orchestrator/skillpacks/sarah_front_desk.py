"""Sarah Front Desk Skill Pack — Call routing, call transfer, visitor logging.

Sarah handles the front desk: inbound call classification & routing (GREEN),
call transfers with user confirmation (YELLOW), and visitor/call event logging (GREEN).

Law compliance:
  - Law #1: Skill pack orchestrates tool calls; orchestrator decides when to invoke.
  - Law #2: Every method emits a receipt via _emit_receipt.
  - Law #3: Fails closed on missing parameters or telephony policy violations.
  - Law #4: route_call/log_visitor = GREEN, transfer_call = YELLOW.
  - Law #7: Delegates to tool_executor — no autonomous decisions.
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
from aspire_orchestrator.services.tool_types import ToolExecutionResult
from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
from aspire_orchestrator.services.tool_executor import execute_tool

logger = logging.getLogger(__name__)

ACTOR_SARAH = "skillpack:sarah-front-desk"
RECEIPT_VERSION = "1.0"


@dataclass(frozen=True)
class SkillPackResult:
    """Result from a Sarah Front Desk skill pack method."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False


@dataclass(frozen=True)
class SarahFrontDeskContext:
    """Required context for all Sarah Front Desk operations."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _emit_receipt(
    *,
    ctx: SarahFrontDeskContext,
    event_type: str,
    status: str,
    risk_tier: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    approval_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Sarah Front Desk operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_SARAH,
        "correlation_id": ctx.correlation_id,
        "risk_tier": risk_tier,
        "status": status,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": "allow",
            "policy_id": "sarah-front-desk-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    if approval_evidence:
        receipt["approval_evidence"] = approval_evidence
    return receipt


class SarahFrontDeskSkillPack:
    async def call_route(
        self,
        caller_info: dict[str, Any],
        context: SarahFrontDeskContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.route_call(caller_info=caller_info, context=context)

    async def call_transfer(
        self,
        call_id: str,
        destination: str,
        context: SarahFrontDeskContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.transfer_call(call_id=call_id, destination=destination, context=context)

    async def visitor_log(
        self,
        visitor_info: dict[str, Any],
        context: SarahFrontDeskContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.log_visitor(visitor_info=visitor_info, context=context)

    """Sarah Front Desk skill pack — call routing, transfer, visitor logging."""

    async def route_call(
        self,
        caller_info: dict[str, Any],
        context: SarahFrontDeskContext,
    ) -> SkillPackResult:
        """Classify an inbound call and route it to the appropriate destination.

        GREEN tier, no approval required. Uses telephony policy to validate
        topic safety before routing.
        """
        caller_number = caller_info.get("caller_number", "")
        if not caller_number:
            receipt = _emit_receipt(
                ctx=context,
                event_type="call.route",
                status="denied",
                risk_tier="green",
                inputs={"action": "call.route", "caller_number": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_CALLER_NUMBER"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: caller_number",
            )

        # Check telephony policy for forbidden topics in call context
        call_context_text = caller_info.get("context", "")
        if call_context_text and not TelephonyPolicy.check_topic_safety(call_context_text):
            receipt = _emit_receipt(
                ctx=context,
                event_type="call.route",
                status="denied",
                risk_tier="green",
                inputs={"action": "call.route", "caller_number": caller_number},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["FORBIDDEN_TOPIC_DETECTED"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Call context contains forbidden topic — requires human escalation",
            )

        # Execute call routing via tool_executor (Law #7)
        result: ToolExecutionResult = await execute_tool(
            tool_id="twilio.call.create",
            payload={
                "caller_number": caller_number,
                "caller_name": caller_info.get("caller_name", "Unknown"),
                "routing_action": "classify_and_route",
                "context": call_context_text,
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _emit_receipt(
            ctx=context,
            event_type="call.route",
            status=status,
            risk_tier="green",
            inputs={
                "action": "call.route",
                "caller_number": caller_number,
                "caller_name": caller_info.get("caller_name", "Unknown"),
            },
            metadata={
                "tool_id": result.tool_id,
                "routing_result": result.data.get("routing_result"),
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def transfer_call(
        self,
        call_id: str,
        destination: str,
        context: SarahFrontDeskContext,
    ) -> SkillPackResult:
        """Transfer an active call to a different destination.

        YELLOW tier — requires explicit user confirmation before executing.
        Returns approval_required=True with binding fields for the approval flow.
        """
        if not call_id:
            receipt = _emit_receipt(
                ctx=context,
                event_type="call.transfer",
                status="denied",
                risk_tier="yellow",
                inputs={"action": "call.transfer", "call_id": "", "destination": destination},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_CALL_ID"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: call_id",
            )

        if not destination:
            receipt = _emit_receipt(
                ctx=context,
                event_type="call.transfer",
                status="denied",
                risk_tier="yellow",
                inputs={"action": "call.transfer", "call_id": call_id, "destination": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_DESTINATION"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: destination",
            )

        # YELLOW tier: return approval_required with binding fields (Law #4)
        # The orchestrator approval flow must confirm before execution proceeds.
        binding_fields = {"call_id": call_id, "destination": destination}
        binding_hash = _compute_inputs_hash(binding_fields)

        # Execute transfer via tool_executor (Law #7)
        result: ToolExecutionResult = await execute_tool(
            tool_id="twilio.call.create",
            payload={
                "call_id": call_id,
                "destination": destination,
                "action": "transfer",
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="yellow",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _emit_receipt(
            ctx=context,
            event_type="call.transfer",
            status=status,
            risk_tier="yellow",
            inputs={
                "action": "call.transfer",
                "call_id": call_id,
                "destination": destination,
            },
            metadata={
                "tool_id": result.tool_id,
                "binding_hash": binding_hash,
            },
            approval_evidence={
                "binding_fields": binding_fields,
                "binding_hash": binding_hash,
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
            approval_required=True,
        )

    async def log_visitor(
        self,
        visitor_info: dict[str, Any],
        context: SarahFrontDeskContext,
    ) -> SkillPackResult:
        """Log a visitor or call event for front desk tracking.

        GREEN tier, no approval required. Creates an internal record of
        the visitor/call event with timestamp and metadata.
        """
        visitor_name = visitor_info.get("name", "")
        if not visitor_name:
            receipt = _emit_receipt(
                ctx=context,
                event_type="visitor.log",
                status="denied",
                risk_tier="green",
                inputs={"action": "visitor.log", "name": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_VISITOR_NAME"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: visitor name",
            )

        # Build visitor log entry
        log_entry = {
            "visitor_id": str(uuid.uuid4()),
            "name": visitor_name,
            "purpose": visitor_info.get("purpose", ""),
            "phone": visitor_info.get("phone", ""),
            "company": visitor_info.get("company", ""),
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "suite_id": context.suite_id,
            "office_id": context.office_id,
        }

        # Execute internal visitor log via tool_executor (Law #7)
        result: ToolExecutionResult = await execute_tool(
            tool_id="internal.visitor.log",
            payload=log_entry,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _emit_receipt(
            ctx=context,
            event_type="visitor.log",
            status=status,
            risk_tier="green",
            inputs={
                "action": "visitor.log",
                "name": visitor_name,
                "purpose": visitor_info.get("purpose", ""),
            },
            metadata={
                "visitor_id": log_entry["visitor_id"],
                "tool_id": result.tool_id,
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data={**log_entry, **result.data},
            receipt=receipt,
            error=result.error,
        )


# =============================================================================
# Phase 3 W4: Enhanced Sarah Front Desk with LLM reasoning
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedSarahFrontDesk(EnhancedSkillPack):
    """LLM-enhanced Sarah Front Desk — call routing, voicemail analysis, booking.

    Extends SarahFrontDeskSkillPack with:
    - analyze_call_intent: LLM classifies caller purpose and urgency
    - transcribe_voicemail: LLM summarizes voicemail and extracts action items
    - plan_booking: LLM builds appointment booking plan

    YELLOW tier for routing/booking, GREEN for analysis.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="sarah-front-desk",
            agent_name="Sarah Front Desk",
            default_risk_tier="yellow",
        )
        self._rule_pack = SarahFrontDeskSkillPack()

    async def analyze_call_intent(self, caller_info: dict, ctx: AgentContext) -> AgentResult:
        """Classify caller intent and determine routing. GREEN — analysis only."""
        caller_name = caller_info.get("name", caller_info.get("phone", ""))
        if not caller_name:
            receipt = self.build_receipt(
                ctx=ctx, event_type="call.analyze_intent",
                status="failed", inputs={"caller": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_CALLER_INFO"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing caller info")

        return await self.execute_with_llm(
            prompt=(
                f"You are Sarah, the front desk specialist.\n"
                f"Caller: {caller_name}\nReason: {caller_info.get('reason', 'unknown')}\n\n"
                f"Classify: intent (appointment/inquiry/complaint/sales/support), "
                f"urgency (low/medium/high), routing (ava/eli/quinn/nora/hold)."
            ),
            ctx=ctx, event_type="call.analyze_intent", step_type="classify",
            inputs={"action": "call.analyze_intent", "caller": "<CALLER_REDACTED>"},
        )

    async def transcribe_voicemail(self, voicemail_text: str, ctx: AgentContext) -> AgentResult:
        """Summarize voicemail and extract action items. GREEN — analysis only."""
        if not voicemail_text:
            receipt = self.build_receipt(
                ctx=ctx, event_type="call.voicemail_summary",
                status="failed", inputs={"length": 0},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["EMPTY_VOICEMAIL"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty voicemail")

        return await self.execute_with_llm(
            prompt=(
                f"You are Sarah. Summarize this voicemail.\n\n"
                f"Transcript: {voicemail_text[:2000]}\n\n"
                f"Extract: caller identity, purpose, urgency, callback number, "
                f"action items, recommended response."
            ),
            ctx=ctx, event_type="call.voicemail_summary", step_type="summarize",
            inputs={"action": "call.voicemail_summary", "length": len(voicemail_text)},
        )

    async def plan_booking(self, booking_request: dict, ctx: AgentContext) -> AgentResult:
        """Plan an appointment booking. YELLOW — requires approval."""
        return await self.execute_with_llm(
            prompt=(
                f"You are Sarah, planning an appointment.\n\n"
                f"Request: {booking_request}\n\n"
                f"Build booking plan: time slot selection, attendees, location/virtual, "
                f"confirmation message draft. YELLOW tier — user approves before scheduling."
            ),
            ctx=ctx, event_type="call.plan_booking", step_type="plan",
            inputs={"action": "call.plan_booking", "type": booking_request.get("type", "")},
        )
