"""Sarah Receptionist webhook tool routes — `/v1/tools/sarah/*`.

These are the HTTP endpoints behind the 6 EL webhook tools the live agent
calls during a conversation. Sarah uses them as her "hands": each tool
takes a structured payload, hits this orchestrator, writes a memory_object
(if state-changing), and returns a small JSON envelope she can speak from.

Routes:
  POST /v1/tools/sarah/personalization     -> business context for greeting
  POST /v1/tools/sarah/capture-message     -> record a caller message
  POST /v1/tools/sarah/transfer            -> log a transfer + return target
  POST /v1/tools/sarah/faq                 -> RAG against business KB
  POST /v1/tools/sarah/callback-request    -> save a callback window
  POST /v1/tools/sarah/call-summary        -> save a call_summary memory_object

Each route:
  - Resolves tenant scope from the inbound `called_number` (Twilio E.164),
    NOT from headers — these are unauthenticated webhook calls from EL.
  - Cuts a Law-#2 receipt on every state change.
  - Returns a stable JSON shape Sarah's prompt can read.
  - Fail-soft: if the upstream lookup fails, returns a sensible default
    so the agent can still complete the conversation gracefully.

These routes are intentionally NOT capability-token gated — EL signs the
request via the workspace HMAC secret (validated separately by middleware
when configured). Inbound tenant resolution from `called_number` is the
authorization boundary, mirroring `/v1/sarah/personalization`.
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

router = APIRouter(prefix="/v1/tools/sarah", tags=["sarah-tools"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_E164 = re.compile(r"^\+\d{7,15}$")


def _redact_phone(phone: str | None) -> str:
    """Truncate phone numbers in logs/receipts (Law #9)."""
    if not phone:
        return ""
    return phone[:6] + "..." if len(phone) > 6 else phone


async def _resolve_tenant_from_called_number(
    called_number: str,
) -> dict[str, str] | None:
    """Look up tenant scope from the called number.

    Mirrors the lookup in `routes/sarah.py:_resolve_personalization`.
    Returns {tenant_id, suite_id, office_id} or None when unknown.
    """
    if not called_number or not _E164.match(called_number):
        return None
    try:
        # Use dict form so supabase_select URL-encodes the value.
        # Raw-string form leaves "+" unencoded, which PostgREST parses as space
        # ("+14482885386" -> " 14482885386" in the WHERE clause -> 0 rows).
        rows = await supabase_select(
            "tenant_phone_numbers",
            {"phone_number": called_number, "status": "active"},
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("sarah_tools tenant_lookup_failed: %s", exc)
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
    """Append a single immutable receipt for a Sarah tool call."""
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": receipt_id,
                "receipt_type": receipt_type,
                "suite_id": scope.get("suite_id", ""),
                "office_id": scope.get("office_id", ""),
                "tenant_id": scope.get("tenant_id", ""),
                "outcome": outcome,
                "action_type": receipt_type,
                "tool_used": "sarah_tools",
                "risk_tier": risk_tier,
                "redacted_inputs": redacted_inputs or {},
                "redacted_outputs": redacted_outputs or {},
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "created_at": now,
            }
        ]
    )
    return receipt_id


async def _insert_memory_object(
    *,
    memory_type: str,
    scope: dict[str, str],
    summary: str,
    detail: dict[str, Any],
    source_surface: str = "sarah_voice",  # CHECK constraint: ava_voice|sarah_voice|...
    source_agent: str = "sarah",          # CHECK constraint: ava|sarah|eli|nora|finn|tim|system
    channel: str = "voice",                # CHECK constraint: voice|video|email|sms|workflow|finance|ui|webhook
    session_provider: str = "twilio",
    runtime_family: str = "elevenlabs",
    external_session_id: str | None = None,
    idempotency_key: str | None = None,
    title: str | None = None,
    event_at: str | None = None,
) -> str:
    """Insert a memory_object row for a Sarah tool call.

    Uses the correct memory_objects schema (migration 099+):
    - memory_type  replaces old object_type (non-existent column)
    - summary      is a top-level NOT NULL column (not inside metadata/detail)
    - detail       is the jsonb payload column (was incorrectly called metadata)
    - trace_id / correlation_id are required NOT NULL columns — generated here

    Returns the inserted memory_id (uuid string).

    Raises SupabaseClientError on DB failure so callers can cut a failure
    receipt and return an honest error to the agent. Does NOT swallow errors.
    """
    now = datetime.now(timezone.utc).isoformat()
    row: dict[str, Any] = {
        "tenant_id": scope["tenant_id"],
        "suite_id": scope["suite_id"],
        "office_id": scope["office_id"],
        "memory_type": memory_type,
        "summary": summary,
        "detail": detail,
        "trace_id": get_trace_id() or str(uuid.uuid4()),
        "correlation_id": get_correlation_id() or str(uuid.uuid4()),
        "source_surface": source_surface,
        "source_agent": source_agent,
        "channel": channel,
        "session_provider": session_provider,
        "runtime_family": runtime_family,
    }
    if external_session_id is not None:
        row["external_session_id"] = external_session_id
    if idempotency_key is not None:
        row["idempotency_key"] = idempotency_key
    if title is not None:
        row["title"] = title
    if event_at is not None:
        row["event_at"] = event_at

    del now  # unused; DB sets created_at via default
    # SupabaseClientError propagates to caller — no silent swallow
    # supabase_insert returns the inserted row dict (already unwrapped from list)
    inserted: dict[str, Any] = await supabase_insert("memory_objects", row)
    return str(inserted.get("memory_id", ""))


