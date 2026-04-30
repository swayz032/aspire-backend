"""Unit tests for SMSIngestionAdapter — Pass 14 Gate Item 2.

Tests: verify_signature, resolve_scope, build_envelope, idempotency guard,
cross-tenant scope mismatch.

All supabase_client and MemoryService calls are mocked — no real DB.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.sms_ingestion import SMSIngestionAdapter
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

TENANT_B = UUID("bb000000-0000-0000-0000-000000000001")
SUITE_B = UUID("bb000000-0000-0000-0000-000000000002")
OFFICE_B = UUID("bb000000-0000-0000-0000-000000000003")

PHONE_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "phone_number": "+12125550198",
}

SMS_PAYLOAD = {
    "MessageSid": "SMtest1234567890abcdef1234567890ab",
    "AccountSid": "ACtest",
    "From": "+15551234567",
    "To": "+12125550198",
    "Body": "Hello from test",
    "NumMedia": "0",
    "MessageStatus": "received",
}


def _twilio_sig(body: bytes, url: str, params: dict | None, token: str) -> str:
    s = url
    if params:
        for k in sorted(params.keys()):
            s += f"{k}{params[k]}"
    digest = hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _make_headers(body: bytes = b"", valid: bool = True, token: str = "test_token") -> dict:
    url = "https://www.aspireos.app/v1/ingest/twilio/sms"
    sig = _twilio_sig(body, url, None, token) if valid else "invalidsig"
    return {
        "X-Twilio-Signature": sig,
        "X-Aspire-Webhook-Url": url,
        "X-Aspire-Form-Params": "",
    }


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestSMSVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        adapter = SMSIngestionAdapter()
        url = "https://www.aspireos.app/v1/ingest/twilio/sms"
        token = "test_auth_token"
        sig = _twilio_sig(b"", url, None, token)
        headers = {
            "X-Twilio-Signature": sig,
            "X-Aspire-Webhook-Url": url,
            "X-Aspire-Form-Params": "",
        }
        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.settings"
        ) as mock_settings:
            mock_settings.twilio_auth_token = token
            result = await adapter.verify_signature(body=b"", headers=headers)
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = SMSIngestionAdapter()
        headers = {
            "X-Twilio-Signature": "bad_signature",
            "X-Aspire-Webhook-Url": "https://www.aspireos.app/v1/ingest/twilio/sms",
            "X-Aspire-Form-Params": "",
        }
        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.settings"
        ) as mock_settings:
            mock_settings.twilio_auth_token = "real_token"
            result = await adapter.verify_signature(body=b"", headers=headers)
        assert result is False


# ---------------------------------------------------------------------------
# resolve_scope
# ---------------------------------------------------------------------------


class TestSMSResolveScope:

    @pytest.mark.asyncio
    async def test_valid_to_number_returns_scope(self) -> None:
        adapter = SMSIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[PHONE_ROW_A]),
        ):
            scope = await adapter.resolve_scope(SMS_PAYLOAD)
        assert scope.tenant_id == TENANT_A
        assert scope.suite_id == SUITE_A

    @pytest.mark.asyncio
    async def test_missing_to_number_raises_ingestion_error(self) -> None:
        adapter = SMSIngestionAdapter()
        with pytest.raises(IngestionError, match="MISSING_TO_NUMBER"):
            await adapter.resolve_scope({"From": "+15551234567"})

    @pytest.mark.asyncio
    async def test_unknown_number_returns_404(self) -> None:
        adapter = SMSIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(SMS_PAYLOAD)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_db_error_returns_503(self) -> None:
        from aspire_orchestrator.services.supabase_client import SupabaseClientError
        adapter = SMSIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.sms_ingestion.supabase_select",
            new=AsyncMock(side_effect=SupabaseClientError("DB down")),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(SMS_PAYLOAD)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# build_envelope
# ---------------------------------------------------------------------------


class TestSMSBuildEnvelope:

    @pytest.mark.asyncio
    async def test_returns_memory_object_in_with_required_fields(self) -> None:
        adapter = SMSIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        envelope = await adapter.build_envelope(SMS_PAYLOAD, scope=scope, thread=None)

        assert envelope.memory_type == "sms_thread"
        assert envelope.idempotency_key == f"twilio-sms-inbound:{SMS_PAYLOAD['MessageSid']}"
        assert envelope.title == "SMS from +15551234567"
        assert "Hello from test" in envelope.summary
        assert envelope.detail["from"] == "+15551234567"
        assert envelope.detail["to"] == "+12125550198"
        assert envelope.detail["message_sid"] == SMS_PAYLOAD["MessageSid"]
        assert envelope.scope == scope
        assert envelope.provenance.runtime_family == "provider_webhook"

    @pytest.mark.asyncio
    async def test_missing_message_sid_raises_error(self) -> None:
        adapter = SMSIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        bad_payload = {k: v for k, v in SMS_PAYLOAD.items() if k != "MessageSid"}
        with pytest.raises(IngestionError, match="MISSING_MESSAGE_SID"):
            await adapter.build_envelope(bad_payload, scope=scope, thread=None)

    @pytest.mark.asyncio
    async def test_long_body_is_truncated_in_summary(self) -> None:
        adapter = SMSIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {**SMS_PAYLOAD, "Body": "x" * 200}
        envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
        assert len(envelope.summary) <= 145  # 140 + "…"

    @pytest.mark.asyncio
    async def test_mms_media_urls_collected(self) -> None:
        adapter = SMSIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {
            **SMS_PAYLOAD,
            "NumMedia": "2",
            "MediaUrl0": "https://api.twilio.com/media/1",
            "MediaUrl1": "https://api.twilio.com/media/2",
        }
        envelope = await adapter.build_envelope(payload, scope=scope, thread=None)
        assert envelope.detail["num_media"] == 2
        assert len(envelope.detail["media_urls"]) == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestSMSIdempotency:

    @pytest.mark.asyncio
    async def test_same_message_sid_same_idempotency_key(self) -> None:
        adapter = SMSIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env1 = await adapter.build_envelope(SMS_PAYLOAD, scope=scope, thread=None)
        env2 = await adapter.build_envelope(SMS_PAYLOAD, scope=scope, thread=None)
        assert env1.idempotency_key == env2.idempotency_key
