"""ElevenLabs webhook tool handlers — `/v1/elevenlabs/tools/*` (Pass G).

These are the orchestrator-side HTTP handlers backing the 3 EL workspace
webhook tools created in Pass G:

  POST /v1/elevenlabs/tools/lookup_contact          — resolve caller by phone or email
  POST /v1/elevenlabs/tools/create_appointment_request — propose appointment (Yellow)
  POST /v1/elevenlabs/tools/verify_caller_identity  — OTP / factual challenge initiation

Design contract (Law #7 — Tools Are Hands):
  - Each handler executes ONE bounded query and returns a stable JSON envelope.
  - Zero autonomous decisions. All retries, escalation, fallback: orchestrator.
  - No secrets or PII in logs (Law #9).
  - Receipt cut on every state-changing call (Law #2).
  - Tenant scope resolved from `called_number` (same as sarah_tools pattern —
    these are unauthenticated EL webhook calls; HMAC verified by middleware when
    ASPIRE_WEBHOOK_SECRET is configured).

Receipt types emitted:
  - appointment_proposal_created   (create_appointment_request)
  - caller_identity_verification_initiated  (verify_caller_identity)
  - lookup_contact                 (lookup_contact — informational, outcome=success)
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/elevenlabs/tools", tags=["elevenlabs-tools"])

_E164 = re.compile(r"^\+\d{7,15}$")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _redact_phone(phone: str | None) -> str:
    """Truncate phone for logs/receipts (Law #9)."""
    if not phone:
        return ""
    return phone[:6] + "..." if len(phone) > 6 else phone


async def _resolve_tenant_from_called_number(
    called_number: str,
) -> dict[str, str] | None:
    """Look up tenant scope from the called number (mirrors sarah_tools pattern)."""
    if not called_number or not _E164.match(called_number):
        return None
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            {"phone_number": called_number, "status": "active"},
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("el_tools tenant_lookup_failed: %s", exc)
        return None
    if not rows:
        return None
    row = rows[0]
    return {
        "tenant_id": str(row.get("tenant_id", "")),
        "suite_id": str(row.get("suite_id", "")),
        "office_id": str(row.get("office_id", "")),
    }


def _cut_receipt(
    *,
    receipt_type: str,
    scope: dict[str, str],
    outcome: str = "success",
    risk_tier: str = "yellow",
    redacted_inputs: dict[str, Any] | None = None,
    redacted_outputs: dict[str, Any] | None = None,
) -> str:
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": rid,
                "receipt_type": receipt_type,
                "action_type": receipt_type,
                "suite_id": scope.get("suite_id", ""),
                "office_id": scope.get("office_id", ""),
                "tenant_id": scope.get("tenant_id", ""),
                "outcome": outcome,
                "tool_used": "elevenlabs_tools",
                "risk_tier": risk_tier,
                "redacted_inputs": redacted_inputs or {},
                "redacted_outputs": redacted_outputs or {},
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "created_at": now,
            }
        ]
    )
    return rid


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class LookupContactRequest(BaseModel):
    phone: str | None = Field(None, description="E.164 phone number to look up")
    email: str | None = Field(None, description="Email address to look up")
    called_number: str = Field(..., description="The Aspire number that was called (tenant resolution)")


class AppointmentWindow(BaseModel):
    start: str = Field(..., description="ISO 8601 start of proposed window")
    end: str = Field(..., description="ISO 8601 end of proposed window")


class AppointmentContact(BaseModel):
    phone: str = Field(..., description="Caller's E.164 phone number")
    name: str | None = None


class CreateAppointmentRequest(BaseModel):
    window: AppointmentWindow
    intent: str = Field(..., max_length=500, description="What the appointment is for")
    contact: AppointmentContact
    called_number: str = Field(..., description="The Aspire number that was called (tenant resolution)")


class VerifyCallerRequest(BaseModel):
    method: str = Field(..., description="'otp' or 'factual'")
    target: str = Field(..., description="Phone or email to deliver OTP / identity challenge")
    called_number: str = Field(..., description="The Aspire number that was called (tenant resolution)")


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/lookup_contact
# ---------------------------------------------------------------------------


@router.post("/lookup_contact")
async def lookup_contact(body: LookupContactRequest) -> dict[str, Any]:
    """Resolve a caller by phone or email.

    Returns the top matching front_desk_routing_contacts row + recent interaction
    summary. Scope resolved from called_number (no capability token — EL webhook).

    Receipt type: lookup_contact (informational, Green)
    """
    if not body.phone and not body.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "phone or email required"},
        )

    scope = await _resolve_tenant_from_called_number(body.called_number) or {}
    suite_id = scope.get("suite_id", "")

    contact: dict[str, Any] | None = None

    if body.phone and _E164.match(body.phone) and suite_id:
        try:
            rows = await supabase_select(
                "front_desk_routing_contacts",
                f"suite_id=eq.{suite_id}&phone=eq.{body.phone}&is_active=eq.true",
                limit=1,
            )
            if rows:
                contact = rows[0]
        except SupabaseClientError as exc:
            logger.warning("lookup_contact db_error phone=%s: %s", _redact_phone(body.phone), exc)

    if not contact and body.email and suite_id:
        try:
            rows = await supabase_select(
                "front_desk_routing_contacts",
                f"suite_id=eq.{suite_id}&email=eq.{body.email}&is_active=eq.true",
                limit=1,
            )
            if rows:
                contact = rows[0]
        except SupabaseClientError as exc:
            logger.warning("lookup_contact db_error email: %s", exc)

    # Fetch last 3 SMS threads as quick interaction summary
    last_interactions: list[str] = []
    if contact and suite_id:
        phone_val = contact.get("phone") or body.phone
        if phone_val:
            try:
                sms_rows = await supabase_select(
                    "sms_messages",
                    f"suite_id=eq.{suite_id}&from_number=eq.{phone_val}",
                    order_by="created_at.desc",
                    limit=3,
                )
                for r in (sms_rows or []):
                    preview = (r.get("body") or "")[:80]
                    if preview:
                        last_interactions.append(preview)
            except SupabaseClientError:
                pass

    _cut_receipt(
        receipt_type="lookup_contact",
        scope=scope,
        outcome="success",
        risk_tier="green",
        redacted_inputs={"phone": _redact_phone(body.phone), "has_email": bool(body.email)},
        redacted_outputs={"found": contact is not None},
    )

    if contact:
        return {
            "found": True,
            "contact": {
                "name": contact.get("label") or contact.get("name"),
                "entity": contact.get("role"),
                "phone": contact.get("phone"),
                "email": contact.get("email"),
                "tags": contact.get("tags") or [],
                "last_interaction_summary": "; ".join(last_interactions) or None,
            },
        }
    return {"found": False, "contact": None}


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/create_appointment_request
# ---------------------------------------------------------------------------


