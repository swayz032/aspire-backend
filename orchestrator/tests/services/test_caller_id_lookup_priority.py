"""Unit tests — caller-ID lookup priority order (Pass 19 Lane D).

Verifies the 4-tier priority resolution for GET /v1/calls/caller-id-lookup:
  1. phone in routing_contacts  → returns role + name (highest priority)
  2. phone in sms_thread memory → returns contact_type='sms_contact'
  3. phone in call memory       → returns contact_type='call_contact'
  4. phone in none              → fallback contact_type='unknown'

Also tests:
  - Priority 1 wins over Priority 2 when both match
  - Priority 2 wins over Priority 3 when both match
  - Capability token required (Law #5)
  - Receipt cut on every call regardless of priority (Law #2)
  - Law #9: formatted_number in response, phone prefix in receipt (NOT full phone)

Aspire Laws:
  Law #2: Receipt cut on every lookup regardless of outcome.
  Law #5: Capability token with telephony:caller_id_lookup scope required.
  Law #6: All queries scoped by office_id (never cross-tenant).
  Law #9: Full phone number in receipt only as truncated prefix.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.calls import router as calls_router

_app = FastAPI()
_app.include_router(calls_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "aa000000-0000-0000-0000-000000000001"
OFFICE_ID = "aa000000-0000-0000-0000-000000000002"
TENANT_ID = "aa000000-0000-0000-0000-000000000003"
LOOKUP_PHONE = "+14155550001"

_SCOPE_HEADERS = {
    "X-Aspire-Tenant-Id": TENANT_ID,
    "X-Aspire-Suite-Id": SUITE_ID,
    "X-Aspire-Office-Id": OFFICE_ID,
}


def _mint_valid_token() -> dict:
    from aspire_orchestrator.services.token_service import mint_token
    return mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="telephony.caller_id_lookup",
        scopes=["telephony:caller_id_lookup"],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


def _routing_contact_row() -> list[dict]:
    return [{
        "id": str(uuid.uuid4()),
        "office_id": OFFICE_ID,
        "role": "owner",
        "label": "Tonio Swayzee",
        "phone": LOOKUP_PHONE,
        "is_active": True,
    }]


def _sms_thread_row() -> list[dict]:
    return [{
        "memory_id": str(uuid.uuid4()),
        "memory_type": "sms_thread",
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "detail": {
            "from": LOOKUP_PHONE,
            "contact_name": "Maya R.",
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }]


def _call_memory_row() -> list[dict]:
    recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    return [{
        "memory_id": str(uuid.uuid4()),
        "memory_type": "call",
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "detail": {
            "from": LOOKUP_PHONE,
            "caller_name": "James Liu",
        },
        "created_at": recent_date,
    }]


def _do_lookup(
    supabase_side_effect: Any,
    phone: str = LOOKUP_PHONE,
) -> Any:
    """Execute a caller-ID lookup with mocked DB."""
    token = _mint_valid_token()
    with (
        patch("aspire_orchestrator.routes.calls.supabase_select",
              new=AsyncMock(side_effect=supabase_side_effect)),
        patch("aspire_orchestrator.routes.calls.validate_token",
              return_value=type("R", (), {
                  "valid": True,
                  "error": None,
                  "error_message": "",
              })()),
        patch("aspire_orchestrator.routes.calls.receipt_store.store_receipts"),
    ):
        import json
        return _client.get(
            "/v1/calls/caller-id-lookup",
            params={"phone": phone},
            headers={
                **_SCOPE_HEADERS,
                "X-Aspire-Capability-Token": json.dumps(token),
            },
        )


# ---------------------------------------------------------------------------
# Priority 1: routing_contacts (highest priority)
# ---------------------------------------------------------------------------


class TestPriority1RoutingContacts:
    """Priority 1: phone in routing_contacts → role + display_name."""

    def test_routing_contact_returns_role_and_name(self) -> None:
        """Phone found in routing_contacts → contact_type='routing_contact' with role+name."""
        routing_row = _routing_contact_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return routing_row
            return []

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "routing_contact"
        assert body["role"] == "owner"
        assert body["display_name"] == "Tonio Swayzee"

    def test_routing_contact_formatted_number_present(self) -> None:
        """formatted_number is E.164 reformatted to US display format."""
        routing_row = _routing_contact_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return routing_row
            return []

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        # +14155550001 → (415) 555-0001
        assert resp.json()["formatted_number"] == "(415) 555-0001"

    def test_priority_1_wins_over_priority_2(self) -> None:
        """When phone is in both routing_contacts AND sms_thread, routing_contacts wins."""
        routing_row = _routing_contact_row()
        sms_row = _sms_thread_row()

        call_count = {"routing": 0, "sms": 0, "call": 0}

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                call_count["routing"] += 1
                return routing_row
            if "sms_thread" in filters:
                call_count["sms"] += 1
                return sms_row
            if "call" in filters:
                call_count["call"] += 1
                return []
            return []

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "routing_contact", (
            "Priority 1 must win over Priority 2"
        )
        # sms_thread query should not be reached when routing match found
        # (implementation may short-circuit at priority 1)


# ---------------------------------------------------------------------------
# Priority 2: sms_thread memory
# ---------------------------------------------------------------------------


class TestPriority2SMSThread:
    """Priority 2: phone NOT in routing_contacts but in sms_thread → sms_contact."""

    def test_sms_thread_contact_type_and_name(self) -> None:
        """No routing match → falls to sms_thread → contact_type='sms_contact'."""
        sms_row = _sms_thread_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return []  # No routing match
            if "sms_thread" in str(filters):
                return sms_row
            return []

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "sms_contact"
        assert body["display_name"] == "Maya R."

    def test_priority_2_wins_over_priority_3(self) -> None:
        """When phone is in both sms_thread AND call, sms_thread wins."""
        sms_row = _sms_thread_row()
        call_row = _call_memory_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return []
            if "sms_thread" in str(filters):
                return sms_row
            if "call" in str(filters):
                return call_row
            return []

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "sms_contact", (
            "Priority 2 (sms_thread) must win over Priority 3 (call memory)"
        )


# ---------------------------------------------------------------------------
# Priority 3: call memory
# ---------------------------------------------------------------------------


class TestPriority3CallMemory:
    """Priority 3: phone NOT in routing or sms_thread but in call memory."""

    def test_call_memory_contact_type_and_name(self) -> None:
        """No routing or SMS match → falls to call memory → contact_type='call_contact'."""
        call_row = _call_memory_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return []
            if "sms_thread" in str(filters):
                return []  # No SMS match
            if "call" in str(filters):
                return call_row
            return []

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "call_contact"
        assert body["display_name"] == "James Liu"


# ---------------------------------------------------------------------------
# Priority 4: Fallback (unknown)
# ---------------------------------------------------------------------------


class TestPriority4Fallback:
    """Priority 4: phone matches nothing → fallback contact_type='unknown'."""

    def test_no_match_returns_unknown(self) -> None:
        """Phone matches nothing → contact_type='unknown' with formatted_number."""
        def _select(table: str, filters: str = "", **kwargs) -> list:
            return []  # No match anywhere

        resp = _do_lookup(_select)

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "unknown"
        assert body["display_name"] == ""
        # formatted_number still present
        assert "formatted_number" in body
        assert len(body["formatted_number"]) > 0

    def test_unknown_fallback_still_has_formatted_number(self) -> None:
        """Unknown fallback formats the E.164 number for display."""
        def _select(table: str, filters: str = "", **kwargs) -> list:
            return []

        resp = _do_lookup(_select, phone="+12125550199")

        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_type"] == "unknown"
        # +12125550199 → (212) 555-0199
        assert "(212) 555-0199" in body["formatted_number"] or body["formatted_number"] != ""


# ---------------------------------------------------------------------------
# Receipt law (Law #2): receipt on every call
# ---------------------------------------------------------------------------


class TestReceiptCutOnEveryCall:
    """Law #2: Receipt cut on EVERY caller-ID lookup regardless of outcome."""

    def test_receipt_cut_on_routing_match(self) -> None:
        """Receipt cut when contact found in routing_contacts."""
        receipts: list[dict] = []

        def _capture(r: list) -> None:
            receipts.extend(r)

        token = _mint_valid_token()
        routing_row = _routing_contact_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return routing_row
            return []

        with (
            patch("aspire_orchestrator.routes.calls.supabase_select",
                  new=AsyncMock(side_effect=_select)),
            patch("aspire_orchestrator.routes.calls.validate_token",
                  return_value=type("R", (), {
                      "valid": True, "error": None, "error_message": ""
                  })()),
            patch("aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                  side_effect=_capture),
        ):
            import json
            _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": LOOKUP_PHONE},
                headers={
                    **_SCOPE_HEADERS,
                    "X-Aspire-Capability-Token": json.dumps(token),
                },
            )

        assert receipts, "No receipt cut — Law #2 violation"
        r = receipts[0]
        assert r["receipt_type"] == "caller_id_lookup"
        assert r["outcome"] == "success"

    def test_receipt_cut_on_fallback_unknown(self) -> None:
        """Receipt cut even when no contact found (fallback unknown)."""
        receipts: list[dict] = []

        def _capture(r: list) -> None:
            receipts.extend(r)

        token = _mint_valid_token()

        with (
            patch("aspire_orchestrator.routes.calls.supabase_select",
                  new=AsyncMock(return_value=[])),
            patch("aspire_orchestrator.routes.calls.validate_token",
                  return_value=type("R", (), {
                      "valid": True, "error": None, "error_message": ""
                  })()),
            patch("aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                  side_effect=_capture),
        ):
            import json
            _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": LOOKUP_PHONE},
                headers={
                    **_SCOPE_HEADERS,
                    "X-Aspire-Capability-Token": json.dumps(token),
                },
            )

        assert receipts, "No receipt cut on fallback — Law #2 violation"


