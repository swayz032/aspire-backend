"""Messages API routes — Pass 19 Lane E1.

Routes:
  GET  /v1/messages/threads                         — list sms_thread memory_objects for tenant
  GET  /v1/messages/threads/{threadId}/messages     — paginated sms_messages for a thread
  PATCH /v1/messages/threads/{threadId}/read        — mark thread read (cuts receipt)
  PATCH /v1/messages/threads/{threadId}/pin         — toggle is_pinned (cuts receipt)
  PATCH /v1/messages/threads/{threadId}/archive     — toggle is_archived (cuts receipt)
  GET  /v1/messages/contacts/search                 — 4-source contact search
  GET  /v1/messages/templates                       — V1 quick-reply templates (static)
  GET  /v1/messages/suggestions                     — proactive_candidate SMS suggestions

Read routes  → capability scope: telephony:sms_read
Write routes → capability scope: telephony:sms_manage

Law compliance:
  Law #2 — receipts cut on every PATCH state change.
  Law #3 — fail closed on missing capability token.
  Law #4 — PATCH routes are YELLOW tier; GET routes are GREEN tier.
  Law #5 — server-side capability token validation before any DB access.
  Law #6 — suite_id + office_id scoping from X- headers; no cross-tenant data.
  Law #9 — phone numbers treated as PII; never logged verbatim.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.services.token_service import validate_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/messages", tags=["messages"])

# ---------------------------------------------------------------------------
# V1 Quick-reply templates (static — custom templates deferred to V1.1)
# ---------------------------------------------------------------------------

_V1_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "tmpl_appointment_confirm",
        "body": (
            "Confirming our appointment for {{date}} at {{time}}. "
            "Reply YES to confirm or call us at {{business_phone}}."
        ),
        "tokens": ["date", "time", "business_phone"],
    },
    {
        "id": "tmpl_quote_followup",
        "body": (
            "Hi {{first_name}} — quick follow-up on the quote we sent "
            "{{relative_time}}. Any questions?"
        ),
        "tokens": ["first_name", "relative_time"],
    },
    {
        "id": "tmpl_inquiry_ack",
        "body": "Thanks for your inquiry. We'll get back to you within {{response_window}}.",
        "tokens": ["response_window"],
    },
    {
        "id": "tmpl_invoice_reminder",
        "body": (
            "Reminder: your invoice #{{invoice_number}} for {{amount}} is due {{due_date}}."
        ),
        "tokens": ["invoice_number", "amount", "due_date"],
    },
    {
        "id": "tmpl_sarah_followup",
        "body": "We received your message. Sarah will follow up shortly.",
        "tokens": [],
    },
]


# ---------------------------------------------------------------------------
# Thread filter enum
# ---------------------------------------------------------------------------

class ThreadFilter(str, Enum):
    all = "all"
    unread = "unread"
    pinned = "pinned"
    archived = "archived"


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class ThreadStateRequest(BaseModel):
    capability_token: dict[str, Any] | None = None


def _parse_capability_token_param(raw: str | None) -> dict[str, Any] | None:
    """Parse capability_token from a JSON-encoded query parameter (GET routes).

    FastAPI cannot route dict types as Query params directly.
    Callers pass capability_token as a JSON-encoded string.
    """
    if not raw:
        return None
    import json
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as sms.py and front_desk.py)
# ---------------------------------------------------------------------------


def _resolve_scope(
    x_tenant_id: str | None,
    x_suite_id: str | None,
    x_office_id: str | None,
) -> ScopedIdentity:
    if not x_tenant_id or not x_suite_id or not x_office_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_SCOPE_HEADERS"},
        )
    try:
        return ScopedIdentity(
            tenant_id=uuid.UUID(x_tenant_id),
            suite_id=uuid.UUID(x_suite_id),
            office_id=uuid.UUID(x_office_id),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_SCOPE_HEADERS", "message": str(exc)},
        ) from exc


def _validate_cap_token(
    cap_token: dict[str, Any] | None,
    scope: ScopedIdentity,
    required_scope: str,
) -> None:
    """Fail-closed (Law #3): deny on missing or invalid token."""
    if cap_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN"},
        )
    result = validate_token(
        cap_token,
        expected_suite_id=str(scope.suite_id),
        expected_office_id=str(scope.office_id),
        required_scope=required_scope,
    )
    if not result.valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": result.error.value if result.error else "INVALID_TOKEN",
                "message": result.error_message or "",
            },
        )


