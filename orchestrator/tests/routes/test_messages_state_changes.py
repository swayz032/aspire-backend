"""Tests for PATCH /v1/messages/threads/{threadId}/read|pin|archive.

TDD: Written BEFORE implementation per plan §5 Lane E1.

Covers:
  - read: sets read_at=NOW(), cuts sms_thread_read receipt
  - pin: toggles is_pinned, cuts sms_thread_pin receipt
  - archive: toggles is_archived, cuts sms_thread_archive receipt
  - requires telephony:sms_manage scope (not sms_read)
  - wrong scope denied (Law #5)
  - cross-tenant evil: tenant B token with tenant A headers -> 401
  - receipt present in response
  - missing scope headers -> 401
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.messages import router as messages_router

_app = FastAPI()
_app.include_router(messages_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"
SUITE_ID_B = "00000000-0000-0000-0000-000000000002"
OFFICE_ID_B = "00000000-0000-0000-0000-000000000022"
THREAD_ID = str(uuid.uuid4())

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}


def _mint_token(scope: str, suite_id: str = SUITE_ID, office_id: str = OFFICE_ID) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=suite_id,
        office_id=office_id,
        tool="messages",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


# ---------------------------------------------------------------------------
# Capability token + scope enforcement
# ---------------------------------------------------------------------------

def test_read_requires_sms_manage_scope():
    """telephony:sms_read scope is insufficient for PATCH read — requires sms_manage."""
    read_only_token = _mint_token("telephony:sms_read")

    resp = _client.patch(
        f"/v1/messages/threads/{THREAD_ID}/read",
        json={"capability_token": read_only_token},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_pin_requires_sms_manage_scope():
    """telephony:sms_read scope is insufficient for PATCH pin."""
    read_only_token = _mint_token("telephony:sms_read")

    resp = _client.patch(
        f"/v1/messages/threads/{THREAD_ID}/pin",
        json={"capability_token": read_only_token},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_archive_requires_sms_manage_scope():
    """telephony:sms_read scope is insufficient for PATCH archive."""
    read_only_token = _mint_token("telephony:sms_read")

    resp = _client.patch(
        f"/v1/messages/threads/{THREAD_ID}/archive",
        json={"capability_token": read_only_token},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_read_no_token_returns_401():
    """No token -> 401."""
    resp = _client.patch(
        f"/v1/messages/threads/{THREAD_ID}/read",
        json={},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


def test_cross_tenant_pin_denied():
    """Token from tenant B cannot pin tenant A thread."""
    tenant_b_token = _mint_token("telephony:sms_manage", suite_id=SUITE_ID_B, office_id=OFFICE_ID_B)

    resp = _client.patch(
        f"/v1/messages/threads/{THREAD_ID}/pin",
        json={"capability_token": tenant_b_token},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy paths with receipt validation
# ---------------------------------------------------------------------------

def test_mark_read_cuts_receipt():
    """PATCH /read sets read_at and returns receipt_id (Law #2)."""
    cap_token = _mint_token("telephony:sms_manage")
    receipt_id = str(uuid.uuid4())

    with patch(
        "aspire_orchestrator.routes.messages._update_thread_state",
        new=AsyncMock(return_value={"read_at": "2026-04-30T12:00:00+00:00"}),
    ), patch(
        "aspire_orchestrator.routes.messages._cut_receipt",
        new=AsyncMock(return_value=receipt_id),
    ):
        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID}/read",
            json={"capability_token": cap_token},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "receipt_id" in data
    assert "read_at" in data


def test_toggle_pin_cuts_receipt():
    """PATCH /pin toggles is_pinned and returns receipt_id (Law #2)."""
    cap_token = _mint_token("telephony:sms_manage")
    receipt_id = str(uuid.uuid4())
    # Mock the DB read that fetches current pinned state
    mock_current_row = [{"memory_id": THREAD_ID, "is_pinned": False, "is_archived": False}]

    with patch(
        "aspire_orchestrator.routes.messages.supabase_select",
        new=AsyncMock(return_value=mock_current_row),
    ), patch(
        "aspire_orchestrator.routes.messages._update_thread_state",
        new=AsyncMock(return_value={"is_pinned": True}),
    ), patch(
        "aspire_orchestrator.routes.messages._cut_receipt",
        new=AsyncMock(return_value=receipt_id),
    ):
        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID}/pin",
            json={"capability_token": cap_token},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "receipt_id" in data
    assert "is_pinned" in data


def test_toggle_archive_cuts_receipt():
    """PATCH /archive toggles is_archived and returns receipt_id (Law #2)."""
    cap_token = _mint_token("telephony:sms_manage")
    receipt_id = str(uuid.uuid4())
    # Mock the DB read that fetches current archive state
    mock_current_row = [{"memory_id": THREAD_ID, "is_pinned": False, "is_archived": True}]

    with patch(
        "aspire_orchestrator.routes.messages.supabase_select",
        new=AsyncMock(return_value=mock_current_row),
    ), patch(
        "aspire_orchestrator.routes.messages._update_thread_state",
        new=AsyncMock(return_value={"is_archived": False}),
    ), patch(
        "aspire_orchestrator.routes.messages._cut_receipt",
        new=AsyncMock(return_value=receipt_id),
    ):
        resp = _client.patch(
            f"/v1/messages/threads/{THREAD_ID}/archive",
            json={"capability_token": cap_token},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "receipt_id" in data
    assert "is_archived" in data


def test_missing_scope_headers_returns_401():
    """PATCH endpoints require X-Tenant-Id / X-Suite-Id / X-Office-Id."""
    cap_token = _mint_token("telephony:sms_manage")

    resp = _client.patch(
        f"/v1/messages/threads/{THREAD_ID}/read",
        json={"capability_token": cap_token},
        # No X- headers
    )
    assert resp.status_code == 401
