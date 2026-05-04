"""Twilio Voice routes — Call Room production wiring.

Two endpoints:

  POST /v1/twilio/voice-token   — Yellow tier, capability-token gated.
                                   Mints a Voice Access Token for the
                                   browser Twilio Voice SDK Device.

  POST /v1/twilio/voice/twiml   — Public webhook hit by Twilio when the
                                   SDK Device places a call. Validated
                                   via the X-Twilio-Signature header.
                                   Returns TwiML <Dial> bridging the
                                   browser leg to the dialed PSTN number,
                                   using the office's purchased Aspire
                                   number as caller_id.

Law compliance:
  Law #2 — every state-affecting hop cuts a receipt.
  Law #3 — both routes fail-closed on missing config / bad signature.
  Law #5 — voice-token route requires a capability token in the body
            (scope: telephony:voice_call).
  Law #6 — caller_id is resolved from tenant_phone_numbers via the
            JWT identity, never from the inbound webhook params.
  Law #9 — phone numbers redacted in logs/receipts; secrets never logged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.routes.front_desk import (  # reuse helpers
    _cap_token_id,
    _resolve_scope,
    _validate_cap_token,
)
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)
from aspire_orchestrator.services.twilio_voice import (
    TwilioVoiceConfigError,
    mint_voice_token,
    parse_identity,
    twilio_signature_url_candidates,
    verify_twilio_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/twilio", tags=["twilio-voice"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class VoiceTokenRequest(BaseModel):
    user_id: str | None = None  # optional — defaults to suite_id when absent
    capability_token: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return phone[:6] + "..." if len(phone) > 6 else phone


async def _resolve_office_aspire_number(office_id: str) -> str:
    """Return the office's purchased Aspire number as E.164, '' when none."""
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            f"office_id=eq.{office_id}&status=eq.active",
            order_by="purchased_at.desc",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning("aspire_number_lookup_failed office_id=%s: %s", office_id, exc)
        return ""
    if not rows:
        return ""
    return str(rows[0].get("phone_number") or "")


# ---------------------------------------------------------------------------
# 1) POST /v1/twilio/voice-token
# ---------------------------------------------------------------------------


@router.post("/voice-token")
async def voice_token(
    req: VoiceTokenRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Mint a Voice Access Token for the browser SDK Device."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(req.capability_token, scope, "telephony:voice_call")

    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    user_id = (req.user_id or suite_id or "anon").strip() or "anon"

    # Don't mint a token if the office has no purchased number — the SDK
    # would dial out from caller_id="" and Twilio would reject. Surface
    # this as a clean 409 so the FE shows the "Set up your number first"
    # CTA, not a generic 500.
    aspire_number = await _resolve_office_aspire_number(office_id)
    if not aspire_number:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "NO_ASPIRE_NUMBER",
                "message": "Purchase an Aspire number on Front Desk Setup before placing calls.",
            },
        )

    # Inbound-only enforcement is currently implicit: an office without an
    # active tenant_phone_numbers row with voice capability gets the 409
    # above and can't mint a token. When a dedicated
    # `front_desk_configs.outbound_disabled` flag is added (UI toggle for
    # "receive calls only"), gate it here with reason_code=OUTBOUND_DISABLED
    # so the FE can show "This office is in inbound-only mode."

    try:
        minted = mint_voice_token(suite_id=suite_id, user_id=user_id)
    except TwilioVoiceConfigError as exc:
        logger.error("voice_token config_missing: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "VOICE_NOT_CONFIGURED",
                "message": "In-browser calling not configured for this environment.",
            },
        ) from exc

    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": receipt_id,
                "receipt_type": "voice_token_minted",
                "suite_id": suite_id,
                "office_id": office_id,
                "tenant_id": tenant_id,
                "outcome": "success",
                "action_type": "voice_token_minted",
                "tool_used": "twilio_voice",
                "risk_tier": "yellow",
                "redacted_inputs": {"user_id": user_id},
                "redacted_outputs": {
                    "identity": minted["identity"],
                    "expires_at": minted["expires_at"],
                    "caller_id": _redact_phone(aspire_number),
                },
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "capability_token_id": _cap_token_id(req.capability_token) or None,
                "created_at": now,
            }
        ]
    )

    return {
        "token": minted["token"],
        "identity": minted["identity"],
        "expires_at": minted["expires_at"],
        "caller_id": aspire_number,
        "caller_id_formatted": _format_e164_us(aspire_number),
        "receipt_id": receipt_id,
    }