def _cap_token_id(cap_token: dict[str, Any] | None) -> str:
    """Extract deterministic capability_token_id for receipt tracing."""
    if not cap_token:
        return ""
    if cap_token.get("token_id"):
        return str(cap_token["token_id"])
    sig = cap_token.get("signature") or cap_token.get("token") or ""
    if sig:
        import hashlib
        return hashlib.sha256(str(sig).encode()).hexdigest()[:16]
    return ""


# ---------------------------------------------------------------------------
# Internal helpers (injectable for testing via patch)
# ---------------------------------------------------------------------------


async def _fetch_threads(
    scope: ScopedIdentity,
    filter_type: ThreadFilter,
    limit: int,
    cursor: str | None,
) -> list[dict[str, Any]]:
    """Fetch sms_thread memory_objects for a tenant/suite/office with filter.

    Cursor format: "{last_activity_at ISO}_{memory_id}"
    Returns rows ordered by last_activity_at DESC.
    """
    base_filter = (
        f"tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
        f"&memory_type=eq.sms_thread"
    )

    # Apply filter
    if filter_type == ThreadFilter.unread:
        base_filter += "&read_at=is.null"
    elif filter_type == ThreadFilter.pinned:
        base_filter += "&is_pinned=eq.true"
    elif filter_type == ThreadFilter.archived:
        base_filter += "&is_archived=eq.true"
    else:
        # all: exclude archived by default (archived is a separate tab)
        base_filter += "&is_archived=eq.false"

    # Cursor-based pagination (keyset on last_activity_at DESC + memory_id)
    if cursor:
        parts = cursor.split("__", 1)
        if len(parts) == 2:
            ts_part, _id_part = parts
            base_filter += f"&last_activity_at=lt.{quote(ts_part, safe='')}"

    try:
        rows = await supabase_select(
            "memory_objects",
            base_filter,
            order_by="last_activity_at.desc",
            limit=limit + 1,  # +1 to detect has_more
        )
    except SupabaseClientError as exc:
        logger.error("Failed to fetch sms_thread memory_objects: %s", exc)
        return []

    return rows


def _thread_row_to_response(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a memory_objects row (sms_thread) to the thread list shape."""
    detail = row.get("detail") or {}
    return {
        "thread_id": str(row.get("memory_id", "")),
        "contact_name": detail.get("contact_name") or "",
        "contact_phone": detail.get("contact_phone") or "",
        "last_message_preview": detail.get("last_message_preview") or "",
        "last_activity_at": row.get("last_activity_at"),
        "unread_count": int(detail.get("unread_count") or 0),
        "is_pinned": bool(row.get("is_pinned", False)),
        "is_archived": bool(row.get("is_archived", False)),
        "last_drafter": detail.get("last_drafter"),
    }


async def _fetch_messages(
    scope: ScopedIdentity,
    thread_id: str,
    limit: int,
    before: str | None,
) -> list[dict[str, Any]]:
    """Fetch sms_messages for a thread, ordered sent_at ASC."""
    base_filter = (
        f"tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
        f"&thread_memory_id=eq.{thread_id}"
    )

    # Cursor: before = "sent_at ISO__message_id"
    if before:
        parts = before.split("__", 1)
        if len(parts) == 2:
            ts_part, _id_part = parts
            base_filter += f"&sent_at=lt.{quote(ts_part, safe='')}"

    try:
        rows = await supabase_select(
            "sms_messages",
            base_filter,
            order_by="sent_at.asc",
            limit=limit + 1,
        )
    except SupabaseClientError as exc:
        logger.error("Failed to fetch sms_messages for thread %s: %s", thread_id[:8], exc)
        return []

    return rows


async def _update_thread_state(
    scope: ScopedIdentity,
    thread_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Apply state updates to a memory_objects row (sms_thread).

    Returns the updated fields.
    Raises HTTPException 404 if thread not found or cross-tenant.
    """
    match_filter = (
        f"memory_id=eq.{thread_id}"
        f"&tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
        f"&memory_type=eq.sms_thread"
    )
    try:
        result = await supabase_update("memory_objects", match_filter, updates)
    except SupabaseClientError as exc:
        logger.error(
            "Failed to update sms_thread state thread=%s: %s", thread_id[:8], exc
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "THREAD_NOT_FOUND", "thread_id": thread_id},
        ) from exc
    return result or updates


