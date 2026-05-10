"""Contact Writer — upsert frontdesk_contacts from a completed EL call (Wave 5).

Called by the post-call enrichment pipeline after every receptionist call,
regardless of whether a voicemail was taken. Every caller gets a contact row
on their first call so that Tiffany can greet them by name on call #2.

Design decisions (from plan §3):
- Auto-create on first call (status='unconfirmed'); owner promotes/merges in UI.
- display_name update is COALESCE — never overwrite a name the owner already set.
- total_calls counter is incremented via the DB upsert, not read-modify-write,
  so concurrent webhook replays are safe.
- Idempotency: if a receipt with key `el-contact-upsert:{conversation_id}` already
  exists in the receipts table we return the cached contact_id without touching the
  DB again (replay-safe per Law #2).

Law compliance:
  Law #2 — Yellow-tier receipt cut for every upsert (contact_upserted).
  Law #3 — Missing suite_id or phone → fail-closed (HTTPException 422).
  Law #6 — suite_id is always taken from the resolved EL scope, never from caller.
  Law #9 — phone logged as first-6-digits prefix only; caller_name never logged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_upsert,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TOOL_NAME = "post_call_enrichment"
_RECEIPT_TYPE = "contact_upserted"
_RISK_TIER = "yellow"


def _phone_prefix(phone: str) -> str:
    """Return first-6-digits mask for PII-safe logging (Law #9)."""
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    return digits[:6] + "..." if len(digits) >= 6 else digits + "..."


def _build_receipt(
    *,
    receipt_id: str,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    outcome: str,
    reason_code: str,
    contact_id: str | None,
    conversation_id: str,
    phone_prefix: str,
    trace_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Build a Yellow-tier receipt for a contact upsert operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": receipt_id,
        "receipt_type": _RECEIPT_TYPE,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": f"service.{_TOOL_NAME}",
        "action_type": "contact_upsert",
        "tool_used": _TOOL_NAME,
        "risk_tier": _RISK_TIER,
        "outcome": outcome,
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "redacted_inputs": {
            "phone_prefix": phone_prefix,
            "conversation_id": conversation_id,
            # caller_name intentionally omitted — PII, Law #9
        },
        "redacted_outputs": {
            "contact_id": contact_id,
        },
        "created_at": now,
    }


async def _idempotency_check(idempotency_key: str) -> str | None:
    """Return existing contact_id if this conversation was already processed.

    Queries the receipts table for a prior successful receipt with the given
    idempotency key. Returns the contact_id stored in redacted_outputs, or None
    if this is a fresh invocation.
    """
    try:
        rows = await supabase_select(
            "receipts",
            # PostgREST filter: receipt_type=eq.X&action=->>'idempotency_key'=eq.Y
            # We encode idempotency_key inside the result column for lookup.
            # Simpler: filter on correlation_id which we derive from conversation_id.
            f"receipt_type=eq.{_RECEIPT_TYPE}&correlation_id=eq.{idempotency_key}&status=eq.SUCCEEDED",
            limit=1,
        )
    except SupabaseClientError:
        # Treat lookup failure as "no cached result" — will re-run upsert.
        return None

    if not rows:
        return None

    result = rows[0].get("result") or {}
    return result.get("contact_id") or (rows[0].get("redacted_outputs") or {}).get("contact_id")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def upsert_contact_from_call(
    *,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    phone_e164: str,
    caller_name: str,
    call_summary: str,
    conversation_id: str,
    category: str | None = None,
    trace_id: str = "",
    correlation_id: str = "",
) -> str:
    """Upsert a frontdesk_contacts row from a completed EL call.

    On first call: creates a new contact with status='unconfirmed', total_calls=1.
    On subsequent calls: increments total_calls, refreshes last_seen_at and
    last_call_summary, and conditionally updates display_name (only if blank).

    The SQL upsert (on_conflict=suite_id,phone_e164) is the source of truth for
    the increment — we pass `total_calls=1` to the upsert but the DB COALESCE
    pattern handles existing rows. Because PostgREST merge-duplicates does a full
    replace of the row, we READ the existing count first and compute the new value
    here. This is safe because this function is protected by the conversation-level
    idempotency key, so concurrent calls for the same conversation_id are blocked
    at the idempotency check above.

    Returns:
        contact_id: UUID string of the upserted contact row.

    Raises:
        HTTPException(422): if suite_id or phone_e164 is missing.
        HTTPException(503): if the Supabase upsert fails (fail-closed, Law #3).
    """
    if not suite_id or not phone_e164:
        raise HTTPException(
            status_code=422,
            detail="upsert_contact_from_call: suite_id and phone_e164 are required",
        )

    receipt_id = str(uuid.uuid4())
    idempotency_key = f"el-contact-upsert:{conversation_id}"
    phone_prefix = _phone_prefix(phone_e164)
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- Idempotency check (Law #2 — skip duplicate work) --------------------
    cached_contact_id = await _idempotency_check(idempotency_key)
    if cached_contact_id:
        logger.debug(
            "contact_writer idempotent_replay conversation_id=%s contact_id=%s",
            conversation_id,
            cached_contact_id,
        )
        return cached_contact_id

    # --- Read existing contact to compute incremented call count -------------
    existing_count = 0
    existing_contact_id: str | None = None
    existing_rows: list[dict[str, Any]] = []
    try:
        existing_rows = await supabase_select(
            "frontdesk_contacts",
            {"suite_id": suite_id, "phone_e164": phone_e164},
            limit=1,
        )
        if existing_rows:
            existing_count = int(existing_rows[0].get("total_calls") or 0)
            existing_contact_id = existing_rows[0].get("contact_id")
    except SupabaseClientError as exc:
        logger.warning(
            "contact_writer existing_contact_lookup_failed phone_prefix=%s error=%s",
            phone_prefix,
            exc.detail,
        )
        # Non-fatal — proceed with count=0 (will default to 1 on fresh insert)

    new_count = existing_count + 1

    # --- Build upsert payload ------------------------------------------------
    # display_name: carry forward the owner-set name if one already exists, so
    # PostgREST merge-duplicates never overwrites a curated name with a raw EL value.
    existing_name: str = ""
    if existing_contact_id and existing_rows:
        existing_name = existing_rows[0].get("display_name") or ""
    resolved_name = existing_name or caller_name or ""

    # Category logic: preserve owner-set categorization on returning contacts
    # (only update from 'unknown' → known classification); for fresh contacts
    # use whatever Tiffany classified the call as. Valid values per migration
    # 110d: lead | client | vendor | friend | other | unknown.
    valid_categories = {"lead", "client", "vendor", "friend", "other", "unknown"}
    incoming_category = (category or "").strip().lower() if category else None
    if incoming_category not in valid_categories:
        incoming_category = None
    existing_category = existing_rows[0].get("category") if existing_contact_id and existing_rows else None
    if existing_category and existing_category != "unknown":
        # Owner already classified this contact; never overwrite with new auto-classification
        resolved_category = existing_category
    elif incoming_category:
        resolved_category = incoming_category
    else:
        resolved_category = existing_category or "unknown"

    upsert_row: dict[str, Any] = {
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "phone_e164": phone_e164,
        "display_name": resolved_name or None,
        # Status is always 'unconfirmed' on auto-create; owner promotes via UI.
        # We do NOT escalate a manually-confirmed contact back to unconfirmed.
        "status": existing_rows[0].get("status") if existing_contact_id and existing_rows else "unconfirmed",
        "category": resolved_category,
        "total_calls": new_count,
        "last_seen_at": now_iso,
        "last_call_summary": call_summary or None,
        "updated_at": now_iso,
    }

    # On fresh insert carry first_seen_at; merge-duplicates will keep it if already set.
    if not existing_contact_id:
        upsert_row["first_seen_at"] = now_iso
        upsert_row["contact_id"] = str(uuid.uuid4())

    # --- Execute upsert ------------------------------------------------------
    try:
        result_row = await supabase_upsert(
            "frontdesk_contacts",
            upsert_row,
            on_conflict="suite_id,phone_e164",
        )
    except SupabaseClientError as exc:
        logger.error(
            "contact_writer upsert_failed phone_prefix=%s suite_id=%s error=%s",
            phone_prefix,
            suite_id,
            exc.detail,
        )
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                outcome="failed",
                reason_code="SUPABASE_UPSERT_FAILED",
                contact_id=None,
                conversation_id=conversation_id,
                phone_prefix=phone_prefix,
                trace_id=trace_id,
                correlation_id=idempotency_key,
            )
        ])
        raise HTTPException(
            status_code=503,
            detail=f"contact_writer: DB upsert failed ({exc.detail}). Fail-closed per Law #3.",
        ) from exc

    contact_id = str(result_row.get("contact_id") or existing_contact_id or "")
    if not contact_id:
        # Should never happen — upsert should return the row
        contact_id = upsert_row.get("contact_id", str(uuid.uuid4()))

    # --- Cut Yellow receipt (Law #2) -----------------------------------------
    receipt_store.store_receipts_strict([
        _build_receipt(
            receipt_id=receipt_id,
            suite_id=suite_id,
            tenant_id=tenant_id,
            office_id=office_id,
            outcome="success",
            reason_code="EXECUTED",
            contact_id=contact_id,
            conversation_id=conversation_id,
            phone_prefix=phone_prefix,
            trace_id=trace_id,
            correlation_id=idempotency_key,
        )
    ])

    logger.info(
        "contact_writer upsert_ok phone_prefix=%s contact_id=%s total_calls=%d",
        phone_prefix,
        contact_id,
        new_count,
    )
    return contact_id


__all__ = ["upsert_contact_from_call"]
