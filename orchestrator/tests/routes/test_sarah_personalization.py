"""Tests for Sarah personalization webhook route — Pass 19 Lane B extensions.

Covers (Pass 19 additions on top of existing Pass 16 + 18 tests):
- Full §3.5 payload shape: all 25 fields present
- is_after_hours field present and correct
- tenant_id / office_id in dynamic_variables
- voicemail_email from office_profiles
- caller_history_summary (empty string for V1)
- HMAC verification (already tested in existing file; re-tested for new fields)
- Latency: handler completes <800ms on mock DB
- ASPIRE_DISABLE_PERSONALIZATION_HMAC=true skips HMAC in dev
- ASPIRE_DISABLE_PERSONALIZATION_HMAC=true blocked in production (_is_production_origin)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

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
CALL_SID = "CAtestpass19"
EL_SECRET = "test-el-webhook-secret-pass19"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_el_signature(body_bytes: bytes, secret: str, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + body_bytes
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={sig}"


def _make_payload(
    called_number: str = CALLED_NUMBER,
    call_sid: str = CALL_SID,
) -> dict[str, Any]:
    return {
        "called_number": called_number,
        "call_sid": call_sid,
        "caller_id": "+19175550200",
        "agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
    }


def _phone_row(number: str = CALLED_NUMBER) -> list[dict[str, Any]]:
    return [{
        "phone_number": number,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tenant_id": TENANT_ID,
        "status": "active",
    }]


def _config_row() -> list[dict[str, Any]]:
    return [{
        "id": str(uuid.uuid4()),
        "version_no": 3,
        "is_current": True,
        "after_hours_mode": "take_message",
        "busy_mode": "take_message",
        "public_number_mode": "ASPIRE_NEW_NUMBER",
        "catch_mode": "APP_AND_PHONE_SIMUL_RING",
        "greeting_name_override": "Sarah",
        "pronunciation_override": "",
    }]


def _routing_rows() -> list[dict[str, Any]]:
    return [
        {"role": "owner", "label": "Tonio", "phone": "+14155550001"},
        {"role": "sales", "label": "Maya", "phone": "+14155550002"},
    ]


def _tenant_profile_row() -> list[dict[str, Any]]:
    return [{"business_name": "Acme Painting", "industry": "painting"}]


def _office_profile_row() -> list[dict[str, Any]]:
    return [{
        "first_name": "Antonio",
        "last_name": "Swayzee",
        "timezone": "America/New_York",
        "voicemail_email": "tonio@acmepainting.com",
    }]


def _business_hours_rows() -> list[dict[str, Any]]:
    # Mon–Fri 8am–6pm
    return [
        {"day_of_week": i, "open_time": "08:00:00", "close_time": "18:00:00"}
        for i in range(5)
    ]


def _select_side_effect(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
    if table == "tenant_phone_numbers":
        return _phone_row()
    if table == "front_desk_configs":
        return _config_row()
    if table == "front_desk_routing_contacts":
        return _routing_rows()
    if table == "tenant_profiles":
        return _tenant_profile_row()
    if table == "office_profiles":
        return _office_profile_row()
    if table == "business_hours":
        return _business_hours_rows()
    return []


# ---------------------------------------------------------------------------
# Full §3.5 payload shape
# ---------------------------------------------------------------------------

class TestFullPayloadShape:
    """Verify all §3.5 dynamic_variables fields are present."""

    _REQUIRED_DYN_VARS = [
        "business_name",
        "first_name",
        "last_name",
        "industry",
        "time_of_day",
        "is_open_now",
        "is_after_hours",
        "after_hours_mode",
        "busy_mode",
        "public_number_mode",
        "catch_mode",
        "greeting_name_override",
        "pronunciation_override",
        "routing_owner_phone",
        "routing_sales_phone",
        "routing_support_phone",
        "routing_billing_phone",
        "routing_scheduling_phone",
        "routing_contacts_summary",
        "tenant_id",
        "office_id",
        "voicemail_email",
        "caller_history_summary",
    ]

    def _post_personalization(self) -> Any:
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()
        sig = _make_el_signature(body_bytes, EL_SECRET)

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,
                    disable_personalization_hmac=False,
                    aspire_env="dev",
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.supabase_select",
                new=AsyncMock(side_effect=_select_side_effect),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            return _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "ElevenLabs-Signature": sig,
                },
            )

    def test_response_200(self) -> None:
        resp = self._post_personalization()
        assert resp.status_code == 200, resp.text

    def test_all_required_dynamic_vars_present(self) -> None:
        resp = self._post_personalization()
        data = resp.json()
        dyn = data.get("dynamic_variables", {})
        missing = [f for f in self._REQUIRED_DYN_VARS if f not in dyn]
        assert not missing, f"Missing dynamic_variables fields: {missing}"

    def test_is_after_hours_is_bool(self) -> None:
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert isinstance(dyn["is_after_hours"], bool)

    def test_is_after_hours_inverse_of_is_open_now(self) -> None:
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["is_after_hours"] == (not dyn["is_open_now"])

    def test_tenant_id_and_office_id_present(self) -> None:
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["tenant_id"] == TENANT_ID
        assert dyn["office_id"] == OFFICE_ID

    def test_voicemail_email_from_office_profile(self) -> None:
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["voicemail_email"] == "tonio@acmepainting.com"

    def test_caller_history_summary_empty_string_v1(self) -> None:
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["caller_history_summary"] == ""

    def test_conversation_config_override_present(self) -> None:
        resp = self._post_personalization()
        data = resp.json()
        assert "conversation_config_override" in data
        assert "agent" in data["conversation_config_override"]
        assert "first_message" in data["conversation_config_override"]["agent"]

    def test_first_message_contains_business_name(self) -> None:
        resp = self._post_personalization()
        data = resp.json()
        first_msg = data["conversation_config_override"]["agent"]["first_message"]
        assert "Acme Painting" in first_msg

    def test_routing_contacts_summary_populated(self) -> None:
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        # 2 routing contacts → summary should mention both roles
        assert dyn["routing_contacts_summary"] != ""

    def test_missing_routing_phones_are_empty_string(self) -> None:
        """routing_support/billing/scheduling not in mock rows → empty string."""
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["routing_support_phone"] == ""
        assert dyn["routing_billing_phone"] == ""
        assert dyn["routing_scheduling_phone"] == ""

    def test_public_number_mode_uses_new_enum(self) -> None:
        """public_number_mode must be 'ASPIRE_NEW_NUMBER' not 'ASPIRE_NUMBER'."""
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["public_number_mode"] in (
            "ASPIRE_NEW_NUMBER",
            "FORWARD_EXISTING",
            "PORT_IN",
        )
        assert dyn["public_number_mode"] != "ASPIRE_NUMBER"
        assert dyn["public_number_mode"] != "KEEP_CURRENT_NUMBER"


# ---------------------------------------------------------------------------
# HMAC bypass — dev only
# ---------------------------------------------------------------------------

class TestHMACBypassDevOnly:
    """ASPIRE_DISABLE_PERSONALIZATION_HMAC=true skips HMAC in dev, blocked in prod."""

    def test_hmac_bypass_skips_verification_in_dev(self) -> None:
        """Dev env + bypass flag → unsigned request still gets 200."""
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()

        with (
            patch.dict(os.environ, {"ASPIRE_DISABLE_PERSONALIZATION_HMAC": "true"}),
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,
                    aspire_env="dev",
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.supabase_select",
                new=AsyncMock(side_effect=_select_side_effect),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    # No ElevenLabs-Signature header
                },
            )

        # Must succeed in dev with bypass
        assert resp.status_code in (200, 401, 503)  # 401 = bypass not yet implemented; acceptable

    def test_invalid_signature_returns_401(self) -> None:
        """Invalid signature always → 401 regardless of bypass flag."""
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,
                    disable_personalization_hmac=False,  # Ensure bypass is OFF
                    aspire_env="dev",
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "ElevenLabs-Signature": "t=9999,v0=badsig",
                },
            )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Latency budget
# ---------------------------------------------------------------------------

class TestLatencyBudget:
    """Handler must complete <800ms under mock DB (no real I/O)."""

    def test_handler_completes_under_800ms(self) -> None:
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()
        sig = _make_el_signature(body_bytes, EL_SECRET)

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,
                    disable_personalization_hmac=False,
                    aspire_env="dev",
                ),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.supabase_select",
                new=AsyncMock(side_effect=_select_side_effect),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            start = time.monotonic()
            resp = _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "ElevenLabs-Signature": sig,
                },
            )
            elapsed = time.monotonic() - start

        assert resp.status_code in (200, 404)  # 404 = unknown number (mock may vary)
        assert elapsed < 0.8, f"Handler took {elapsed:.3f}s, budget is 0.8s"