@router.post("/create_appointment_request")
async def create_appointment_request(body: CreateAppointmentRequest) -> dict[str, Any]:
    """Create a YELLOW-tier appointment proposal awaiting owner approval.

    Writes to approval_requests table (existing). Returns proposal_id + receipt.
    Receipt type: appointment_proposal_created
    """
    scope = await _resolve_tenant_from_called_number(body.called_number) or {}
    suite_id = scope.get("suite_id", "")

    # Validate window ISO 8601
    try:
        datetime.fromisoformat(body.window.start.replace("Z", "+00:00"))
        datetime.fromisoformat(body.window.end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "window.start/end must be ISO 8601"},
        ) from exc

    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    approval_row: dict[str, Any] = {
        "id": proposal_id,
        "suite_id": suite_id,
        "office_id": scope.get("office_id", "") or None,
        "action_type": "appointment_request",
        "risk_tier": "yellow",
        "status": "pending",
        "requested_by": "elevenlabs_agent",
        "payload": {
            "window": {"start": body.window.start, "end": body.window.end},
            "intent": body.intent,
            "contact_phone": _redact_phone(body.contact.phone),
            "contact_name": body.contact.name,
        },
        "created_at": now,
        "updated_at": now,
    }

    try:
        await supabase_insert("approval_requests", approval_row)
    except SupabaseClientError as exc:
        logger.warning("appointment_proposal_insert_failed suite_id=%s: %s", suite_id, exc)
        receipt_id = _cut_receipt(
            receipt_type="appointment_proposal_created",
            scope=scope,
            outcome="failed",
            redacted_inputs={
                "phone": _redact_phone(body.contact.phone),
                "intent_len": len(body.intent),
            },
        )
        return {
            "proposed": False,
            "proposal_id": None,
            "awaiting_approval": False,
            "receipt_id": receipt_id,
            "error": "DB write failed",
        }

    receipt_id = _cut_receipt(
        receipt_type="appointment_proposal_created",
        scope=scope,
        outcome="success",
        risk_tier="yellow",
        redacted_inputs={
            "phone": _redact_phone(body.contact.phone),
            "window_start": body.window.start,
            "intent_len": len(body.intent),
        },
        redacted_outputs={"proposal_id": proposal_id},
    )
    logger.info(
        "appointment_proposal_created id=%s suite_id=%s receipt=%s",
        proposal_id, suite_id, receipt_id,
    )
    return {
        "proposed": True,
        "proposal_id": proposal_id,
        "awaiting_approval": True,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/elevenlabs/tools/verify_caller_identity
# ---------------------------------------------------------------------------


@router.post("/verify_caller_identity")
async def verify_caller_identity(body: VerifyCallerRequest) -> dict[str, Any]:
    """Initiate caller identity verification.

    Phase G skeleton: generates a session_token for the verification challenge
    and returns instructions for the agent. Future hardening: deliver real OTP
    via Twilio and track verification state in a dedicated table.

    Receipt type: caller_identity_verification_initiated
    """
    if body.method not in ("otp", "factual"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "method must be 'otp' or 'factual'"},
        )

    scope = await _resolve_tenant_from_called_number(body.called_number) or {}
    session_token = str(uuid.uuid4())

    instructions: dict[str, str] = {
        "otp": (
            "Ask the caller to provide the 6-digit code just sent to their phone. "
            "Confirm the code matches before proceeding."
        ),
        "factual": (
            "Ask the caller to confirm their full name and zip code on file. "
            "Confirm both values match our records before proceeding."
        ),
    }[body.method]

    receipt_id = _cut_receipt(
        receipt_type="caller_identity_verification_initiated",
        scope=scope,
        outcome="success",
        risk_tier="yellow",
        redacted_inputs={
            "method": body.method,
            "target_prefix": body.target[:6] + "...",
        },
        redacted_outputs={"session_token_prefix": session_token[:8]},
    )
    logger.info(
        "caller_identity_verification_initiated method=%s suite_id=%s receipt=%s",
        body.method, scope.get("suite_id", ""), receipt_id,
    )
    return {
        "method": body.method,
        "instructions": instructions,
        "session_token": session_token,
        "receipt_id": receipt_id,
    }
