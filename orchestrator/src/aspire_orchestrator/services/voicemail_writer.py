"""Voicemail Writer — INSERT a frontdesk_voicemails row (Wave 5).

Only called when the receptionist's EL `take_message` data-collection tool fired
AND a callback_number was captured. The caller (elevenlabs_ingestion.py) makes
this determination — this function just inserts the structured row.

data_collection_results is the dict ElevenLabs populates from the 6 configured
data-collection fields. We map it to explicit typed columns AND store the raw JSON
in data_collection_json for forensic recovery if the mapping logic ever changes.

Schema columns populated here (migration 114):
  caller_name, callback_number, call_reason, call_summary, urgency,
  wants_callback, preferred_callback_window, contact_id FK, call_session_id FK,
  data_collection_json, audio_uri, transcript_status.

Idempotency: enforced on conversation_id. 409 from Supabase = replay, return
existing row id.

Law compliance:
  Law #2 — Yellow-tier receipt: voicemail_received.
  Law #3 — Missing suite_id or conversation_id → HTTPException 422/503.
  Law #6 — suite_id from EL scope, never from caller payload.
  Law #9 — callback_number logged as 6-digit prefix; caller_name not logged.
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
_RECEIPT_TYPE = "voicemail_received"
_RISK_TIER = "yellow"

# EL data-collection field name → DB column name
_DC_FIELD_MAP: dict[str, str] = {
    "caller_name": "caller_name",
    "callback_number": "callback_number",
    "call_reason": "call_reason",
    "call_summary": "call_summary",
    "urgency": "urgency",
    "wants_callback": "wants_callback",
    "preferred_callback_window": "preferred_callback_window",
}

# Accepted urgency values (DB CHECK constraint)
_VALID_URGENCY = frozenset({"low", "medium", "high"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _phone_prefix(phone: str) -> str:
    """First-6-digit mask for PII-safe logging (Law #9)."""
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    return digits[:6] + "..." if len(digits) >= 6 else digits + "..."


def _coerce_bool(value: Any) -> bool | None:
    """Coerce EL data-collection string booleans to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    if isinstance(value, int):
        return bool(value)
    return None


def _extract_dc_fields(
    data_collection_results: dict[str, Any],
) -> dict[str, Any]:
    """Map EL data_collection_results to typed DB column values.

    EL returns either direct key-value pairs or nested dicts with a 'value' key,
    depending on the agent's data-collection configuration version. We handle both
    shapes here so the pipeline is robust to EL API changes.
    """
    resolved: dict[str, Any] = {}
    for el_key, db_col in _DC_FIELD_MAP.items():
        raw = data_collection_results.get(el_key)
        # EL v2 nesting: {"value": "...", "reasoning": "..."}
        if isinstance(raw, dict):
            raw = raw.get("value")
        if raw is None:
            continue
        if db_col == "wants_callback":
            coerced = _coerce_bool(raw)
            if coerced is not None:
                resolved[db_col] = coerced
        elif db_col == "urgency":
            urgency_str = str(raw).strip().lower()
            if urgency_str in _VALID_URGENCY:
                resolved[db_col] = urgency_str
            else:
                # Default unknown urgency to medium — never fail on bad EL value
                resolved[db_col] = "medium"
        else:
            resolved[db_col] = str(raw).strip() or None
    return resolved


def _build_receipt(
    *,
    receipt_id: str,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    outcome: str,
    reason_code: str,
    voicemail_id: str | None,
    conversation_id: str,
    trace_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Build a Yellow-tier receipt for a voicemail insert (Law #2)."""
    return {
        "id": receipt_id,
        "receipt_type": _RECEIPT_TYPE,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": f"service.{_TOOL_NAME}",
        "action_type": "voicemail_insert",
        "tool_used": _TOOL_NAME,
        "risk_tier": _RISK_TIER,
        "outcome": outcome,
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "redacted_inputs": {
            "conversation_id": conversation_id,
            # callback_number and caller_name intentionally omitted — PII, Law #9
        },
        "redacted_outputs": {
            "voicemail_id": voicemail_id,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def write_voicemail(
    *,
    suite_id: str,
    tenant_id: str,
    office_id: str,
    contact_id: str | None,
    call_session_id: str | None,
    from_e164: str,
    to_e164: str,
    duration_seconds: int,
    recording_uri: str,
    transcript_text: str,
    data_collection_results: dict[str, Any],
    conversation_id: str,
    trace_id: str = "",
    correlation_id: str = "",
) -> str:
    """INSERT a frontdesk_voicemails row from a completed take-message call.

    Maps EL data_collection_results to typed columns and stores the raw JSON for
    forensic recovery. transcript_status is set to 'transcribed' when transcript_text
    is non-empty, otherwise 'pending' to signal that async transcription is needed.

    The caller (elevenlabs_ingestion.py enrichment block) is responsible for
    deciding whether a voicemail should be written — this function assumes the
    decision has been made and just does the insert.

    Returns:
        voicemail_id: UUID string of the inserted row.

    Raises:
        HTTPException(422): missing required identifiers.
        HTTPException(503): DB insert failed (fail-closed, Law #3).
    """
    if not suite_id or not conversation_id:
        raise HTTPException(
            status_code=422,
            detail="write_voicemail: suite_id and conversation_id are required",
        )

    receipt_id = str(uuid.uuid4())
    idempotency_key = f"el-voicemail:{conversation_id}"
    from_prefix = _phone_prefix(from_e164) if from_e164 else "?"
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- Map data-collection fields ------------------------------------------
    dc_fields = _extract_dc_fields(data_collection_results)

    # --- Build insert row ----------------------------------------------------
    voicemail_id = str(uuid.uuid4())
    transcript_status = "transcribed" if transcript_text and transcript_text.strip() else "pending"

    row: dict[str, Any] = {
        "id": voicemail_id,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
        "contact_id": contact_id or None,
        "call_session_id": call_session_id or None,
        "from_e164": from_e164 or None,
        "to_e164": to_e164 or None,
        "duration_seconds": duration_seconds,
        "recording_uri": recording_uri or None,
        "transcript_text": transcript_text or None,
        "transcript_status": transcript_status,
        "data_collection_json": data_collection_results or None,
        "conversation_id": conversation_id,
        "created_at": now_iso,
        **dc_fields,
    }

    # --- Execute insert -------------------------------------------------------
    try:
        await supabase_insert("frontdesk_voicemails", row)
    except SupabaseClientError as exc:
        # 409 Conflict == duplicate conversation_id — idempotent replay
        if exc.status_code == 409:
            logger.debug(
                "voicemail_writer idempotent_replay conversation_id=%s",
                conversation_id,
            )
            try:
                existing = await supabase_select(
                    "frontdesk_voicemails",
                    {"conversation_id": conversation_id, "suite_id": suite_id},
                    limit=1,
                )
                if existing:
                    return str(existing[0].get("id") or existing[0].get("voicemail_id") or voicemail_id)
            except SupabaseClientError:
                pass
            return voicemail_id

        logger.error(
            "voicemail_writer insert_failed from_prefix=%s conversation_id=%s error=%s",
            from_prefix,
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
                voicemail_id=None,
                conversation_id=conversation_id,
                trace_id=trace_id,
                correlation_id=idempotency_key,
            )
        ])
        raise HTTPException(
            status_code=503,
            detail=f"voicemail_writer: DB insert failed ({exc.detail}). Fail-closed per Law #3.",
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
            voicemail_id=voicemail_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            correlation_id=idempotency_key,
        )
    ])

    logger.info(
        "voicemail_writer insert_ok from_prefix=%s voicemail_id=%s transcript_status=%s",
        from_prefix,
        voicemail_id,
        transcript_status,
    )
    return voicemail_id


__all__ = ["write_voicemail"]