async def _cut_receipt(
    action_type: str,
    scope: ScopedIdentity,
    thread_id: str,
    cap_token: dict[str, Any] | None,
    outcome: str,
) -> str:
    """Cut an immutable receipt for a thread state change (Law #2)."""
    receipt_id = str(uuid.uuid4())
    correlation_id = get_correlation_id()
    trace_id = get_trace_id()
    receipt_store.store_receipts([
        {
            "id": receipt_id,
            "suite_id": str(scope.suite_id),
            "office_id": str(scope.office_id),
            "tenant_id": str(scope.tenant_id),
            "correlation_id": correlation_id,
            "trace_id": trace_id,
            "receipt_type": "messages",
            "action_type": action_type,
            "tool_used": "messages",
            "risk_tier": "yellow",
            "capability_token_id": _cap_token_id(cap_token),
            "actor_type": "USER",
            "actor_id": str(scope.suite_id),
            "outcome": outcome,
            "redacted_inputs": {"thread_id": thread_id[:8] + "..."},
            "redacted_outputs": {},
        }
    ])
    return receipt_id


async def _search_contacts(
    scope: ScopedIdentity,
    q: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Search across 4 sources; returns list ordered by source priority."""
    results: list[dict[str, Any]] = []

    # --- Source 1: routing_contacts (highest priority, role badge) ---
    routing_filter = (
        f"tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
    )
    if q:
        q_enc = quote(q, safe="")
        routing_filter += f"&or=(name.ilike.*{q_enc}*,phone.ilike.*{q_enc}*)"
    try:
        routing_rows = await supabase_select(
            "front_desk_routing_contacts",
            routing_filter,
            order_by="sort_order.asc",
            limit=limit,
        )
        for row in routing_rows:
            results.append({
                "display_name": row.get("name") or row.get("phone") or "",
                "phone": row.get("phone") or "",
                "source": "routing",
                "role": row.get("role"),
                "last_interaction_at": row.get("updated_at"),
            })
    except SupabaseClientError as exc:
        logger.warning("routing_contacts search failed: %s", exc)

    # --- Source 2: recent sms_thread contacts ---
    sms_filter = (
        f"tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
        f"&memory_type=eq.sms_thread"
    )
    try:
        sms_rows = await supabase_select(
            "memory_objects",
            sms_filter,
            order_by="last_activity_at.desc",
            limit=limit,
        )
        for row in sms_rows:
            detail = row.get("detail") or {}
            contact_name = detail.get("contact_name") or ""
            contact_phone = detail.get("contact_phone") or ""
            if q and q.lower() not in contact_name.lower() and q not in contact_phone:
                continue
            # Skip if already found in routing
            if any(r["phone"] == contact_phone for r in results):
                continue
            results.append({
                "display_name": contact_name or contact_phone,
                "phone": contact_phone,
                "source": "sms",
                "role": None,
                "last_interaction_at": row.get("last_activity_at"),
            })
    except SupabaseClientError as exc:
        logger.warning("sms_thread contacts search failed: %s", exc)

    # --- Source 3: recent call memory entities (last 90 days) ---
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    call_filter = (
        f"tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
        f"&memory_type=eq.call"
        f"&last_activity_at=gte.{quote(cutoff, safe='')}"
    )
    try:
        call_rows = await supabase_select(
            "memory_objects",
            call_filter,
            order_by="last_activity_at.desc",
            limit=limit,
        )
        for row in call_rows:
            detail = row.get("detail") or {}
            caller_number = detail.get("caller_number") or detail.get("from_number") or ""
            caller_name = detail.get("caller_name") or detail.get("contact_name") or caller_number
            if q and q.lower() not in caller_name.lower() and q not in caller_number:
                continue
            if any(r["phone"] == caller_number for r in results):
                continue
            results.append({
                "display_name": caller_name,
                "phone": caller_number,
                "source": "call",
                "role": None,
                "last_interaction_at": row.get("last_activity_at"),
            })
    except SupabaseClientError as exc:
        logger.warning("call memory contacts search failed: %s", exc)

    # --- Source 4: manual E.164 entry ---
    import re
    if q and re.match(r"^\+?1?[2-9]\d{9}$", q.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")):
        # Normalize to E.164
        digits = re.sub(r"\D", "", q)
        if len(digits) == 10:
            digits = "1" + digits
        e164 = "+" + digits
        if not any(r["phone"] == e164 for r in results):
            results.append({
                "display_name": e164,
                "phone": e164,
                "source": "manual",
                "role": None,
                "last_interaction_at": None,
            })

    return results[:limit]


async def _fetch_suggestions(
    scope: ScopedIdentity,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch SMS-relevant proactive_candidates for this tenant.

    Filters to candidates where recommended_action='sms_reply_needed'
    OR recommended_action='sms_followup' and status='open'.
    """
    sug_filter = (
        f"tenant_id=eq.{scope.tenant_id}"
        f"&suite_id=eq.{scope.suite_id}"
        f"&office_id=eq.{scope.office_id}"
        f"&status=eq.open"
        f"&or=(recommended_action.eq.sms_reply_needed,recommended_action.eq.sms_followup)"
    )
    try:
        rows = await supabase_select(
            "proactive_candidates",
            sug_filter,
            order_by="last_activity_at.desc",
            limit=limit,
        )
    except SupabaseClientError as exc:
        logger.warning("proactive_candidates fetch failed: %s", exc)
        return []

    suggestions = []
    for row in rows:
        detail = {}
        # Extract contact info from source_memory_ids (best-effort)
        suggestions.append({
            "thread_id": str(row["thread_id"]) if row.get("thread_id") else None,
            "contact_name": detail.get("contact_name") or "",
            "contact_phone": detail.get("contact_phone") or "",
            "suggested_body": row.get("why_now") or "",
            "reason": row.get("recommended_action", "sms_followup"),
            "candidate_id": str(row.get("candidate_id", "")),
        })
    return suggestions


# ---------------------------------------------------------------------------
# Route: GET /v1/messages/threads
# ---------------------------------------------------------------------------


@router.get("/threads")
async def list_threads(
    filter: ThreadFilter = Query(ThreadFilter.all),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    capability_token: str | None = Query(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """GREEN tier: list sms_thread memory_objects with filter and pagination.

    Requires capability token with scope=telephony:sms_read.
    capability_token: JSON-encoded capability token dict passed as query param.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(_parse_capability_token_param(capability_token), scope, "telephony:sms_read")

    rows = await _fetch_threads(scope, filter, limit, cursor)

    # Detect has_more using the +1 trick
    has_more = len(rows) > limit
    rows = rows[:limit]

    threads = [_thread_row_to_response(r) for r in rows]

    # Build next_cursor from last item
    next_cursor: str | None = None
    if has_more and threads:
        last = rows[-1]
        next_cursor = f"{last.get('last_activity_at', '')}_{last.get('memory_id', '')}"

    return {
        "threads": threads,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# Route: GET /v1/messages/threads/{threadId}/messages
# ---------------------------------------------------------------------------


@router.get("/threads/{thread_id}/messages")
async def list_thread_messages(
    thread_id: str,
    limit: int = Query(100, ge=1, le=200),
    before: str | None = Query(None),
    capability_token: str | None = Query(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """GREEN tier: paginated sms_messages for a thread (sent_at ASC).

    Requires capability token with scope=telephony:sms_read.
    Cursor pagination via `before` param: "{sent_at_iso}__{message_id}".
    capability_token: JSON-encoded capability token dict passed as query param.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(_parse_capability_token_param(capability_token), scope, "telephony:sms_read")

    rows = await _fetch_messages(scope, thread_id, limit, before)

    has_more = len(rows) > limit
    rows = rows[:limit]

    messages = []
    for row in rows:
        messages.append({
            "id": str(row.get("id", "")),
            "direction": row.get("direction", ""),
            "from_number": row.get("from_number", ""),
            "to_number": row.get("to_number", ""),
            "body": row.get("body", ""),
            "status": row.get("status", ""),
            "sent_at": row.get("sent_at"),
            "delivered_at": row.get("delivered_at"),
            "media_urls": row.get("media_urls") or [],
            "twilio_message_sid": row.get("twilio_message_sid", ""),
        })

    return {
        "messages": messages,
        "has_more": has_more,
    }


# ---------------------------------------------------------------------------
# Route: PATCH /v1/messages/threads/{threadId}/read
# ---------------------------------------------------------------------------


@router.patch("/threads/{thread_id}/read")
async def mark_thread_read(
    thread_id: str,
    req: ThreadStateRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """YELLOW tier: mark a thread as read (sets read_at=NOW()).

    Requires capability token with scope=telephony:sms_manage.
    Cuts sms_thread_read receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "telephony:sms_manage")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = await _update_thread_state(scope, thread_id, {"read_at": now_iso})
    receipt_id = await _cut_receipt(
        "sms_thread_read", scope, thread_id, req.capability_token, "success"
    )

    return {
        "success": True,
        "thread_id": thread_id,
        "read_at": updated.get("read_at") or now_iso,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# Route: PATCH /v1/messages/threads/{threadId}/pin
# ---------------------------------------------------------------------------


@router.patch("/threads/{thread_id}/pin")
async def toggle_thread_pin(
    thread_id: str,
    req: ThreadStateRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """YELLOW tier: toggle is_pinned on an sms_thread.

    Requires capability token with scope=telephony:sms_manage.
    Cuts sms_thread_pin receipt (Law #2).

    Toggle logic: reads current value, flips it, writes back.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "telephony:sms_manage")

    # Read current pinned state
    try:
        rows = await supabase_select(
            "memory_objects",
            (
                f"memory_id=eq.{thread_id}"
                f"&tenant_id=eq.{scope.tenant_id}"
                f"&suite_id=eq.{scope.suite_id}"
                f"&office_id=eq.{scope.office_id}"
                f"&memory_type=eq.sms_thread"
            ),
            limit=1,
        )
    except SupabaseClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "THREAD_NOT_FOUND"},
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "THREAD_NOT_FOUND", "thread_id": thread_id},
        )

    current_pinned = bool(rows[0].get("is_pinned", False))
    new_pinned = not current_pinned

    updated = await _update_thread_state(scope, thread_id, {"is_pinned": new_pinned})
    receipt_id = await _cut_receipt(
        "sms_thread_pin", scope, thread_id, req.capability_token, "success"
    )

    return {
        "success": True,
        "thread_id": thread_id,
        "is_pinned": updated.get("is_pinned", new_pinned),
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# Route: PATCH /v1/messages/threads/{threadId}/archive
# ---------------------------------------------------------------------------


@router.patch("/threads/{thread_id}/archive")
async def toggle_thread_archive(
    thread_id: str,
    req: ThreadStateRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """YELLOW tier: toggle is_archived on an sms_thread.

    Requires capability token with scope=telephony:sms_manage.
    Cuts sms_thread_archive receipt (Law #2).
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "telephony:sms_manage")

    # Read current archive state
    try:
        rows = await supabase_select(
            "memory_objects",
            (
                f"memory_id=eq.{thread_id}"
                f"&tenant_id=eq.{scope.tenant_id}"
                f"&suite_id=eq.{scope.suite_id}"
                f"&office_id=eq.{scope.office_id}"
                f"&memory_type=eq.sms_thread"
            ),
            limit=1,
        )
    except SupabaseClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "THREAD_NOT_FOUND"},
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "THREAD_NOT_FOUND", "thread_id": thread_id},
        )

    current_archived = bool(rows[0].get("is_archived", False))
    new_archived = not current_archived

    updated = await _update_thread_state(scope, thread_id, {"is_archived": new_archived})
    receipt_id = await _cut_receipt(
        "sms_thread_archive", scope, thread_id, req.capability_token, "success"
    )

    return {
        "success": True,
        "thread_id": thread_id,
        "is_archived": updated.get("is_archived", new_archived),
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# Route: GET /v1/messages/contacts/search
# ---------------------------------------------------------------------------


@router.get("/contacts/search")
async def search_contacts(
    q: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    capability_token: str | None = Query(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """GREEN tier: search contacts across 4 sources with priority ordering.

    Sources (priority):
      1. front_desk_routing_contacts (role badge)
      2. sms_thread memory_objects (recent SMS contacts)
      3. call memory_objects (last 90 days)
      4. Manual E.164 entry (fallback when q matches phone pattern)

    Requires capability token with scope=telephony:sms_read.
    capability_token: JSON-encoded capability token dict passed as query param.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(_parse_capability_token_param(capability_token), scope, "telephony:sms_read")

    contacts = await _search_contacts(scope, q, limit)

    return {"contacts": contacts}


# ---------------------------------------------------------------------------
# Route: GET /v1/messages/templates
# ---------------------------------------------------------------------------


@router.get("/templates")
async def get_templates(
    capability_token: str | None = Query(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """GREEN tier: return V1 quick-reply templates (static, no DB call).

    Requires capability token with scope=telephony:sms_read.
    Returns exactly 5 templates per plan §3.9.7.
    capability_token: JSON-encoded capability token dict passed as query param.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(_parse_capability_token_param(capability_token), scope, "telephony:sms_read")

    return {"templates": _V1_TEMPLATES}


# ---------------------------------------------------------------------------
# Route: GET /v1/messages/suggestions
# ---------------------------------------------------------------------------


@router.get("/suggestions")
async def get_suggestions(
    limit: int = Query(5, ge=1, le=50),
    capability_token: str | None = Query(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """GREEN tier: SMS suggestions from proactive_candidate engine.

    Filters candidates where recommended_action is 'sms_reply_needed'
    OR 'sms_followup' and status='open'. Returns empty list if none.

    Requires capability token with scope=telephony:sms_read.
    capability_token: JSON-encoded capability token dict passed as query param.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(_parse_capability_token_param(capability_token), scope, "telephony:sms_read")

    suggestions = await _fetch_suggestions(scope, limit)

    return {"suggestions": suggestions}
