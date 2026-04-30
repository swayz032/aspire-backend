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
from aspire_orchestrator.services.ingestion.sms_ingestion import SMSIngestionAdapter

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
    """STUB — Twilio recording.completed callback (RecordingStatusCallback).

    Subagent (Lane B): implement `CallIngestionAdapter` in
    `services/ingestion/call_ingestion.py`. Required fields per plan §14.C:
    entity (caller_phone resolved to contact), direction, from, to, duration,
    recording_url, outcome (transferred/voicemail/message_taken).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "twilio voice recording-status adapter pending (Pass 14 Lane B)",
        },
    )


@router.post("/twilio/voice/transcription-callback")
async def twilio_voice_transcription_callback(request: Request) -> dict[str, Any]:
    """STUB — Twilio transcription.completed callback.

    Subagent (Lane B): adds `transcription_text` + `outcome` to the existing
    `call` memory_object via supersede pattern.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "twilio voice transcription-callback adapter pending (Pass 14 Lane B)",
        },
    )


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
    """STUB — Stripe events router (invoice.created / invoice.paid / invoice.voided).

    Subagent (Lane A): implement `InvoiceIngestionAdapter` (and sub-routes for
    each Stripe event type if needed). Required fields per plan §14.C: entity
    (customer), amount, due_date, status='draft', line_items[], pdf_url.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "stripe ingestion adapter pending (Pass 14 Lane A)",
        },
    )


# ===========================================================================
# PandaDoc (quotes / contracts)
# ===========================================================================


@router.post("/pandadoc")
async def pandadoc_webhook(request: Request) -> dict[str, Any]:
    """STUB — PandaDoc events router (quote.sent / quote.viewed / quote.accepted/rejected).

    Subagent (Lane A): implement `QuoteIngestionAdapter`. Required fields per
    plan §14.C: entity, amount, expiration, line_items[], pdf_url, quote_number.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "pandadoc ingestion adapter pending (Pass 14 Lane A)",
        },
    )


# ===========================================================================
# ElevenLabs — post-call webhook (per agent voice session)
# ===========================================================================


@router.post("/elevenlabs/post-call")
async def elevenlabs_post_call(request: Request) -> dict[str, Any]:
    """STUB — ElevenLabs post_call_webhook (every voice session for the 6 agents).

    Subagent (Lane D): create both `session_summary` (refined) AND `transcript`
    (raw) memory_objects. Plan §14.C: agent_id, agent_name, runtime_family,
    intents_detected, tools_invoked, correlation_id, duration.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "elevenlabs post-call adapter pending (Pass 14 Lane D)",
        },
    )


# ===========================================================================
# Anam — session-end webhook (Ava-video / Finn-video)
# ===========================================================================


@router.post("/anam/session-end")
async def anam_session_end(request: Request) -> dict[str, Any]:
    """STUB — Anam session-end webhook.

    Subagent (Lane E): same shape as EL but `runtime_family='anam_video'`.
    If `handoff_id` present, link to voice precursor via `linked_memory_ids`.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "anam session-end adapter pending (Pass 14 Lane E)",
        },
    )


# ===========================================================================
# Zoom — recording + transcript
# ===========================================================================


@router.post("/zoom/recording-completed")
async def zoom_recording_completed(request: Request) -> dict[str, Any]:
    """STUB — Zoom recording.completed.

    Subagent (Lane F): create `meeting` memory_object with meeting_id, topic,
    participants, duration, recording_url, host. Body initially null (filled
    by transcript-completed follow-up).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "zoom recording-completed adapter pending (Pass 14 Lane F)",
        },
    )


@router.post("/zoom/transcript-completed")
async def zoom_transcript_completed(request: Request) -> dict[str, Any]:
    """STUB — Zoom transcript.completed.

    Subagent (Lane F): supersede the existing `meeting` memory_object with
    transcript_text + extracted key_decisions[] + action_items[].
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "ok": False,
            "code": "NOT_IMPLEMENTED",
            "message": "zoom transcript-completed adapter pending (Pass 14 Lane F)",
        },
    )


# ---------------------------------------------------------------------------
# Health probe — for monitoring + smoke tests. Always 200; never authed.
# ---------------------------------------------------------------------------


@router.get("/healthz")
async def ingestion_healthz() -> dict[str, Any]:
    """Cheap 200 probe — confirms the ingestion router is mounted."""
    return {
        "ok": True,
        "service": "ingestion",
        "wired_adapters": ["twilio_sms_inbound"],
        "stub_adapters": [
            "twilio_voice_recording_status",
            "twilio_voice_transcription_callback",
            "twilio_sms_status",
            "stripe",
            "pandadoc",
            "elevenlabs_post_call",
            "anam_session_end",
            "zoom_recording_completed",
            "zoom_transcript_completed",
        ],
    }
