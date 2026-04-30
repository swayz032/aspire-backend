"""Unit tests for ElevenLabsIngestionAdapter — Pass 14 Gate Item 2."""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.elevenlabs_ingestion import (
    ElevenLabsIngestionAdapter,
    _resolve_el_scope,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity, MemoryObjectOut

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

PHONE_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "phone_number": "+12125550198",
}

PROVIDER_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
}

EL_PAYLOAD = {
    "type": "post_call_transcription",
    "event_timestamp": time.time(),
    "data": {
        "agent_id": "agent_test123",
        "conversation_id": "conv_test_abc",
        "status": "done",
        "transcript": [
            {"role": "agent", "message": "Hello how can I help?", "time_in_call_secs": 0.5},
            {"role": "user", "message": "I need support", "time_in_call_secs": 2.1},
        ],
        "metadata": {
            "duration_secs": 45,
            "called_number": "+12125550198",
        },
        "analysis": {
            "transcript_summary": "User requested support. Agent helped.",
        },
    },
}


def _el_sig(body: bytes, secret: str) -> str:
    ts = int(time.time())
    signed = f"{ts}.".encode() + body
    v0 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={v0}"


class TestELVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        body = b'{"type":"post_call_transcription"}'
        secret = "el_secret"
        adapter = ElevenLabsIngestionAdapter()
        sig = _el_sig(body, secret)
        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.settings"
        ) as mock_settings:
            mock_settings.elevenlabs_webhook_secret = secret
            result = await adapter.verify_signature(
                body=body, headers={"ElevenLabs-Signature": sig}
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = ElevenLabsIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.settings"
        ) as mock_settings:
            mock_settings.elevenlabs_webhook_secret = "real"
            result = await adapter.verify_signature(
                body=b"body", headers={"ElevenLabs-Signature": "t=123,v0=bad"}
            )
        assert result is False


class TestELResolveScope:

    @pytest.mark.asyncio
    async def test_resolves_via_phone_number_first(self) -> None:
        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.supabase_select",
            new=AsyncMock(return_value=[PHONE_ROW_A]),
        ):
            scope = await _resolve_el_scope("+12125550198", "agent_test123")
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_id_when_phone_not_found(self) -> None:
        call_count = [0]

        async def _mock_select(table: str, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return []  # phone lookup empty
            return [PROVIDER_ROW_A]  # agent_id lookup succeeds

        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.supabase_select",
            new=_mock_select,
        ):
            scope = await _resolve_el_scope("+10000000000", "agent_test123")
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_unknown_agent_raises_404(self) -> None:
        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await _resolve_el_scope(None, "agent_unknown")
        assert exc_info.value.status_code == 404


class TestELIngest:

    @pytest.mark.asyncio
    async def test_two_writes_on_valid_payload(self) -> None:
        import uuid

        fake_transcript_memory = MagicMock(spec=MemoryObjectOut)
        fake_transcript_memory.memory_id = uuid.uuid4()
        fake_summary_memory = MagicMock(spec=MemoryObjectOut)
        fake_summary_memory.memory_id = uuid.uuid4()

        write_calls = []

        async def mock_write(envelope, *, scope, embed):
            write_calls.append(envelope.memory_type)
            if envelope.memory_type == "transcript":
                return fake_transcript_memory
            return fake_summary_memory

        secret = "el_secret"
        body = b'{"type":"post_call_transcription"}'
        sig = _el_sig(body, secret)

        adapter = ElevenLabsIngestionAdapter()
        adapter._memory_service.write = mock_write

        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.settings"
        ) as mock_settings, patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.supabase_select",
            new=AsyncMock(return_value=[PHONE_ROW_A]),
        ):
            mock_settings.elevenlabs_webhook_secret = secret
            result = await adapter.ingest(
                body=body,
                headers={"ElevenLabs-Signature": sig},
                payload=EL_PAYLOAD,
            )

        assert write_calls == ["transcript", "session_summary"]
        assert result.memory == fake_summary_memory

    @pytest.mark.asyncio
    async def test_bad_signature_raises_401(self) -> None:
        adapter = ElevenLabsIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.settings"
        ) as mock_settings:
            mock_settings.elevenlabs_webhook_secret = "real"
            with pytest.raises(IngestionError) as exc_info:
                await adapter.ingest(
                    body=b"body",
                    headers={"ElevenLabs-Signature": "t=123,v0=bad"},
                    payload=EL_PAYLOAD,
                )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_conversation_id_raises_422(self) -> None:
        secret = "el_secret"
        body = b'{"type":"post_call_transcription"}'
        sig = _el_sig(body, secret)
        bad_payload = {
            "type": "post_call_transcription",
            "data": {"agent_id": "agent_test", "conversation_id": "", "metadata": {}},
        }
        adapter = ElevenLabsIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.elevenlabs_ingestion.settings"
        ) as mock_settings:
            mock_settings.elevenlabs_webhook_secret = secret
            with pytest.raises(IngestionError) as exc_info:
                await adapter.ingest(body=body, headers={"ElevenLabs-Signature": sig}, payload=bad_payload)
        assert exc_info.value.status_code == 422
