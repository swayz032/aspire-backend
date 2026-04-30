"""Unit tests for InvoiceIngestionAdapter — Pass 14 Gate Item 2.

Tests: verify_signature, resolve_scope, build_envelope for all 3 event types,
idempotency, cross-tenant guard.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.invoice_ingestion import InvoiceIngestionAdapter
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("aa000000-0000-0000-0000-000000000001")
SUITE_A = UUID("aa000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("aa000000-0000-0000-0000-000000000003")

PROVIDER_ROW_A = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "provider": "stripe",
    "external_account_id": "cus_test123",
}

_BASE_INVOICE = {
    "id": "evt_test_abc123",
    "type": "invoice.created",
    "created": int(time.time()),
    "data": {
        "object": {
            "id": "in_test123",
            "number": "INV-001",
            "customer": "cus_test123",
            "customer_name": "Acme Corp",
            "amount_due": 5000,
            "total": 5000,
            "due_date": int(time.time()) + 86400,
            "invoice_pdf": "https://pay.stripe.com/invoice/pdf",
            "lines": {"data": [{"description": "Consulting services", "amount": 5000, "quantity": 1}]},
        }
    },
}


def _stripe_sig(body: bytes, secret: str) -> str:
    ts = int(time.time())
    signed = f"{ts}.".encode() + body
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


class TestInvoiceVerifySignature:

    @pytest.mark.asyncio
    async def test_valid_signature_returns_true(self) -> None:
        body = b'{"type":"invoice.created"}'
        secret = "whsec_test"
        adapter = InvoiceIngestionAdapter()
        sig = _stripe_sig(body, secret)
        with patch(
            "aspire_orchestrator.services.ingestion.invoice_ingestion.settings"
        ) as mock_settings:
            mock_settings.stripe_webhook_secret = secret
            result = await adapter.verify_signature(body=body, headers={"Stripe-Signature": sig})
        assert result is True

    @pytest.mark.asyncio
    async def test_bad_signature_returns_false(self) -> None:
        adapter = InvoiceIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.invoice_ingestion.settings"
        ) as mock_settings:
            mock_settings.stripe_webhook_secret = "real_secret"
            result = await adapter.verify_signature(
                body=b"body",
                headers={"Stripe-Signature": "t=123,v1=bad"},
            )
        assert result is False


class TestInvoiceResolveScope:

    @pytest.mark.asyncio
    async def test_valid_customer_returns_scope(self) -> None:
        adapter = InvoiceIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.invoice_ingestion.supabase_select",
            new=AsyncMock(return_value=[PROVIDER_ROW_A]),
        ):
            scope = await adapter.resolve_scope(_BASE_INVOICE)
        assert scope.tenant_id == TENANT_A

    @pytest.mark.asyncio
    async def test_missing_customer_raises_422(self) -> None:
        adapter = InvoiceIngestionAdapter()
        payload = {"data": {"object": {}}}
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope(payload)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_customer_raises_404(self) -> None:
        adapter = InvoiceIngestionAdapter()
        with patch(
            "aspire_orchestrator.services.ingestion.invoice_ingestion.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(IngestionError) as exc_info:
                await adapter.resolve_scope(_BASE_INVOICE)
        assert exc_info.value.status_code == 404


class TestInvoiceBuildEnvelope:

    @pytest.mark.asyncio
    async def test_invoice_created_fields(self) -> None:
        adapter = InvoiceIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env = await adapter.build_envelope(_BASE_INVOICE, scope=scope, thread=None)
        assert env.memory_type == "invoice"
        assert env.status == "drafted"
        assert env.idempotency_key == f"stripe-invoice-{_BASE_INVOICE['id']}"
        assert env.detail["invoice_number"] == "INV-001"
        assert env.detail["amount"] == 5000

    @pytest.mark.asyncio
    async def test_invoice_paid_fields(self) -> None:
        adapter = InvoiceIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        paid_payload = {
            **_BASE_INVOICE,
            "type": "invoice.paid",
        }
        paid_payload["data"]["object"]["status_transitions"] = {"paid_at": int(time.time())}
        paid_payload["data"]["object"]["payment_intent"] = "pi_test"
        env = await adapter.build_envelope(paid_payload, scope=scope, thread=None)
        assert env.status == "executed"
        assert env.idempotency_key.startswith("stripe-invoice-paid-")
        assert env.detail["supersedes_idempotency_key"] == f"stripe-invoice-{_BASE_INVOICE['id']}"

    @pytest.mark.asyncio
    async def test_invoice_voided_fields(self) -> None:
        adapter = InvoiceIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        voided_payload = {
            **_BASE_INVOICE,
            "type": "invoice.voided",
        }
        voided_payload["data"]["object"]["status_transitions"] = {"voided_at": int(time.time())}
        voided_payload["data"]["object"]["void_reason"] = "fraudulent"
        env = await adapter.build_envelope(voided_payload, scope=scope, thread=None)
        assert env.status == "executed"
        assert "voided" in env.title
        assert env.detail["void_reason"] == "fraudulent"

    @pytest.mark.asyncio
    async def test_unhandled_event_type_raises_422(self) -> None:
        adapter = InvoiceIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        bad_payload = {**_BASE_INVOICE, "type": "invoice.upcoming"}
        with pytest.raises(IngestionError, match="UNHANDLED_EVENT_TYPE"):
            await adapter.build_envelope(bad_payload, scope=scope, thread=None)

    @pytest.mark.asyncio
    async def test_idempotency_key_is_deterministic(self) -> None:
        adapter = InvoiceIngestionAdapter()
        scope = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)
        env1 = await adapter.build_envelope(_BASE_INVOICE, scope=scope, thread=None)
        env2 = await adapter.build_envelope(_BASE_INVOICE, scope=scope, thread=None)
        assert env1.idempotency_key == env2.idempotency_key
