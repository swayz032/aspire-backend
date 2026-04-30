"""Unit tests for ContractIngestionAdapter — Pass 14 expansion."""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.contract_ingestion import ContractIngestionAdapter
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("cc000000-0000-0000-0000-000000000001")
SUITE_A = UUID("cc000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("cc000000-0000-0000-0000-000000000003")

SCOPE_A = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)

PROVIDER_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "provider": "pandadoc",
    "external_account_id": "ws_contract123",
}

_BASE_PAYLOAD = {
    "event_id": "evt_contract_001",
    "action": "document_state_changed",
    "workspace_id": "ws_contract123",
    "data": {
        "id": "doc_contract_001",
        "name": "Service Agreement",
        "status": "sent",
        "tags": ["contract"],
        "recipients": [{"name": "Bob Smith", "email": "bob@corp.com"}],
    },
}


def _pd_sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

class TestContractVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        body = b'{"event":"document_state_changed"}'
        secret = "pd_secret"
        adapter = ContractIngestionAdapter()
        sig = _pd_sig(body, secret)
        with patch(
            "aspire_orchestrator.services.ingestion.contract_ingestion.settings"
        ) as mock_settings:
            mock_settings.pandadoc_webhook_secret = secret
            result = await adapter.verify_signature(
                body=body, headers={"X-PandaDoc-Signature": sig}
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = ContractIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.contract_ingestion.settings"
        ) as mock_settings:
            mock_settings.pandadoc_webhook_secret = "real_secret"
            result = await adapter.verify_signature(
                body=b"body",
                headers={"X-PandaDoc-Signature": "badhex"},
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_signature_header_returns_false(self) -> None:
        adapter = ContractIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.contract_ingestion.settings"
        ) as mock_settings:
            mock_settings.pandadoc_webhook_secret = "secret"
            result = await adapter.verify_signature(body=b"body", headers={})
        assert result is False


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

class TestContractResolveScope:

    @pytest.mark.asyncio
    async def test_valid_workspace_returns_scope(self) -> None:
        adapter = ContractIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.contract_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            scope = await adapter.resolve_scope(_BASE_PAYLOAD)
        assert scope.tenant_id == TENANT_A
        assert scope.suite_id == SUITE_A
        assert scope.office_id == OFFICE_A

    @pytest.mark.asyncio
    async def test_missing_workspace_id_raises_422(self) -> None:
        adapter = ContractIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope({"data": {}})
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "MISSING_WORKSPACE_ID"

    @pytest.mark.asyncio
    async def test_unknown_workspace_raises_404(self) -> None:
        adapter = ContractIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.contract_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(_BASE_PAYLOAD)
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "UNKNOWN_WORKSPACE"

    @pytest.mark.asyncio
    async def test_cross_tenant_isolation(self) -> None:
        """Different workspace_id → different tenant → can't cross-pollinate."""
        tenant_b_row = {**PROVIDER_ROW_A, "tenant_id": "bb000000-0000-0000-0000-000000000001"}
        adapter = ContractIngestionAdapter()
        payload_b = {**_BASE_PAYLOAD, "workspace_id": "ws_other"}
        with patch(
            "aspire_orchestrator.services.ingestion.contract_ingestion.supabase_select",
            new=AsyncMock(return_value=[tenant_b_row]),
        ):
            scope = await adapter.resolve_scope(payload_b)
        assert scope.tenant_id != TENANT_A


# ---------------------------------------------------------------------------
# Envelope building — happy paths
# ---------------------------------------------------------------------------

class TestContractBuildEnvelope:

    @pytest.mark.asyncio
    async def test_sent_state(self) -> None:
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.memory_type == "contract"
        assert env.status == "drafted"
        assert "sent" in env.title.lower()
        assert env.detail["recipient_name"] == "Bob Smith"
        assert env.idempotency_key == "pandadoc-contract-doc_contract_001-document_state_changed"

    @pytest.mark.asyncio
    async def test_viewed_state_is_approved(self) -> None:
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "viewed"}}
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "approved"
        assert "viewed" in env.title.lower()

    @pytest.mark.asyncio
    async def test_completed_state_is_executed(self) -> None:
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "completed"}}
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "executed"
        assert "signed" in env.title.lower()
        assert "Bob Smith" in env.summary

    @pytest.mark.asyncio
    async def test_rejected_state(self) -> None:
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "rejected"}}
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "rejected"
        assert "rejected" in env.title.lower()

    @pytest.mark.asyncio
    async def test_expired_state_is_failed(self) -> None:
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "expired"}}
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "failed"
        assert "expired" in env.title.lower()

    @pytest.mark.asyncio
    async def test_voided_state_is_failed(self) -> None:
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "voided"}}
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "failed"

    @pytest.mark.asyncio
    async def test_draft_state_raises_200(self) -> None:
        """Draft state is non-actionable — should return 200 without writing."""
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "draft"}}
        adapter = ContractIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_state_raises_200(self) -> None:
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "mystery_state"}}
        adapter = ContractIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 200

    @pytest.mark.asyncio
    async def test_document_prefix_state_normalized(self) -> None:
        """'document.sent' should work same as 'sent'."""
        payload = {**_BASE_PAYLOAD, "data": {**_BASE_PAYLOAD["data"], "status": "document.sent"}}
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.status == "drafted"

    @pytest.mark.asyncio
    async def test_missing_event_id_raises_422(self) -> None:
        payload = {**_BASE_PAYLOAD, "event_id": "", "data": _BASE_PAYLOAD["data"]}
        adapter = ContractIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_idempotency_key_is_deterministic(self) -> None:
        adapter = ContractIngestionAdapter()
        env1 = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        env2 = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env1.idempotency_key == env2.idempotency_key

    @pytest.mark.asyncio
    async def test_detail_fields_present(self) -> None:
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        required_fields = {"document_id", "recipient_name", "recipient_email", "pdf_url", "tags", "status"}
        for field in required_fields:
            assert field in env.detail, f"Missing detail field: {field}"

    @pytest.mark.asyncio
    async def test_no_pii_in_idempotency_key(self) -> None:
        """idempotency_key must not contain PII (email, full name)."""
        adapter = ContractIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert "bob@corp.com" not in env.idempotency_key
        assert "Bob Smith" not in env.idempotency_key
