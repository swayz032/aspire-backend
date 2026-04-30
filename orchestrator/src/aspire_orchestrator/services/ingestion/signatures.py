"""Webhook signature verifiers — one function per upstream provider.

Each function takes the request's raw body bytes + signature header value (+
secret from settings) and returns True/False. **Never raise on bad signature —
return False and let the route layer respond 401.**

Providers covered (all read-only signature checks, no side effects):
  - Stripe        (Stripe-Signature: t=...,v1=...)
  - PandaDoc      (X-PandaDoc-Signature: hex SHA-256 HMAC of body)
  - Twilio        (X-Twilio-Signature: base64 SHA-1 HMAC of url + sorted params)
  - ElevenLabs    (ElevenLabs-Signature: t=...,v0=...)
  - Anam          (X-Anam-Signature: hex SHA-256 HMAC of body)
  - Zoom          (X-Zm-Signature: 'v0=' + hex SHA-256 HMAC of 'v0:'+ts+':'+body)

Constant-time comparison via `hmac.compare_digest` everywhere — never `==` on
HMAC results (timing-attack safe).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Mapping
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# Replay-window — webhooks older than this are rejected even with a valid
# signature. 5 minutes matches Stripe + EL defaults.
_REPLAY_WINDOW_SECONDS = 300


# ---------------------------------------------------------------------------
# Stripe — t=<timestamp>,v1=<sig>,v0=<deprecated>
# ---------------------------------------------------------------------------


def verify_stripe(body: bytes, sig_header: str, secret: str) -> bool:
    """Stripe HMAC SHA-256 with timestamp + replay window.

    Per https://stripe.com/docs/webhooks/signatures.
    """
    if not sig_header or not secret:
        return False
    try:
        parts = dict(p.strip().split("=", 1) for p in sig_header.split(",") if "=" in p)
        ts_str = parts.get("t")
        v1 = parts.get("v1")
        if not ts_str or not v1:
            return False
        ts = int(ts_str)
        if abs(time.time() - ts) > _REPLAY_WINDOW_SECONDS:
            logger.warning("stripe_signature replay rejected: ts=%s", ts)
            return False
        signed_payload = f"{ts}.".encode() + body
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)
    except (ValueError, KeyError) as exc:
        logger.warning("stripe_signature parse error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# PandaDoc — hex SHA-256 HMAC of raw body
# ---------------------------------------------------------------------------


def verify_pandadoc(body: bytes, sig_header: str, secret: str) -> bool:
    """PandaDoc webhook signature: SHA-256 HMAC of raw request body, hex-encoded."""
    if not sig_header or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header.strip())


# ---------------------------------------------------------------------------
# Twilio — base64 SHA-1 HMAC of full URL + sorted params (form-encoded webhooks)
# ---------------------------------------------------------------------------


def verify_twilio(
    full_url: str,
    params: Mapping[str, str] | None,
    sig_header: str,
    auth_token: str,
) -> bool:
    """Twilio request signature.

    Per https://www.twilio.com/docs/usage/webhooks/webhooks-security:
      string_to_sign = full_url + concat(sorted(param_name + param_value))
      signature = base64(HMAC-SHA1(auth_token, string_to_sign))

    For raw-body webhooks (e.g. recording status URL with no body), pass
    `params=None` and ensure the URL contains all query params.
    """
    if not sig_header or not auth_token:
        return False
    string_to_sign = full_url
    if params:
        for key in sorted(params.keys()):
            string_to_sign += f"{key}{params[key]}"
    digest = hmac.new(
        auth_token.encode(),
        string_to_sign.encode(),
        hashlib.sha1,
    ).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, sig_header.strip())


# ---------------------------------------------------------------------------
# ElevenLabs — t=<timestamp>,v0=<sig>
# ---------------------------------------------------------------------------


def verify_elevenlabs(body: bytes, sig_header: str, secret: str) -> bool:
    """ElevenLabs webhook signature: timestamped HMAC SHA-256.

    Per EL docs (Conversational AI webhooks):
      header = "t=<unix_ts>,v0=<hmac_sha256_hex>"
      signed_payload = f"{ts}.{raw_body}"
    """
    if not sig_header or not secret:
        return False
    try:
        parts = dict(p.strip().split("=", 1) for p in sig_header.split(",") if "=" in p)
        ts_str = parts.get("t")
        v0 = parts.get("v0")
        if not ts_str or not v0:
            return False
        ts = int(ts_str)
        if abs(time.time() - ts) > _REPLAY_WINDOW_SECONDS:
            logger.warning("elevenlabs_signature replay rejected: ts=%s", ts)
            return False
        signed_payload = f"{ts}.".encode() + body
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v0)
    except (ValueError, KeyError) as exc:
        logger.warning("elevenlabs_signature parse error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Anam — hex SHA-256 HMAC of raw body (PandaDoc-style)
# ---------------------------------------------------------------------------


def verify_anam(body: bytes, sig_header: str, secret: str) -> bool:
    """Anam webhook signature: SHA-256 HMAC of raw body, hex-encoded."""
    if not sig_header or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header.strip())


# ---------------------------------------------------------------------------
# Zoom — 'v0=' + hex SHA-256 HMAC of 'v0:' + ts + ':' + body
# ---------------------------------------------------------------------------


def verify_zoom(body: bytes, sig_header: str, ts_header: str, secret: str) -> bool:
    """Zoom webhook signature.

    Per https://developers.zoom.us/docs/api/rest/webhook-reference/#verify-webhook-events:
      message = "v0:" + x_zm_request_timestamp + ":" + raw_body
      hash    = HMAC-SHA256(secret, message)
      header  = "v0=" + hex(hash)
    """
    if not sig_header or not ts_header or not secret:
        return False
    try:
        ts = int(ts_header)
        if abs(time.time() - ts) > _REPLAY_WINDOW_SECONDS:
            logger.warning("zoom_signature replay rejected: ts=%s", ts)
            return False
        message = f"v0:{ts_header}:".encode() + body
        digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        expected = f"v0={digest}"
        return hmac.compare_digest(expected, sig_header.strip())
    except ValueError as exc:
        logger.warning("zoom_signature parse error: %s", exc)
        return False


__all__ = [
    "verify_stripe",
    "verify_pandadoc",
    "verify_twilio",
    "verify_elevenlabs",
    "verify_anam",
    "verify_zoom",
]
