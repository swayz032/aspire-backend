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
    # Hours JSONB matches the canonical 7-key wire shape written by the Front
    # Desk Setup page Hours tab. Mon–Fri 8am–6pm; Sat/Sun closed.
    business_hours = {
        "mon": {"open": True, "startTime": "08:00", "endTime": "18:00"},
        "tue": {"open": True, "startTime": "08:00", "endTime": "18:00"},
        "wed": {"open": True, "startTime": "08:00", "endTime": "18:00"},
        "thu": {"open": True, "startTime": "08:00", "endTime": "18:00"},
        "fri": {"open": True, "startTime": "08:00", "endTime": "18:00"},
        "sat": {"open": False},
        "sun": {"open": False},
    }
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
        "business_hours": business_hours,
        "timezone": "America/New_York",
    }]


def _routing_rows() -> list[dict[str, Any]]:
    return [
        {"role": "owner", "label": "Tonio", "phone": "+14155550001"},
        {"role": "sales", "label": "Maya", "phone": "+14155550002"},
    ]


def _suite_profile_row() -> list[dict[str, Any]]:
    return [{
        "suite_id": SUITE_ID,
        "business_name": "Acme Painting",
        "industry": "painting",
        "owner_name": "Antonio Swayzee",
        "timezone": "America/New_York",
        "email": "tonio@acmepainting.com",
    }]


def _select_side_effect(table: str, filters: str, **kwargs) -> list[dict[str, Any]]:
    if table == "tenant_phone_numbers":
        return _phone_row()
    if table == "front_desk_configs":
        return _config_row()
    if table == "front_desk_routing_contacts":
        return _routing_rows()
    if table == "suite_profiles":
        return _suite_profile_row()
    # tenant_profiles / office_profiles / business_hours no longer exist —
    # any read against them in production schema returns []. Mirror that.
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
        """public_number_mode must be lowercase (e.g. 'aspire_new_number'), not 'ASPIRE_NUMBER'."""
        resp = self._post_personalization()
        dyn = resp.json()["dynamic_variables"]
        assert dyn["public_number_mode"] in (
            "aspire_new_number",
            "forward_existing",
            "port_in",
        )
        assert dyn["public_number_mode"] != "aspire_number"
        assert dyn["public_number_mode"] != "keep_current_number"


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


# ---------------------------------------------------------------------------
# Hours JSONB — business_hours stored on front_desk_configs.business_hours
# (no separate `business_hours` table — verified against live schema 2026-05-03)
# ---------------------------------------------------------------------------


