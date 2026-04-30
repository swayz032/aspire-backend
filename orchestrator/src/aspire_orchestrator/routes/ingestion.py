"""Ingestion routes — every important business artifact webhook lands here.

Pass 14 of the Office Memory Engine plan (the-image-was-off-calm-lynx).

Mounted in `server.py`. Each route:
  1. Receives the raw webhook from a provider (Stripe / Twilio / EL / Anam / Zoom / PandaDoc).
  2. Reads the body bytes (signature verification needs them raw).
  3. Parses headers + payload.
  4. Hands off to the corresponding `*IngestionAdapter` from `services.ingestion`.
  5. Maps `IngestionError.status_code` to HTTP status.
  6. Returns minimal JSON (`{ok: true, memory_id: ...}` or `{ok: false, code: ...}`).

Aspire Laws:
  - Law #2: every successful ingest cuts a receipt inside `MemoryService.write`.
  - Law #3: bad signatures → 401 (fail-closed).
  - Law #6: scope is resolved from payload — never from request headers (no
    `X-Tenant-Id` trust on webhook routes; webhooks come from upstream
    providers, not authenticated browser clients).

Status of adapters as of Pass 14 first commit:
  - twilio_sms_inbound       : WIRED (reference adapter)
  - twilio_voice_recording   : STUB (subagent-mcp-toolsmith Lane B)
  - twilio_voice_transcription: STUB (subagent-mcp-toolsmith Lane B)
  - twilio_sms_status        : STUB (subagent-mcp-toolsmith Lane G)
  - stripe                   : STUB (subagent-mcp-toolsmith Lane A)
  - pandadoc                 : STUB (subagent-mcp-toolsmith Lane A)
  - elevenlabs_post_call     : STUB (subagent-mcp-toolsmith Lane D)
  - anam_session_end         : STUB (subagent-mcp-toolsmith Lane E)
  - zoom_recording_completed : STUB (subagent-mcp-toolsmith Lane F)
  - zoom_transcript_completed: STUB (subagent-mcp-toolsmith Lane F)

STUB routes return 501 Not Implemented with `{ok:false, code:'NOT_IMPLEMENTED'}`
so the routes are mounted + introspectable from day one. Subagents replace
each stub with a working adapter call; no route table changes needed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from aspire_orchestrator.services.ingestion import (
    BaseIngestionAdapter,
    IngestionError,
)
from aspire_orchestrator.services.ingestion.anam_ingestion import AnamIngestionAdapter
from aspire_orchestrator.services.ingestion.call_ingestion import (
    CallRecordingIngestionAdapter,
    CallTranscriptionIngestionAdapter,
)
from aspire_orchestrator.services.ingestion.elevenlabs_ingestion import ElevenLabsIngestionAdapter
from aspire_orchestrator.services.ingestion.invoice_ingestion import InvoiceIngestionAdapter
from aspire_orchestrator.services.ingestion.quote_ingestion import QuoteIngestionAdapter
from aspire_orchestrator.services.ingestion.sms_ingestion import SMSIngestionAdapter
from aspire_orchestrator.services.ingestion.zoom_ingestion import (
    ZoomRecordingIngestionAdapter,
    ZoomTranscriptIngestionAdapter,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingest", tags=["ingestion"])


# ---------------------------------------------------------------------------
# Common dispatch helper
# ---------------------------------------------------------------------------


async def _dispatch(
    adapter: BaseIngestionAdapter,
    *,
    request: Request,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run an adapter end-to-end and shape the HTTP response.

    Always returns a JSON-serializable dict. Raises HTTPException on adapter
    failure so FastAPI maps to the right status code.
    """
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    # Inject the full request URL so Twilio signature verification can
    # reconstruct the canonical signing string.
    headers["x-aspire-webhook-url"] = str(request.url)
    # Provide normalized header names too (Twilio header names vary by case)
    if "x-twilio-signature" in headers and "X-Twilio-Signature" not in headers:
        headers["X-Twilio-Signature"] = headers["x-twilio-signature"]
    try:
        result = await adapter.ingest(body=body, headers=headers, payload=payload)
    except IngestionError as exc:
        logger.warning(
            "ingestion_route_error provider=%s code=%s status=%d",
            adapter.provider_name,
            exc.code,
            exc.status_code,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"ok": False, "code": exc.code, "message": str(exc)},
        ) from exc
    return {
        "ok": True,
        "memory_id": str(result.memory.memory_id),
        "memory_type": result.memory.memory_type,
        "deduplicated": result.deduplicated,
    }


# ===========================================================================
# Twilio — voice
# ===========================================================================


@router.post("/twilio/voice/recording-status")
async def twilio_voice_recording_status(request: Request) -> dict[str, Any]:
    """Twilio RecordingStatusCallback → initial `call` memory_object.

    Fires when the .mp3 recording is ready. Creates the `call` row with
    direction, duration, recording_url. Transcript fields are null at this
    stage — they arrive via the transcription-callback below.
    Form-encoded payload; signature verified via X-Twilio-Signature.
    """
    form = await request.form()
    payload = {k: str(v) for k, v in form.items()}
    return await _dispatch(CallRecordingIngestionAdapter(), request=request, payload=payload)


@router.post("/twilio/voice/transcription-callback")
async def twilio_voice_transcription_callback(request: Request) -> dict[str, Any]:
    """Twilio TranscribeCallback → superseding `call` memory_object.

    Fires when transcription is ready. Writes a NEW append-only row with
    transcription_text + outcome, linking back to the recording row via
    detail.supersedes_idempotency_key (Law #2 — no UPDATE).
    Form-encoded payload; signature verified via X-Twilio-Signature.
    """
    form = await request.form()
    payload = {k: str(v) for k, v in form.items()}
    return await _dispatch(CallTranscriptionIngestionAdapter(), request=request, payload=payload)


