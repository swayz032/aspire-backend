"""ElevenLabs phone-number import + attach service (Pass 16 — §16.C).

Verified endpoint contract (EL OpenAPI spec, 2026-04-29):
  POST  /v1/convai/phone-numbers        — import a Twilio number
  PATCH /v1/convai/phone-numbers/{id}   — update assigned agent
  GET   /v1/convai/phone-numbers        — list phone numbers
  GET   /v1/convai/phone-numbers/{id}   — get phone number
  DELETE /v1/convai/phone-numbers/{id}  — delete phone number

Notes:
  - `supports_inbound` / `supports_outbound` are DEPRECATED — not sent.
  - `provider` must be 'twilio' when importing from Twilio.
  - EL auto-writes Twilio voice_url to its media plane on successful import.
  - Idempotent: if already imported, GET existing record and return its ID.

Law compliance:
  Law #3 — fail closed on missing API key.
  Law #9 — API key never logged; only 8-char prefix in debug logs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

# Verified 2026-04-29 via `mcp__elevenlabs__list_agents`
SARAH_RECEPTIONIST_AGENT_ID = "agent_6501kp71h69jfqysgd055hemqhrq"

_EL_BASE_URL = "https://api.elevenlabs.io"
_TIMEOUT_SECONDS = 4.5  # <5s per Law #10 reliability standard


def _el_headers() -> dict[str, str]:
    """Build ElevenLabs API request headers. Fail closed if API key missing."""
    key = settings.elevenlabs_api_key
    if not key:
        raise ElevenLabsPhoneError(
            "MISSING_API_KEY",
            "ElevenLabs API key not configured (ASPIRE_ELEVENLABS_API_KEY). "
            "Fail-closed per Law #3.",
        )
    return {
        "xi-api-key": key,
        "Content-Type": "application/json",
    }


class ElevenLabsPhoneError(Exception):
    """Raised on ElevenLabs phone-number API failures."""

    def __init__(self, code: str, message: str, status_code: int = 0) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


async def import_to_elevenlabs(
    phone_number: str,
    label: str,
    twilio_sid: str,
    twilio_token: str,
) -> str:
    """Import a Twilio number into ElevenLabs Conversational AI.

    POST /v1/convai/phone-numbers with {phone_number, label, provider:'twilio', sid, token}.
    Returns the EL phone_number_id (format: 'pn_...').

    Idempotent: if already imported (phone_number + twilio_sid), returns existing ID.
    EL auto-configures voice_url on Twilio number upon successful import.

    Law #9: twilio_sid and twilio_token are NOT logged.
    """
    headers = _el_headers()
    body: dict[str, Any] = {
        "phone_number": phone_number,
        "label": label,
        "provider": "twilio",
        "sid": twilio_sid,
        "token": twilio_token,
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{_EL_BASE_URL}/v1/convai/phone-numbers",
            json=body,
            headers=headers,
        )

    # Idempotency: if already imported, EL returns 409 or a duplicate-matching error.
    # GET existing record in that case.
    if resp.status_code == 409:
        logger.info(
            "el_phone_import idempotent_replay phone=%s — fetching existing",
            phone_number,
        )
        return await _get_existing_el_phone_id(phone_number, twilio_sid)

    if resp.status_code >= 400:
        _raise_el_error("import_to_elevenlabs", resp)

    data = resp.json()
    el_id = data.get("phone_number_id") or data.get("id") or ""
    if not el_id:
        raise ElevenLabsPhoneError(
            "MISSING_PHONE_NUMBER_ID",
            f"EL import succeeded but response missing phone_number_id: {list(data.keys())}",
            resp.status_code,
        )

    logger.info(
        "el_phone_import success phone=%s el_id=%s...",
        phone_number,
        el_id[:12],
    )
    return el_id


async def _get_existing_el_phone_id(phone_number: str, twilio_sid: str) -> str:
    """Fetch the existing EL phone_number_id for a Twilio number (idempotency path)."""
    headers = _el_headers()
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{_EL_BASE_URL}/v1/convai/phone-numbers",
            headers=headers,
        )
    if resp.status_code >= 400:
        _raise_el_error("get_existing_el_phone", resp)

    records = resp.json()
    if isinstance(records, dict) and "phone_numbers" in records:
        records = records["phone_numbers"]
    for record in records or []:
        if record.get("phone_number") == phone_number:
            el_id = record.get("phone_number_id") or record.get("id") or ""
            if el_id:
                logger.debug(
                    "el_phone_import idempotent found existing el_id=%s...",
                    el_id[:12],
                )
                return el_id
    raise ElevenLabsPhoneError(
        "IMPORT_REPLAY_NOT_FOUND",
        f"EL returned 409 for {phone_number} but GET found no matching record. "
        f"Manual inspection required (twilio_sid prefix: {twilio_sid[:6]}...).",
    )


async def attach_to_agent(
    el_phone_number_id: str,
    agent_id: str = SARAH_RECEPTIONIST_AGENT_ID,
) -> None:
    """Attach an EL phone number to an agent.

    PATCH /v1/convai/phone-numbers/{el_phone_number_id} with {agent_id}.
    Default agent: Sarah Receptionist (verified 2026-04-29).
    """
    headers = _el_headers()
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.patch(
            f"{_EL_BASE_URL}/v1/convai/phone-numbers/{el_phone_number_id}",
            json={"agent_id": agent_id},
            headers=headers,
        )
    if resp.status_code >= 400:
        _raise_el_error("attach_to_agent", resp)

    logger.info(
        "el_phone_attach success el_id=%s... agent_id=%s...",
        el_phone_number_id[:12],
        agent_id[:16],
    )


async def detach_from_elevenlabs(el_phone_number_id: str) -> None:
    """Delete (detach + remove) an EL phone number record.

    DELETE /v1/convai/phone-numbers/{el_phone_number_id}.
    Called before Twilio release to ensure EL no longer owns the voice_url.
    """
    headers = _el_headers()
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.delete(
            f"{_EL_BASE_URL}/v1/convai/phone-numbers/{el_phone_number_id}",
            headers=headers,
        )
    # 404 = already removed — treat as success (idempotent)
    if resp.status_code == 404:
        logger.info(
            "el_phone_detach 404 el_id=%s... — already removed",
            el_phone_number_id[:12],
        )
        return
    if resp.status_code >= 400:
        _raise_el_error("detach_from_elevenlabs", resp)

    logger.info("el_phone_detach success el_id=%s...", el_phone_number_id[:12])


async def outbound_call(
    agent_id: str,
    to_number: str,
    el_phone_number_id: str,
    dynamic_variables: dict[str, Any],
) -> str:
    """Initiate an outbound call via EL Twilio integration.

    POST /v1/convai/twilio/outbound-call.
    Returns call_sid from the EL response.
    Yellow tier — capability token validated upstream by route layer.
    """
    headers = _el_headers()
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "to_number": to_number,
        "phone_number_id": el_phone_number_id,
        "dynamic_variables": dynamic_variables,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{_EL_BASE_URL}/v1/convai/twilio/outbound-call",
            json=body,
            headers=headers,
        )
    if resp.status_code >= 400:
        _raise_el_error("outbound_call", resp)

    data = resp.json()
    call_sid = data.get("call_sid") or data.get("sid") or ""
    logger.info(
        "el_outbound_call initiated to=%s call_sid=%s...",
        to_number,
        call_sid[:16] if call_sid else "unknown",
    )
    return call_sid


def _raise_el_error(operation: str, resp: httpx.Response) -> None:
    """Parse EL error response and raise ElevenLabsPhoneError."""
    detail = f"HTTP {resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict):
            msg = body.get("detail") or body.get("message") or body.get("error") or ""
            if msg:
                detail = str(msg)
    except Exception:
        pass
    logger.error("elevenlabs_phone op=%s status=%d detail=%s", operation, resp.status_code, detail)
    raise ElevenLabsPhoneError(
        f"EL_{operation.upper()}_FAILED",
        f"ElevenLabs {operation} failed: {detail}",
        resp.status_code,
    )


__all__ = [
    "SARAH_RECEPTIONIST_AGENT_ID",
    "ElevenLabsPhoneError",
    "import_to_elevenlabs",
    "attach_to_agent",
    "detach_from_elevenlabs",
    "outbound_call",
]
