"""Tests for callback_promises routes (Pass G — Front Desk Hub).

Covers:
  - GET /v1/callbacks — list by bucket, tenant-scoped
  - PATCH /v1/callbacks/{id} — reschedule, receipt emitted
  - POST /v1/callbacks/{id}/complete — mark complete, receipt emitted
  - RLS evil tests: cross-suite reads return 404, not data
  - Missing auth headers → 401
  - Invalid input (bad due_at) → 400, not 500
  - Receipt cut on every write (Law #2)
  - Phone numbers redacted in receipts (Law #9)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.callbacks import router as callbacks_router

_app = FastAPI()
_app.include_router(callbacks_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "bb000000-0000-0000-0000-000000000001"
OTHER_SUITE_ID = "cc000000-0000-0000-0000-000000000099"
OFFICE_ID = "bb000000-0000-0000-0000-000000000002"
TENANT_ID = "bb000000-0000-0000-0000-000000000003"

_SCOPE_HEADERS = {
    "x-tenant-id": TENANT_ID,
    "x-suite-id": SUITE_ID,
    "x-office-id": OFFICE_ID,
}

_OTHER_SCOPE_HEADERS = {
    "x-tenant-id": TENANT_ID,
    "x-suite-id": OTHER_SUITE_ID,
    "x-office-id": OFFICE_ID,
}

_CALLBACK_ROW = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
    "contact_phone": "+14155550001",
    "contact_name": "Bob Plumber",
    "promise_context": "Wants quote for water heater",
    "due_at": "2026-05-12T14:00:00+00:00",
    "status": "due_today",
    "created_at": "2026-05-12T08:00:00+00:00",
}


# ---------------------------------------------------------------------------
# GET /v1/callbacks
# ---------------------------------------------------------------------------


def test_list_callbacks_success():
    with patch(
        "aspire_orchestrator.routes.callbacks.supabase_select",
        new_callable=AsyncMock,
        return_value=[_CALLBACK_ROW],
    ):
        r = _client.get("/v1/callbacks", headers=_SCOPE_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["items"][0]["id"] == _CALLBACK_ROW["id"]


def test_list_callbacks_bucket_filter():
    """Bucket parameter is forwarded to query filter (due_today only)."""
    call_args = []

    async def mock_select(table, filters, **kwargs):
        call_args.append(filters)
        return []

    with patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select):
        r = _client.get("/v1/callbacks?bucket=due_today", headers=_SCOPE_HEADERS)
    assert r.status_code == 200
    assert "due_today" in call_args[0]


def test_list_callbacks_missing_headers_401():
    """Missing scope headers → 401 (Law #3 fail-closed)."""
    r = _client.get("/v1/callbacks")
    assert r.status_code == 401
    assert "MISSING_SCOPE_HEADERS" in r.text


def test_list_callbacks_db_error_502():
    """DB failure → 502, not 500."""
    from aspire_orchestrator.services.supabase_client import SupabaseClientError
    with patch(
        "aspire_orchestrator.routes.callbacks.supabase_select",
        new_callable=AsyncMock,
        side_effect=SupabaseClientError("select/callback_promises", 503, "timeout"),
    ):
        r = _client.get("/v1/callbacks", headers=_SCOPE_HEADERS)
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# PATCH /v1/callbacks/{id} — reschedule
# ---------------------------------------------------------------------------


def test_reschedule_callback_success_emits_receipt():
    """Reschedule emits receipt_type=callback_rescheduled (Law #2)."""
    cb_id = str(uuid.uuid4())
    stored: list[list] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kwargs):
        if table == "callback_promises":
            return [{**_CALLBACK_ROW, "id": cb_id}]
        return []

    async def mock_update(table, filters, data):
        return {}

    with (
        patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.callbacks.supabase_update", side_effect=mock_update),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        r = _client.patch(
            f"/v1/callbacks/{cb_id}",
            json={"due_at": "2026-05-13T10:00:00+00:00"},
            headers=_SCOPE_HEADERS,
        )

    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert "receipt_id" in data
    assert any(rec.get("receipt_type") == "callback_rescheduled" for rec in stored)
    assert any(rec.get("outcome") == "success" for rec in stored)


def test_reschedule_callback_invalid_due_at_400():
    """Invalid ISO 8601 → 400, not 500 (INVALID_INPUT)."""
    cb_id = str(uuid.uuid4())
    r = _client.patch(
        f"/v1/callbacks/{cb_id}",
        json={"due_at": "not-a-date"},
        headers=_SCOPE_HEADERS,
    )
    assert r.status_code == 400
    assert "INVALID_INPUT" in r.text


