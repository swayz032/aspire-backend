"""Council Service — Meeting of Minds multi-model triage (Law #1: orchestrator decides).

Phase 2.5 scope: In-memory structure only (no actual multi-model API calls).
Phase 3: Wire to actual GPT/Gemini/Claude API calls.

Flow:
  1. Robots failure / incident → spawn_council(incident_id, evidence_pack)
  2. Each advisor submits structured triage proposal
  3. Ava (orchestrator) adjudicates → picks best proposal
  4. All steps produce receipts (Law #2)

Council members (Phase 3):
  - GPT-5.2: architecture critic
  - Gemini: research cross-check
  - Claude Opus 4.6: implementation plan

Law compliance:
  - Law #1: Ava adjudicates (Single Brain)
  - Law #2: All council actions produce receipts
  - Law #7: Council members advise, never execute
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)


class CouncilTrigger(str, Enum):
    """What triggered the council session."""

    INCIDENT = "incident"
    ROBOT_FAILURE = "robot_failure"
    PRODUCTION_ALERT = "production_alert"
    MANUAL = "manual"


class ProposalStatus(str, Enum):
    """Status of a council proposal."""

    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class CouncilProposal:
    """A structured triage proposal from a council member."""

    proposal_id: str
    session_id: str
    member: str
    root_cause: str
    fix_plan: str
    tests: list[str]
    risk_tier: str
    evidence_links: list[str]
    confidence: float  # 0.0 - 1.0
    submitted_at: str
    status: ProposalStatus = ProposalStatus.SUBMITTED


@dataclass
class CouncilSession:
    """A Meeting of Minds council session."""

    session_id: str
    trigger: CouncilTrigger
    incident_id: str
    evidence_pack: dict[str, Any]
    members: list[str]
    proposals: list[CouncilProposal] = field(default_factory=list)
    decision: dict[str, Any] | None = None
    created_at: str = ""
    decided_at: str | None = None
    status: str = "open"


# In-memory council sessions (Phase 2.5)
_sessions: dict[str, CouncilSession] = {}


def spawn_council(
    *,
    incident_id: str,
    trigger: CouncilTrigger = CouncilTrigger.INCIDENT,
    evidence_pack: dict[str, Any] | None = None,
    members: list[str] | None = None,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[CouncilSession, dict[str, Any]]:
    """Create a new council session for triage.

    Returns (session, receipt).
    """
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    session = CouncilSession(
        session_id=session_id,
        trigger=trigger,
        incident_id=incident_id,
        evidence_pack=evidence_pack or {},
        members=members or ["gpt", "gemini", "claude"],
        created_at=now,
    )
    _sessions[session_id] = session

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": incident_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "council_service",
        "action_type": "council.session.created",
        "risk_tier": "green",
        "tool_used": "orchestrator.council",
        "created_at": now,
        "outcome": "success",
        "reason_code": "council_spawned",
        "receipt_type": "council.session.created",
        "receipt_hash": "",
        "details": {
            "session_id": session_id,
            "trigger": trigger.value,
            "incident_id": incident_id,
            "members": session.members,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Council session created: id=%s, trigger=%s, incident=%s, members=%d",
        session_id[:8], trigger.value, incident_id[:8], len(session.members),
    )

    return session, receipt


def submit_proposal(
    *,
    session_id: str,
    member: str,
    root_cause: str,
    fix_plan: str,
    tests: list[str] | None = None,
    risk_tier: str = "green",
    evidence_links: list[str] | None = None,
    confidence: float = 0.5,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[CouncilProposal, dict[str, Any]]:
    """Submit a structured triage proposal from a council member.

    Returns (proposal, receipt).
    """
    session = _sessions.get(session_id)
    if session is None:
        raise ValueError(f"Council session not found: {session_id}")

    if session.status == "decided":
        raise ValueError(
            f"Council session {session_id} already decided — "
            "proposals rejected after adjudication"
        )

    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    proposal = CouncilProposal(
        proposal_id=proposal_id,
        session_id=session_id,
        member=member,
        root_cause=root_cause,
        fix_plan=fix_plan,
        tests=tests or [],
        risk_tier=risk_tier,
        evidence_links=evidence_links or [],
        confidence=confidence,
        submitted_at=now,
    )
    session.proposals.append(proposal)

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": session.incident_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "agent",
        "actor_id": f"council.{member}",
        "action_type": "council.member.proposal",
        "risk_tier": "green",
        "tool_used": "orchestrator.council",
        "created_at": now,
        "outcome": "success",
        "reason_code": "proposal_submitted",
        "receipt_type": "council.member.proposal",
        "receipt_hash": "",
        "details": {
            "session_id": session_id,
            "proposal_id": proposal_id,
            "member": member,
            "confidence": confidence,
            "risk_tier": risk_tier,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Council proposal submitted: session=%s, member=%s, confidence=%.2f",
        session_id[:8], member, confidence,
    )

    return proposal, receipt


def adjudicate(
    *,
    session_id: str,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ava adjudicates — picks the best proposal (Law #1: Single Brain).

    Phase 2.5: Simple highest-confidence selection.
    Phase 3: LLM-powered adjudication with reasoning.

    Returns (decision, receipt).
    """
    session = _sessions.get(session_id)
    if session is None:
        raise ValueError(f"Council session not found: {session_id}")

    if not session.proposals:
        raise ValueError(f"No proposals to adjudicate in session: {session_id}")

    now = datetime.now(timezone.utc).isoformat()

    # Phase 2.5: Pick highest confidence proposal
    best = max(session.proposals, key=lambda p: p.confidence)
    best.status = ProposalStatus.ACCEPTED

    # Mark others as rejected
    for p in session.proposals:
        if p.proposal_id != best.proposal_id:
            p.status = ProposalStatus.REJECTED

    decision = {
        "selected_proposal_id": best.proposal_id,
        "selected_member": best.member,
        "root_cause": best.root_cause,
        "fix_plan": best.fix_plan,
        "tests": best.tests,
        "risk_tier": best.risk_tier,
        "confidence": best.confidence,
        "total_proposals": len(session.proposals),
        "adjudication_method": "highest_confidence",
    }

    session.decision = decision
    session.decided_at = now
    session.status = "decided"

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": session.incident_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "ava.adjudicator",
        "action_type": "council.decision",
        "risk_tier": "green",
        "tool_used": "orchestrator.council",
        "created_at": now,
        "outcome": "success",
        "reason_code": "triage_decision",
        "receipt_type": "council.decision",
        "receipt_hash": "",
        "details": decision,
    }

    store_receipts([receipt])

    logger.info(
        "Council adjudication: session=%s, winner=%s (confidence=%.2f, %d proposals)",
        session_id[:8], best.member, best.confidence, len(session.proposals),
    )

    return decision, receipt


def get_session(session_id: str) -> CouncilSession | None:
    """Get a council session by ID."""
    return _sessions.get(session_id)


def list_sessions(status: str | None = None) -> list[CouncilSession]:
    """List council sessions, optionally filtered by status."""
    sessions = list(_sessions.values())
    if status:
        sessions = [s for s in sessions if s.status == status]
    return sessions


def clear_sessions() -> None:
    """Clear all sessions. Testing only."""
    _sessions.clear()
