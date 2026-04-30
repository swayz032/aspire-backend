"""Tests for Sarah personalization webhook route (Pass 16 + Pass 18 -- Law #2, #3, #6).

Covers:
- Happy path: valid HMAC, valid called_number, populated config -> correct 16-var shape
- Invalid signature -> 401 + personalization_denied receipt
- Missing webhook secret -> 503 MISCONFIGURED (Pass 18 fix)
- Invalid called_number E.164 (THREAT-014) -> 422 INVALID_CALLED_NUMBER
- Unknown number -> 404
- Routing phone dynamic vars: 3 contacts + 2 empty
- time_of_day: morning/afternoon/evening
- is_open_now: within hours -> True; outside -> False
- Receipt cut on successful resolve
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.sarah import router as sarah_router

_app = FastAPI()
_app.include_router(sarah_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "00000000-0000-0000-0000-000000000001"
OFFICE_ID = "00000000-0000-0000-0000-000000000011"
TENANT_ID = "00000000-0000-0000-0000-000000000099"
CALLED_NUMBER = "+12125550100"
CALL_SID = "CAxxxxxxxx"
EL_SECRET = "test-el-webhook-secret-xyz"


def _make_el_signature(body_bytes: bytes, secret: str, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + body_bytes
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={sig}"


def _make_payload(called_number: str = CALLED_NUMBER, call_sid: str = CALL_SID) -> dict:
    return {
        "called_number": called_number,
        "call_sid": call_sid,
        "caller_id": "+19175550200",
        "agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
    }


def _phone_row(number: str = CALLED_NUMBER) -> list:
    return [{
        "phone_number": number,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tenant_id": TENANT_ID,
        "status": "active",
    }]


def _config_row() -> list:
    return [{
        "id": str(uuid.uuid4()),
        "version_no": 3,
        "is_current": True,
        "after_hours_mode": "take_message",
        "busy_mode": "take_message",
        "public_number_mode": "ASPIRE_NUMBER",
        "catch_mode": "APP_AND_PHONE_SIMUL_RING",
        "greeting_name_override": "",
        "pronunciation_override": "",
    }]


def _post_personalization(payload: dict, secret: str = EL_SECRET):
    body = json.dumps(payload).encode()
    now_ts = int(time.time())
    sig = _make_el_signature(body, secret, ts=now_ts)
    with patch("aspire_orchestrator.services.ingestion.signatures.time") as mock_time:
        mock_time.time.return_value = float(now_ts)
        return _client.post(
            "/v1/sarah/personalization",
            content=body,
            headers={"Content-Type": "application/json", "ElevenLabs-Signature": sig},
        )


def test_personalization_happy_path():
    """Valid HMAC + valid called_number + populated config -> 200 with 16 dynamic vars."""
    payload = _make_payload()

    def _mock_select(table, filters, order_by=None, limit=None):
        if table == "tenant_phone_numbers":
            return _phone_row()
        if table == "front_desk_configs":
            return _config_row()
        if table == "front_desk_routing_contacts":
            return [
                {"role": "owner", "phone": "+12125550001", "label": "Owner"},
                {"role": "sales", "phone": "+12125550002", "label": "Sales"},
                {"role": "support", "phone": "+12125550003", "label": "Support"},
            ]
        if table == "tenant_profiles":
            return [{"business_name": "Acme Corp", "industry": "legal"}]
        if table == "office_profiles":
            return [{"first_name": "Jane", "last_name": "Doe", "timezone": "America/New_York"}]
        return []

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=EL_SECRET), \
         patch("aspire_orchestrator.routes.sarah.supabase_select",
               new=AsyncMock(side_effect=_mock_select)), \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts") as mock_receipt:

        resp = _post_personalization(payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "conversation_initiation_client_data"

    dyn = data["dynamic_variables"]
    # All 5 routing phone vars present
    assert "routing_owner_phone" in dyn
    assert "routing_sales_phone" in dyn
    assert "routing_support_phone" in dyn
    assert "routing_billing_phone" in dyn
    assert "routing_scheduling_phone" in dyn
    # Populated
    assert dyn["routing_owner_phone"] == "+12125550001"
    assert dyn["routing_sales_phone"] == "+12125550002"
    assert dyn["routing_support_phone"] == "+12125550003"
    # Empty for unconfigured
    assert dyn["routing_billing_phone"] == ""
    assert dyn["routing_scheduling_phone"] == ""
    assert dyn["business_name"] == "Acme Corp"
    # conversation_config_override present
    assert "agent" in data["conversation_config_override"]
    assert "first_message" in data["conversation_config_override"]["agent"]
    # Receipt cut
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "personalization_resolve"
    assert r["outcome"] == "success"


def test_personalization_invalid_signature_fails_closed_401():
    """Bad HMAC -> 401 + personalization_denied receipt."""
    payload = _make_payload()
    body = json.dumps(payload).encode()
    bad_sig = "t=99999,v0=badhash"

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=EL_SECRET), \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts") as mock_receipt:

        resp = _client.post(
            "/v1/sarah/personalization",
            content=body,
            headers={"Content-Type": "application/json", "ElevenLabs-Signature": bad_sig},
        )

    assert resp.status_code == 401
    assert "INVALID_SIGNATURE" in str(resp.json())
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "personalization_denied"
    assert r["reason_code"] == "INVALID_SIGNATURE"


def test_personalization_missing_secret_503():
    """elevenlabs_webhook_secret='' -> 503 MISCONFIGURED + receipt (Pass 18 fix)."""
    payload = _make_payload()
    body = json.dumps(payload).encode()
    sig = _make_el_signature(body, "whatever")

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=""), \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts") as mock_receipt:

        resp = _client.post(
            "/v1/sarah/personalization",
            content=body,
            headers={"Content-Type": "application/json", "ElevenLabs-Signature": sig},
        )

    assert resp.status_code == 503
    assert "MISCONFIGURED" in str(resp.json())
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["reason_code"] == "MISSING_WEBHOOK_SECRET"


def test_personalization_invalid_called_number_e164():
    """Injected called_number '+1234&suite_id=neq.X' -> 422 INVALID_CALLED_NUMBER (THREAT-014)."""
    injected_payload = _make_payload(called_number="+1234&suite_id=neq.X")
    body = json.dumps(injected_payload).encode()
    now_ts = int(time.time())
    sig = _make_el_signature(body, EL_SECRET, ts=now_ts)

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=EL_SECRET), \
         patch("aspire_orchestrator.services.ingestion.signatures.time") as mock_time, \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts") as mock_receipt:

        mock_time.time.return_value = float(now_ts)
        resp = _client.post(
            "/v1/sarah/personalization",
            content=body,
            headers={"Content-Type": "application/json", "ElevenLabs-Signature": sig},
        )

    assert resp.status_code == 422
    assert "INVALID_CALLED_NUMBER" in str(resp.json())
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["reason_code"] == "INVALID_CALLED_NUMBER"


def test_personalization_unknown_number_404():
    """Valid format but no tenant_phone_numbers row -> 404."""
    payload = _make_payload()

    def _mock_select(table, filters, order_by=None, limit=None):
        if table == "tenant_phone_numbers":
            return []
        return []

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=EL_SECRET), \
         patch("aspire_orchestrator.routes.sarah.supabase_select",
               new=AsyncMock(side_effect=_mock_select)), \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts") as mock_receipt:

        resp = _post_personalization(payload)

    assert resp.status_code == 404
    assert "UNKNOWN_NUMBER" in str(resp.json())
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "personalization_unknown_number"


def test_personalization_routing_phone_dynamic_vars():
    """3 routing contacts -> owner/sales/support populated; billing/scheduling empty."""
    payload = _make_payload()

    def _mock_select(table, filters, order_by=None, limit=None):
        if table == "tenant_phone_numbers":
            return _phone_row()
        if table == "front_desk_configs":
            return _config_row()
        if table == "front_desk_routing_contacts":
            return [
                {"role": "owner", "phone": "+12125550001", "label": "Owner"},
                {"role": "sales", "phone": "+12125550002", "label": "Sales"},
                {"role": "support", "phone": "+12125550003", "label": "Support"},
            ]
        return []

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=EL_SECRET), \
         patch("aspire_orchestrator.routes.sarah.supabase_select",
               new=AsyncMock(side_effect=_mock_select)), \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts"):

        resp = _post_personalization(payload)

    assert resp.status_code == 200
    dyn = resp.json()["dynamic_variables"]
    assert dyn["routing_owner_phone"] == "+12125550001"
    assert dyn["routing_sales_phone"] == "+12125550002"
    assert dyn["routing_support_phone"] == "+12125550003"
    assert dyn["routing_billing_phone"] == ""
    assert dyn["routing_scheduling_phone"] == ""


def test_personalization_time_of_day_morning():
    """Hour 9 in America/New_York -> time_of_day='morning'."""
    from aspire_orchestrator.routes.sarah import _compute_time_of_day
    from zoneinfo import ZoneInfo

    fake_dt = datetime(2026, 4, 29, 9, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    with patch("aspire_orchestrator.routes.sarah.datetime") as mock_dt:
        mock_dt.now.return_value = fake_dt
        result = _compute_time_of_day("America/New_York")

    assert result == "morning"


def test_personalization_time_of_day_afternoon():
    """Hour 14:30 in America/New_York -> time_of_day='afternoon'."""
    from aspire_orchestrator.routes.sarah import _compute_time_of_day
    from zoneinfo import ZoneInfo

    fake_dt = datetime(2026, 4, 29, 14, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    with patch("aspire_orchestrator.routes.sarah.datetime") as mock_dt:
        mock_dt.now.return_value = fake_dt
        result = _compute_time_of_day("America/New_York")

    assert result == "afternoon"


def test_personalization_time_of_day_evening():
    """Hour 19:00 in America/New_York -> time_of_day='evening'."""
    from aspire_orchestrator.routes.sarah import _compute_time_of_day
    from zoneinfo import ZoneInfo

    fake_dt = datetime(2026, 4, 29, 19, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    with patch("aspire_orchestrator.routes.sarah.datetime") as mock_dt:
        mock_dt.now.return_value = fake_dt
        result = _compute_time_of_day("America/New_York")

    assert result == "evening"


def test_personalization_is_open_now_within_hours():
    """Within business hours -> is_open_now=True."""
    from aspire_orchestrator.routes.sarah import _is_open_now
    from zoneinfo import ZoneInfo

    # Tuesday 10:00 AM (weekday=1)
    fake_dt = datetime(2026, 4, 28, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    hours_rows = [{"day_of_week": 1, "open_time": "09:00:00", "close_time": "17:00:00"}]

    with patch("aspire_orchestrator.routes.sarah.datetime") as mock_dt:
        mock_dt.now.return_value = fake_dt
        result = _is_open_now(hours_rows, "America/New_York")

    assert result is True


def test_personalization_is_open_now_outside_hours():
    """Outside business hours -> is_open_now=False."""
    from aspire_orchestrator.routes.sarah import _is_open_now
    from zoneinfo import ZoneInfo

    # Tuesday 20:00 PM (after close)
    fake_dt = datetime(2026, 4, 28, 20, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    hours_rows = [{"day_of_week": 1, "open_time": "09:00:00", "close_time": "17:00:00"}]

    with patch("aspire_orchestrator.routes.sarah.datetime") as mock_dt:
        mock_dt.now.return_value = fake_dt
        result = _is_open_now(hours_rows, "America/New_York")

    assert result is False


def test_personalization_receipt_cut_on_resolve():
    """Successful resolve -> personalization_resolve receipt with version_no + config_id."""
    payload = _make_payload()
    config_id = str(uuid.uuid4())

    def _mock_select(table, filters, order_by=None, limit=None):
        if table == "tenant_phone_numbers":
            return _phone_row()
        if table == "front_desk_configs":
            return [{
                "id": config_id,
                "version_no": 5,
                "is_current": True,
                "after_hours_mode": "take_message",
                "busy_mode": "take_message",
                "public_number_mode": "ASPIRE_NUMBER",
                "catch_mode": "APP_AND_PHONE_SIMUL_RING",
                "greeting_name_override": "",
                "pronunciation_override": "",
            }]
        return []

    with patch("aspire_orchestrator.routes.sarah.settings",
               elevenlabs_webhook_secret=EL_SECRET), \
         patch("aspire_orchestrator.routes.sarah.supabase_select",
               new=AsyncMock(side_effect=_mock_select)), \
         patch("aspire_orchestrator.routes.sarah.receipt_store.store_receipts") as mock_receipt:

        resp = _post_personalization(payload)

    assert resp.status_code == 200
    mock_receipt.assert_called_once()
    r = mock_receipt.call_args[0][0][0]
    assert r["receipt_type"] == "personalization_resolve"
    assert r["suite_id"] == SUITE_ID
    outputs = r["redacted_outputs"]
    assert outputs["version_no"] == 5
    assert outputs["front_desk_config_id"] == config_id
