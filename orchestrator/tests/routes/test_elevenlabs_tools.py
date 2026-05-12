"""Tests for ElevenLabs webhook tool handlers (Pass G — Front Desk Hub).

Covers:
  POST /v1/elevenlabs/tools/lookup_contact
  POST /v1/elevenlabs/tools/create_appointment_request
  POST /v1/elevenlabs/tools/verify_caller_identity

Evil / Security tests:
  - Missing phone AND email → 400 (lookup_contact)
  - Invalid method → 400 (verify_caller_identity)
  - Invalid window ISO 8601 → 400 (create_appointment_request)
  - PII (phone) redacted in receipts (Law #9)
  - Receipt cut on every state-changing call (Law #2)
  - DB failure → graceful error response, not unhandled 500
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.elevenlabs_tools import router as el_tools_router

_app = FastAPI()
_app.include_router(el_tools_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "dd000000-0000-0000-0000-000000000001"
OFFICE_ID = "dd000000-0000-0000-0000-000000000002"
TENANT_ID = "dd000000-0000-0000-0000-000000000003"

_PHONE_NUMBER = "+14482885386"
_CALLED_NUMBER = "+18005551234"

_SCOPE = {
    "tenant_id": TENANT_ID,
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
}

_CONTACT_ROW = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
    "role": "owner",
    "label": "Alice Contractor",
    "phone": _PHONE_NUMBER,
    "email": "alice@example.com",
    "is_active": True,
    "tags": ["VIP"],
}

_PHONE_NUMBER_ROW = [
    {
        "tenant_id": TENANT_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "phone_number": _CALLED_NUMBER,
        "status": "active",
    }
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope_side_effect(table, filters, **kw):
    """Mock supabase_select: tenant_phone_numbers → scope, rest → empty."""
    if table == "tenant_phone_numbers":
        return _PHONE_NUMBER_ROW
    return []


# ---------------------------------------------------------------------------
# lookup_contact
# ---------------------------------------------------------------------------


def test_lookup_contact_found_by_phone():
    """Known phone → found=True, contact details returned."""

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        if table == "front_desk_routing_contacts":
            return [_CONTACT_ROW]
        return []

    with patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select):
        r = _client.post(
            "/v1/elevenlabs/tools/lookup_contact",
            json={"phone": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    assert data["contact"]["name"] == "Alice Contractor"


def test_lookup_contact_not_found():
    """Unknown phone → found=False, no error."""

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select):
        r = _client.post(
            "/v1/elevenlabs/tools/lookup_contact",
            json={"phone": "+19995550000", "called_number": _CALLED_NUMBER},
        )
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_lookup_contact_missing_phone_and_email_400():
    """Neither phone nor email → 400 INVALID_INPUT."""
    r = _client.post(
        "/v1/elevenlabs/tools/lookup_contact",
        json={"called_number": _CALLED_NUMBER},
    )
    assert r.status_code == 400
    assert "INVALID_INPUT" in r.text


def test_lookup_contact_receipt_emitted():
    """lookup_contact emits a receipt on every call (Law #2)."""
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        _client.post(
            "/v1/elevenlabs/tools/lookup_contact",
            json={"phone": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )

    assert any(rec.get("receipt_type") == "lookup_contact" for rec in stored)


def test_lookup_contact_phone_redacted_in_receipt():
    """Full phone number must not appear in receipt redacted_inputs (Law #9)."""
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        _client.post(
            "/v1/elevenlabs/tools/lookup_contact",
            json={"phone": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )

    for rec in stored:
        inputs = rec.get("redacted_inputs", {})
        phone_in_receipt = inputs.get("phone", "")
        assert _PHONE_NUMBER not in phone_in_receipt, f"full phone leaked: {phone_in_receipt}"


# ---------------------------------------------------------------------------
# create_appointment_request
# ---------------------------------------------------------------------------


def test_create_appointment_success_emits_receipt():
    """Valid appointment request → proposed=True, awaiting_approval=True, receipt emitted."""
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    async def mock_insert(table, data):
        return data

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        r = _client.post(
            "/v1/elevenlabs/tools/create_appointment_request",
            json={
                "window": {"start": "2026-05-15T09:00:00+00:00", "end": "2026-05-15T11:00:00+00:00"},
                "intent": "HVAC inspection and cleaning",
                "contact": {"phone": _PHONE_NUMBER, "name": "Bob Smith"},
                "called_number": _CALLED_NUMBER,
            },
        )

    assert r.status_code == 200
    data = r.json()
    assert data["proposed"] is True
    assert data["awaiting_approval"] is True
    assert "proposal_id" in data
    assert "receipt_id" in data
    assert any(rec.get("receipt_type") == "appointment_proposal_created" for rec in stored)


def test_create_appointment_invalid_window_400():
    """Bad ISO 8601 in window.start → 400 INVALID_INPUT."""
    r = _client.post(
        "/v1/elevenlabs/tools/create_appointment_request",
        json={
            "window": {"start": "not-a-date", "end": "2026-05-15T11:00:00+00:00"},
            "intent": "inspection",
            "contact": {"phone": _PHONE_NUMBER},
            "called_number": _CALLED_NUMBER,
        },
    )
    assert r.status_code == 400
    assert "INVALID_INPUT" in r.text


def test_create_appointment_db_failure_returns_error_not_500():
    """DB write failure → proposed=False, no unhandled 500."""
    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    async def mock_insert(table, data):
        raise SupabaseClientError("insert/approval_requests", 503, "DB down")

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_insert", side_effect=mock_insert),
    ):
        r = _client.post(
            "/v1/elevenlabs/tools/create_appointment_request",
            json={
                "window": {"start": "2026-05-15T09:00:00+00:00", "end": "2026-05-15T11:00:00+00:00"},
                "intent": "inspection",
                "contact": {"phone": _PHONE_NUMBER},
                "called_number": _CALLED_NUMBER,
            },
        )

    assert r.status_code == 200  # Returns graceful degraded response (not 500)
    assert r.json()["proposed"] is False


# ---------------------------------------------------------------------------
# verify_caller_identity
# ---------------------------------------------------------------------------


def test_verify_caller_identity_otp_success():
    """OTP method → instructions + session_token returned."""

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select):
        r = _client.post(
            "/v1/elevenlabs/tools/verify_caller_identity",
            json={"method": "otp", "target": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["method"] == "otp"
    assert "instructions" in data
    assert "session_token" in data
    assert "receipt_id" in data


def test_verify_caller_identity_factual_method():
    """Factual method → different instructions."""

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select):
        r = _client.post(
            "/v1/elevenlabs/tools/verify_caller_identity",
            json={"method": "factual", "target": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["method"] == "factual"
    assert "name and zip" in data["instructions"].lower()


def test_verify_caller_identity_invalid_method_400():
    """Unknown method → 400 INVALID_INPUT."""
    r = _client.post(
        "/v1/elevenlabs/tools/verify_caller_identity",
        json={"method": "biometric", "target": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
    )
    assert r.status_code == 400
    assert "INVALID_INPUT" in r.text


def test_verify_caller_identity_receipt_emitted():
    """verify_caller_identity emits receipt on every call (Law #2)."""
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        _client.post(
            "/v1/elevenlabs/tools/verify_caller_identity",
            json={"method": "otp", "target": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )

    assert any(rec.get("receipt_type") == "caller_identity_verification_initiated" for rec in stored)


def test_verify_caller_identity_target_redacted_in_receipt():
    """Target (phone/email) must not appear verbatim in receipt (Law #9)."""
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        if table == "tenant_phone_numbers":
            return _PHONE_NUMBER_ROW
        return []

    with (
        patch("aspire_orchestrator.routes.elevenlabs_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        _client.post(
            "/v1/elevenlabs/tools/verify_caller_identity",
            json={"method": "otp", "target": _PHONE_NUMBER, "called_number": _CALLED_NUMBER},
        )

    for rec in stored:
        inputs = rec.get("redacted_inputs", {})
        target_in_receipt = inputs.get("target_prefix", "")
        assert _PHONE_NUMBER not in target_in_receipt, f"full target leaked: {target_in_receipt}"
