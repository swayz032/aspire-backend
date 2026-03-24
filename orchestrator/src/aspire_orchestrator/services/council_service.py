"""Council Service — Meeting of Minds multi-model triage (Law #1: orchestrator decides).

Production version: Supabase persistence + real multi-model API calls.

Flow:
  1. Incident -> spawn_council() -> insert council_sessions row
  2. run_council() -> query 3 advisors concurrently -> insert proposals -> adjudicate
  3. adjudicate() -> LLM-powered reasoning across proposals -> update session
  4. All steps produce receipts (Law #2)

Backward compatibility:
  The sync functions (spawn_council, submit_proposal, adjudicate, get_session,
  list_sessions, clear_sessions) are preserved for existing tests that use the
  in-memory store. New code uses the async variants: run_council(),
  async get_session_async(), async list_sessions_async().
"""

from __future__ import annotations

import asyncio
import json
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


# =========================================================================
# In-memory store (backward compat for sync callers / tests)
# =========================================================================

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
    """Create a new council session (sync, in-memory — backward compat)."""
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
    """Submit a structured triage proposal (sync, in-memory — backward compat)."""
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
    """Ava adjudicates — picks the best proposal (sync, in-memory — backward compat)."""
    session = _sessions.get(session_id)
    if session is None:
        raise ValueError(f"Council session not found: {session_id}")

    if not session.proposals:
        raise ValueError(f"No proposals to adjudicate in session: {session_id}")

    now = datetime.now(timezone.utc).isoformat()

    best = max(session.proposals, key=lambda p: p.confidence)
    best.status = ProposalStatus.ACCEPTED

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
    """Get a council session by ID (sync, in-memory — backward compat)."""
    return _sessions.get(session_id)


def list_sessions(status: str | None = None) -> list[CouncilSession]:
    """List council sessions (sync, in-memory — backward compat)."""
    sessions = list(_sessions.values())
    if status:
        sessions = [s for s in sessions if s.status == status]
    return sessions


def clear_sessions() -> None:
    """Clear all sessions. Testing only."""
    _sessions.clear()


# =========================================================================
# Async / Supabase-backed functions (production path)
# =========================================================================


async def spawn_council_async(
    *,
    incident_id: str,
    trigger: CouncilTrigger = CouncilTrigger.INCIDENT,
    evidence_pack: dict[str, Any] | None = None,
    members: list[str] | None = None,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[CouncilSession, dict[str, Any]]:
    """Create a new council session — persists to Supabase."""
    from aspire_orchestrator.services.supabase_client import supabase_insert

    now = datetime.now(timezone.utc).isoformat()
    member_list = members or ["gpt", "gemini", "claude"]

    row = await supabase_insert("council_sessions", {
        "incident_id": incident_id,
        "trigger": trigger.value if isinstance(trigger, CouncilTrigger) else trigger,
        "evidence_pack": evidence_pack or {},
        "members": member_list,
        "status": "open",
        "created_by": "ava_admin",
    })

    session_id = str(row.get("id", uuid.uuid4()))

    session = CouncilSession(
        session_id=session_id,
        trigger=trigger,
        incident_id=incident_id,
        evidence_pack=evidence_pack or {},
        members=member_list,
        created_at=row.get("created_at", now),
    )

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
            "trigger": trigger.value if isinstance(trigger, CouncilTrigger) else trigger,
            "incident_id": incident_id,
            "members": member_list,
        },
    }
    store_receipts([receipt])

    logger.info(
        "Council session created: id=%s, trigger=%s, incident=%s",
        session_id[:8], trigger.value if isinstance(trigger, CouncilTrigger) else trigger, incident_id[:8],
    )

    return session, receipt


