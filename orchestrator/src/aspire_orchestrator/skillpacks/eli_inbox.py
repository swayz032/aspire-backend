"""Eli Inbox Skill Pack — Email read, triage, draft, send.

GREEN tier: read_emails, triage_email (keyword classification, no LLM)
YELLOW tier: draft_response, send_email (external communication)

Law compliance:
  - Law #1: Skill pack orchestrates tool calls; orchestrator decides when to invoke.
  - Law #2: Every method emits a receipt via _emit_receipt.
  - Law #3: Fails closed on missing params or provider errors.
  - Law #4: GREEN (read/triage), YELLOW (draft/send) with approval_required flag.
  - Law #7: Delegates to tool_executor — no autonomous decisions.
  - Law #9: DLP redaction on email content in receipts.
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
from aspire_orchestrator.services.tool_executor import execute_tool

logger = logging.getLogger(__name__)

ACTOR_ELI = "skillpack:eli-inbox"
RECEIPT_VERSION = "1.0"

# Triage keyword categories — simple keyword matching (GREEN tier, no LLM)
_TRIAGE_KEYWORDS: dict[str, list[str]] = {
    "support": [
        "help", "issue", "problem", "error", "broken", "bug", "fix",
        "support", "assistance", "trouble", "not working", "complaint",
    ],
    "sales": [
        "quote", "pricing", "price", "cost", "proposal", "buy", "purchase",
        "interested", "demo", "trial", "discount", "offer", "deal",
    ],
    "billing": [
        "invoice", "payment", "bill", "charge", "refund", "receipt",
        "overdue", "balance", "account", "subscription", "plan",
    ],
    "spam": [
        "unsubscribe", "opt out", "click here", "limited time",
        "act now", "free", "winner", "congratulations", "lottery",
    ],
}


@dataclass(frozen=True)
class SkillPackResult:
    """Result from an Eli Inbox skill pack method."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False


