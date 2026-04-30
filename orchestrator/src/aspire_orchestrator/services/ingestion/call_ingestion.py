"""Twilio voice call ingestion — two-event lifecycle → `memory_objects` of type `call`.

Pass 14 Lane B adapter.

Twilio fires two form-encoded webhooks per call:

  1. `RecordingStatusCallback` (recording.completed) — fires when the recording
     .mp3 is ready. We create the initial `call` memory_object at this point.
     Fields: CallSid, RecordingSid, AccountSid, RecordingUrl, RecordingStatus,
     RecordingDuration, RecordingChannels, RecordingSource, From, To.

  2. `TranscribeCallback` (transcription.completed) — fires when Twilio's
     Transcription service finishes. We write a SECOND memory_object that
     supersedes the recording row via `detail.supersedes_idempotency_key`.
     Fields: CallSid, RecordingSid, TranscriptionSid, AccountSid,
     TranscriptionText, TranscriptionStatus, TranscriptionUrl, From, To.

Scope resolution: `tenant_phone_numbers.phone_number = To` lookup.
Until Pass 16 lands, this table may not exist — both adapters raise
`TENANT_PHONE_NUMBERS_UNAVAILABLE` (503) so Twilio retries once it's ready.

memory_type = 'call' per migration 101 / plan §14.C.

Idempotency:
  Recording:      "twilio-call-recording:{CallSid}:{RecordingSid}"
  Transcription:  "twilio-call-transcription:{CallSid}:{TranscriptionSid}"

Trace + correlation are stable across both events (keyed on CallSid) so the
two rows share the same trace in receipts and the memory pipeline.

Law compliance:
  Law #2: MemoryService.write cuts receipts internally.
  Law #3: bad signature → 401; missing scope → 422/503.
  Law #6: scope resolved from payload (To-number), never from request headers.
  Law #9: TranscriptionText is NOT logged at full fidelity (first 80 chars only).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
    ThreadOut,
)
from aspire_orchestrator.services.ingestion.base import (
    BaseIngestionAdapter,
    IngestionError,
)
from aspire_orchestrator.services.ingestion.signatures import verify_twilio
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

# UUID namespace shared by all call trace/correlation generators
_NS = uuid.NAMESPACE_URL


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_direction(from_number: str, to_number: str, tenant_number: str) -> str:
    """Return 'inbound' if the tenant's number is the destination, else 'outbound'."""
    return "inbound" if to_number == tenant_number else "outbound"


async def _resolve_call_scope(payload: dict[str, Any]) -> tuple[ScopedIdentity, str]:
    """Lookup To-number in tenant_phone_numbers; return (scope, tenant_number).

    Shared by both adapters.  Raises IngestionError on all failure paths.
    """
    to_number = payload.get("To") or payload.get("to")
    if not to_number:
        raise IngestionError(
            "Twilio voice payload missing 'To' field",
            code="MISSING_TO_NUMBER",
            status_code=422,
        )
    try:
        rows = await supabase_select(
            table="tenant_phone_numbers",
            filter_="phone_number=eq." + to_number,
            limit=1,
        )
    except SupabaseClientError as exc:
        # Pass 14 → Pass 16 dependency: table not yet provisioned.
        # 503 causes Twilio to retry once it exists.
        raise IngestionError(
            f"tenant_phone_numbers query failed (Pass 16 prereq): {exc.detail}",
            code="TENANT_PHONE_NUMBERS_UNAVAILABLE",
            status_code=503,
        ) from exc
    if not rows:
        raise IngestionError(
            f"To-number {to_number} not registered in tenant_phone_numbers",
            code="UNKNOWN_NUMBER",
            status_code=404,
        )
    row = rows[0]
    scope = ScopedIdentity(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
    )
    return scope, to_number