async def _insert_proposal(session_id: str, advisor_result: dict[str, Any]) -> dict[str, Any]:
    """Insert a proposal into Supabase council_proposals table."""
    from aspire_orchestrator.services.supabase_client import supabase_insert

    row = await supabase_insert("council_proposals", {
        "session_id": session_id,
        "member": advisor_result["advisor"],
        "root_cause": advisor_result.get("root_cause", ""),
        "fix_plan": advisor_result.get("fix_plan", ""),
        "tests": advisor_result.get("tests", []),
        "risk_tier": advisor_result.get("risk_tier", "yellow"),
        "confidence": advisor_result.get("confidence", 0.0),
        "raw_response": advisor_result,
        "model_used": advisor_result.get("model_used", ""),
        "tokens_used": advisor_result.get("tokens_used", 0),
        "latency_ms": advisor_result.get("latency_ms", 0),
    })
    return row


async def _adjudicate_with_llm(proposals: list[dict[str, Any]], incident_id: str) -> dict[str, Any]:
    """Use GPT-5.2 to reason across proposals and pick the best one."""
    from aspire_orchestrator.services.council_advisors import _call_openai

    prompt = (
        f"You are Ava, the adjudicator for incident {incident_id}.\n\n"
        "You have received these council proposals:\n\n"
    )
    for i, p in enumerate(proposals, 1):
        prompt += (
            f"--- Proposal {i} (from {p.get('advisor', 'unknown')}, confidence={p.get('confidence', 0)}) ---\n"
            f"Root cause: {p.get('root_cause', 'N/A')}\n"
            f"Fix plan: {p.get('fix_plan', 'N/A')}\n"
            f"Risk tier: {p.get('risk_tier', 'N/A')}\n"
            f"Reasoning: {p.get('reasoning', 'N/A')}\n\n"
        )

    prompt += (
        "Select the best proposal. Respond with ONLY a JSON object:\n"
        '{"selected_index": 1, "selected_member": "gpt", "reasoning": "...", "confidence": 0.9}'
    )

    try:
        result = await _call_openai(prompt, model="gpt-5.2")
        idx = int(result.get("selected_index", 1)) - 1
        idx = max(0, min(idx, len(proposals) - 1))
        selected = proposals[idx]
        return {
            "selected_proposal_id": selected.get("proposal_id", ""),
            "selected_member": selected.get("advisor", result.get("selected_member", "")),
            "root_cause": selected.get("root_cause", ""),
            "fix_plan": selected.get("fix_plan", ""),
            "tests": selected.get("tests", []),
            "risk_tier": selected.get("risk_tier", "yellow"),
            "confidence": float(result.get("confidence", selected.get("confidence", 0.5))),
            "total_proposals": len(proposals),
            "adjudication_method": "llm_reasoning",
            "adjudication_reasoning": result.get("reasoning", ""),
        }
    except Exception as e:
        logger.warning("LLM adjudication failed, falling back to highest confidence: %s", e)
        best = max(proposals, key=lambda p: p.get("confidence", 0))
        return {
            "selected_proposal_id": best.get("proposal_id", ""),
            "selected_member": best.get("advisor", ""),
            "root_cause": best.get("root_cause", ""),
            "fix_plan": best.get("fix_plan", ""),
            "tests": best.get("tests", []),
            "risk_tier": best.get("risk_tier", "yellow"),
            "confidence": float(best.get("confidence", 0.5)),
            "total_proposals": len(proposals),
            "adjudication_method": "highest_confidence_fallback",
            "adjudication_reasoning": f"LLM adjudication failed: {e}",
        }


