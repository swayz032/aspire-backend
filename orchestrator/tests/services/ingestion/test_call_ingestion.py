"""Unit tests for CallRecordingIngestionAdapter and CallTranscriptionIngestionAdapter.

Pass 14 Gate Item 2. Both adapters in one file (they share a module).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.call_ingestion import (
    CallRecordingIngestionAdapter,
    CallTranscriptionIngestionAdapter,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

PHONE_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "phone_number": "+12125550198",
}

RECORDING_PAYLOAD = {
    "CallSid": "CAtest1234567890abcdef1234567890ab",
    "RecordingSid": "REtest1234567890abcdef1234567890ab",
    "AccountSid": "ACtest",
    "From": "+15551234567",
    "To": "+12125550198",
    "RecordingUrl": "https://api.twilio.com/Accounts/AC/Recordings/RE",
    "RecordingStatus": "completed",
    "RecordingDuration": "120",
    "RecordingChannels": "1",
}

TRANSCRIPTION_PAYLOAD = {
    "CallSid": "CAtest1234567890abcdef1234567890ab",
    "RecordingSid": "REtest1234567890abcdef1234567890ab",
    "TranscriptionSid": "TRtest1234567890abcdef1234567890ab",
    "AccountSid": "ACtest",
    "From": "+15551234567",
    "To": "+12125550198",
    "TranscriptionText": "Hello this is a test transcript",
    "TranscriptionStatus": "completed",
    "TranscriptionUrl": "https://api.twilio.com/Transcriptions/TR",
}


def _twilio_sig(url: str, params: dict | None, token: str) -> str:
    s = url
    if params:
        for k in sorted(params.keys()):
            s += f"{k}{params[k]}"
    digest = hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _make_headers(token: str = "real_token", valid: bool = True) -> dict:
    url = "https://www.aspireos.app/v1/ingest/twilio/recording"
    sig = _twilio_sig(url, None, token) if valid else "bad_sig"
    return {
        "X-Twilio-Signature": sig,
        "X-Aspire-Webhook-Url": url,
        "X-Aspire-Form-Params": "",
    }


# ---------------------------------------------------------------------------
# CallRecordingIngestionAdapter
# ---------------------------------------------------------------------------


class TestCallRecordingVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        token = "real_token"
        adapter = CallRecordingIngestionAdapter()
        headers = _make_headers(token=token, valid=True)
        with patch(
            "aspire_orchestrator.services.ingestion.call_ingestion.settings"
        ) as mock_settings:
            mock_settings.twilio_auth_token = token
            result = await adapter.verify_signature(body=b"", headers=headers)
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        headers = _make_headers(valid=False)
        with patch(
            "aspire_orchestrator.services.ingestion.call_ingestion.settings"
        ) as mock_settings:
            mock_settings.twilio_auth_token = "real_token"
            result = await adapter.verify_signature(body=b"", headers=headers)
        assert result is False


class TestCallRecordingResolveScope:

    @pytest.mark.asyncio
    async def test_valid_to_number_returns_scope(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.call_ingestion.supabase_select",
            new=AsyncMock(return_value=[PHONE_ROW_A]),
        ):
            scope = await adapter.resolve_scope(RECORDING_PAYLOAD)
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_missing_to_number_raises_422(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        with pytest.raises(IngestionError, match="MISSING_TO_NUMBER"):
            await adapter.resolve_scope({"From": "+15551234567"})

    @pytest.mark.asyncio
    async def test_unknown_number_raises_404(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.call_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(RECORDING_PAYLOAD)
        assert exc_info.value.status_code == 404


class TestCallRecordingBuildEnvelope:

    @pytest.mark.asyncio
    async def test_recording_envelope_fields(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env = await adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        assert env.memory_type == "call"
        call_sid = RECORDING_PAYLOAD["CallSid"]
        recording_sid = RECORDING_PAYLOAD["RecordingSid"]
        assert env.idempotency_key == f"twilio-call-recording:{call_sid}:{recording_sid}"
        assert env.detail["duration_seconds"] == 120
        assert env.detail["recording_url"].endswith(".mp3")
        assert env.detail["transcription_text"] is None  # filled by transcription event

    @pytest.mark.asyncio
    async def test_missing_call_sid_raises_error(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        bad = {k: v for k, v in RECORDING_PAYLOAD.items() if k != "CallSid"}
        with pytest.raises(IngestionError, match="MISSING_CALL_SID"):
            await adapter.build_envelope(bad, scope=scope, thread=None)

    @pytest.mark.asyncio
    async def test_idempotency_is_stable(self) -> None:
        adapter = CallRecordingIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env1 = await adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        env2 = await adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        assert env1.idempotency_key == env2.idempotency_key


# ---------------------------------------------------------------------------
# CallTranscriptionIngestionAdapter
# ---------------------------------------------------------------------------


class TestCallTranscriptionBuildEnvelope:

    @pytest.mark.asyncio
    async def test_transcription_envelope_fields(self) -> None:
        adapter = CallTranscriptionIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env = await adapter.build_envelope(TRANSCRIPTION_PAYLOAD, scope=scope, thread=None)
        assert env.memory_type == "call"
        call_sid = TRANSCRIPTION_PAYLOAD["CallSid"]
        trans_sid = TRANSCRIPTION_PAYLOAD["TranscriptionSid"]
        assert env.idempotency_key == f"twilio-call-transcription:{call_sid}:{trans_sid}"
        assert env.detail["transcription_text"] == "Hello this is a test transcript"
        assert env.detail["outcome"] == "completed"
        assert "supersedes_idempotency_key" in env.detail

    @pytest.mark.asyncio
    async def test_failed_transcription_sets_null_text(self) -> None:
        adapter = CallTranscriptionIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {**TRANSCRIPTION_PAYLOAD, "TranscriptionStatus": "failed"}
        env = await adapter.build_envelope(payload, scope=scope, thread=None)
        assert env.detail["transcription_text"] is None
        assert env.detail["outcome"] == "transcription_failed"

    @pytest.mark.asyncio
    async def test_voicemail_detected_in_transcript(self) -> None:
        adapter = CallTranscriptionIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {
            **TRANSCRIPTION_PAYLOAD,
            "TranscriptionText": "Please leave a message after the beep",
        }
        env = await adapter.build_envelope(payload, scope=scope, thread=None)
        assert env.detail["outcome"] == "voicemail"

    @pytest.mark.asyncio
    async def test_missing_transcription_sid_raises_error(self) -> None:
        adapter = CallTranscriptionIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        bad = {k: v for k, v in TRANSCRIPTION_PAYLOAD.items() if k != "TranscriptionSid"}
        with pytest.raises(IngestionError, match="MISSING_TRANSCRIPTION_SID"):
            await adapter.build_envelope(bad, scope=scope, thread=None)

    @pytest.mark.asyncio
    async def test_trace_ids_stable_across_both_adapters(self) -> None:
        """Recording and transcription adapters share trace_id from CallSid."""
        recording_adapter = CallRecordingIngestionAdapter()
        transcription_adapter = CallTranscriptionIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env_r = await recording_adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        env_t = await transcription_adapter.build_envelope(TRANSCRIPTION_PAYLOAD, scope=scope, thread=None)
        assert env_r.provenance.trace_id == env_t.provenance.trace_id
        assert env_r.provenance.correlation_id == env_t.provenance.correlation_id