# ===========================================================================
# Twilio — SMS (REFERENCE ADAPTER, fully wired)
# ===========================================================================


@router.post("/twilio/sms/inbound")
async def twilio_sms_inbound(request: Request) -> dict[str, Any]:
    """Twilio inbound SMS → memory_objects (type='sms_thread').

    Twilio sends form-encoded bodies. We parse with `request.form()` then pass
    the dict to `SMSIngestionAdapter.ingest`. Signature verification reads the
    raw body via `request.body()` inside the dispatch helper.
    """
    form = await request.form()
    payload = {k: str(v) for k, v in form.items()}
    return await _dispatch(SMSIngestionAdapter(), request=request, payload=payload)


@router.post("/twilio/sms/status")
async def twilio_sms_status(request: Request) -> dict[str, Any]:
    """STUB — Twilio outbound SMS status callback (delivered / failed / undelivered).

    Subagent (Lane G — Pass 16 prereq): updates `sms_messages.status` for
    outbound messages. Pass 14 only handles inbound.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "twilio sms status adapter pending (Pass 16 — sms_messages table)",
        },
    )


# ===========================================================================
# Stripe (financial)
# ===========================================================================


@router.post("/stripe")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    """Stripe invoice events → memory_objects (type='invoice').

    Handles invoice.created / invoice.paid / invoice.voided. Stripe sends JSON
    bodies. Signature verified via Stripe-Signature header (HMAC SHA-256).
    """
    payload = await request.json()
    return await _dispatch(InvoiceIngestionAdapter(), request=request, payload=payload)


# ===========================================================================
# PandaDoc (quotes / contracts)
# ===========================================================================


@router.post("/pandadoc")
async def pandadoc_webhook(request: Request) -> dict[str, Any]:
    """PandaDoc document_state_changed events → memory_objects (type='quote').

    Handles sent / viewed / completed / declined states. Signature verified via
    X-PandaDoc-Signature header (SHA-256 HMAC of raw body, hex-encoded).
    """
    payload = await request.json()
    return await _dispatch(QuoteIngestionAdapter(), request=request, payload=payload)


# ===========================================================================
# ElevenLabs — post-call webhook (per agent voice session)
# ===========================================================================


@router.post("/elevenlabs/post-call")
async def elevenlabs_post_call(request: Request) -> dict[str, Any]:
    """ElevenLabs post_call_webhook → `transcript` + `session_summary` memory_objects.

    Fires after every completed voice session for the 6 agents (Ava, Finn, Eli,
    Nora, Receptionist Sarah, Front Desk Sarah). JSON body; signature verified
    via ElevenLabs-Signature header (t=...,v0=... HMAC SHA-256).

    Two memory_objects per session (both idempotent on conversation_id):
      - transcript (raw turns)
      - session_summary (refined, links to transcript via linked_memory_ids)
    Returns the session_summary memory_id.
    """
    payload = await request.json()
    return await _dispatch(ElevenLabsIngestionAdapter(), request=request, payload=payload)


# ===========================================================================
# Anam — session-end webhook (Ava-video / Finn-video)
# ===========================================================================


@router.post("/anam/session-end")
async def anam_session_end(request: Request) -> dict[str, Any]:
    """Anam session.ended webhook → `transcript` + `session_summary` memory_objects.

    Fires after every Ava-video / Finn-video persona session. JSON body;
    signature verified via X-Anam-Signature header (hex SHA-256 HMAC of body).

    If metadata.handoff_id is present, the session_summary linked_memory_ids
    will include the prior voice session memory_object (voice → video chain).
    Returns the session_summary memory_id.
    """
    payload = await request.json()
    return await _dispatch(AnamIngestionAdapter(), request=request, payload=payload)


# ===========================================================================
# Zoom — recording + transcript
# ===========================================================================


@router.post("/zoom/recording-completed")
async def zoom_recording_completed(request: Request) -> dict[str, Any]:
    """Zoom recording.completed → initial `meeting` memory_object.

    Creates the meeting row with topic, duration, participant_count, and the
    recording_files list. Status is null — transcript fields are filled by the
    transcript-completed event below. JSON body; signature verified via
    X-Zm-Signature + X-Zm-Request-Timestamp headers (HMAC SHA-256).
    """
    payload = await request.json()
    return await _dispatch(ZoomRecordingIngestionAdapter(), request=request, payload=payload)


@router.post("/zoom/transcript-completed")
async def zoom_transcript_completed(request: Request) -> dict[str, Any]:
    """Zoom recording.transcript_completed → enriched `meeting` memory_object.

    Creates a NEW append-only memory_object (Law #2 — no UPDATE) with
    transcript_text / transcript_download_url and linked_memory_ids referencing
    the recording row. JSON body; signature verified via X-Zm-Signature +
    X-Zm-Request-Timestamp headers (HMAC SHA-256).
    """
    payload = await request.json()
    return await _dispatch(ZoomTranscriptIngestionAdapter(), request=request, payload=payload)


# ---------------------------------------------------------------------------
# Health probe — for monitoring + smoke tests. Always 200; never authed.
# ---------------------------------------------------------------------------


@router.get("/healthz")
async def ingestion_healthz() -> dict[str, Any]:
    """Cheap 200 probe — confirms the ingestion router is mounted."""
    return {
        "ok": True,
        "service": "ingestion",
        "wired_adapters": [
            "twilio_sms_inbound",
            "twilio_voice_recording_status",
            "twilio_voice_transcription_callback",
            "stripe",
            "pandadoc",
            "elevenlabs_post_call",
            "anam_session_end",
            "zoom_recording_completed",
            "zoom_transcript_completed",
        ],
        "stub_adapters": [
            "twilio_sms_status",
        ],
    }