async def run_council(
    *,
    incident_id: str,
    evidence_pack: dict[str, Any] | None = None,
    suite_id: str = "system",
    office_id: str = "system",
) -> dict[str, Any]:
    """Full council flow: spawn -> 3 advisors -> adjudicate -> return result."""
    from aspire_orchestrator.services.council_advisors import query_advisor
    from aspire_orchestrator.services.supabase_client import supabase_update

    # 1. Spawn session
    session, _ = await spawn_council_async(
        incident_id=incident_id,
        evidence_pack=evidence_pack,
        suite_id=suite_id,
        office_id=office_id,
    )

    # 2. Update status to collecting
    try:
        await supabase_update(
            "council_sessions",
            f"id=eq.{session.session_id}",
            {"status": "collecting"},
        )
    except Exception as e:
        logger.warning("Failed to update session status: %s", e)

    # 3. Query all 3 advisors concurrently
    advisor_tasks = [
        query_advisor(advisor=member, evidence_pack=evidence_pack or {}, incident_id=incident_id)
        for member in session.members
    ]
    advisor_results = await asyncio.gather(*advisor_tasks, return_exceptions=True)

    # 4. Process results and insert proposals
    proposals: list[dict[str, Any]] = []
    for result in advisor_results:
        if isinstance(result, Exception):
            logger.error("Advisor task failed: %s", result)
            continue
        try:
            await _insert_proposal(session.session_id, result)
        except Exception as e:
            logger.warning("Failed to persist proposal: %s", e)
        proposals.append(result)

    if not proposals:
        try:
            await supabase_update(
                "council_sessions",
                f"id=eq.{session.session_id}",
                {"status": "error"},
            )
        except Exception:
            pass
        return {
            "session_id": session.session_id,
            "status": "error",
            "proposals": [],
            "decision": None,
            "error": "All advisors failed to respond",
        }

    # 5. Update status to deliberating
    try:
        await supabase_update(
            "council_sessions",
            f"id=eq.{session.session_id}",
            {"status": "deliberating"},
        )
    except Exception as e:
        logger.warning("Failed to update session status: %s", e)

    # 6. Adjudicate
    decision = await _adjudicate_with_llm(proposals, incident_id)
    now = datetime.now(timezone.utc).isoformat()

    # 7. Update session with decision
    try:
        await supabase_update(
            "council_sessions",
            f"id=eq.{session.session_id}",
            {"status": "decided", "decision": json.dumps(decision), "decided_at": now},
        )
    except Exception as e:
        logger.warning("Failed to persist decision: %s", e)

    # 8. Emit decision receipt
    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": incident_id,
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

    return {
        "session_id": session.session_id,
        "status": "decided",
        "proposals": proposals,
        "decision": decision,
    }


async def get_session_async(session_id: str) -> CouncilSession | None:
    """Get a council session by ID from Supabase."""
    from aspire_orchestrator.services.supabase_client import supabase_select

    try:
        rows = await supabase_select("council_sessions", {"id": f"eq.{session_id}"}, limit=1)
        if not rows:
            return None
        row = rows[0]
        return CouncilSession(
            session_id=str(row["id"]),
            trigger=CouncilTrigger(row.get("trigger", "manual")),
            incident_id=row["incident_id"],
            evidence_pack=row.get("evidence_pack", {}),
            members=row.get("members", []),
            decision=row.get("decision"),
            created_at=row.get("created_at", ""),
            decided_at=row.get("decided_at"),
            status=row.get("status", "open"),
        )
    except Exception as e:
        logger.error("Failed to get council session: %s", e)
        return None


async def list_sessions_async(status: str | None = None) -> list[CouncilSession]:
    """List council sessions from Supabase, optionally filtered by status."""
    from aspire_orchestrator.services.supabase_client import supabase_select

    try:
        filters: dict[str, str] = {}
        if status:
            filters["status"] = f"eq.{status}"
        rows = await supabase_select("council_sessions", filters, order_by="created_at.desc", limit=50)
        return [
            CouncilSession(
                session_id=str(row["id"]),
                trigger=CouncilTrigger(row.get("trigger", "manual")),
                incident_id=row["incident_id"],
                evidence_pack=row.get("evidence_pack", {}),
                members=row.get("members", []),
                decision=row.get("decision"),
                created_at=row.get("created_at", ""),
                decided_at=row.get("decided_at"),
                status=row.get("status", "open"),
            )
            for row in rows
        ]
    except Exception as e:
        logger.error("Failed to list council sessions: %s", e)
        return []