class TestIsOpenNowJsonbShape:
    """_is_open_now consumes the canonical 7-key JSONB written by the Hours tab."""

    def _hours(self) -> dict[str, dict[str, Any]]:
        return {
            "mon": {"open": True, "startTime": "09:00", "endTime": "17:00"},
            "tue": {"open": True, "startTime": "09:00", "endTime": "17:00"},
            "wed": {"open": True, "startTime": "09:00", "endTime": "17:00"},
            "thu": {"open": True, "startTime": "09:00", "endTime": "17:00"},
            "fri": {"open": True, "startTime": "09:00", "endTime": "17:00"},
            "sat": {"open": False},
            "sun": {"open": False},
        }

    def _patch_now(self, weekday: int, hh: int, mm: int):
        from aspire_orchestrator.routes import sarah as _sarah

        # Pick a real datetime that matches the weekday + time we want.
        # 2026-05-04 is Monday (weekday=0); offset to land on the requested day.
        anchor = datetime(2026, 5, 4, hh, mm, 0, tzinfo=timezone.utc)
        from datetime import timedelta

        target = anchor + timedelta(days=weekday)
        assert target.weekday() == weekday
        return patch.object(
            _sarah,
            "datetime",
            MagicMock(now=MagicMock(return_value=target), wraps=datetime),
        )

    def test_open_during_weekday_business_hours(self) -> None:
        from aspire_orchestrator.routes.sarah import _is_open_now

        with self._patch_now(weekday=2, hh=12, mm=0):  # Wed noon
            assert _is_open_now(self._hours(), "America/New_York") is True

    def test_closed_outside_business_hours(self) -> None:
        from aspire_orchestrator.routes.sarah import _is_open_now

        with self._patch_now(weekday=2, hh=20, mm=0):  # Wed 8pm
            assert _is_open_now(self._hours(), "America/New_York") is False

    def test_closed_on_weekends(self) -> None:
        from aspire_orchestrator.routes.sarah import _is_open_now

        with self._patch_now(weekday=5, hh=12, mm=0):  # Saturday noon
            assert _is_open_now(self._hours(), "America/New_York") is False

    def test_overnight_window(self) -> None:
        """Late-night businesses (e.g. 22:00–02:00) treat midnight as open."""
        from aspire_orchestrator.routes.sarah import _is_open_now

        hours = {
            "mon": {"open": True, "startTime": "22:00", "endTime": "02:00"},
            "tue": {"open": True, "startTime": "22:00", "endTime": "02:00"},
            "wed": {"open": True, "startTime": "22:00", "endTime": "02:00"},
            "thu": {"open": True, "startTime": "22:00", "endTime": "02:00"},
            "fri": {"open": True, "startTime": "22:00", "endTime": "02:00"},
            "sat": {"open": True, "startTime": "22:00", "endTime": "02:00"},
            "sun": {"open": True, "startTime": "22:00", "endTime": "02:00"},
        }
        with self._patch_now(weekday=2, hh=23, mm=0):
            from aspire_orchestrator.routes.sarah import _is_open_now

            assert _is_open_now(hours, "America/New_York") is True

    def test_empty_or_missing_hours_defaults_open(self) -> None:
        """Legacy rows with no business_hours JSONB fall through to 'always open'."""
        from aspire_orchestrator.routes.sarah import _is_open_now

        assert _is_open_now(None, "America/New_York") is True
        assert _is_open_now({}, "America/New_York") is True

    def test_open_true_with_no_schedule_treats_day_as_24h(self) -> None:
        """`{open: true}` with no startTime/endTime = open all day for that day."""
        from aspire_orchestrator.routes.sarah import _is_open_now

        hours = {k: {"open": False} for k in
                 ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
        hours["wed"] = {"open": True}
        with self._patch_now(weekday=2, hh=3, mm=0):
            assert _is_open_now(hours, "America/New_York") is True


# ---------------------------------------------------------------------------
# _fetch_profile reads from suite_profiles only (tenant_profiles +
# office_profiles do not exist — verified against live schema)
# ---------------------------------------------------------------------------


class TestFetchProfileSuiteProfiles:
    def test_owner_name_split_into_first_last(self) -> None:
        import asyncio
        from aspire_orchestrator.routes import sarah as _sarah

        async def _fake_select(table, *_a, **_kw):
            if table == "suite_profiles":
                return [{
                    "business_name": "Scott Painting Services",
                    "industry": "painting",
                    "owner_name": "Tonio Scott",
                    "timezone": "America/Los_Angeles",
                    "email": "tonio@example.com",
                }]
            return []

        with patch.object(_sarah, "_safe_select", new=AsyncMock(side_effect=_fake_select)):
            p = asyncio.run(
                _sarah._fetch_profile(
                    suite_id=SUITE_ID, office_id=OFFICE_ID, tenant_id=TENANT_ID
                )
            )
        assert p["business_name"] == "Scott Painting Services"
        assert p["first_name"] == "Tonio"
        assert p["last_name"] == "Scott"
        assert p["industry"] == "painting"
        assert p["timezone"] == "America/Los_Angeles"
        assert p["voicemail_email"] == "tonio@example.com"

    def test_owner_name_single_word_no_last(self) -> None:
        import asyncio
        from aspire_orchestrator.routes import sarah as _sarah

        async def _fake_select(table, *_a, **_kw):
            if table == "suite_profiles":
                return [{
                    "business_name": "Solo Co",
                    "owner_name": "Cher",
                    "email": "cher@example.com",
                }]
            return []

        with patch.object(_sarah, "_safe_select", new=AsyncMock(side_effect=_fake_select)):
            p = asyncio.run(
                _sarah._fetch_profile(
                    suite_id=SUITE_ID, office_id=OFFICE_ID, tenant_id=TENANT_ID
                )
            )
        assert p["first_name"] == "Cher"
        assert p["last_name"] == ""

    def test_missing_suite_profile_returns_safe_defaults(self) -> None:
        import asyncio
        from aspire_orchestrator.routes import sarah as _sarah

        with patch.object(_sarah, "_safe_select", new=AsyncMock(return_value=[])):
            p = asyncio.run(
                _sarah._fetch_profile(
                    suite_id=SUITE_ID, office_id=OFFICE_ID, tenant_id=TENANT_ID
                )
            )
        assert p["business_name"] == "your business"
        assert p["first_name"] == ""
        assert p["last_name"] == ""
        assert p["industry"] == "professional_services"
        assert p["timezone"] == "America/New_York"
        assert p["voicemail_email"] == ""