def _format_e164_us(e164: str) -> str:
    """Mirror routes/front_desk.py:_format_e164_us — kept local to avoid
    a cross-route import + circular-import risk."""
    if not e164 or not isinstance(e164, str):
        return e164 or ""
    s = e164.strip()
    if s.startswith("+1") and len(s) == 12 and s[2:].isdigit():
        return f"+1 ({s[2:5]}) {s[5:8]}-{s[8:]}"
    return s


# ---------------------------------------------------------------------------
# 2) POST /v1/twilio/voice/twiml
# ---------------------------------------------------------------------------


@router.post("/voice/twiml")
async def voice_twiml(request: Request) -> Response:
    """TwiML webhook — Twilio calls this when the SDK Device places a call.

    Resolves the office from the SDK identity, looks up the Aspire number
    to use as caller_id, returns a <Dial> response bridging to the user-
    supplied `To` PSTN number.
    """
    form = await request.form()
    form_params = {k: str(v) for k, v in form.multi_items()}

    # Twilio webhook signature lives in this header. Validate against the
    # full request URL we received (needs to match what's configured on
    # the TwiML App). Fail-closed if missing or invalid.
    signature_header = request.headers.get("X-Twilio-Signature", "")
    request_urls = twilio_signature_url_candidates(
        received_url=str(request.url),
        forwarded_proto=request.headers.get("x-forwarded-proto"),
        forwarded_host=request.headers.get("x-forwarded-host"),
        host=request.headers.get("host"),
    )
    if not any(
        verify_twilio_signature(
            request_url=request_url,
            form_params=form_params,
            signature_header=signature_header,
        )
        for request_url in request_urls
    ):
        logger.warning(
            "voice_twiml invalid_signature candidates=%s received_url=%s",
            len(request_urls),
            request_urls[0] if request_urls else "",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_SIGNATURE"},
        )

    to_number = (form_params.get("To") or "").strip()
    identity = (form_params.get("From") or "").strip()
    parsed = parse_identity(identity)
    suite_id = parsed.get("suite_id") or ""
    user_id = parsed.get("user_id") or ""

    if not suite_id or not to_number:
        logger.warning(
            "voice_twiml missing_required identity=%s to_present=%s",
            identity,
            bool(to_number),
        )
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>Sorry, this call could not be completed.</Say>"
                "<Hangup/></Response>"
            ),
            media_type="application/xml",
            status_code=status.HTTP_200_OK,  # 200 so Twilio plays the message
        )

    # Resolve caller_id from the suite's office. The SDK identity carries
    # suite_id only — for v1 we pick the most-recently-purchased active
    # number for any office under that suite (1:1 model — one number per
    # business). When multi-office support lands we'll thread office_id
    # through the SDK params instead.
    aspire_number = ""
    try:
        rows = await supabase_select(
            "tenant_phone_numbers",
            f"suite_id=eq.{suite_id}&status=eq.active",
            order_by="purchased_at.desc",
            limit=1,
        )
        if rows:
            aspire_number = str(rows[0].get("phone_number") or "")
    except SupabaseClientError as exc:
        logger.warning("voice_twiml supabase_lookup_failed: %s", exc)

    if not aspire_number:
        logger.warning("voice_twiml no_aspire_number suite_id=%s", suite_id)
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>Your Aspire number is not yet configured. "
                "Please complete Front Desk Setup.</Say><Hangup/></Response>"
            ),
            media_type="application/xml",
            status_code=status.HTTP_200_OK,
        )

    # Cut a receipt for the dial attempt — useful audit trail because the
    # Twilio /Calls.json POST itself isn't initiated by us; Twilio bridges
    # automatically based on this TwiML.
    receipt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    receipt_store.store_receipts(
        [
            {
                "id": receipt_id,
                "receipt_type": "voice_call_dial_started",
                "suite_id": suite_id,
                "office_id": "",  # unknown at this layer — looked up via suite
                "tenant_id": "",
                "outcome": "success",
                "action_type": "voice_call_dial_started",
                "tool_used": "twilio_voice",
                "risk_tier": "yellow",
                "redacted_inputs": {
                    "to": _redact_phone(to_number),
                    "from_identity": identity,
                },
                "redacted_outputs": {
                    "caller_id": _redact_phone(aspire_number),
                    "user_id": user_id,
                },
                "trace_id": get_trace_id(),
                "correlation_id": get_correlation_id(),
                "created_at": now,
            }
        ]
    )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial callerId="{aspire_number}" answerOnBridge="true" timeout="30">'
        f"<Number>{_xml_escape(to_number)}</Number>"
        "</Dial>"
        "</Response>"
    )
    return Response(
        content=twiml,
        media_type="application/xml",
        status_code=status.HTTP_200_OK,
    )


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
