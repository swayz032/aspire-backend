"""Unit tests for AnamIngestionAdapter — Pass 14 Gate Item 2."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.anam_ingestion import (
    AnamIngestionAdapter,
    _resolve_anam_scope,
)
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity, MemoryObjectOut

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

PROVIDER_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "provider": "anam",
    "external_account_id": "persona_ava_test",
}

ANAM_PAYLOAD = {
    "event": "session.ended",
    "session": {
        "persona_id": "persona_ava_test",
        "session_id": "sess_test_abc123",
        "duration_seconds": 90,
        "transcript": [
            {"role": "agent", "message": "How can I help you today?"},
            {"role": "user", "message": "I want to know about pricing"},
        ],
        "metadata": {
            "tenant_id": None,
            "handoff_id": None,
        },
    },
}


def _anam_sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestAnamVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        body = b'{"event":"session.ended"}'
        secret = "anam_secret"
        adapter = AnamIngestionAdapter()
        sig = _anam_sig(body, secret)
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.settings"
        ) as mock_settings:
            mock_settings.anam_webhook_secret = secret
            result = await adapter.verify_signature(
                body=body, headers={"X-Anam-Signature": sig}
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = AnamIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.settings"
        ) as mock_settings:
            mock_settings.anam_webhook_secret = "real"
            result = await adapter.verify_signature(
                body=b"body", headers={"X-Anam-Signature": "deadbeef"}
            )
        assert result is False


class TestAnamResolveScope:

    @pytest.mark.asyncio
    async def test_resolves_via_persona_id(self) -> None:
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            scope = await _resolve_anam_scope(None, "persona_ava_test")
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_resolves_via_direct_tenant_id(self) -> None:
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            scope = await _resolve_anam_scope(str(TENANT_A), "persona_ava_test")
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_unknown_persona_raises_404(self) -> None:
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await _resolve_anam_scope(None, "persona_unknown")
        assert exc_info.value.status_code == 404


class TestAnamIngest:

    @pytest.mark.asyncio
    async def test_two_writes_on_valid_payload(self) -> None:
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

        secret = "anam_secret"
        body = b'{"event":"session.ended"}'
        sig = _anam_sig(body, secret)

        adapter = AnamIngestionAdapter()
        adapter._memory_service.write = mock_write

        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.settings"
        ) as mock_settings, patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            mock_settings.anam_webhook_secret = secret
            result = await adapter.ingest(
                body=body,
                headers={"X-Anam-Signature": sig},
                payload=ANAM_PAYLOAD,
            )

        assert write_calls == ["transcript", "session_summary"]
        assert result.memory == fake_summary_memory

    @pytest.mark.asyncio
    async def test_bad_signature_raises_401(self) -> None:
        adapter = AnamIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.settings"
        ) as mock_settings:
            mock_settings.anam_webhook_secret = "real"
            with pytest.raises(IngestionError) as exc_info:
                await adapter.ingest(
                    body=b"body",
                    headers={"X-Anam-Signature": "bad"},
                    payload=ANAM_PAYLOAD,
                )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_session_id_raises_422(self) -> None:
        secret = "anam_secret"
        body = b'{"event":"session.ended"}'
        sig = _anam_sig(body, secret)
        payload = {
            "event": "session.ended",
            "session": {"persona_id": "persona_ava_test", "session_id": "", "metadata": {}},
        }
        adapter = AnamIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.settings"
        ) as mock_settings:
            mock_settings.anam_webhook_secret = secret
            with pytest.raises(IngestionError) as exc_info:
                await adapter.ingest(body=body, headers={"X-Anam-Signature": sig}, payload=payload)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_handoff_id_linked_when_prior_memory_exists(self) -> None:
        """When handoff_id is set and a prior memory exists, summary includes its ID."""
        handoff_mem_id = uuid.uuid4()
        fake_transcript_memory = MagicMock(spec=MemoryObjectOut)
        fake_transcript_memory.memory_id = uuid.uuid4()
        fake_summary_memory = MagicMock(spec=MemoryObjectOut)
        fake_summary_memory.memory_id = uuid.uuid4()
        captured_detail = {}

        async def mock_write(envelope, *, scope, embed):
            if envelope.memory_type == "session_summary":
                captured_detail.update(envelope.detail)
                return fake_summary_memory
            return fake_transcript_memory

        secret = "anam_secret"
        body = b'{"event":"session.ended"}'
        sig = _anam_sig(body, secret)
        payload_with_handoff = {
            **ANAM_PAYLOAD,
            "session": {
                **ANAM_PAYLOAD["session"],
                "metadata": {"tenant_id": None, "handoff_id": "el-conv:corr:voice123"},
            },
        }

        adapter = AnamIngestionAdapter()
        adapter._memory_service.write = mock_write

        with patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.settings"
        ) as mock_settings, patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ), patch(
            "aspire_orchestrator.services.ingestion.anam_ingestion._resolve_handoff_memory_id",
            new=AsyncMock(return_value=handoff_mem_id),
        ):
            mock_settings.anam_webhook_secret = secret
            await adapter.ingest(body=body, headers={"X-Anam-Signature": sig}, payload=payload_with_handoff)

        assert str(handoff_mem_id) in captured_detail.get("linked_memory_ids", [])