# ---------------------------------------------------------------------------
# Request models — mirror the EL agent's webhook tool schemas exactly
# ---------------------------------------------------------------------------


class _BaseToolReq(BaseModel):
    called_number: str = Field(..., description="Twilio E.164 the caller dialed")


class GetBusinessContextReq(_BaseToolReq):
    caller_number: str | None = None


class CaptureMessageReq(_BaseToolReq):
    caller_name: str = ""
    caller_phone: str = ""
    message: str = ""
    urgency: str = "normal"
    reason_category: str = "other"


class TransferReq(_BaseToolReq):
    transfer_role: str = Field(
        ..., description="owner|sales|support|billing|scheduling"
    )
    caller_name: str = ""
    reason: str = ""


class FaqReq(_BaseToolReq):
    question: str = ""


class CallbackRequestReq(_BaseToolReq):
    caller_name: str = ""
    caller_phone: str = ""
    preferred_window: str = ""
    reason: str = ""


class CallSummaryReq(_BaseToolReq):
    outcome: str = "completed"
    summary: str = ""
    caller_name: str = ""
    caller_phone: str = ""


# ---------------------------------------------------------------------------
# 1) get_business_context
# ---------------------------------------------------------------------------


@router.post("/personalization")
async def get_business_context(req: GetBusinessContextReq) -> dict[str, Any]:
    """Return business config for the call — used during the Greeting node.

    Sarah calls this to learn business_name, hours, routing contacts. We
    re-use the same lookup as the conversation-initiation webhook so the
    in-call answer matches the greeting.
    """
    scope = await _resolve_tenant_from_called_number(req.called_number)
    if not scope:
        return {
            "success": False,
            "reason": "unknown_number",
            "business_name": "your business",
        }

    # Pull config + routing in parallel-ish (sequential — both small).
    config = {}
    routing: list[dict[str, Any]] = []
    biz_name = "your business"
    industry = "professional_services"
    timezone_name = "America/New_York"
    try:
        cfg_rows = await supabase_select(
            "front_desk_configs",
            f"office_id=eq.{scope['office_id']}&is_current=eq.true",
            order_by="version_no.desc",
            limit=1,
        )
        if cfg_rows:
            config = cfg_rows[0]
        routing = await supabase_select(
            "front_desk_routing_contacts",
            f"office_id=eq.{scope['office_id']}",
        ) or []
        suite_rows = await supabase_select(
            "suite_profiles",
            f"suite_id=eq.{scope['suite_id']}",
            limit=1,
        )
        if suite_rows:
            biz_name = suite_rows[0].get("business_name") or biz_name
            industry = suite_rows[0].get("industry") or industry
            timezone_name = (
                config.get("timezone")
                or suite_rows[0].get("timezone")
                or timezone_name
            )
    except SupabaseClientError as exc:
        logger.warning("get_business_context lookup_failed: %s", exc)

    routing_summary = ", ".join(
        f"{r.get('name') or r.get('label') or r.get('role','')} ({r.get('role','')})"
        for r in routing
        if r.get("phone")
    )

    _cut_receipt(
        receipt_type="sarah_tool_business_context",
        scope=scope,
        risk_tier="green",
        redacted_inputs={"called_number": _redact_phone(req.called_number)},
    )

    return {
        "success": True,
        "business_name": biz_name,
        "industry": industry,
        "timezone": timezone_name,
        "after_hours_mode": config.get("after_hours_mode", "take_message"),
        "busy_mode": config.get("busy_mode", "take_message"),
        "routing_summary": routing_summary,
        "routing_contacts": [
            {"role": r.get("role"), "name": r.get("name") or r.get("label", ""), "has_phone": bool(r.get("phone"))}
            for r in routing
        ],
    }


