"""Nora Conference Skill Pack — Meeting rooms, scheduling, and post-meeting summaries.

Nora is the Conference desk. She handles:
  - Room creation (GREEN — non-destructive, auto-approved)
  - Meeting scheduling (YELLOW — requires user confirmation)
  - Meeting summarization (GREEN — read-only transcription + AI summary)

Providers:
  - LiveKit: Room creation and management
  - Deepgram: Audio transcription (Nova-3)
  - ElevenLabs: Text-to-speech (for voice summaries, future)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters or tool errors
  - Law #4: GREEN (create_room, summarize_meeting), YELLOW (schedule_meeting)
  - Law #7: Uses tool_executor for all provider calls (tools are hands)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, ReceiptType, RiskTier
from aspire_orchestrator.services.tool_executor import execute_tool

logger = logging.getLogger(__name__)

ACTOR_NORA = "skillpack:nora-conference"


@dataclass
class SkillPackResult:
    """Result of a skill pack operation."""

    success: bool
    data: dict[str, Any]
    receipt: dict[str, Any]
    error: str | None = None
    approval_required: bool = False


@dataclass
class NoraContext:
    """Tenant-scoped execution context for Nora operations."""

    suite_id: str
    office_id: str
    correlation_id: str


def _make_receipt(
    *,
    ctx: NoraContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Nora conference operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": "1.0",
        "receipt_id": f"rcpt-nora-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_NORA,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": f"sha256:{uuid.uuid4().hex}",
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "nora-conference-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "redactions": [],
    }
    if tool_used:
        receipt["tool_used"] = tool_used
    if metadata:
        receipt["metadata"] = metadata
    return receipt


class NoraConferenceSkillPack:
    """Nora Conference Skill Pack — governed meeting operations.

    All methods require a NoraContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).
    """

    async def create_room(
        self,
        room_name: str,
        settings: dict[str, Any] | None,
        context: NoraContext,
    ) -> SkillPackResult:
        """Create a conference room via LiveKit (GREEN — auto-approved).

        Args:
            room_name: Name for the conference room
            settings: Optional room settings (empty_timeout, max_participants)
            context: Tenant-scoped execution context

        Returns:
            SkillPackResult with room details and receipt
        """
        if not room_name:
            receipt = _make_receipt(
                ctx=context,
                action_type="meeting.create_room",
                risk_tier="green",
                outcome="failed",
                reason_code="MISSING_ROOM_NAME",
                tool_used="livekit.room.create",
            )
            return SkillPackResult(
                success=False,
                data={},
                receipt=receipt,
                error="Missing required parameter: room_name",
            )

        room_settings = settings or {}
        payload = {
            "name": room_name,
            "empty_timeout": room_settings.get("empty_timeout", 300),
            "max_participants": room_settings.get("max_participants", 20),
        }

        # Law #7: Use tool_executor for the actual LiveKit call
        try:
            result = await execute_tool(
                tool_id="livekit.room.create",
                payload=payload,
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
                risk_tier="green",
            )

            if result.outcome == Outcome.SUCCESS:
                receipt = _make_receipt(
                    ctx=context,
                    action_type="meeting.create_room",
                    risk_tier="green",
                    outcome="success",
                    reason_code="EXECUTED",
                    tool_used="livekit.room.create",
                    metadata={"room_name": room_name},
                )
                return SkillPackResult(
                    success=True,
                    data=result.data,
                    receipt=receipt,
                )
            else:
                receipt = _make_receipt(
                    ctx=context,
                    action_type="meeting.create_room",
                    risk_tier="green",
                    outcome="failed",
                    reason_code=result.error or "TOOL_EXECUTION_FAILED",
                    tool_used="livekit.room.create",
                )
                return SkillPackResult(
                    success=False,
                    data={},
                    receipt=receipt,
                    error=result.error,
                )
        except Exception as exc:
            logger.error("create_room failed: %s", exc)
            receipt = _make_receipt(
                ctx=context,
                action_type="meeting.create_room",
                risk_tier="green",
                outcome="failed",
                reason_code="INTERNAL_ERROR",
                tool_used="livekit.room.create",
            )
            return SkillPackResult(
                success=False,
                data={},
                receipt=receipt,
                error=str(exc),
            )

    async def schedule_meeting(
        self,
        participants: list[str],
        time: str,
        agenda: str,
        context: NoraContext,
    ) -> SkillPackResult:
        """Schedule a meeting (YELLOW — requires user approval).

        This is a YELLOW-tier action because it involves external communication
        with participants and creates calendar commitments.

        Args:
            participants: List of participant identifiers (email or user ID)
            time: ISO 8601 datetime for the meeting start
            agenda: Meeting agenda/description
            context: Tenant-scoped execution context

        Returns:
            SkillPackResult with approval_required=True
        """
        if not participants:
            receipt = _make_receipt(
                ctx=context,
                action_type="meeting.schedule",
                risk_tier="yellow",
                outcome="failed",
                reason_code="MISSING_PARTICIPANTS",
                tool_used="livekit.meeting.schedule",
            )
            return SkillPackResult(
                success=False,
                data={},
                receipt=receipt,
                error="Missing required parameter: participants",
            )

        if not time:
            receipt = _make_receipt(
                ctx=context,
                action_type="meeting.schedule",
                risk_tier="yellow",
                outcome="failed",
                reason_code="MISSING_TIME",
                tool_used="livekit.meeting.schedule",
            )
            return SkillPackResult(
                success=False,
                data={},
                receipt=receipt,
                error="Missing required parameter: time",
            )

        # YELLOW tier: Return plan with approval_required=True
        # The orchestrator (Law #1) decides whether to proceed after user approval
        meeting_plan = {
            "participants": participants,
            "time": time,
            "agenda": agenda,
            "risk_tier": "yellow",
            "room_name": f"meeting-{uuid.uuid4().hex[:8]}",
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="meeting.schedule",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="livekit.meeting.schedule",
            metadata={
                "participant_count": len(participants),
                "scheduled_time": time,
            },
        )

        return SkillPackResult(
            success=True,
            data=meeting_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def summarize_meeting(
        self,
        room_id: str,
        context: NoraContext,
    ) -> SkillPackResult:
        """Summarize a meeting from its transcript (GREEN — auto-approved).

        Uses Deepgram to transcribe the meeting audio, then formats
        a structured summary. This is a read-only operation.

        Args:
            room_id: LiveKit room SID or recording URL
            context: Tenant-scoped execution context

        Returns:
            SkillPackResult with transcript and summary
        """
        if not room_id:
            receipt = _make_receipt(
                ctx=context,
                action_type="meeting.summarize",
                risk_tier="green",
                outcome="failed",
                reason_code="MISSING_ROOM_ID",
                tool_used="deepgram.transcribe",
            )
            return SkillPackResult(
                success=False,
                data={},
                receipt=receipt,
                error="Missing required parameter: room_id",
            )

        # Law #7: Use tool_executor for the Deepgram transcription
        try:
            result = await execute_tool(
                tool_id="deepgram.transcribe",
                payload={"audio_url": room_id},
                correlation_id=context.correlation_id,
                suite_id=context.suite_id,
                office_id=context.office_id,
                risk_tier="green",
            )

            if result.outcome == Outcome.SUCCESS:
                transcript = result.data.get("transcript", "")
                confidence = result.data.get("confidence", 0.0)
                duration = result.data.get("duration", 0.0)

                # Format structured summary from transcript
                summary = _format_meeting_summary(
                    transcript=transcript,
                    duration=duration,
                    room_id=room_id,
                )

                receipt = _make_receipt(
                    ctx=context,
                    action_type="meeting.summarize",
                    risk_tier="green",
                    outcome="success",
                    reason_code="EXECUTED",
                    tool_used="deepgram.transcribe",
                    metadata={
                        "room_id": room_id,
                        "transcript_length": len(transcript),
                        "confidence": confidence,
                        "duration": duration,
                    },
                )
                return SkillPackResult(
                    success=True,
                    data={
                        "transcript": transcript,
                        "summary": summary,
                        "confidence": confidence,
                        "duration": duration,
                    },
                    receipt=receipt,
                )
            else:
                receipt = _make_receipt(
                    ctx=context,
                    action_type="meeting.summarize",
                    risk_tier="green",
                    outcome="failed",
                    reason_code=result.error or "TRANSCRIPTION_FAILED",
                    tool_used="deepgram.transcribe",
                )
                return SkillPackResult(
                    success=False,
                    data={},
                    receipt=receipt,
                    error=result.error,
                )
        except Exception as exc:
            logger.error("summarize_meeting failed: %s", exc)
            receipt = _make_receipt(
                ctx=context,
                action_type="meeting.summarize",
                risk_tier="green",
                outcome="failed",
                reason_code="INTERNAL_ERROR",
                tool_used="deepgram.transcribe",
            )
            return SkillPackResult(
                success=False,
                data={},
                receipt=receipt,
                error=str(exc),
            )


def _format_meeting_summary(
    *,
    transcript: str,
    duration: float,
    room_id: str,
) -> dict[str, Any]:
    """Format a structured meeting summary from transcript text.

    In production, this would use an LLM to extract key points,
    action items, and decisions. For now, returns a structured format
    with the raw transcript and basic metadata.
    """
    word_count = len(transcript.split()) if transcript else 0
    duration_minutes = round(duration / 60, 1) if duration else 0.0

    return {
        "room_id": room_id,
        "duration_minutes": duration_minutes,
        "word_count": word_count,
        "transcript_preview": transcript[:500] if transcript else "",
        "key_points": [],  # Phase 3: LLM extraction via EnhancedNora
        "action_items": [],  # Phase 3: LLM extraction via EnhancedNora
        "decisions": [],  # Phase 3: LLM extraction via EnhancedNora
    }


# =============================================================================
# Phase 3 W3: Enhanced Nora Conference with LLM reasoning
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


# Risk trigger keywords — Nora detects these during transcription and
# routes to the appropriate specialist agent.
RISK_TRIGGER_KEYWORDS: dict[str, dict[str, Any]] = {
    "money_movement": {
        "keywords": ["payment", "transfer", "wire", "pay", "invoice", "bill", "refund", "deposit"],
        "specialist": "quinn",
        "risk_tier": "yellow",
        "escalation": "Ava routes to Quinn for financial operations",
    },
    "contracts": {
        "keywords": ["contract", "agreement", "sign", "signature", "nda", "terms", "clause", "binding"],
        "specialist": "clara",
        "risk_tier": "red",
        "escalation": "Ava routes to Clara for legal operations",
    },
    "payroll": {
        "keywords": ["payroll", "salary", "wage", "compensation", "bonus", "w2", "1099", "gusto"],
        "specialist": "milo",
        "risk_tier": "red",
        "escalation": "Ava routes to Milo for payroll operations",
    },
    "email_followup": {
        "keywords": ["email", "follow up", "send message", "notify", "respond", "reply"],
        "specialist": "eli",
        "risk_tier": "yellow",
        "escalation": "Ava routes to Eli for email operations",
    },
    "scheduling": {
        "keywords": ["schedule", "calendar", "appointment", "book", "reschedule", "cancel meeting"],
        "specialist": "nora",
        "risk_tier": "yellow",
        "escalation": "Nora handles scheduling internally",
    },
}


class EnhancedNoraConference(EnhancedSkillPack):
    """LLM-enhanced Nora Conference — risk detection, smart summaries, specialist routing.

    Extends NoraConferenceSkillPack with:
    - detect_risk_triggers: LLM analyzes transcript for risk-tier keywords
    - smart_summarize: LLM extracts key points, action items, decisions
    - route_to_specialist: Determines which agent should handle detected triggers

    Voice ID: 6aDn1KB0hjpdcocrUkmq (ElevenLabs)
    STT Model: Deepgram Nova-3
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="nora-conference",
            agent_name="Nora Conference",
            default_risk_tier="green",
        )
        self._rule_pack = NoraConferenceSkillPack()

    async def detect_risk_triggers(
        self,
        transcript: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Analyze transcript for risk-tier trigger keywords using LLM.

        Uses fast_general (GPT-5) to:
        1. Scan transcript for risk trigger categories
        2. Classify severity (informational, actionable, urgent)
        3. Recommend specialist routing (Quinn, Clara, Milo, Eli)
        4. Extract exact quotes that triggered the detection

        GREEN tier — analysis only, no external actions.
        """
        if not transcript or not transcript.strip():
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="meeting.risk_detect",
                status="failed",
                inputs={"transcript_length": 0},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["EMPTY_TRANSCRIPT"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty transcript")

        # First pass: rule-based keyword scan (fast, no LLM cost)
        rule_triggers = []
        lower_transcript = transcript.lower()
        for category, config in RISK_TRIGGER_KEYWORDS.items():
            for keyword in config["keywords"]:
                if keyword in lower_transcript:
                    rule_triggers.append({
                        "category": category,
                        "keyword": keyword,
                        "specialist": config["specialist"],
                        "risk_tier": config["risk_tier"],
                    })
                    break  # One match per category is enough

        # Second pass: LLM analysis for nuanced detection
        return await self.execute_with_llm(
            prompt=(
                f"You are Nora, the conference specialist. Analyze this meeting transcript\n"
                f"for risk triggers that require specialist attention.\n\n"
                f"Transcript (first 2000 chars):\n{transcript[:2000]}\n\n"
                f"Rule-based triggers found: {rule_triggers}\n\n"
                f"Classify each trigger:\n"
                f"1. Severity: informational / actionable / urgent\n"
                f"2. Specialist: quinn (invoicing), clara (legal), milo (payroll), eli (email)\n"
                f"3. Exact quote from transcript\n"
                f"4. Recommended next action\n\n"
                f"Also identify any triggers the rule-based scan missed."
            ),
            ctx=ctx,
            event_type="meeting.risk_detect",
            step_type="classify",
            inputs={
                "action": "meeting.risk_detect",
                "transcript_length": len(transcript),
                "rule_trigger_count": len(rule_triggers),
            },
        )

    async def smart_summarize(
        self,
        transcript: str,
        room_id: str,
        ctx: AgentContext,
    ) -> AgentResult:
        """Generate an intelligent meeting summary using LLM reasoning.

        Uses primary_reasoner (GPT-5.2) to:
        1. Extract key discussion points with timestamps
        2. Identify action items with assignees
        3. Capture decisions and their rationale
        4. Flag unresolved items for follow-up
        5. Generate a 2-paragraph executive summary

        GREEN tier — read-only analysis of existing transcript.
        """
        if not transcript or not transcript.strip():
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="meeting.smart_summarize",
                status="failed",
                inputs={"room_id": room_id, "transcript_length": 0},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["EMPTY_TRANSCRIPT"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty transcript")

        word_count = len(transcript.split())
        duration_estimate = round(word_count / 150, 1)  # ~150 words/minute

        return await self.execute_with_llm(
            prompt=(
                f"You are Nora, the conference specialist. Create a comprehensive\n"
                f"meeting summary from this transcript.\n\n"
                f"Room: {room_id}\n"
                f"Estimated duration: {duration_estimate} minutes\n"
                f"Transcript:\n{transcript[:4000]}\n\n"
                f"Produce a structured summary:\n"
                f"1. Executive summary (2 paragraphs max)\n"
                f"2. Key discussion points (bulleted)\n"
                f"3. Action items (owner, description, due date if mentioned)\n"
                f"4. Decisions made (decision, rationale)\n"
                f"5. Unresolved items requiring follow-up\n"
                f"6. Risk triggers detected (see Nora routing policy)"
            ),
            ctx=ctx,
            event_type="meeting.smart_summarize",
            step_type="summarize",
            inputs={
                "action": "meeting.smart_summarize",
                "room_id": room_id,
                "transcript_length": len(transcript),
                "word_count": word_count,
            },
        )

    async def route_to_specialist(
        self,
        trigger: dict[str, Any],
        meeting_context: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult:
        """Determine specialist routing for a detected risk trigger.

        Uses cheap_classifier (GPT-5-mini) to validate the trigger and
        generate a structured routing recommendation for Ava.

        GREEN tier — produces routing recommendation, Ava decides.
        """
        category = trigger.get("category", "")
        specialist = trigger.get("specialist", "")

        if not category or not specialist:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="meeting.route_specialist",
                status="failed",
                inputs={"category": category, "specialist": specialist},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["INCOMPLETE_TRIGGER"]
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False, receipt=receipt, error="Trigger missing category or specialist",
            )

        return await self.execute_with_llm(
            prompt=(
                f"You are Nora, recommending specialist routing to Ava.\n\n"
                f"Trigger category: {category}\n"
                f"Recommended specialist: {specialist}\n"
                f"Trigger details: {trigger}\n"
                f"Meeting context: {meeting_context}\n\n"
                f"Generate a structured routing recommendation:\n"
                f"1. Confirm or override specialist selection\n"
                f"2. Urgency level (low, medium, high)\n"
                f"3. Context packet for the specialist (what they need to know)\n"
                f"4. Suggested action for the specialist"
            ),
            ctx=ctx,
            event_type="meeting.route_specialist",
            step_type="classify",
            inputs={
                "action": "meeting.route_specialist",
                "category": category,
                "specialist": specialist,
            },
        )
