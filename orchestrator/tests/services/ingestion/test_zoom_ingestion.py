"""Unit tests for ZoomRecordingIngestionAdapter and ZoomTranscriptIngestionAdapter.

Pass 14 Gate Item 2.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.zoom_ingestion import (
    ZoomRecordingIngestionAdapter,
    ZoomTranscriptIngestionAdapter,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

PROVIDER_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "provider": "zoom",
    "external_account_id": "zoom_acct_test123",
}

RECORDING_PAYLOAD = {
    "event": "recording.completed",
    "account_id": "zoom_acct_test123",
    "payload": {
        "account_id": "zoom_acct_test123",
        "object": {
            "uuid": "meeting-uuid-test-abc",
            "id": 123456789,
            "host_id": "host_test",
            "topic": "Q1 Review",
            "start_time": "2026-04-28T14:00:00Z",
            "duration": 60,
            "participant_count": 5,
            "recording_files": [
                {
                    "file_type": "MP4",
                    "download_url": "https://zoom.us/rec/download/mp4",
                    "recording_start": "2026-04-28T14:00:00Z",
                    "recording_end": "2026-04-28T15:00:00Z",
                }
            ],
        },
    },
}

TRANSCRIPT_PAYLOAD = {
    "event": "recording.transcript_completed",
    "account_id": "zoom_acct_test123",
    "payload": {
        "account_id": "zoom_acct_test123",
        "object": {
            "uuid": "meeting-uuid-test-abc",
            "id": 123456789,
            "topic": "Q1 Review",
            "recording_files": [
                {
                    "file_type": "TRANSCRIPT",
                    "download_url": "https://zoom.us/rec/download/vtt",
                    "file_extension": "VTT",
                    "status": "completed",
                    "transcript_text": "This is the meeting transcript.",
                }
            ],
        },
    },
}


def _zoom_sig(body: bytes, secret: str, ts: int | None = None) -> tuple[str, str]:
    ts_str = str(ts or int(time.time()))
    message = f"v0:{ts_str}:".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"v0={digest}", ts_str


def _make_zoom_headers(body: bytes, secret: str = "zoom_secret") -> dict:
    sig, ts = _zoom_sig(body, secret)
    return {
        "X-Zm-Signature": sig,
        "X-Zm-Request-Timestamp": ts,
    }


class TestZoomRecordingVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        body = b'{"event":"recording.completed"}'
        secret = "zoom_secret"
        adapter = ZoomRecordingIngestionAdapter()
        headers = _make_zoom_headers(body, secret)
        with patch(
            "aspire_orchestrator.services.ingestion.zoom_ingestion.settings"
        ) as mock_settings:
            mock_settings.zoom_webhook_secret = secret
            result = await adapter.verify_signature(body=body, headers=headers)
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        headers = {"X-Zm-Signature": "v0=bad", "X-Zm-Request-Timestamp": str(int(time.time()))}
        with patch(
            "aspire_orchestrator.services.ingestion.zoom_ingestion.settings"
        ) as mock_settings:
            mock_settings.zoom_webhook_secret = "real"
            result = await adapter.verify_signature(body=b"body", headers=headers)
        assert result is False


class TestZoomRecordingResolveScope:

    @pytest.mark.asyncio
    async def test_valid_account_returns_scope(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.zoom_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            scope = await adapter.resolve_scope(RECORDING_PAYLOAD)
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_unknown_account_raises_404(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.zoom_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(RECORDING_PAYLOAD)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_account_id_raises_422(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope({"payload": {"object": {}}})
        assert exc_info.value.status_code == 422


class TestZoomRecordingBuildEnvelope:

    @pytest.mark.asyncio
    async def test_recording_envelope_fields(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env = await adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        assert env.memory_type == "meeting"
        assert env.idempotency_key == "zoom-recording:meeting-uuid-test-abc"
        assert env.detail["topic"] == "Q1 Review"
        assert env.detail["duration_minutes"] == 60
        assert env.detail["participant_count"] == 5
        assert env.detail["transcript_text"] is None  # filled by transcript adapter

    @pytest.mark.asyncio
    async def test_missing_uuid_raises_error(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        bad_payload = {
            "account_id": "zoom_acct_test123",
            "payload": {"account_id": "zoom_acct_test123", "object": {"topic": "Test"}},
        }
        with pytest.raises(IngestionError, match="MISSING_MEETING_UUID"):
            await adapter.build_envelope(bad_payload, scope=scope, thread=None)

    @pytest.mark.asyncio
    async def test_idempotency_key_stable(self) -> None:
        adapter = ZoomRecordingIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env1 = await adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        env2 = await adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)
        assert env1.idempotency_key == env2.idempotency_key


class TestZoomTranscriptBuildEnvelope:

    @pytest.mark.asyncio
    async def test_transcript_envelope_links_recording(self) -> None:
        recording_mem_id = uuid.uuid4()
        adapter = ZoomTranscriptIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)

        with patch.object(
            adapter,
            "_lookup_recording_memory_id",
            new=AsyncMock(return_value=recording_mem_id),
        ):
            env = await adapter.build_envelope(TRANSCRIPT_PAYLOAD, scope=scope, thread=None)

        assert env.idempotency_key == "zoom-transcript:meeting-uuid-test-abc"
        assert env.detail["transcript_text"] == "This is the meeting transcript."
        assert str(recording_mem_id) in env.detail["linked_memory_ids"]
        assert env.detail["supersedes_idempotency_key"] == "zoom-recording:meeting-uuid-test-abc"

    @pytest.mark.asyncio
    async def test_transcript_trace_matches_recording_trace(self) -> None:
        """Both adapters produce the same trace_id / correlation_id for the same meeting."""
        recording_adapter = ZoomRecordingIngestionAdapter()
        transcript_adapter = ZoomTranscriptIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)

        env_r = await recording_adapter.build_envelope(RECORDING_PAYLOAD, scope=scope, thread=None)

        with patch.object(
            transcript_adapter,
            "_lookup_recording_memory_id",
            new=AsyncMock(return_value=None),
        ):
            env_t = await transcript_adapter.build_envelope(TRANSCRIPT_PAYLOAD, scope=scope, thread=None)

        assert env_r.provenance.trace_id == env_t.provenance.trace_id
        assert env_r.provenance.correlation_id == env_t.provenance.correlation_id
