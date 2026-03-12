"""POST /v1/intents — Ava User intent classification and routing.

Ties together the Brain Layer services (IntentClassifier, SkillRouter)
into an HTTP endpoint that the Gateway calls on behalf of end users.

This endpoint does NOT execute actions — it classifies and routes.
Actual execution happens through the LangGraph pipeline (POST /v1/intents
on server.py, which invokes the full graph). This route is the "fast path"
for the Ava User frontend: classify intent, get routing plan, show
governance metadata to the user BEFORE they confirm execution.

Law compliance:
  - Law #1: This route proposes. Orchestrator decides.
  - Law #2: Classification failures produce receipts.
  - Law #3: Missing auth headers -> 401. Fail-closed on classifier errors.
  - Law #9: Raw utterance is never echoed back in the response.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aspire_orchestrator.services.intent_classifier import (
    IntentClassifier,
    IntentResult,
    get_intent_classifier,
)
from aspire_orchestrator.services.skill_router import (
    RoutingPlan,
    SkillRouter,
    get_skill_router,
)
from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------


class IntentRequest(BaseModel):
    """Matches ava_orchestrator_request.schema.json."""

    schema_version: str = Field(default="1.0")
    suite_id: str = Field(min_length=1)
    office_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    timestamp: str  # ISO 8601
    task_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class IntentResponse(BaseModel):
    """Matches ava_result.schema.json."""

    schema_version: str = "1.0"
    request_id: str
    correlation_id: str
    route: dict[str, Any]
    risk: dict[str, Any]
    governance: dict[str, Any]
    plan: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    actor_id: str,
    action_type: str,
    outcome: str,
    reason_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a classification receipt (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": "green",
        "tool_used": "intent_classifier",
        "outcome": outcome,
        "reason_code": reason_code,
        "created_at": now,
        "receipt_type": "classification",
        "receipt_hash": str(uuid.uuid4()),  # placeholder for chain
        "redacted_inputs": None,
        "redacted_outputs": details,
    }


def _error_json(
    *,
    error: str,
    message: str,
    correlation_id: str = "unknown",
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": error,
            "message": message,
            "correlation_id": correlation_id,
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/intents/classify
# ---------------------------------------------------------------------------


@router.post("/v1/intents/classify")
async def classify_intent(request: Request) -> JSONResponse:
    """Classify a user utterance and return a routing plan.

    Flow:
      1. Validate request body (Pydantic)
      2. Extract auth context from headers
      3. Classify intent via IntentClassifier
      4. If requires_clarification -> return clarification response
      5. If confidence < 0.5 -> return escalation response
      6. Route to skill pack via SkillRouter
      7. If routing denied -> return denied response with reason
      8. Return routing plan with governance metadata
    """
    # -- Auth context from Gateway headers (Law #3: missing = deny) ----------
    suite_id = request.headers.get("x-suite-id")
    office_id = request.headers.get("x-office-id")
    actor_id = request.headers.get("x-actor-id")

    if not suite_id or not office_id or not actor_id:
        missing = [h for h, v in [("X-Suite-Id", suite_id), ("X-Office-Id", office_id), ("X-Actor-Id", actor_id)] if not v]
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        # Law #2: emit denial receipt before returning 401
        receipt = _build_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id or "unknown",
            office_id=office_id or "unknown",
            actor_id="fail_closed_guard",
            action_type="intent.classify",
            outcome="denied",
            reason_code="AUTH_REQUIRED",
            details={"missing_headers": missing},
        )
        store_receipts([receipt])
        return _error_json(
            error="AUTH_REQUIRED",
            message=f"Missing required auth headers: {', '.join(missing)}",
            correlation_id=correlation_id,
            status_code=401,
        )

    # -- Parse request body --------------------------------------------------
    try:
        body = await request.json()
    except Exception:
        return _error_json(
            error="SCHEMA_VALIDATION_FAILED",
            message="Invalid JSON body",
        )

    try:
        req = IntentRequest(**body)
    except Exception as e:
        return _error_json(
            error="SCHEMA_VALIDATION_FAILED",
            message=f"Request validation failed: {e}",
            correlation_id=body.get("correlation_id", "unknown") if isinstance(body, dict) else "unknown",
        )

    correlation_id = req.correlation_id

    # -- Extract utterance from payload --------------------------------------
    utterance = req.payload.get("utterance", "")
    if not utterance:
        return _error_json(
            error="SCHEMA_VALIDATION_FAILED",
            message="payload.utterance is required",
            correlation_id=correlation_id,
        )

    # -- Classify intent (Brain Layer step 1) --------------------------------
    classifier: IntentClassifier = get_intent_classifier()

    try:
        intent_result: IntentResult = await classifier.classify(
            utterance=utterance,
            context=req.payload.get("context"),
        )
    except Exception as exc:
        logger.exception("Intent classification error: %s", type(exc).__name__)

        # Law #2: receipt for classification failure
        receipt = _build_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            action_type="intent.classify",
            outcome="failed",
            reason_code="classification_error",
            details={"error_type": type(exc).__name__},
        )
        store_receipts([receipt])

        return _error_json(
            error="CLASSIFICATION_FAILED",
            message="Intent classification failed",
            correlation_id=correlation_id,
            status_code=500,
        )

    # -- Handle low-confidence / clarification cases -------------------------

    # Confidence < 0.5 -> escalation (cannot route)
    if intent_result.confidence < 0.5:
        receipt = _build_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            action_type="intent.classify",
            outcome="denied",
            reason_code="low_confidence",
            details={
                "confidence": intent_result.confidence,
                "action_type": intent_result.action_type,
            },
        )
        store_receipts([receipt])

        return JSONResponse(
            status_code=200,
            content=IntentResponse(
                request_id=req.request_id,
                correlation_id=correlation_id,
                route={
                    "action": "escalate",
                    "reason": "low_confidence",
                    "confidence": intent_result.confidence,
                },
                risk={"tier": "yellow"},
                governance={
                    "approvals_required": [],
                    "presence_required": False,
                    "capability_token_required": False,
                    "receipt_ids": [receipt["id"]],
                },
                plan={"status": "escalated", "steps": []},
            ).model_dump(),
        )

    # Requires clarification (0.5 <= confidence < 0.85)
    if intent_result.requires_clarification:
        receipt = _build_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            action_type="intent.classify",
            outcome="success",
            reason_code="requires_clarification",
            details={
                "confidence": intent_result.confidence,
                "action_type": intent_result.action_type,
            },
        )
        store_receipts([receipt])

        return JSONResponse(
            status_code=200,
            content=IntentResponse(
                request_id=req.request_id,
                correlation_id=correlation_id,
                route={
                    "action": "clarify",
                    "classified_action": intent_result.action_type,
                    "skill_pack": intent_result.skill_pack,
                    "confidence": intent_result.confidence,
                    "clarification_prompt": intent_result.clarification_prompt,
                },
                risk={"tier": intent_result.risk_tier.value},
                governance={
                    "approvals_required": [],
                    "presence_required": False,
                    "capability_token_required": False,
                    "receipt_ids": [receipt["id"]],
                },
                plan={"status": "awaiting_clarification", "steps": []},
            ).model_dump(),
        )

    # -- Route to skill pack (Brain Layer step 2) ----------------------------
    skill_router: SkillRouter = get_skill_router()
    requested_agent = (
        req.payload.get("requested_agent")
        or req.payload.get("agent")
        or "ava"
    )
    current_agent = str(requested_agent).strip().lower() or "ava"

    try:
        allow_internal_routing = bool(
            req.payload.get("allow_internal_routing")
            or req.payload.get("admin_bridge_approved")
        )
        routing_plan: RoutingPlan = await skill_router.route(
            intent_result,
            context={
                "suite_id": suite_id,
                "office_id": office_id,
                "current_agent": current_agent,
                "allow_internal_routing": allow_internal_routing,
            },
        )
    except Exception as exc:
        logger.exception("Skill routing error: %s", type(exc).__name__)

        receipt = _build_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            action_type="intent.route",
            outcome="failed",
            reason_code="routing_error",
            details={"error_type": type(exc).__name__},
        )
        store_receipts([receipt])

        return _error_json(
            error="ROUTING_FAILED",
            message="Skill routing failed",
            correlation_id=correlation_id,
            status_code=500,
        )

    # -- Handle denied routing plan ------------------------------------------
    if routing_plan.deny_reason:
        receipt = _build_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_id=actor_id,
            action_type="intent.route",
            outcome="denied",
            reason_code=routing_plan.deny_reason,
            details={
                "action_type": intent_result.action_type,
                "deny_reason": routing_plan.deny_reason,
            },
        )
        store_receipts([receipt])

        return JSONResponse(
            status_code=200,
            content=IntentResponse(
                request_id=req.request_id,
                correlation_id=correlation_id,
                route={
                    "action": "denied",
                    "classified_action": intent_result.action_type,
                    "skill_pack": intent_result.skill_pack,
                    "deny_reason": routing_plan.deny_reason,
                },
                risk={"tier": intent_result.risk_tier.value},
                governance={
                    "approvals_required": [],
                    "presence_required": False,
                    "capability_token_required": False,
                    "receipt_ids": [receipt["id"]],
                },
                plan={"status": "denied", "steps": []},
            ).model_dump(),
        )

    # -- Build successful routing response -----------------------------------
    classification_receipt = _build_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        actor_id=actor_id,
        action_type="intent.classify",
        outcome="success",
        details={
            "confidence": intent_result.confidence,
            "action_type": intent_result.action_type,
            "skill_pack": intent_result.skill_pack,
        },
    )
    store_receipts([classification_receipt])

    # Determine governance requirements from the plan
    approvals_required: list[str] = []
    presence_required = False
    capability_token_required = False

    for step in routing_plan.steps:
        if step.approval_required:
            approvals_required.append(step.action_type)
        if step.presence_required:
            presence_required = True
        capability_token_required = True  # always required for execution

    plan_steps = [
        {
            "step_id": step.step_id,
            "skill_pack": step.skill_pack,
            "action_type": step.action_type,
            "tools": step.tools,
            "risk_tier": step.risk_tier.value,
            "approval_required": step.approval_required,
            "presence_required": step.presence_required,
            "depends_on": step.depends_on or [],
        }
        for step in routing_plan.steps
    ]

    return JSONResponse(
        status_code=200,
        content=IntentResponse(
            request_id=req.request_id,
            correlation_id=correlation_id,
            route={
                "action": "execute",
                "classified_action": intent_result.action_type,
                "skill_pack": intent_result.skill_pack,
                "confidence": intent_result.confidence,
                "entities": intent_result.entities,
            },
            risk={"tier": routing_plan.estimated_risk_tier.value},
            governance={
                "approvals_required": approvals_required,
                "presence_required": presence_required,
                "capability_token_required": capability_token_required,
                "receipt_ids": [classification_receipt["id"]],
            },
            plan={
                "status": "ready",
                "execution_strategy": routing_plan.execution_strategy.value,
                "delegation_required": routing_plan.delegation_required,
                "requires_compound_approval": routing_plan.requires_compound_approval,
                "steps": plan_steps,
            },
        ).model_dump(),
    )
