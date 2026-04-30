"""Webhook signature verification tests — Pass 14 Gate Item 5.

Tests each verify_* function with valid + invalid signatures.
Validates constant-time comparison (hmac.compare_digest) and replay-window
rejection for Stripe / ElevenLabs / Zoom (timestamps outside 5 min).

Aspire Laws:
  Law #3: Fail Closed — bad signature → False, never raise.
  Law #9: No side-effects during signature verification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.ingestion.signatures import (
    verify_anam,
    verify_elevenlabs,
    verify_pandadoc,
    verify_stripe,
    verify_twilio,
    verify_zoom,
)

_WINDOW = 300  # 5 minutes replay window

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stripe_sig(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}.".encode() + body
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


def _el_sig(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}.".encode() + body
    v0 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={v0}"


def _zoom_sig(body: bytes, secret: str, ts: int | None = None) -> tuple[str, str]:
    ts_str = str(ts or int(time.time()))
    message = f"v0:{ts_str}:".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"v0={digest}", ts_str


def _twilio_sig(full_url: str, params: dict | None, auth_token: str) -> str:
    s = full_url
    if params:
        for k in sorted(params.keys()):
            s += f"{k}{params[k]}"
    digest = hmac.new(auth_token.encode(), s.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _pandadoc_sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _anam_sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


class TestVerifyStripe:
    """verify_stripe: timestamped HMAC SHA-256 with 5min replay window."""

    BODY = b'{"type":"invoice.created"}'
    SECRET = "whsec_test_stripe"

    def test_valid_signature_returns_true(self) -> None:
        sig = _stripe_sig(self.BODY, self.SECRET)
        assert verify_stripe(self.BODY, sig, self.SECRET) is True

    def test_wrong_secret_returns_false(self) -> None:
        sig = _stripe_sig(self.BODY, self.SECRET)
        assert verify_stripe(self.BODY, sig, "wrong_secret") is False

    def test_tampered_body_returns_false(self) -> None:
        sig = _stripe_sig(self.BODY, self.SECRET)
        assert verify_stripe(b'{"type":"invoice.paid"}', sig, self.SECRET) is False

    def test_tampered_v1_returns_false(self) -> None:
        ts = int(time.time())
        bad_sig = f"t={ts},v1=deadbeefdeadbeef"
        assert verify_stripe(self.BODY, bad_sig, self.SECRET) is False

    def test_missing_sig_header_returns_false(self) -> None:
        assert verify_stripe(self.BODY, "", self.SECRET) is False

    def test_missing_secret_returns_false(self) -> None:
        sig = _stripe_sig(self.BODY, self.SECRET)
        assert verify_stripe(self.BODY, sig, "") is False

    def test_replay_old_timestamp_returns_false(self) -> None:
        old_ts = int(time.time()) - (_WINDOW + 60)
        sig = _stripe_sig(self.BODY, self.SECRET, ts=old_ts)
        assert verify_stripe(self.BODY, sig, self.SECRET) is False

    def test_future_timestamp_beyond_window_returns_false(self) -> None:
        future_ts = int(time.time()) + (_WINDOW + 60)
        sig = _stripe_sig(self.BODY, self.SECRET, ts=future_ts)
        assert verify_stripe(self.BODY, sig, self.SECRET) is False

    def test_malformed_header_returns_false(self) -> None:
        assert verify_stripe(self.BODY, "not_a_real_header", self.SECRET) is False

    def test_uses_constant_time_compare(self) -> None:
        """Verify hmac.compare_digest is in the call path (not plain ==)."""
        sig = _stripe_sig(self.BODY, self.SECRET)
        with patch("aspire_orchestrator.services.ingestion.signatures.hmac.compare_digest") as mock_cd:
            mock_cd.return_value = True
            result = verify_stripe(self.BODY, sig, self.SECRET)
        mock_cd.assert_called_once()


# ---------------------------------------------------------------------------
# PandaDoc
# ---------------------------------------------------------------------------


class TestVerifyPandadoc:
    """verify_pandadoc: plain HMAC SHA-256 of body (no timestamp)."""

    BODY = b'{"event":"document_state_changed"}'
    SECRET = "pd_test_secret"

    def test_valid_returns_true(self) -> None:
        sig = _pandadoc_sig(self.BODY, self.SECRET)
        assert verify_pandadoc(self.BODY, sig, self.SECRET) is True

    def test_wrong_secret_returns_false(self) -> None:
        sig = _pandadoc_sig(self.BODY, self.SECRET)
        assert verify_pandadoc(self.BODY, sig, "wrong") is False

    def test_tampered_body_returns_false(self) -> None:
        sig = _pandadoc_sig(self.BODY, self.SECRET)
        assert verify_pandadoc(b'tampered', sig, self.SECRET) is False

    def test_empty_sig_returns_false(self) -> None:
        assert verify_pandadoc(self.BODY, "", self.SECRET) is False

    def test_empty_secret_returns_false(self) -> None:
        sig = _pandadoc_sig(self.BODY, self.SECRET)
        assert verify_pandadoc(self.BODY, sig, "") is False

    def test_sig_with_leading_trailing_whitespace_accepted(self) -> None:
        sig = "  " + _pandadoc_sig(self.BODY, self.SECRET) + "  "
        assert verify_pandadoc(self.BODY, sig, self.SECRET) is True


# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------


class TestVerifyTwilio:
    """verify_twilio: HMAC SHA-1 of URL + sorted params, base64-encoded."""

    URL = "https://www.aspireos.app/v1/ingest/twilio/sms"
    PARAMS = {"Body": "hello", "From": "+15551234567", "To": "+12125550198"}
    TOKEN = "twilio_auth_token_test"

    def test_valid_with_params_returns_true(self) -> None:
        sig = _twilio_sig(self.URL, self.PARAMS, self.TOKEN)
        assert verify_twilio(self.URL, self.PARAMS, sig, self.TOKEN) is True

    def test_valid_no_params_returns_true(self) -> None:
        sig = _twilio_sig(self.URL, None, self.TOKEN)
        assert verify_twilio(self.URL, None, sig, self.TOKEN) is True

    def test_wrong_token_returns_false(self) -> None:
        sig = _twilio_sig(self.URL, self.PARAMS, self.TOKEN)
        assert verify_twilio(self.URL, self.PARAMS, sig, "wrong") is False

    def test_wrong_url_returns_false(self) -> None:
        sig = _twilio_sig(self.URL, self.PARAMS, self.TOKEN)
        assert verify_twilio("https://evil.com/hook", self.PARAMS, sig, self.TOKEN) is False

    def test_extra_param_returns_false(self) -> None:
        sig = _twilio_sig(self.URL, self.PARAMS, self.TOKEN)
        modified = {**self.PARAMS, "Extra": "injected"}
        assert verify_twilio(self.URL, modified, sig, self.TOKEN) is False

    def test_empty_sig_returns_false(self) -> None:
        assert verify_twilio(self.URL, self.PARAMS, "", self.TOKEN) is False

    def test_empty_token_returns_false(self) -> None:
        sig = _twilio_sig(self.URL, self.PARAMS, self.TOKEN)
        assert verify_twilio(self.URL, self.PARAMS, sig, "") is False


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------


class TestVerifyElevenLabs:
    """verify_elevenlabs: timestamped HMAC SHA-256 (t=...,v0=...)."""

    BODY = b'{"type":"post_call_transcription"}'
    SECRET = "el_webhook_secret_test"

    def test_valid_returns_true(self) -> None:
        sig = _el_sig(self.BODY, self.SECRET)
        assert verify_elevenlabs(self.BODY, sig, self.SECRET) is True

    def test_wrong_secret_returns_false(self) -> None:
        sig = _el_sig(self.BODY, self.SECRET)
        assert verify_elevenlabs(self.BODY, sig, "wrong") is False

    def test_tampered_body_returns_false(self) -> None:
        sig = _el_sig(self.BODY, self.SECRET)
        assert verify_elevenlabs(b'tampered', sig, self.SECRET) is False

    def test_replay_old_timestamp_returns_false(self) -> None:
        old_ts = int(time.time()) - (_WINDOW + 60)
        sig = _el_sig(self.BODY, self.SECRET, ts=old_ts)
        assert verify_elevenlabs(self.BODY, sig, self.SECRET) is False

    def test_future_timestamp_beyond_window_returns_false(self) -> None:
        future_ts = int(time.time()) + (_WINDOW + 60)
        sig = _el_sig(self.BODY, self.SECRET, ts=future_ts)
        assert verify_elevenlabs(self.BODY, sig, self.SECRET) is False

    def test_empty_sig_returns_false(self) -> None:
        assert verify_elevenlabs(self.BODY, "", self.SECRET) is False

    def test_malformed_header_returns_false(self) -> None:
        assert verify_elevenlabs(self.BODY, "garbage", self.SECRET) is False


# ---------------------------------------------------------------------------
# Anam
# ---------------------------------------------------------------------------


class TestVerifyAnam:
    """verify_anam: plain HMAC SHA-256 of body (same as PandaDoc)."""

    BODY = b'{"event":"session.ended"}'
    SECRET = "anam_secret_test"

    def test_valid_returns_true(self) -> None:
        sig = _anam_sig(self.BODY, self.SECRET)
        assert verify_anam(self.BODY, sig, self.SECRET) is True

    def test_wrong_secret_returns_false(self) -> None:
        sig = _anam_sig(self.BODY, self.SECRET)
        assert verify_anam(self.BODY, sig, "wrong") is False

    def test_tampered_body_returns_false(self) -> None:
        sig = _anam_sig(self.BODY, self.SECRET)
        assert verify_anam(b'modified', sig, self.SECRET) is False

    def test_empty_sig_returns_false(self) -> None:
        assert verify_anam(self.BODY, "", self.SECRET) is False


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------


class TestVerifyZoom:
    """verify_zoom: HMAC SHA-256 with timestamp replay protection."""

    BODY = b'{"event":"recording.completed"}'
    SECRET = "zoom_secret_test"

    def test_valid_returns_true(self) -> None:
        sig, ts = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(self.BODY, sig, ts, self.SECRET) is True

    def test_wrong_secret_returns_false(self) -> None:
        sig, ts = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(self.BODY, sig, ts, "wrong") is False

    def test_tampered_body_returns_false(self) -> None:
        sig, ts = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(b'tampered', sig, ts, self.SECRET) is False

    def test_replay_old_timestamp_returns_false(self) -> None:
        old_ts = int(time.time()) - (_WINDOW + 60)
        sig, ts_str = _zoom_sig(self.BODY, self.SECRET, ts=old_ts)
        assert verify_zoom(self.BODY, sig, ts_str, self.SECRET) is False

    def test_future_timestamp_beyond_window_returns_false(self) -> None:
        future_ts = int(time.time()) + (_WINDOW + 60)
        sig, ts_str = _zoom_sig(self.BODY, self.SECRET, ts=future_ts)
        assert verify_zoom(self.BODY, sig, ts_str, self.SECRET) is False

    def test_empty_sig_returns_false(self) -> None:
        _, ts = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(self.BODY, "", ts, self.SECRET) is False

    def test_empty_ts_header_returns_false(self) -> None:
        sig, _ = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(self.BODY, sig, "", self.SECRET) is False

    def test_empty_secret_returns_false(self) -> None:
        sig, ts = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(self.BODY, sig, ts, "") is False

    def test_malformed_ts_header_returns_false(self) -> None:
        sig, _ = _zoom_sig(self.BODY, self.SECRET)
        assert verify_zoom(self.BODY, sig, "not_a_number", self.SECRET) is False