@dataclass(frozen=True)
class EliInboxContext:
    """Required context for all Eli Inbox operations."""

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
    ctx: EliInboxContext,
    event_type: str,
    status: str,
    risk_tier: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    approval_required: bool = False,
) -> dict[str, Any]:
    """Build a receipt for an Eli Inbox operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_ELI,
        "correlation_id": ctx.correlation_id,
        "status": status,
        "risk_tier": risk_tier,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": "allow",
            "policy_id": "eli-inbox-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    if approval_required:
        receipt["approval_required"] = True
    return receipt


def _classify_email(subject: str, body: str) -> str:
    """Classify email by keyword matching (GREEN tier — no LLM).

    Returns one of: support, sales, billing, spam, personal, unknown.
    """
    text = f"{subject} {body}".lower()

    scores: dict[str, int] = {}
    for category, keywords in _TRIAGE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[category] = score

    if not scores:
        return "unknown"

    best = max(scores, key=lambda k: scores[k])
    return best


def _redact_email_fields(data: dict[str, Any]) -> dict[str, Any]:
    """DLP-redact email content for receipt storage (Law #9)."""
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("subject", "body", "body_html", "body_text"):
            redacted[key] = "<REDACTED>"
        elif key in ("to", "from", "from_address", "reply_to"):
            redacted[key] = "<EMAIL_REDACTED>"
        else:
            redacted[key] = value
    return redacted


class EliInboxSkillPack:
    async def email_read(
        self,
        filters: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.read_emails(filters=filters, context=context)

    async def email_triage(
        self,
        email_id: str,
        subject: str,
        body: str,
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.triage_email(email_id=email_id, subject=subject, body=body, context=context)

    async def email_draft(
        self,
        email_id: str,
        draft_content: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.draft_response(email_id=email_id, draft_content=draft_content, context=context)

    async def email_send(
        self,
        draft_id: str,
        send_payload: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Compatibility wrapper for registry-aligned action validation."""
        return await self.send_email(draft_id=draft_id, send_payload=send_payload, context=context)

    async def office_read(
        self,
        filters: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        return await self._execute_office_action(
            action="office.read", tool_id="internal.office.read", payload=filters, context=context, risk_tier="green"
        )

    async def office_create(
        self,
        office_payload: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        return await self._execute_office_action(
            action="office.create", tool_id="internal.office.create", payload=office_payload, context=context, risk_tier="yellow", approval_required=True
        )

    async def office_draft(
        self,
        office_payload: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        return await self._execute_office_action(
            action="office.draft", tool_id="internal.office.draft", payload=office_payload, context=context, risk_tier="yellow", approval_required=True
        )

    async def office_send(
        self,
        office_payload: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        return await self._execute_office_action(
            action="office.send", tool_id="internal.office.send", payload=office_payload, context=context, risk_tier="yellow", approval_required=True
        )

    async def _execute_office_action(
        self,
        *,
        action: str,
        tool_id: str,
        payload: dict[str, Any],
        context: EliInboxContext,
        risk_tier: str,
        approval_required: bool = False,
    ) -> SkillPackResult:
        if not payload:
            receipt = _emit_receipt(
                ctx=context,
                event_type=action,
                status="denied",
                risk_tier=risk_tier,
                inputs={"action": action, "payload": {}},
                approval_required=approval_required,
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_PAYLOAD"]
            return SkillPackResult(success=False, receipt=receipt, error="Missing required parameter: payload", approval_required=approval_required)

        result: ToolExecutionResult = await execute_tool(
            tool_id=tool_id,
            payload=payload,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier=risk_tier,
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _emit_receipt(
            ctx=context,
            event_type=action,
            status=status,
            risk_tier=risk_tier,
            inputs={"action": action, "payload": payload},
            metadata={"tool_id": result.tool_id},
            approval_required=approval_required,
        )
        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
            approval_required=approval_required,
        )

    """Eli Inbox skill pack — email read, triage, draft, send."""

    async def read_emails(
        self,
        filters: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Read emails via PolarisM.

        GREEN tier, no approval required.

        Filters may include: folder, unread_only, limit, since.
        """
        if not filters:
            receipt = _emit_receipt(
                ctx=context,
                event_type="email.read",
                status="denied",
                risk_tier="green",
                inputs={"action": "email.read", "filters": {}},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_FILTERS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: filters",
            )

        result: ToolExecutionResult = await execute_tool(
            tool_id="polaris.email.read",
            payload=filters,
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
            event_type="email.read",
            status=status,
            risk_tier="green",
            inputs={"action": "email.read", "filters": filters},
            metadata={
                "tool_id": result.tool_id,
                "email_count": len(result.data.get("emails", [])),
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def triage_email(
        self,
        email_id: str,
        subject: str,
        body: str,
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Classify an email into a triage category.

        GREEN tier, no approval required.
        Uses keyword matching (not LLM) to preserve GREEN tier compliance.

        Categories: support, sales, billing, spam, personal, unknown.
        """
        if not email_id:
            receipt = _emit_receipt(
                ctx=context,
                event_type="email.triage",
                status="denied",
                risk_tier="green",
                inputs={"action": "email.triage", "email_id": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_EMAIL_ID"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: email_id",
            )

        category = _classify_email(subject, body)

        receipt = _emit_receipt(
            ctx=context,
            event_type="email.triage",
            status="ok",
            risk_tier="green",
            inputs={"action": "email.triage", "email_id": email_id},
            metadata={
                "category": category,
                "email_id": email_id,
            },
        )

        return SkillPackResult(
            success=True,
            data={
                "email_id": email_id,
                "category": category,
                "confidence": "keyword_match",
            },
            receipt=receipt,
        )

    async def draft_response(
        self,
        email_id: str,
        draft_content: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Create an email draft response.

        YELLOW tier — requires user approval before sending.

        draft_content:
          - to: str — recipient email address
          - subject: str — email subject
          - body_html: str — HTML body
          - body_text: str — plaintext body
          - from_address: str — sender address
        """
        to = draft_content.get("to", "")
        subject = draft_content.get("subject", "")
        from_address = draft_content.get("from_address", "")

        if not all([email_id, to, subject, from_address]):
            redacted = _redact_email_fields(draft_content)
            receipt = _emit_receipt(
                ctx=context,
                event_type="email.draft",
                status="denied",
                risk_tier="yellow",
                inputs={"action": "email.draft", "email_id": email_id, "draft": redacted},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_FIELDS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required fields: email_id, to, subject, from_address",
                approval_required=True,
            )

        result: ToolExecutionResult = await execute_tool(
            tool_id="polaris.email.draft",
            payload=draft_content,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="yellow",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        redacted = _redact_email_fields(draft_content)
        receipt = _emit_receipt(
            ctx=context,
            event_type="email.draft",
            status=status,
            risk_tier="yellow",
            inputs={"action": "email.draft", "email_id": email_id, "draft": redacted},
            metadata={
                "tool_id": result.tool_id,
                "draft_id": result.data.get("draft_id", ""),
            },
            approval_required=True,
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
            approval_required=True,
        )

    async def send_email(
        self,
        draft_id: str,
        send_payload: dict[str, Any],
        context: EliInboxContext,
    ) -> SkillPackResult:
        """Send an approved email draft.

        YELLOW tier — requires explicit user approval.
        Binding fields: to, subject, from_address (approve-then-swap defense).

        send_payload:
          - to: str — recipient
          - subject: str — subject line
          - body_html: str — HTML body
          - body_text: str — plaintext body
          - from_address: str — sender address
        """
        to = send_payload.get("to", "")
        subject = send_payload.get("subject", "")
        from_address = send_payload.get("from_address", "")

        if not all([draft_id, to, subject, from_address]):
            redacted = _redact_email_fields(send_payload)
            receipt = _emit_receipt(
                ctx=context,
                event_type="email.send",
                status="denied",
                risk_tier="yellow",
                inputs={"action": "email.send", "draft_id": draft_id, "payload": redacted},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_FIELDS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required fields: draft_id, to, subject, from_address",
                approval_required=True,
            )

        result: ToolExecutionResult = await execute_tool(
            tool_id="polaris.email.send",
            payload=send_payload,
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="yellow",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        redacted = _redact_email_fields(send_payload)
        receipt = _emit_receipt(
            ctx=context,
            event_type="email.send",
            status=status,
            risk_tier="yellow",
            inputs={"action": "email.send", "draft_id": draft_id, "payload": redacted},
            metadata={
                "tool_id": result.tool_id,
                "message_id": result.data.get("message_id", ""),
                "binding_fields": {
                    "to": "<EMAIL_REDACTED>",
                    "subject": "<SUBJECT_REDACTED>",
                    "from_address": "<EMAIL_REDACTED>",
                },
            },
            approval_required=True,
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
            approval_required=True,
        )


# =============================================================================
# Phase 3 W4: Enhanced Eli Inbox with LLM reasoning
# =============================================================================

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedEliInbox(AgenticSkillPack):
    """LLM-enhanced Eli Inbox — intelligent triage, draft generation, DLP-aware.

    Voice ID: c6kFzbpMaJ8UMD5P6l72 (ElevenLabs)
    DLP: All email content passes through PII redaction before receipts.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="eli-inbox",
            agent_name="Eli",
            default_risk_tier="yellow",
            memory_enabled=True,
        )
        self._rule_pack = EliInboxSkillPack()

    async def get_greeting(
        self, ctx: AgentContext, *, user_name: str | None = None, time_of_day: str | None = None,
    ) -> str:
        """Eli's greeting — efficient communication personality (7b)."""
        if time_of_day is None:
            from datetime import datetime, timezone
            hour = datetime.now(timezone.utc).hour
            time_of_day = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")

        name_part = f" {user_name}" if user_name else ""
        is_returning = False
        if self._memory_enabled:
            try:
                episodes = await self.recall_episodes(ctx, limit=1)
                is_returning = bool(episodes)
            except Exception:
                pass

        if is_returning:
            return f"Good {time_of_day}{name_part}. Let me check your inbox."
        else:
            return f"Good {time_of_day}{name_part}, I'm Eli — I manage your inbox. I triage, draft, and keep your communications organized."

    async def get_error_message(
        self, missing_fields: list[str] | None = None, error_type: str = "generic",
    ) -> str:
        """Eli's error messages — clear and action-oriented (7c)."""
        if error_type == "missing_fields" and missing_fields:
            fields_str = " and ".join(missing_fields)
            return f"I need the {fields_str} to handle that email. Can you fill {'those' if len(missing_fields) > 1 else 'that'} in?"
        elif error_type == "validation":
            return "That email doesn't look right — check the recipient and subject, then try again."
        elif error_type == "dlp":
            return "I can't send that — it contains sensitive information that needs to be reviewed first."
        else:
            return "I couldn't process that communication. What would you like me to try instead?"

    async def triage_email(self, email_data: dict, ctx: AgentContext) -> AgentResult:
        """Classify and prioritize incoming email. GREEN within YELLOW pack."""
        subject = email_data.get("subject", "")
        sender = email_data.get("from", "")
        if not subject and not sender:
            receipt = self.build_receipt(
                ctx=ctx, event_type="email.triage",
                status="failed", inputs={"subject": "", "from": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_EMAIL_DATA"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing email data")

        return await self.execute_with_llm(
            prompt=(
                f"You are Eli, the inbox specialist. Classify this email.\n\n"
                f"Subject: {subject}\nFrom: {sender}\n"
                f"Body: {email_data.get('body', '')[:1000]}\n\n"
                f"Classify: priority (urgent/high/normal/low), category "
                f"(billing/support/legal/scheduling/marketing/spam), "
                f"action (reply/forward/archive/escalate), "
                f"specialist routing (quinn/clara/nora/none)."
            ),
            ctx=ctx, event_type="email.triage", step_type="classify",
            inputs={"action": "email.triage", "subject": "<SUBJECT_REDACTED>"},
        )

    async def draft_reply(self, email_data: dict, reply_intent: str, ctx: AgentContext) -> AgentResult:
        """Draft a professional reply. YELLOW — requires approval to send."""
        if not reply_intent:
            receipt = self.build_receipt(
                ctx=ctx, event_type="email.draft_reply",
                status="failed", inputs={"reply_intent": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REPLY_INTENT"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing reply_intent")

        return await self.execute_with_llm(
            prompt=(
                f"You are Eli, drafting a reply.\n\n"
                f"Original: {email_data.get('subject', '')}\n"
                f"Body: {email_data.get('body', '')[:1500]}\n"
                f"Intent: {reply_intent}\n\n"
                f"Draft professional reply. DRAFT only — user approves before sending."
            ),
            ctx=ctx, event_type="email.draft_reply", step_type="draft",
            inputs={"action": "email.draft_reply", "subject": "<SUBJECT_REDACTED>"},
        )

    async def extract_action_items(self, email_thread: list[dict], ctx: AgentContext) -> AgentResult:
        """Extract actionable items from email thread. GREEN — analysis only."""
        if not email_thread:
            receipt = self.build_receipt(
                ctx=ctx, event_type="email.extract_actions",
                status="failed", inputs={"thread_length": 0},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["EMPTY_THREAD"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty thread")

        thread_text = "\n---\n".join([
            f"From: {m.get('from', '?')}\n{m.get('body', '')[:500]}"
            for m in email_thread[:10]
        ])

        return await self.execute_with_llm(
            prompt=(
                f"You are Eli, analyzing an email thread.\n\n"
                f"Thread ({len(email_thread)} messages):\n{thread_text[:3000]}\n\n"
                f"Extract: action items, decisions, outstanding questions, follow-up needed."
            ),
            ctx=ctx, event_type="email.extract_actions", step_type="extract",
            inputs={"action": "email.extract_actions", "thread_length": len(email_thread)},
        )