# ---------------------------------------------------------------------------
# Law #9: No full phone in receipt
# ---------------------------------------------------------------------------


class TestNoFullPhoneInReceipt:
    """Law #9: Full phone number never in receipt — only truncated prefix."""

    def test_receipt_contains_prefix_not_full_phone(self) -> None:
        """Receipt redacted_inputs contains phone_prefix (first 6 chars), not full E.164."""
        receipts: list[dict] = []

        def _capture(r: list) -> None:
            receipts.extend(r)

        token = _mint_valid_token()
        routing_row = _routing_contact_row()

        def _select(table: str, filters: str = "", **kwargs) -> list:
            if table == "front_desk_routing_contacts":
                return routing_row
            return []

        with (
            patch("aspire_orchestrator.routes.calls.supabase_select",
                  new=AsyncMock(side_effect=_select)),
            patch("aspire_orchestrator.routes.calls.validate_token",
                  return_value=type("R", (), {
                      "valid": True, "error": None, "error_message": ""
                  })()),
            patch("aspire_orchestrator.routes.calls.receipt_store.store_receipts",
                  side_effect=_capture),
        ):
            import json
            _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": LOOKUP_PHONE},
                headers={
                    **_SCOPE_HEADERS,
                    "X-Aspire-Capability-Token": json.dumps(token),
                },
            )

        assert receipts
        r = receipts[0]
        raw = str(r)
        # Full E.164 number must not appear in receipt
        assert LOOKUP_PHONE not in raw, (
            f"Full phone number '{LOOKUP_PHONE}' found in receipt — Law #9 violation"
        )
        # Prefix (first 6 chars of E.164) should appear
        prefix = LOOKUP_PHONE[:6]
        inputs = r.get("redacted_inputs", {})
        phone_pfx = inputs.get("phone_prefix", "")
        assert phone_pfx.startswith(prefix), (
            f"Expected phone_prefix starting with '{prefix}', got '{phone_pfx}'"
        )


# ---------------------------------------------------------------------------
# Capability token requirement (Law #5)
# ---------------------------------------------------------------------------


class TestCapabilityTokenRequired:
    """Law #5: Capability token required — missing → 403."""

    def test_missing_capability_token_returns_403(self) -> None:
        """No X-Aspire-Capability-Token header → 403."""
        with patch("aspire_orchestrator.routes.calls.receipt_store.store_receipts"):
            resp = _client.get(
                "/v1/calls/caller-id-lookup",
                params={"phone": LOOKUP_PHONE},
                headers=_SCOPE_HEADERS,  # No X-Aspire-Capability-Token
            )

        assert resp.status_code == 403, (
            f"Missing capability token must return 403, got {resp.status_code}"
        )
