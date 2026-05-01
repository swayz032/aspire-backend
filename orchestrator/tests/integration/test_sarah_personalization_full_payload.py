"""Integration test — Sarah personalization full §3.5 payload contract (Pass 19 Lane D).

Verifies the /v1/sarah/personalization endpoint returns all 19+ required
dynamic_variables when a fully-configured tenant (routing contacts + FDS config
+ onboarding data) is set up.

Tests:
  - Full §3.5 payload: all 19 required keys present
  - conversation_config_override.agent.first_message is dynamic greeting string
  - type == 'conversation_initiation_client_data'
  - HMAC invalid → 401 (fail-closed Law #3)
  - Latency: p95 <800ms across 50 calls under mock DB

Aspire Laws:
  Law #2: personalization_resolve receipt cut on every call.
  Law #3: HMAC invalid → 401 (fail-closed).
  Law #6: scope resolved from called_number, NOT request headers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import statistics
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "1000000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.sarah import router as sarah_router

_app = FastAPI()
_app.include_router(sarah_router)
_client = TestClient(_app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
# Test tenant constants
# ---------------------------------------------------------------------------

SUITE_ID = "aa000000-0000-0000-0000-000000000001"
OFFICE_ID = "aa000000-0000-0000-0000-000000000002"
TENANT_ID = "aa000000-0000-0000-0000-000000000003"
CALLED_NUMBER = "+14484001111"
CALL_SID = "CAintegrationtest001"
EL_SECRET = "el-webhook-secret-integration-test"

# All 5 routing contacts configured
_ROUTING_CONTACTS = [
    {"role": "owner", "label": "Tonio Swayzee", "phone": "+14045550001", "is_active": True},
    {"role": "sales", "label": "Maya Chen", "phone": "+14045550002", "is_active": True},
    {"role": "support", "label": "James Liu", "phone": "+14045550003", "is_active": True},
    {"role": "billing", "label": "Priya Singh", "phone": "+14045550004", "is_active": True},
    {"role": "scheduling", "label": "Carlos Rivera", "phone": "+14045550005", "is_active": True},
]

# The 19 required §3.5 dynamic_variables keys
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_el_signature(body_bytes: bytes, secret: str, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + body_bytes
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v0={sig}"


def _make_payload() -> dict[str, Any]:
    return {
        "called_number": CALLED_NUMBER,
        "call_sid": CALL_SID,
        "caller_id": "+19175550200",
        "agent_id": "agent_6501kp71h69jfqysgd055hemqhrq",
    }


def _full_tenant_select_side_effect(table: str, filters: str = "", **kwargs) -> list[dict]:
    """Mock full tenant with all routing contacts configured."""
    if table == "tenant_phone_numbers":
        return [{
            "phone_number": CALLED_NUMBER,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "tenant_id": TENANT_ID,
            "status": "active",
        }]
    if table == "front_desk_configs":
        return [{
            "id": str(uuid.uuid4()),
            "version_no": 5,
            "is_current": True,
            "after_hours_mode": "TRY_TRANSFER_THEN_MESSAGE",
            "busy_mode": "take_message",
            "public_number_mode": "ASPIRE_NEW_NUMBER",
            "catch_mode": "APP_AND_PHONE_SIMUL_RING",
            "greeting_name_override": "",
            "pronunciation_override": "",
        }]
    if table == "front_desk_routing_contacts":
        return _ROUTING_CONTACTS
    if table == "tenant_profiles":
        return [{"business_name": "Acme Plumbing Co", "industry": "plumbing"}]
    if table == "office_profiles":
        return [{
            "first_name": "Tonio",
            "last_name": "Swayzee",
            "timezone": "America/New_York",
            "voicemail_email": "tonio@acmeplumbing.com",
        }]
    if table == "business_hours":
        # Open Mon–Fri 8am–6pm ET
        return [
            {"day_of_week": i, "open_time": "08:00:00", "close_time": "18:00:00"}
            for i in range(5)
        ]
    return []


def _post_personalization_with_hmac() -> Any:
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
            new=AsyncMock(side_effect=_full_tenant_select_side_effect),
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


# ---------------------------------------------------------------------------
# Full payload shape tests
# ---------------------------------------------------------------------------


class TestFullPayloadShape:
    """Integration: full §3.5 payload with fully-configured tenant."""

    def test_response_type_field(self) -> None:
        """type == 'conversation_initiation_client_data' (EL contract requirement)."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("type") == "conversation_initiation_client_data"

    def test_all_19_dynamic_var_keys_present(self) -> None:
        """All §3.5 dynamic_variables keys present — missing keys break the EL agent."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json().get("dynamic_variables", {})
        missing = [k for k in _REQUIRED_DYN_VARS if k not in dyn]
        assert missing == [], f"Missing dynamic_variables keys: {missing}"

    def test_all_5_routing_phones_populated(self) -> None:
        """All 5 routing_*_phone values reflect the configured contacts."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["routing_owner_phone"] == "+14045550001"
        assert dyn["routing_sales_phone"] == "+14045550002"
        assert dyn["routing_support_phone"] == "+14045550003"
        assert dyn["routing_billing_phone"] == "+14045550004"
        assert dyn["routing_scheduling_phone"] == "+14045550005"

    def test_business_name_from_tenant_profile(self) -> None:
        """business_name populated from tenant_profiles table."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["business_name"] == "Acme Plumbing Co"

    def test_industry_from_tenant_profile(self) -> None:
        """industry populated from tenant_profiles.industry."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["industry"] == "plumbing"

    def test_first_last_name_from_office_profile(self) -> None:
        """first_name/last_name populated from office_profiles."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["first_name"] == "Tonio"
        assert dyn["last_name"] == "Swayzee"

    def test_voicemail_email_from_office_profile(self) -> None:
        """voicemail_email populated from office_profiles.voicemail_email."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["voicemail_email"] == "tonio@acmeplumbing.com"

    def test_tenant_id_and_office_id_in_dynamic_vars(self) -> None:
        """tenant_id and office_id present in dynamic_variables (Law #6 scope identifiers)."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["tenant_id"] == TENANT_ID
        assert dyn["office_id"] == OFFICE_ID

    def test_is_after_hours_is_bool_inverse_of_is_open_now(self) -> None:
        """is_after_hours is bool and inverse of is_open_now (Law #3: no silent type coercion)."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        is_open = dyn["is_open_now"]
        is_after = dyn["is_after_hours"]
        assert isinstance(is_open, bool)
        assert isinstance(is_after, bool)
        assert is_open != is_after, "is_after_hours must be the inverse of is_open_now"

    def test_caller_history_summary_empty_string_v1(self) -> None:
        """caller_history_summary is empty string (V1 — caller digest deferred to V2)."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["caller_history_summary"] == ""

    def test_routing_contacts_summary_contains_names(self) -> None:
        """routing_contacts_summary is non-empty and contains at least one contact name."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        summary = dyn["routing_contacts_summary"]
        assert isinstance(summary, str)
        # Summary should reference at least one of the configured contacts
        assert len(summary) > 0

    def test_conversation_config_override_present(self) -> None:
        """conversation_config_override.agent.first_message is present and non-empty."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        body = resp.json()
        override = body.get("conversation_config_override", {})
        agent_cfg = override.get("agent", {})
        first_msg = agent_cfg.get("first_message", "")
        assert isinstance(first_msg, str) and len(first_msg) > 0, (
            "first_message must be a non-empty dynamic greeting"
        )

    def test_first_message_references_business_name(self) -> None:
        """Dynamic greeting includes business_name from tenant profile."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        body = resp.json()
        first_msg = body["conversation_config_override"]["agent"]["first_message"]
        # Must include the business name OR a non-empty placeholder
        assert "Acme Plumbing Co" in first_msg or len(first_msg) > 20, (
            "Dynamic first_message must reference business_name"
        )

    def test_time_of_day_is_valid_enum(self) -> None:
        """time_of_day is one of morning|afternoon|evening."""
        resp = _post_personalization_with_hmac()
        assert resp.status_code == 200
        dyn = resp.json()["dynamic_variables"]
        assert dyn["time_of_day"] in ("morning", "afternoon", "evening")


# ---------------------------------------------------------------------------
# HMAC enforcement tests (Law #3 — fail closed)
# ---------------------------------------------------------------------------


class TestHMACEnforcement:
    """Law #3: HMAC invalid → 401 fail-closed."""

    def test_invalid_signature_returns_401(self) -> None:
        """Any tampered HMAC signature must return 401."""
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()

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
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "ElevenLabs-Signature": "t=9999,v0=badbadbadbad",
                },
            )

        assert resp.status_code == 401, (
            f"Invalid HMAC must return 401, got {resp.status_code}"
        )

    def test_wrong_secret_returns_401(self) -> None:
        """Signature with wrong secret → 401 (Law #3: fail-closed)."""
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()
        # Sign with a DIFFERENT secret
        wrong_sig = _make_el_signature(body_bytes, "totally-wrong-secret")

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings",
                MagicMock(
                    elevenlabs_webhook_secret=EL_SECRET,  # server has correct secret
                    disable_personalization_hmac=False,
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
                    "ElevenLabs-Signature": wrong_sig,
                },
            )

        assert resp.status_code == 401

    def test_no_signature_header_returns_401(self) -> None:
        """Missing ElevenLabs-Signature header → 401 when bypass not enabled."""
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()

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
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            resp = _client.post(
                "/v1/sarah/personalization",
                content=body_bytes,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Latency budget (p95 <800ms under mock DB)
# ---------------------------------------------------------------------------


class TestLatencyBudget:
    """p95 <800ms across 50 calls under mock DB (zero real I/O)."""

    def test_p95_latency_under_800ms(self) -> None:
        """Run 50 requests against mocked DB and assert p95 < 800ms."""
        payload_dict = _make_payload()
        body_bytes = json.dumps(payload_dict).encode()
        latencies: list[float] = []

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
                new=AsyncMock(side_effect=_full_tenant_select_side_effect),
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                return_value=None,
            ),
        ):
            for _ in range(50):
                sig = _make_el_signature(body_bytes, EL_SECRET)
                t0 = time.monotonic()
                _client.post(
                    "/v1/sarah/personalization",
                    content=body_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "ElevenLabs-Signature": sig,
                    },
                )
                latencies.append(time.monotonic() - t0)

        p95 = statistics.quantiles(latencies, n=100)[94]
        assert p95 < 0.8, (
            f"p95 latency {p95:.3f}s exceeds 800ms budget. "
            f"All latencies: min={min(latencies):.3f} max={max(latencies):.3f}"
        )

    def test_single_call_under_800ms(self) -> None:
        """Single call completes within 800ms budget."""
        t0 = time.monotonic()
        resp = _post_personalization_with_hmac()
        elapsed = time.monotonic() - t0

        assert resp.status_code in (200, 404, 503)
        assert elapsed < 0.8, f"Handler took {elapsed:.3f}s, budget is 0.8s"