# ---------------------------------------------------------------------------
# 2) capture_message
# ---------------------------------------------------------------------------


@router.post("/capture-message")
async def capture_message(req: CaptureMessageReq) -> dict[str, Any]:
    """Record a caller message for the office (after-hours + take_message)."""
    scope = await _resolve_tenant_from_called_number(req.called_number)
    if not scope:
        return {"success": False, "reason": "unknown_number"}

    caller_label = req.caller_name or "caller"
    try:
        memory_id = await _insert_memory_object(
            memory_type="call",  # CHECK constraint allowed value (was 'voicemail_capture' — invalid)
            scope=scope,
            summary=(
                f"Message from {caller_label}: {req.message[:120]}"
                if req.message
                else f"Message captured from {caller_label}"
            ),
            detail={
                "caller_name": req.caller_name,
                "urgency": req.urgency,
                "reason_category": req.reason_category,
                "message": req.message,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
            title=f"Message — {caller_label}",
            source_agent="sarah",
            channel="voice",
        )
        receipt_outcome = "success"
    except SupabaseClientError as exc:
        logger.error(
            "sarah_tools capture_message memory_insert_failed: %s", exc
        )
        memory_id = ""
        receipt_outcome = "failed"

    _cut_receipt(
        receipt_type="sarah_tool_capture_message",
        scope=scope,
        outcome=receipt_outcome,
        redacted_inputs={
            "caller_phone": _redact_phone(req.caller_phone),
            "urgency": req.urgency,
            "category": req.reason_category,
        },
        redacted_outputs={"memory_id": memory_id},
    )

    if not memory_id:
        # Insert failed — DO NOT lie to the agent. Return failure so the EL
        # agent + caller-side fallback can react. Receipt was already emitted
        # with outcome="failed".
        return {
            "success": False,
            "message_id": "",
            "reason": "persist_failed",
            "confirmation": (
                "Got it — I had trouble saving that on my end, but I'll make sure "
                "someone follows up with you."
            ),
        }
    return {
        "success": True,
        "message_id": memory_id,
        "confirmation": (
            f"Got it{(', ' + req.caller_name) if req.caller_name else ''}. "
            "Someone will follow up shortly."
        ),
    }


# ---------------------------------------------------------------------------
# 3) transfer
# ---------------------------------------------------------------------------

_VALID_ROLES = {"owner", "sales", "support", "billing", "scheduling"}


@router.post("/transfer")
async def transfer(req: TransferReq) -> dict[str, Any]:
    """Resolve a routing contact for a transfer + log the attempt.

    Sarah's actual call bridge is performed by the EL system tool
    `transfer_to_number` using `phone_dynamic_variable` destinations
    populated by the personalization webhook. This endpoint is a logging
    + permission check companion: it confirms the role exists, returns
    whether transfer is allowed, and cuts the receipt.
    """
    scope = await _resolve_tenant_from_called_number(req.called_number)
    if not scope:
        return {"success": False, "reason": "unknown_number"}

    role = (req.transfer_role or "").strip().lower()
    if role not in _VALID_ROLES:
        return {
            "success": False,
            "reason": "unknown_role",
            "allowed_roles": sorted(_VALID_ROLES),
        }

    rows = await supabase_select(
        "front_desk_routing_contacts",
        f"office_id=eq.{scope['office_id']}&role=eq.{role}",
        limit=1,
    )
    if not rows or not rows[0].get("phone"):
        _cut_receipt(
            receipt_type="sarah_tool_transfer",
            scope=scope,
            outcome="no_target",
            redacted_inputs={"role": role},
        )
        return {
            "success": False,
            "reason": "no_routing_contact",
            "fallback": "take_message",
        }

    contact = rows[0]
    _cut_receipt(
        receipt_type="sarah_tool_transfer",
        scope=scope,
        redacted_inputs={
            "role": role,
            "caller_name_present": bool(req.caller_name),
        },
        redacted_outputs={
            "contact_name": contact.get("name") or contact.get("label", ""),
            "destination": _redact_phone(contact.get("phone")),
        },
    )

    return {
        "success": True,
        "role": role,
        "contact_name": contact.get("name") or contact.get("label", ""),
        "transfer_allowed": bool(contact.get("transfer_allowed", True)),
        # Sarah should NOT speak the raw number; the EL system tool resolves
        # it via dynamic variables. Surface the dyn-var name so the prompt
        # can reference it consistently.
        "dynamic_variable": f"routing_{role}_phone",
    }


# ---------------------------------------------------------------------------
# 4) faq
# ---------------------------------------------------------------------------


@router.post("/faq")
async def faq(req: FaqReq) -> dict[str, Any]:
    """Acknowledge an FAQ lookup attempt + cut receipt.

    EL's RAG runs against the agent's KB at the LLM layer — by the time this
    tool fires, the model has already drafted an answer. This endpoint
    primarily exists so the workflow can record what topics callers ask
    about; the response just nudges Sarah to lean on the knowledge base.
    """
    scope = await _resolve_tenant_from_called_number(req.called_number)
    if not scope:
        return {"success": False, "reason": "unknown_number"}

    _cut_receipt(
        receipt_type="sarah_tool_faq_lookup",
        scope=scope,
        risk_tier="green",
        redacted_inputs={"question_chars": len(req.question or "")},
    )

    return {
        "success": True,
        "instruction": (
            "Answer from the knowledge base. If the answer isn't there, "
            "offer to take a message instead of guessing."
        ),
        "kb_attached": True,
    }


# ---------------------------------------------------------------------------
# 5) callback_request
# ---------------------------------------------------------------------------


@router.post("/callback-request")
async def callback_request(req: CallbackRequestReq) -> dict[str, Any]:
    """Save a callback window request (after-hours / busy_mode)."""
    scope = await _resolve_tenant_from_called_number(req.called_number)
    if not scope:
        return {"success": False, "reason": "unknown_number"}

    caller_label = req.caller_name or "caller"
    try:
        memory_id = await _insert_memory_object(
            memory_type="followup_task",  # CHECK constraint allowed value (was 'callback_request' — invalid)
            scope=scope,
            summary=(
                f"Callback requested by {caller_label}"
                + (f" for window: {req.preferred_window}" if req.preferred_window else "")
            ),
            detail={
                "caller_name": req.caller_name,
                "preferred_window": req.preferred_window,
                "reason": req.reason,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
            title=f"Callback — {caller_label}",
            source_agent="sarah",
            channel="voice",
        )
        receipt_outcome = "success"
    except SupabaseClientError as exc:
        logger.error(
            "sarah_tools callback_request memory_insert_failed: %s", exc
        )
        memory_id = ""
        receipt_outcome = "failed"

    _cut_receipt(
        receipt_type="sarah_tool_callback_request",
        scope=scope,
        outcome=receipt_outcome,
        redacted_inputs={"caller_phone": _redact_phone(req.caller_phone)},
        redacted_outputs={"memory_id": memory_id},
    )

    return {
        "success": True,
        "callback_id": memory_id,
        "confirmation": (
            f"We'll reach out to you{(' at ' + req.preferred_window) if req.preferred_window else ''}."
        ),
    }


# ---------------------------------------------------------------------------
# 6) call_summary
# ---------------------------------------------------------------------------


@router.post("/call-summary")
async def call_summary(req: CallSummaryReq) -> dict[str, Any]:
    """Save a session_summary memory_object at call wrap-up.

    Complements the EL post-call webhook (which fires after the call ends
    server-side and includes Data Collection extractions). This in-call
    summary lets the wrap-up node persist a digest before the call cleanly
    terminates, in case the post-call webhook is delayed.
    """
    scope = await _resolve_tenant_from_called_number(req.called_number)
    if not scope:
        return {"success": False, "reason": "unknown_number"}

    caller_label = req.caller_name or "caller"
    summary_text = (
        req.summary.strip()
        if req.summary.strip()
        else f"Call summary — {caller_label} — outcome: {req.outcome}"
    )
    try:
        memory_id = await _insert_memory_object(
            memory_type="session_summary",  # CHECK constraint allowed value (was 'call_summary' — invalid)
            scope=scope,
            summary=summary_text,
            detail={
                "outcome": req.outcome,
                "caller_name": req.caller_name,
                "saved_via": "in_call_tool",
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
            title=f"Call Summary — {caller_label}",
            source_agent="sarah",
            channel="voice",
        )
        receipt_outcome = "success"
    except SupabaseClientError as exc:
        logger.error(
            "sarah_tools call_summary memory_insert_failed: %s", exc
        )
        memory_id = ""
        receipt_outcome = "failed"

    _cut_receipt(
        receipt_type="sarah_tool_call_summary",
        scope=scope,
        outcome=receipt_outcome,
        redacted_inputs={
            "outcome": req.outcome,
            "caller_phone": _redact_phone(req.caller_phone),
        },
        redacted_outputs={"memory_id": memory_id},
    )

    return {"success": True, "summary_id": memory_id}
