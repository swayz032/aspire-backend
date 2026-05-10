"""Pass 3 — Agent display name registry tests.

Covers:
- Known agent_ids resolve to the correct display name.
- Unknown agent_id raises UnknownAgentError from the internal helper.
- POST /v1/sarah/personalization returns 400 on unknown agent_id.
- POST /v1/sarah/personalization never returns blank agent_name in dynamic_variables.
- Receipt emitted on unknown agent_id: headers_sha256 is a 64-char hex, raw
  headers are absent (Law #9 PII guard).
- Edge cases: empty string, whitespace-only, leading/trailing whitespace, None.

Agent IDs are case-sensitive (EL workspace canonical IDs).
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.sarah import (
    UnknownAgentError,
    _AGENT_DISPLAY_NAME,
    _resolve_agent_display_name,
    router as sarah_router,
)

# ── App fixture ───────────────────────────────────────────────────────────────

_app = FastAPI()
_app.include_router(sarah_router)
_client = TestClient(_app, raise_server_exceptions=False)

# ── Constants ─────────────────────────────────────────────────────────────────

TIFFANY_AGENT_ID = "agent_4801kqtapvsre2gb0gyb1ng631qr"
SARAH_RECEPTIONIST_AGENT_ID = "agent_6501kp71h69jfqysgd055hemqhrq"
SARAH_FRONTDESK_AGENT_ID = "agent_8901kmqdjnrte7psp6en4f85m4kt"

CALLED_NUMBER = "+12125550188"
CALL_SID = "CApass3test001"
EL_SECRET = "test-secret-pass3"

SUITE_ID = "00000000-0000-0000-0000-000000000003"
OFFICE_ID = "00000000-0000-0000-0000-000000000033"
TENANT_ID = "00000000-0000-0000-0000-000000000093"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _personalization_payload(
    agent_id: str,
    called_number: str = CALLED_NUMBER,
    call_sid: str = CALL_SID,
) -> dict[str, Any]:
    return {
        "called_number": called_number,
        "call_sid": call_sid,
        "caller_id": "+19175550300",
        "agent_id": agent_id,
    }


def _mock_resolution(agent_id: str) -> dict[str, Any]:
    """Return a minimal _resolve_personalization result for a known agent."""
    return {
        "unknown_number": False,
        "dyn_vars": {
            "business_name": "Acme Plumbing",
            "first_name": "Antonio",
            "last_name": "Scott",
            "industry": "plumbing",
            "industry_specialty": "",
            "business_city": "Tallahassee",
            "business_state": "FL",
            "owner_title": "Owner",
            "time_of_day": "morning",
            "is_open_now": True,
            "is_after_hours": False,
            "after_hours_mode": "take_message",
            "busy_mode": "take_message",
            "public_number_mode": "ASPIRE_NEW_NUMBER",
            "catch_mode": "APP_AND_PHONE_SIMUL_RING",
            "greeting_name_override": "",
            "pronunciation_override": "",
            "routing_contacts_summary": "",
            "routing_owner_phone": "+14155550001",
            "routing_sales_phone": "",
            "routing_support_phone": "",
            "routing_billing_phone": "",
            "routing_scheduling_phone": "",
            "routing_owner_name": "Tonio",
            "routing_sales_name": "",
            "routing_support_name": "",
            "routing_billing_name": "",
            "routing_scheduling_name": "",
            "owner_salutation": "Mr.",
            "owner_formal_name": "Mr. Scott",
            "configured_roles": "owner",
            "tenant_id": TENANT_ID,
            "office_id": OFFICE_ID,
            "voicemail_email": "tonio@acmeplumbing.com",
            "caller_history_summary": "",
            "caller_is_known": False,
            "caller_display_name": "",
            "caller_first_name": "",
            "caller_company": "",
            "caller_last_call_summary": "",
            "caller_total_calls": 0,
            "caller_last_seen_days_ago": 0,
            "caller_category": "",
        },
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "tenant_id": TENANT_ID,
        "front_desk_config_id": str(uuid.uuid4()),
        "is_open": True,
        "time_of_day": "morning",
        "version_no": 1,
    }


def _post_personalization(
    agent_id: str,
    called_number: str = CALLED_NUMBER,
    captured_receipts: list[dict[str, Any]] | None = None,
) -> Any:
    """POST /v1/sarah/personalization with auth bypass + mocked DB resolution.

    If `captured_receipts` is provided, receipt_store.store_receipts calls are
    intercepted and appended to it.
    """
    def _fake_store(receipts: list[dict[str, Any]]) -> None:
        if captured_receipts is not None:
            captured_receipts.extend(receipts)

    with (
        patch(
            "aspire_orchestrator.routes.sarah.settings.disable_personalization_hmac",
            True,
        ),
        patch(
            "aspire_orchestrator.routes.sarah._is_production_origin",
            return_value=False,
        ),
        patch(
            "aspire_orchestrator.routes.sarah._resolve_personalization",
            return_value=_mock_resolution(agent_id),
        ),
        patch(
            "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
            side_effect=_fake_store,
        ),
        patch(
            "aspire_orchestrator.routes.sarah.METRICS",
            MagicMock(),
        ),
    ):
        payload = _personalization_payload(agent_id, called_number=called_number)
        return _client.post(
            "/v1/sarah/personalization",
            json=payload,
            headers={"X-Aspire-Webhook-Secret": EL_SECRET},
        )


# ── Contract tests: internal helper ──────────────────────────────────────────

class TestResolveAgentDisplayName:
    """Unit tests for the _resolve_agent_display_name strict helper."""

    def test_known_agent_id_resolves_to_display_name(self) -> None:
        assert _resolve_agent_display_name(TIFFANY_AGENT_ID) == "Tiffany"
        assert _resolve_agent_display_name(SARAH_RECEPTIONIST_AGENT_ID) == "Sarah"
        assert _resolve_agent_display_name(SARAH_FRONTDESK_AGENT_ID) == "Sarah"

    def test_unknown_agent_id_raises_unknown_agent_error(self) -> None:
        with pytest.raises(UnknownAgentError) as exc_info:
            _resolve_agent_display_name("agent_bogus0000000000000000000")
        assert "agent_bogus0000000000000000000" in str(exc_info.value)

    def test_empty_string_raises_unknown_agent_error(self) -> None:
        with pytest.raises(UnknownAgentError):
            _resolve_agent_display_name("")

    def test_none_like_empty_raises_unknown_agent_error(self) -> None:
        # _resolve_agent_display_name expects a str; empty-string-equivalent check.
        with pytest.raises(UnknownAgentError):
            _resolve_agent_display_name("   ")  # whitespace-only

    def test_whitespace_stripped_then_unknown_raises(self) -> None:
        # Leading/trailing whitespace is stripped; if still not in registry → raise.
        with pytest.raises(UnknownAgentError):
            _resolve_agent_display_name("  agent_unknown  ")

    def test_whitespace_stripped_known_agent_resolves(self) -> None:
        # Leading/trailing whitespace stripped; real id still resolves.
        assert _resolve_agent_display_name(f"  {TIFFANY_AGENT_ID}  ") == "Tiffany"

    def test_case_sensitivity_uppercase_fails(self) -> None:
        # Agent IDs are case-sensitive. Uppercased id must not resolve.
        with pytest.raises(UnknownAgentError):
            _resolve_agent_display_name(TIFFANY_AGENT_ID.upper())

    def test_return_value_never_empty(self) -> None:
        for agent_id, expected in _AGENT_DISPLAY_NAME.items():
            result = _resolve_agent_display_name(agent_id)
            assert result, f"Got empty string for agent_id={agent_id}"
            assert result == expected


# ── Contract tests: HTTP endpoint ─────────────────────────────────────────────

class TestPersonalizationEndpointAgentValidation:
    """Integration tests for the /v1/sarah/personalization agent_id guard."""

    def test_personalization_endpoint_returns_400_on_unknown_agent(self) -> None:
        receipts: list[dict[str, Any]] = []
        resp = _post_personalization("agent_bogus00000bogus", captured_receipts=receipts)
        assert resp.status_code == 400
        body = resp.json()
        assert body.get("detail", {}).get("code") == "UNKNOWN_AGENT"
        assert "agent_bogus00000bogus" in body["detail"]["detail"]
        assert "trace_id" in body["detail"]

    def test_personalization_endpoint_returns_400_on_empty_agent_id(self) -> None:
        receipts: list[dict[str, Any]] = []
        resp = _post_personalization("", captured_receipts=receipts)
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "UNKNOWN_AGENT"

    def test_personalization_endpoint_returns_400_on_whitespace_agent_id(self) -> None:
        resp = _post_personalization("   ")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "UNKNOWN_AGENT"

    def test_personalization_endpoint_never_returns_blank_agent_name_tiffany(self) -> None:
        resp = _post_personalization(TIFFANY_AGENT_ID)
        assert resp.status_code == 200
        first_message: str = resp.json()["conversation_config_override"]["agent"]["first_message"]
        assert "Tiffany" in first_message, f"Expected 'Tiffany' in first_message: {first_message!r}"

    def test_personalization_endpoint_never_returns_blank_agent_name_sarah_receptionist(self) -> None:
        resp = _post_personalization(SARAH_RECEPTIONIST_AGENT_ID)
        assert resp.status_code == 200
        first_message: str = resp.json()["conversation_config_override"]["agent"]["first_message"]
        assert "Sarah" in first_message, f"Expected 'Sarah' in first_message: {first_message!r}"

    def test_personalization_endpoint_never_returns_blank_agent_name_sarah_frontdesk(self) -> None:
        resp = _post_personalization(SARAH_FRONTDESK_AGENT_ID)
        assert resp.status_code == 200
        first_message: str = resp.json()["conversation_config_override"]["agent"]["first_message"]
        assert "Sarah" in first_message, f"Expected 'Sarah' in first_message: {first_message!r}"

    def test_receipt_emitted_with_redacted_headers_on_unknown_agent(self) -> None:
        receipts: list[dict[str, Any]] = []
        resp = _post_personalization("agent_bogus_receipt_test", captured_receipts=receipts)
        assert resp.status_code == 400

        # Find the unknown_agent_in_personalization receipt.
        unknown_receipts = [
            r for r in receipts
            if r.get("receipt_type") == "unknown_agent_in_personalization"
        ]
        assert len(unknown_receipts) == 1, (
            f"Expected 1 unknown_agent receipt, got {len(unknown_receipts)}. "
            f"All receipts: {receipts}"
        )

        receipt = unknown_receipts[0]
        redacted = receipt.get("redacted_inputs", {})

        # headers_sha256 must be a 64-char hex string (SHA256).
        sha256_val = redacted.get("headers_sha256", "")
        assert len(sha256_val) == 64, f"headers_sha256 should be 64 chars, got: {sha256_val!r}"
        assert all(c in "0123456789abcdef" for c in sha256_val), (
            f"headers_sha256 is not a lowercase hex string: {sha256_val!r}"
        )

        # Raw header values must NOT appear in the receipt body.
        receipt_str = str(receipt)
        assert "X-Aspire-Webhook-Secret" not in receipt_str, (
            "Raw header name appeared in receipt — Law #9 violation"
        )
        assert EL_SECRET not in receipt_str, (
            "Raw secret value appeared in receipt — Law #9 violation"
        )

        # attempted_agent_id and source_ip must be present.
        assert redacted.get("attempted_agent_id") == "agent_bogus_receipt_test"
        assert "source_ip" in redacted

        # outcome and reason_code must be correct.
        assert receipt.get("outcome") == "denied"
        assert receipt.get("reason_code") == "UNKNOWN_AGENT"

    def test_receipt_store_failure_does_not_raise_into_request_path(self) -> None:
        """Law #2 + robustness: receipt store error must never 500 the caller."""
        def _exploding_store(_receipts: list[dict[str, Any]]) -> None:
            raise RuntimeError("Supabase is down")

        with (
            patch(
                "aspire_orchestrator.routes.sarah.settings.disable_personalization_hmac",
                True,
            ),
            patch(
                "aspire_orchestrator.routes.sarah._is_production_origin",
                return_value=False,
            ),
            patch(
                "aspire_orchestrator.routes.sarah.receipt_store.store_receipts",
                side_effect=_exploding_store,
            ),
            patch(
                "aspire_orchestrator.routes.sarah.METRICS",
                MagicMock(),
            ),
        ):
            payload = _personalization_payload("agent_bogus_exploding")
            resp = _client.post(
                "/v1/sarah/personalization",
                json=payload,
                headers={"X-Aspire-Webhook-Secret": EL_SECRET},
            )
        # Must still return 400 (not 500).
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "UNKNOWN_AGENT"

    def test_known_agent_id_produces_200_and_non_empty_first_message(self) -> None:
        for agent_id, expected_name in _AGENT_DISPLAY_NAME.items():
            resp = _post_personalization(agent_id)
            assert resp.status_code == 200, (
                f"Expected 200 for {agent_id}, got {resp.status_code}"
            )
            body = resp.json()
            first_message = body["conversation_config_override"]["agent"]["first_message"]
            assert first_message, f"first_message is empty for agent_id={agent_id}"
            assert expected_name in first_message, (
                f"Expected name {expected_name!r} in first_message for "
                f"agent_id={agent_id}: {first_message!r}"
            )
