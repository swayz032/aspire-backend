"""Unit tests for QuoteIngestionAdapter — Pass 14 Gate Item 2."""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.quote_ingestion import QuoteIngestionAdapter
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

PROVIDER_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "provider": "pandadoc",
    "external_account_id": "ws_test123",
}

_BASE_PAYLOAD = {
    "event_id": "evt_pd_001",
    "action": "document_state_changed",
    "workspace_id": "ws_test123",
    "data": {
        "status": "sent",
        "id": "doc_001",
        "name": "Test Quote",
        "recipients": [{"name": "Jane Doe", "email": "jane@acme.com"}],
        "grand_total": 1200.0,
        "items": [{"name": "Consulting", "price": 1200.0, "qty": 1}],
    },
}


def _pd_sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestQuoteVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        body = b'{"event":"document_state_changed"}'
        secret = "pd_secret"
        adapter = QuoteIngestionAdapter()
        sig = _pd_sig(body, secret)
        with patch(
            "aspire_orchestrator.services.ingestion.quote_ingestion.settings"
        ) as mock_settings:
            mock_settings.pandadoc_webhook_secret = secret
            result = await adapter.verify_signature(
                body=body, headers={"X-PandaDoc-Signature": sig}
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = QuoteIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.quote_ingestion.settings"
        ) as mock_settings:
            mock_settings.pandadoc_webhook_secret = "real"
            result = await adapter.verify_signature(
                body=b"body",
                headers={"X-PandaDoc-Signature": "deadbeef"},
            )
        assert result is False


class TestQuoteResolveScope:

    @pytest.mark.asyncio
    async def test_valid_workspace_returns_scope(self) -> None:
        adapter = QuoteIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.quote_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            scope = await adapter.resolve_scope(_BASE_PAYLOAD)
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_missing_workspace_id_raises_422(self) -> None:
        adapter = QuoteIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope({"data": {}})
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_workspace_raises_404(self) -> None:
        adapter = QuoteIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.quote_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(_BASE_PAYLOAD)
        assert exc_info.value.status_code == 404


class TestQuoteBuildEnvelope:

    @pytest.mark.asyncio
    async def test_sent_state_fields(self) -> None:
        adapter = QuoteIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=scope, thread=None)
        assert env.memory_type == "quote"
        assert env.status == "drafted"
        assert "sent" in env.detail["status"]
        assert env.detail["entity"] == "Jane Doe"
        assert env.idempotency_key == "pandadoc-evt_pd_001-document_state_changed"

    @pytest.mark.asyncio
    async def test_completed_state_is_executed(self) -> None:
        adapter = QuoteIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "completed"}}
        env = await adapter.build_envelope(payload, scope=scope, thread=None)
        assert env.status == "executed"
        assert "accepted" in env.title

    @pytest.mark.asyncio
    async def test_declined_state_is_rejected(self) -> None:
        adapter = QuoteIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "declined"}}
        env = await adapter.build_envelope(payload, scope=scope, thread=None)
        assert env.status == "rejected"
        assert "declined" in env.title

    @pytest.mark.asyncio
    async def test_non_actionable_state_raises_200(self) -> None:
        adapter = QuoteIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "draft"}}
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=scope, thread=None)
        assert exc_info.value.status_code == 200

    @pytest.mark.asyncio
    async def test_idempotency_key_is_deterministic(self) -> None:
        adapter = QuoteIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env1 = await adapter.build_envelope(_BASE_PAYLOAD, scope=scope, thread=None)
        env2 = await adapter.build_envelope(_BASE_PAYLOAD, scope=scope, thread=None)
        assert env1.idempotency_key == env2.idempotency_key
