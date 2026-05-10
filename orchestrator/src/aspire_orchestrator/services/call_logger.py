"""Call Logger — INSERT a call_sessions row from a completed EL call (Wave 5).

Every receptionist call (voicemail or not) produces exactly one call_sessions row.
The row anchors the contact_id FK so the desktop "Recent Calls" list can join to
frontdesk_contacts and show the caller's name without a separate lookup.

Idempotency is enforced on conversation_id (unique partial index added in migration 114:
  UNIQUE (conversation_id) WHERE conversation_id IS NOT NULL).
If Supabase returns a 409 conflict on that column we treat it as a successful replay —
we look up the existing row and return its call_session_id. This means a webhook
replay never double-counts a call.

Schema mapping (actual call_sessions columns vs. EL post-call data):
  call_session_id  → new UUID generated here
  suite_id         → from resolved EL scope
  owner_office_id  → from resolved EL scope (office_id)
  contact_id       → from contact_writer upsert (nullable)
  direction        → always 'inbound' for receptionist calls
  status           → 'completed' once post-call fires
  provider         → 'elevenlabs'
  provider_call_id → conversation_id (unique per EL call)
  from_number      → caller_id extracted from EL metadata
  to_number        → called_number (Aspire's Twilio number)
  duration_seconds → from metadata.duration_secs
  started_at       → from metadata.start_time_unix_secs (nullable — DB default now())
  ended_at         → from metadata.end_time_unix_secs (nullable)
  recording_url    → from metadata.recording_url
  transcript_summary → from analysis.transcript_summary
  conversation_id  → EL conversation_id (idempotency column)
  data_collection_json → analysis.data_collection_results
  metadata         → extra jsonb blob (DB default '{}')
  business_line_id → resolved from tenant_phone_numbers (nullable per migration fix)

Law compliance:
  Law #2 — Yellow-tier receipt cut for every insert (call_session_logged).
  Law #3 — Missing suite_id or conversation_id → fail-closed (HTTPException 422/503).
  Law #6 — suite_id always from resolved EL scope, never from caller payload.
  Law #9 — phone numbers logged as 6-digit prefix; transcript text not logged.
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
    supabase_insert,
    supabase_select,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "post_call_enrichment"
_RECEIPT_TYPE = "call_session_logged"
_RISK_TIER = "yellow"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _phone_prefix(phone: str) -> str:
    """First-6-digit mask for PII-safe logging (Law #9)."""
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
    call_session_id: str | None,
    conversation_id: str,
    trace_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Build a Yellow-tier receipt for a call_sessions insert (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": receipt_id,
        "receipt_type": _RECEIPT_TYPE,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": f"service.{_TOOL_NAME}",
        "action_type": "call_session_insert",
        "tool_used": _TOOL_NAME,
        "risk_tier": _RISK_TIER,
        "outcome": outcome,
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "redacted_inputs": {
            "conversation_id": conversation_id,
            # from/to phone numbers intentionally omitted — PII, Law #9
        },
        "redacted_outputs": {
            "call_session_id": call_session_id,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def _resolve_business_line_id(
    *,
    suite_id: str,
    to_number: str,
) -> str | None:
    """Look up business_line_id via the business_lines table by matching phone.

    call_sessions.business_line_id is nullable (migration fix applied) so
    returning None is safe — the row will still insert without it.
    """
    if not to_number:
        return None
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            {"phone_number": to_number, "suite_id": suite_id},
            limit=1,
        )
        if rows:
            # tenant_phone_numbers has no business_line_id column directly;
            # business_line_id on call_sessions is nullable after migration fix.
            # For now return None — a follow-up pass can join business_lines via
            # the tenant's suite_id if needed.
            return None
    except SupabaseClientError as exc:
        logger.debug(
            "call_logger business_line_id_lookup_failed to_prefix=%s error=%s",
            _phone_prefix(to_number),
            exc.detail,
        )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def log_call_session(
    *,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    contact_id: str | None,
    conversation_id: str,
    from_number: str,
    to_number: str,
    started_at: datetime | None,
    ended_at: datetime | None,
    duration_seconds: int,
    transcript_summary: str,
    recording_url: str,
    data_collection_json: dict[str, Any] | None,
    trace_id: str = "",
    correlation_id: str = "",
) -> str:
    """INSERT a call_sessions row for a completed EL voice call.

    Maps EL post-call enrichment data to the actual call_sessions schema:
      - PK column is call_session_id (not id)
      - direction is always 'inbound' for receptionist calls
      - status is 'completed' (call is done when post-call fires)
      - provider is 'elevenlabs'
      - provider_call_id maps to conversation_id
      - owner_office_id maps to office_id from scope
      - business_line_id is nullable (migration fix)

    Idempotent on conversation_id — if the row already exists (409 from Supabase),
    we look up the existing row and return its id without cutting a duplicate receipt.

    Returns:
        call_session_id: UUID string of the inserted (or replayed) row.

    Raises:
        HTTPException(422): if suite_id or conversation_id is missing.
        HTTPException(503): if the Supabase insert fails with a non-409 error.
    """
    if not suite_id or not conversation_id:
        raise HTTPException(
            status_code=422,
            detail="log_call_session: suite_id and conversation_id are required",
        )

    receipt_id = str(uuid.uuid4())
    idempotency_key = f"el-call-session:{conversation_id}"
    from_prefix = _phone_prefix(from_number) if from_number else "?"
    to_prefix = _phone_prefix(to_number) if to_number else "?"
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- Resolve business_line_id (best-effort, nullable) --------------------
    business_line_id = await _resolve_business_line_id(
        suite_id=suite_id,
        to_number=to_number,
    )

    # --- Build insert row using correct call_sessions column names -----------
    call_session_id = str(uuid.uuid4())
    row: dict[str, Any] = {
        "call_session_id": call_session_id,
        "suite_id": suite_id,
        # NOTE: call_sessions uses owner_office_id, not office_id
        "owner_office_id": office_id,
        "contact_id": contact_id or None,
        "conversation_id": conversation_id,
        "from_number": from_number or None,
        "to_number": to_number or None,
        "direction": "inbound",          # EL receptionist calls are always inbound
        "status": "completed",           # Post-call webhook fires after call ends
        "provider": "elevenlabs",
        "provider_call_id": conversation_id,
        "duration_seconds": duration_seconds,
        "transcript_summary": transcript_summary or None,
        "recording_url": recording_url or None,
        "data_collection_json": data_collection_json or None,
        "metadata": {},
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    # started_at/ended_at: DB defaults to now() but we set explicitly if available
    if started_at is not None:
        row["started_at"] = started_at.isoformat()
    if ended_at is not None:
        row["ended_at"] = ended_at.isoformat()
    # business_line_id is nullable (migration fix); only set when resolved
    if business_line_id:
        row["business_line_id"] = business_line_id

    # --- Execute insert -------------------------------------------------------
    try:
        await supabase_insert("call_sessions", row)
    except SupabaseClientError as exc:
        # 409 Conflict == duplicate conversation_id — idempotent replay
        if exc.status_code == 409:
            logger.debug(
                "call_logger idempotent_replay conversation_id=%s",
                conversation_id,
            )
            # Fetch the existing row to return its id
            try:
                existing = await supabase_select(
                    "call_sessions",
                    {"conversation_id": conversation_id, "suite_id": suite_id},
                    limit=1,
                )
                if existing:
                    return str(
                        existing[0].get("call_session_id")
                        or existing[0].get("id")
                        or call_session_id
                    )
            except SupabaseClientError:
                pass
            return call_session_id  # Fallback: return what we generated

        logger.error(
            "call_logger insert_failed from_prefix=%s to_prefix=%s conversation_id=%s error=%s",
            from_prefix,
            to_prefix,
            conversation_id,
            exc.detail,
        )
        receipt_store.store_receipts_strict([
            _build_receipt(
                receipt_id=receipt_id,
                suite_id=suite_id,
                tenant_id=tenant_id,
                office_id=office_id,
                outcome="failed",
                reason_code="SUPABASE_INSERT_FAILED",
                call_session_id=None,
                conversation_id=conversation_id,
                trace_id=trace_id,
                correlation_id=idempotency_key,
            )
        ])
        raise HTTPException(
            status_code=503,
            detail=f"call_logger: DB insert failed ({exc.detail}). Fail-closed per Law #3.",
        ) from exc

    # --- Cut Yellow receipt (Law #2) -----------------------------------------
    receipt_store.store_receipts_strict([
        _build_receipt(
            receipt_id=receipt_id,
            suite_id=suite_id,
            tenant_id=tenant_id,
            office_id=office_id,
            outcome="success",
            reason_code="EXECUTED",
            call_session_id=call_session_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            correlation_id=idempotency_key,
        )
    ])

    logger.info(
        "call_logger insert_ok conversation_id=%s from_prefix=%s to_prefix=%s duration=%ds",
        conversation_id,
        from_prefix,
        to_prefix,
        duration_seconds,
    )
    return call_session_id


__all__ = ["log_call_session"]