def _stable_ids(call_sid: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (trace_id, correlation_id) stable across both call events."""
    trace_id = uuid.uuid5(_NS, f"twilio-call:trace:{call_sid}")
    correlation_id = uuid.uuid5(_NS, f"twilio-call:corr:{call_sid}")
    return trace_id, correlation_id


def _verify_twilio_sig(headers: Mapping[str, str]) -> tuple[str, str, dict[str, str] | None]:
    """Extract (sig, url, params) from injected headers for verify_twilio call."""
    sig = headers.get("X-Twilio-Signature") or headers.get("x-twilio-signature", "")
    full_url = headers.get("X-Aspire-Webhook-Url", "")
    import json as _json
    params_json = headers.get("X-Aspire-Form-Params", "")
    params: dict[str, str] | None = _json.loads(params_json) if params_json else None
    return sig, full_url, params


# ---------------------------------------------------------------------------
# Adapter 1 — CallRecordingIngestionAdapter
# ---------------------------------------------------------------------------


class CallRecordingIngestionAdapter(BaseIngestionAdapter):
    """Twilio RecordingStatusCallback → initial `call` memory_object.

    Fires when the recording .mp3 is ready. Creates the memory row with
    duration, recording_url, direction, from/to. Transcript fields are null
    at this stage — they arrive in the transcription event.
    """

    provider_name = "twilio_voice_recording"
    memory_type = "call"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Twilio HMAC SHA-1 of full URL + sorted form params."""
        sig, full_url, params = _verify_twilio_sig(headers)
        return verify_twilio(
            full_url=full_url,
            params=params,
            sig_header=sig,
            auth_token=settings.twilio_auth_token,
        )

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        scope, _ = await _resolve_call_scope(payload)
        return scope

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build `call` memory_object from RecordingStatusCallback payload."""
        call_sid = payload.get("CallSid") or payload.get("call_sid")
        recording_sid = payload.get("RecordingSid") or payload.get("recording_sid")
        from_number = payload.get("From") or payload.get("from")
        to_number = payload.get("To") or payload.get("to")
        recording_url = payload.get("RecordingUrl") or payload.get("recording_url") or ""
        recording_status = payload.get("RecordingStatus") or payload.get("recording_status", "")
        channels = payload.get("RecordingChannels") or payload.get("recording_channels", "1")

        # Duration arrives as a string integer representing seconds
        raw_duration = payload.get("RecordingDuration") or payload.get("recording_duration", "0")
        try:
            duration_seconds = int(raw_duration)
        except (TypeError, ValueError):
            duration_seconds = 0

        if not call_sid:
            raise IngestionError(
                "Twilio voice payload missing CallSid",
                code="MISSING_CALL_SID",
                status_code=422,
            )
        if not recording_sid:
            raise IngestionError(
                "Twilio voice payload missing RecordingSid",
                code="MISSING_RECORDING_SID",
                status_code=422,
            )
        if not from_number or not to_number:
            raise IngestionError(
                "Twilio voice payload missing From/To",
                code="MISSING_PHONE_NUMBERS",
                status_code=422,
            )

        # Scope was resolved from to_number — use it for direction
        direction = _resolve_direction(from_number, to_number, to_number)

        trace_id, correlation_id = _stable_ids(call_sid)

        title = (
            f"Call from {from_number}"
            if direction == "inbound"
            else f"Call to {to_number}"
        )
        summary = f"{duration_seconds}s {direction} call — recording ready"

        # Enforce .mp3 URL suffix for direct playback compatibility
        if recording_url and not recording_url.endswith(".mp3"):
            recording_url = recording_url + ".mp3"

        idempotency_key = f"twilio-call-recording:{call_sid}:{recording_sid}"

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="provider_webhook",
                channel="voice",
                source_record_id=call_sid,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="call",
            entity_type="phone_contact",
            entity_id=None,  # Pass 16 will resolve to contact UUID
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail={
                "direction": direction,
                "from": from_number,
                "to": to_number,
                "duration_seconds": duration_seconds,
                "recording_url": recording_url,
                "recording_sid": recording_sid,
                "call_sid": call_sid,
                "channels": int(channels) if channels else 1,
                "recording_status": recording_status,
                # Filled in by transcription event
                "outcome": None,
                "transcription_text": None,
            },
            confidence=None,
            visibility_scope="office",
            status=None,  # Calls are not approval-gated
            event_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# Adapter 2 — CallTranscriptionIngestionAdapter
# ---------------------------------------------------------------------------


class CallTranscriptionIngestionAdapter(BaseIngestionAdapter):
    """Twilio TranscribeCallback → superseding `call` memory_object.

    Fires when transcription is ready. Writes a NEW memory_object with the
    same trace/correlation as the recording row. The `detail.supersedes_
    idempotency_key` field links back to the recording row for traceability
    (Law #2 append-only — no UPDATE on the original).

    TranscriptionStatus can be 'completed' or 'failed'. Both paths write a
    memory_object so that the failure is also auditable. On 'failed', outcome
    is set to 'transcription_failed' and transcription_text is null.
    """

    provider_name = "twilio_voice_transcription"
    memory_type = "call"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Twilio HMAC SHA-1 of full URL + sorted form params."""
        sig, full_url, params = _verify_twilio_sig(headers)
        return verify_twilio(
            full_url=full_url,
            params=params,
            sig_header=sig,
            auth_token=settings.twilio_auth_token,
        )

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        scope, _ = await _resolve_call_scope(payload)
        return scope

    def _infer_outcome(self, transcription_text: str) -> str:
        """Heuristic outcome from transcript content.

        'voicemail' if transcript contains voicemail-related keywords,
        'completed' otherwise.  This is intentionally conservative — more
        sophisticated NLP belongs in Pass 18's post-processing pipeline.
        """
        if not transcription_text:
            return "completed"
        lower = transcription_text.lower()
        voicemail_signals = ("voicemail", "leave a message", "after the beep", "at the tone")
        if any(sig in lower for sig in voicemail_signals):
            return "voicemail"
        return "completed"

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build superseding `call` memory_object from TranscribeCallback payload."""
        call_sid = payload.get("CallSid") or payload.get("call_sid")
        recording_sid = payload.get("RecordingSid") or payload.get("recording_sid")
        transcription_sid = payload.get("TranscriptionSid") or payload.get("transcription_sid")
        from_number = payload.get("From") or payload.get("from")
        to_number = payload.get("To") or payload.get("to")
        transcription_text = payload.get("TranscriptionText") or payload.get("transcription_text") or ""
        transcription_status = payload.get("TranscriptionStatus") or payload.get("transcription_status", "completed")
        transcription_url = payload.get("TranscriptionUrl") or payload.get("transcription_url") or ""

        if not call_sid:
            raise IngestionError(
                "Twilio transcription payload missing CallSid",
                code="MISSING_CALL_SID",
                status_code=422,
            )
        if not transcription_sid:
            raise IngestionError(
                "Twilio transcription payload missing TranscriptionSid",
                code="MISSING_TRANSCRIPTION_SID",
                status_code=422,
            )
        if not from_number or not to_number:
            raise IngestionError(
                "Twilio transcription payload missing From/To",
                code="MISSING_PHONE_NUMBERS",
                status_code=422,
            )

        direction = _resolve_direction(from_number, to_number, to_number)
        trace_id, correlation_id = _stable_ids(call_sid)

        # Outcome: null on 'failed', heuristic on 'completed'
        transcription_failed = transcription_status == "failed"
        if transcription_failed:
            outcome = "transcription_failed"
            effective_text: str | None = None
        else:
            effective_text = transcription_text or None
            outcome = self._infer_outcome(transcription_text)

        # Title and summary
        title = f"Call transcript — {from_number}"
        if effective_text:
            # First 140 chars, never log the full body (Law #9)
            summary = (effective_text[:140] + "…") if len(effective_text) > 140 else effective_text
        elif transcription_failed:
            summary = "Transcription failed — recording available"
        else:
            summary = "Transcription complete — no text returned"

        # Log only a truncated preview (Law #9 — no full transcript in logs)
        preview = (transcription_text[:80] + "…") if len(transcription_text) > 80 else transcription_text
        logger.debug(
            "ingestion_transcription_preview call_sid=%s status=%s preview=%r",
            call_sid,
            transcription_status,
            preview,
        )

        # Recording SID may be absent from the transcription webhook in some
        # Twilio configurations — store what we have for the supersede reference
        recording_idempotency_ref = (
            f"twilio-call-recording:{call_sid}:{recording_sid}"
            if recording_sid
            else f"twilio-call-recording:{call_sid}:unknown"
        )

        idempotency_key = f"twilio-call-transcription:{call_sid}:{transcription_sid}"

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="provider_webhook",
                channel="voice",
                source_record_id=call_sid,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="call",
            entity_type="phone_contact",
            entity_id=None,  # Pass 16 resolves to contact UUID
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail={
                "direction": direction,
                "from": from_number,
                "to": to_number,
                "call_sid": call_sid,
                "recording_sid": recording_sid,
                "transcription_sid": transcription_sid,
                "transcription_url": transcription_url,
                "transcription_status": transcription_status,
                "transcription_text": effective_text,
                "outcome": outcome,
                # Supersede traceability — links back to the recording row
                # (Law #2: no UPDATE; this is a separate append-only write)
                "supersedes_idempotency_key": recording_idempotency_ref,
            },
            confidence=None,
            visibility_scope="office",
            status="executed",  # Call is terminal and immutable at this point
            event_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )


__all__ = [
    "CallRecordingIngestionAdapter",
    "CallTranscriptionIngestionAdapter",
]