def test_reschedule_callback_cross_suite_404():
    """Cross-suite reschedule → 404 (no cross-tenant hints, Law #6)."""
    cb_id = str(uuid.uuid4())

    async def mock_select(table, filters, **kwargs):
        # Simulates RLS: returns empty when suite_id doesn't match
        if OTHER_SUITE_ID in filters:
            return []
        return [{**_CALLBACK_ROW, "id": cb_id}]

    with patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select):
        r = _client.patch(
            f"/v1/callbacks/{cb_id}",
            json={"due_at": "2026-05-13T10:00:00+00:00"},
            headers=_OTHER_SCOPE_HEADERS,  # Different suite
        )
    assert r.status_code == 404
    assert "RESOURCE_NOT_FOUND" in r.text


def test_reschedule_callback_missing_headers_401():
    r = _client.patch(f"/v1/callbacks/{uuid.uuid4()}", json={"due_at": "2026-05-13T10:00:00+00:00"})
    assert r.status_code == 401


def test_reschedule_phone_redacted_in_receipt():
    """Phone number must be redacted in receipt (Law #9)."""
    cb_id = str(uuid.uuid4())
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        return [{**_CALLBACK_ROW, "id": cb_id}]

    async def mock_update(table, filters, data):
        return {}

    with (
        patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.callbacks.supabase_update", side_effect=mock_update),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        _client.patch(
            f"/v1/callbacks/{cb_id}",
            json={"due_at": "2026-05-13T10:00:00+00:00"},
            headers=_SCOPE_HEADERS,
        )

    for rec in stored:
        inputs = rec.get("redacted_inputs", {})
        phone_in_receipt = inputs.get("phone", "")
        # Raw phone "+14155550001" must NOT appear — only truncated prefix
        assert "+14155550001" not in phone_in_receipt, "full phone leaked into receipt"


# ---------------------------------------------------------------------------
# POST /v1/callbacks/{id}/complete
# ---------------------------------------------------------------------------


def test_complete_callback_success_emits_receipt():
    """Complete emits receipt_type=callback_completed (Law #2)."""
    cb_id = str(uuid.uuid4())
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    async def mock_select(table, filters, **kw):
        return [{**_CALLBACK_ROW, "id": cb_id}]

    async def mock_update(table, filters, data):
        return {}

    with (
        patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.callbacks.supabase_update", side_effect=mock_update),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        r = _client.post(f"/v1/callbacks/{cb_id}/complete", headers=_SCOPE_HEADERS)

    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert "receipt_id" in data
    assert any(rec.get("receipt_type") == "callback_completed" for rec in stored)


def test_complete_callback_not_found_404():
    """Completing a non-existent (or cross-suite) callback → 404."""
    cb_id = str(uuid.uuid4())

    async def mock_select(table, filters, **kw):
        return []  # RLS returns empty for cross-suite or not-found

    with patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select):
        r = _client.post(f"/v1/callbacks/{cb_id}/complete", headers=_SCOPE_HEADERS)
    assert r.status_code == 404


def test_complete_callback_missing_headers_401():
    r = _client.post(f"/v1/callbacks/{uuid.uuid4()}/complete")
    assert r.status_code == 401


def test_complete_callback_db_write_failure_still_cuts_receipt():
    """DB write failure → 502 but receipt with outcome=failed is still cut (Law #2)."""
    cb_id = str(uuid.uuid4())
    stored: list[dict] = []

    def store_mock(receipts: list):
        stored.extend(receipts)

    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    async def mock_select(table, filters, **kw):
        return [{**_CALLBACK_ROW, "id": cb_id}]

    async def mock_update(table, filters, data):
        raise SupabaseClientError("update/callback_promises", 503, "DB unreachable")

    with (
        patch("aspire_orchestrator.routes.callbacks.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.callbacks.supabase_update", side_effect=mock_update),
        patch("aspire_orchestrator.services.receipt_store.store_receipts", side_effect=store_mock),
    ):
        r = _client.post(f"/v1/callbacks/{cb_id}/complete", headers=_SCOPE_HEADERS)

    assert r.status_code == 502
    # Receipt must still be cut even on failure
    assert any(rec.get("receipt_type") == "callback_completed" for rec in stored)
    assert any(rec.get("outcome") == "failed" for rec in stored)
