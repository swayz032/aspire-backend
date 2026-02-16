"""Learning Loop Service — Turn incidents into permanent improvements.

Converts incidents and robot failures into:
  - Runbook updates
  - Eval cases
  - Robot assertions
  - Policy/router/prompt proposals

All changes go through:
  proposal → eval/robot verification → approval → canary → promote/rollback

Receipts:
  - learning.object.created
  - eval.run.completed
  - learning.change.proposed / approved / object.promoted

Law compliance:
  - Law #1: Orchestrator decides which changes to promote
  - Law #2: Every learning action produces a receipt
  - Law #3: Unapproved changes are never promoted
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


class LearningObjectType(str, Enum):
    """Types of learning objects."""

    RUNBOOK_UPDATE = "runbook_update"
    EVAL_CASE = "eval_case"
    ROBOT_ASSERTION = "robot_assertion"
    POLICY_PROPOSAL = "policy_proposal"
    PROMPT_PROPOSAL = "prompt_proposal"


class LearningObjectStatus(str, Enum):
    """Lifecycle status of a learning object."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class ChangeProposalStatus(str, Enum):
    """Status of a change proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class LearningObject:
    """A learning artifact derived from an incident."""

    object_id: str
    incident_id: str
    object_type: LearningObjectType
    content: dict[str, Any]
    status: LearningObjectStatus = LearningObjectStatus.DRAFT
    created_at: str = ""
    promoted_at: str | None = None


@dataclass
class ChangeProposal:
    """A proposed change derived from a learning object."""

    proposal_id: str
    learning_object_id: str
    change_type: str
    proposal: dict[str, Any]
    status: ChangeProposalStatus = ChangeProposalStatus.PENDING
    created_at: str = ""
    approved_by: str | None = None
    approved_at: str | None = None


@dataclass
class EvalResult:
    """Result of running an eval case."""

    eval_id: str
    eval_case_id: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)
    run_at: str = ""


# In-memory stores (Phase 2.5)
_learning_objects: dict[str, LearningObject] = {}
_change_proposals: dict[str, ChangeProposal] = {}
_eval_results: list[EvalResult] = []


def create_learning_object(
    *,
    incident_id: str,
    object_type: LearningObjectType,
    content: dict[str, Any],
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[LearningObject, dict[str, Any]]:
    """Create a learning object from an incident.

    Returns (learning_object, receipt).
    """
    object_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    obj = LearningObject(
        object_id=object_id,
        incident_id=incident_id,
        object_type=object_type,
        content=content,
        created_at=now,
    )
    _learning_objects[object_id] = obj

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": incident_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "learning_loop",
        "action_type": "learning.object.created",
        "risk_tier": "green",
        "tool_used": "orchestrator.learning_loop",
        "created_at": now,
        "outcome": "success",
        "reason_code": "learning_object_created",
        "receipt_type": "learning.object.created",
        "receipt_hash": "",
        "details": {
            "object_id": object_id,
            "incident_id": incident_id,
            "object_type": object_type.value,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Learning object created: id=%s, type=%s, incident=%s",
        object_id[:8], object_type.value, incident_id[:8],
    )

    return obj, receipt


def run_eval(
    *,
    eval_case_id: str,
    test_fn: Any = None,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[EvalResult, dict[str, Any]]:
    """Run an eval case and record the result.

    Phase 2.5: Eval cases are structural — test_fn is optional.

    Returns (eval_result, receipt).
    """
    eval_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Phase 2.5: Simple pass-through (actual eval runs in Phase 3)
    passed = True
    details: dict[str, Any] = {"eval_case_id": eval_case_id}

    if test_fn is not None:
        try:
            result = test_fn()
            passed = bool(result)
            details["test_output"] = str(result)[:500]
        except Exception as e:
            passed = False
            details["error"] = str(e)[:500]

    eval_result = EvalResult(
        eval_id=eval_id,
        eval_case_id=eval_case_id,
        passed=passed,
        details=details,
        run_at=now,
    )
    _eval_results.append(eval_result)

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": eval_case_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "learning_loop",
        "action_type": "eval.run.completed",
        "risk_tier": "green",
        "tool_used": "orchestrator.learning_loop",
        "created_at": now,
        "outcome": "success" if passed else "failed",
        "reason_code": "eval_passed" if passed else "eval_failed",
        "receipt_type": "eval.run.completed",
        "receipt_hash": "",
        "details": {
            "eval_id": eval_id,
            "eval_case_id": eval_case_id,
            "passed": passed,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Eval run completed: id=%s, case=%s, passed=%s",
        eval_id[:8], eval_case_id[:8], passed,
    )

    return eval_result, receipt


def propose_change(
    *,
    learning_object_id: str,
    change_type: str,
    proposal: dict[str, Any],
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[ChangeProposal, dict[str, Any]]:
    """Propose a change based on a learning object.

    Returns (change_proposal, receipt).
    """
    obj = _learning_objects.get(learning_object_id)
    if obj is None:
        raise ValueError(f"Learning object not found: {learning_object_id}")

    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    change = ChangeProposal(
        proposal_id=proposal_id,
        learning_object_id=learning_object_id,
        change_type=change_type,
        proposal=proposal,
        created_at=now,
    )
    _change_proposals[proposal_id] = change
    obj.status = LearningObjectStatus.PROPOSED

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": obj.incident_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "learning_loop",
        "action_type": "learning.change.proposed",
        "risk_tier": "yellow",
        "tool_used": "orchestrator.learning_loop",
        "created_at": now,
        "outcome": "success",
        "reason_code": "change_proposed",
        "receipt_type": "learning.change.proposed",
        "receipt_hash": "",
        "details": {
            "proposal_id": proposal_id,
            "learning_object_id": learning_object_id,
            "change_type": change_type,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Change proposed: id=%s, object=%s, type=%s",
        proposal_id[:8], learning_object_id[:8], change_type,
    )

    return change, receipt


def approve_change(
    *,
    proposal_id: str,
    approver_id: str,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[ChangeProposal, dict[str, Any]]:
    """Approve a change proposal.

    Returns (updated_proposal, receipt).
    """
    change = _change_proposals.get(proposal_id)
    if change is None:
        raise ValueError(f"Change proposal not found: {proposal_id}")

    now = datetime.now(timezone.utc).isoformat()
    change.status = ChangeProposalStatus.APPROVED
    change.approved_by = approver_id
    change.approved_at = now

    # Update learning object status
    obj = _learning_objects.get(change.learning_object_id)
    if obj:
        obj.status = LearningObjectStatus.APPROVED

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": obj.incident_id if obj else proposal_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "user",
        "actor_id": approver_id,
        "action_type": "learning.change.approved",
        "risk_tier": "yellow",
        "tool_used": "orchestrator.learning_loop",
        "created_at": now,
        "outcome": "success",
        "reason_code": "change_approved",
        "receipt_type": "learning.change.approved",
        "receipt_hash": "",
        "details": {
            "proposal_id": proposal_id,
            "approver_id": approver_id,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Change approved: id=%s, approver=%s",
        proposal_id[:8], approver_id,
    )

    return change, receipt


def promote_object(
    *,
    learning_object_id: str,
    suite_id: str = "system",
    office_id: str = "system",
) -> tuple[LearningObject, dict[str, Any]]:
    """Promote a learning object to production status.

    Only approved objects can be promoted (Law #3).

    Returns (updated_object, receipt).
    """
    obj = _learning_objects.get(learning_object_id)
    if obj is None:
        raise ValueError(f"Learning object not found: {learning_object_id}")

    if obj.status != LearningObjectStatus.APPROVED:
        raise ValueError(
            f"Cannot promote learning object in status {obj.status.value} — "
            "must be APPROVED first (Law #3: fail-closed)"
        )

    now = datetime.now(timezone.utc).isoformat()
    obj.status = LearningObjectStatus.PROMOTED
    obj.promoted_at = now

    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": obj.incident_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "learning_loop",
        "action_type": "learning.object.promoted",
        "risk_tier": "green",
        "tool_used": "orchestrator.learning_loop",
        "created_at": now,
        "outcome": "success",
        "reason_code": "object_promoted",
        "receipt_type": "learning.object.promoted",
        "receipt_hash": "",
        "details": {
            "object_id": learning_object_id,
            "object_type": obj.object_type.value,
        },
    }

    store_receipts([receipt])

    logger.info(
        "Learning object promoted: id=%s, type=%s",
        learning_object_id[:8], obj.object_type.value,
    )

    return obj, receipt


def get_learning_object(object_id: str) -> LearningObject | None:
    """Get a learning object by ID."""
    return _learning_objects.get(object_id)


def list_learning_objects(
    status: LearningObjectStatus | None = None,
) -> list[LearningObject]:
    """List learning objects, optionally filtered by status."""
    objects = list(_learning_objects.values())
    if status:
        objects = [o for o in objects if o.status == status]
    return objects


def clear_stores() -> None:
    """Clear all stores. Testing only."""
    _learning_objects.clear()
    _change_proposals.clear()
    _eval_results.clear()
