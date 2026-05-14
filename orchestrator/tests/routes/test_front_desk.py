"""Tests for front_desk routes (Pass 16 -- Law #2, #3, #4, #5).

Covers:
- GET /config happy path: JWT+scope -> current config + routing_contacts
- PATCH /config without capability token -> 401
- PATCH /config versioned write: version_no incremented
- PATCH /config receipt cut: front_desk_config_save receipt
- Routing contacts CRUD: POST/PATCH/DELETE each cuts a receipt
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.front_desk import router as front_desk_router

_app = FastAPI()
_app.include_router(front_desk_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}


def _mint_valid_token(scope: str) -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="front_desk",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


def _config_row(version_no: int = 3) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "version_no": version_no,
        "is_current": True,
        "after_hours_mode": "take_message",
        "busy_mode": "take_message",
        "public_number_mode": "ASPIRE_NUMBER",
        "catch_mode": "APP_AND_PHONE_SIMUL_RING",
        "greeting_name_override": "",
        "pronunciation_override": "",
    }


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_get_config_happy():
    """JWT + scope headers -> returns current config + routing_contacts."""
    config = _config_row()
    routing = [{"role": "owner", "phone": "+12125550001", "label": "Owner", "is_active": True}]

    with patch("aspire_orchestrator.routes.front_desk.supabase_select",
               new=AsyncMock(side_effect=[[config], routing])):

        resp = _client.get("/v1/front-desk/config", headers=_SCOPE_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["config"]["version_no"] == 3
    assert len(data["routing_contacts"]) == 1
    assert data["routing_contacts"][0]["role"] == "owner"


def test_get_config_missing_scope_headers_401():
    """No scope headers -> 401."""
    resp = _client.get("/v1/front-desk/config")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /config -- capability token required
# ---------------------------------------------------------------------------


def test_patch_config_yellow_tier_capability_token_required():
    """No capability_token -> 401 MISSING_CAPABILITY_TOKEN."""
    resp = _client.patch(
        "/v1/front-desk/config",
        json={"after_hours_mode": "voicemail"},
        headers=_SCOPE_HEADERS,
    )
    assert resp.status_code == 401
    assert "MISSING_CAPABILITY_TOKEN" in str(resp.json())


def test_patch_config_versioned_write():
    """PATCH increments version_no (new row with version_no = max + 1)."""
    cap_token = _mint_valid_token("front_desk:config_save")
    current = _config_row(version_no=3)
    new_row = {**current, "id": str(uuid.uuid4()), "version_no": 4}

    with patch("aspire_orchestrator.routes.front_desk.supabase_select",
               new=AsyncMock(return_value=[current])), \
         patch("aspire_orchestrator.routes.front_desk.supabase_insert",
               new=AsyncMock(return_value=new_row)) as mock_insert, \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"):

        resp = _client.patch(
            "/v1/front-desk/config",
            json={"after_hours_mode": "voicemail", "capability_token": cap_token},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    # New row inserted with version_no = 4
    mock_insert.assert_called_once()
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["version_no"] == 4
    assert inserted_data["after_hours_mode"] == "voicemail"
    assert inserted_data["is_current"] is True


def test_patch_config_normalizes_frontend_modes():
    """Frontend UPPERCASE modes are normalized to DB-safe lowercase values."""
    cap_token = _mint_valid_token("front_desk:config_save")
    current = _config_row(version_no=7)
    new_row = {**current, "id": str(uuid.uuid4()), "version_no": 8}

    with patch("aspire_orchestrator.routes.front_desk.supabase_select",
               new=AsyncMock(return_value=[current])), \
         patch("aspire_orchestrator.routes.front_desk.supabase_insert",
               new=AsyncMock(return_value=new_row)) as mock_insert, \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"):

        resp = _client.patch(
            "/v1/front-desk/config",
            json={
                "after_hours_mode": "TRY_TRANSFER_THEN_MESSAGE",
                "busy_mode": "ASK_CALLBACK_WINDOW",
                "capability_token": cap_token,
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["after_hours_mode"] == "try_transfer_then_message"
    assert inserted_data["busy_mode"] == "callback_window"


def test_patch_config_receipt_cut():
    """PATCH -> front_desk_config_save receipt cut with new version_no."""
    cap_token = _mint_valid_token("front_desk:config_save")
    current = _config_row(version_no=2)
    new_row = {**current, "id": str(uuid.uuid4()), "version_no": 3}

    with patch("aspire_orchestrator.routes.front_desk.supabase_select",
               new=AsyncMock(return_value=[current])), \
         patch("aspire_orchestrator.routes.front_desk.supabase_insert",
               new=AsyncMock(return_value=new_row)), \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts") as mock_receipt:

        resp = _client.patch(
            "/v1/front-desk/config",
            json={"catch_mode": "FORWARD", "capability_token": cap_token},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "front_desk_config_save"
    assert r["outcome"] == "success"
    assert r["risk_tier"] == "yellow"
    assert r["redacted_outputs"]["version_no"] == 3


# ---------------------------------------------------------------------------
# Routing contacts CRUD
# ---------------------------------------------------------------------------


def test_routing_contacts_post_cuts_receipt():
    """POST /routing-contacts -> creates contact + cuts receipt."""
    cap_token = _mint_valid_token("front_desk:routing_write")
    inserted = {"id": str(uuid.uuid4()), "role": "sales", "label": "Sales Team"}

    with patch("aspire_orchestrator.routes.front_desk.supabase_insert",
               new=AsyncMock(return_value=inserted)), \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts") as mock_receipt:

        resp = _client.post(
            "/v1/front-desk/routing-contacts",
            json={
                "role": "sales",
                "label": "Sales Team",
                "phone": "+12125550002",
                "capability_token": cap_token,
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "routing_contact_create"
    assert r["outcome"] == "success"


def test_routing_contacts_post_persists_normalized_fields():
    """Routing contact create preserves transfer/fallback/order and normalizes role aliases."""
    cap_token = _mint_valid_token("front_desk:routing_write")
    inserted = {"id": str(uuid.uuid4()), "role": "custom", "name": "Ops Desk"}

    with patch("aspire_orchestrator.routes.front_desk.supabase_insert",
               new=AsyncMock(return_value=inserted)) as mock_insert, \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"):

        resp = _client.post(
            "/v1/front-desk/routing-contacts",
            json={
                "role": "operations",
                "name": "Ops Desk",
                "phone": "+12125550002",
                "transfer_allowed": False,
                "fallback_mode": "MESSAGE_ONLY",
                "sort_order": 9,
                "capability_token": cap_token,
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["role"] == "custom"
    assert inserted_data["transfer_allowed"] is False
    assert inserted_data["fallback_mode"] == "message_only"
    assert inserted_data["sort_order"] == 9


def test_routing_contacts_patch_cuts_receipt():
    """PATCH /routing-contacts/{id} -> updates contact + cuts receipt."""
    cap_token = _mint_valid_token("front_desk:routing_write")
    contact_id = str(uuid.uuid4())

    with patch("aspire_orchestrator.routes.front_desk.supabase_update",
               new=AsyncMock(return_value={"id": contact_id})), \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts") as mock_receipt:

        resp = _client.patch(
            f"/v1/front-desk/routing-contacts/{contact_id}",
            json={"label": "Updated Sales", "capability_token": cap_token},
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "routing_contact_update"


def test_routing_contacts_patch_normalizes_fallback_and_role():
    """PATCH normalizes frontend fallback casing and operations alias."""
    cap_token = _mint_valid_token("front_desk:routing_write")
    contact_id = str(uuid.uuid4())

    with patch("aspire_orchestrator.routes.front_desk.supabase_update",
               new=AsyncMock(return_value={"id": contact_id})) as mock_update, \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts"):

        resp = _client.patch(
            f"/v1/front-desk/routing-contacts/{contact_id}",
            json={
                "role": "operations",
                "fallback_mode": "TRANSFER_ALLOWED",
                "capability_token": cap_token,
            },
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    update_data = mock_update.call_args[0][2]
    assert update_data["role"] == "custom"
    assert update_data["fallback_mode"] == "transfer_allowed"


def test_routing_contacts_delete_cuts_receipt():
    """DELETE /routing-contacts/{id} -> hard delete + cuts receipt.
    The route accepts capability_token as a JSON body (FastAPI body param).
    Live schema has no soft-delete column; receipt preserves audit trail."""
    cap_token = _mint_valid_token("front_desk:routing_write")
    contact_id = str(uuid.uuid4())

    with patch("aspire_orchestrator.routes.front_desk.supabase_delete",
               new=AsyncMock(return_value=None)), \
         patch("aspire_orchestrator.routes.front_desk.receipt_store.store_receipts") as mock_receipt:

        # FastAPI treats capability_token as a body param on DELETE --
        # send via request() with json= to pass it as the request body
        resp = _client.request(
            "DELETE",
            f"/v1/front-desk/routing-contacts/{contact_id}",
            json=cap_token,
            headers=_SCOPE_HEADERS,
        )

    assert resp.status_code == 200
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "routing_contact_delete"
    assert r["outcome"] == "success"
