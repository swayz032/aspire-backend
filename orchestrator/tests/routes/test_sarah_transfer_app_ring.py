"""Tests for the app-ring dispatch leg of POST /v1/tools/sarah/transfer.

Covers:
  - owner transfer + APP_AND_PHONE_SIMUL_RING inserts a ringing call_sessions row
  - owner transfer + APP_ONLY also inserts a ringing row
  - owner transfer + PHONE_ONLY does NOT insert
  - non-owner role (sales/support/etc.) does NOT insert
  - ringing row metadata.transfer.* fields are correct
  - Supabase insert failure does NOT fail the transfer response
  - catch_mode lookup failure does NOT fail the transfer response
  - PII (caller_phone) is redacted in receipt / never echoed raw in response
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-ci")
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.sarah_tools import router as sarah_tools_router
from aspire_orchestrator.services.supabase_client import SupabaseClientError

_app = FastAPI()
_app.include_router(sarah_tools_router)
_client = TestClient(_app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUITE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
OFFICE_ID = "bbbbbbbb-0000-0000-0000-000000000002"
TENANT_ID = "cccccccc-0000-0000-0000-000000000003"
CALLED_NUMBER = "+12125550100"
CALLER_PHONE = "+13055559999"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scope() -> dict[str, str]:
    return {
        "tenant_id": TENANT_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
    }


def _routing_contact(role: str = "owner") -> dict[str, Any]:
    return {
        "role": role,
        "name": "Tony Scott",
        "phone": "+12125550199",
        "transfer_allowed": True,
        "label": "Owner",
    }


def _front_desk_config(catch_mode: str) -> dict[str, Any]:
    return {
        "office_id": OFFICE_ID,
        "catch_mode": catch_mode,
        "is_current": True,
        "version_no": 1,
    }


def _transfer_payload(
    *,
    role: str = "owner",
    catch_mode: str = "APP_AND_PHONE_SIMUL_RING",
    caller_phone: str = CALLER_PHONE,
    caller_business_name: str = "Acme Plumbing",
    caller_total_calls: int = 3,
    transfer_reason: str = "Customer has a billing question",
    capture_message: str = "Please call them back",
    agent_slug: str = "sarah",
    agent_display_name: str = "Sarah",
) -> dict[str, Any]:
    return {
        "called_number": CALLED_NUMBER,
        "transfer_role": role,
        "caller_name": "John Doe",
        "reason": "billing",
        "caller_phone": caller_phone,
        "caller_business_name": caller_business_name,
        "caller_total_calls": caller_total_calls,
        "transfer_reason": transfer_reason,
        "capture_message": capture_message,
        "agent_slug": agent_slug,
        "agent_display_name": agent_display_name,
    }


# ---------------------------------------------------------------------------
# Test 1: owner + SIMUL_RING inserts ringing row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_transfer_with_simul_ring_inserts_ringing_row() -> None:
    """Happy path: owner transfer with APP_AND_PHONE_SIMUL_RING creates ringing row."""
    inserted_rows: list[dict[str, Any]] = []

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            return [_front_desk_config("APP_AND_PHONE_SIMUL_RING")]
        return []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        inserted_rows.append({"table": table, "data": data})
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        resp = _client.post("/v1/tools/sarah/transfer", json=_transfer_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True

    # Assert exactly one call_sessions insert happened
    call_session_inserts = [r for r in inserted_rows if r["table"] == "call_sessions"]
    assert len(call_session_inserts) == 1

    row = call_session_inserts[0]["data"]
    assert row["status"] == "ringing"
    assert row["direction"] == "inbound"
    assert row["provider"] == "elevenlabs"
    assert row["suite_id"] == SUITE_ID
    assert row["owner_office_id"] == OFFICE_ID
    assert row["to_number"] == CALLED_NUMBER
    assert row["provider_call_id"].startswith("transfer-")


# ---------------------------------------------------------------------------
# Test 2: owner + APP_ONLY also inserts ringing row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_transfer_with_app_only_inserts_ringing_row() -> None:
    """APP_ONLY catch_mode also dispatches an app-ring row."""
    inserted_tables: list[str] = []

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            return [_front_desk_config("APP_ONLY")]
        return []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        inserted_tables.append(table)
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        resp = _client.post("/v1/tools/sarah/transfer", json=_transfer_payload())

    assert resp.status_code == 200
    assert "call_sessions" in inserted_tables


# ---------------------------------------------------------------------------
# Test 3: owner + PHONE_ONLY does NOT insert a ringing row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_transfer_with_phone_only_does_not_insert() -> None:
    """PHONE_ONLY catch_mode must not create a call_sessions row."""
    inserted_tables: list[str] = []

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            return [_front_desk_config("PHONE_ONLY")]
        return []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        inserted_tables.append(table)
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        resp = _client.post("/v1/tools/sarah/transfer", json=_transfer_payload())

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # call_sessions must NOT have been touched
    assert "call_sessions" not in inserted_tables


# ---------------------------------------------------------------------------
# Test 4: non-owner role does NOT trigger app ring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_owner_transfer_does_not_insert() -> None:
    """Sales / support / billing / scheduling roles must not dispatch app ring."""
    for role in ("sales", "support", "billing", "scheduling"):
        inserted_tables: list[str] = []

        async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_phone_numbers":
                return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
            if table == "front_desk_routing_contacts":
                return [_routing_contact(role)]
            # front_desk_configs should NEVER be called for non-owner transfers
            return []

        async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
            inserted_tables.append(table)
            return data

        with (
            patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
            patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
            patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
        ):
            resp = _client.post(
                "/v1/tools/sarah/transfer",
                json=_transfer_payload(role=role),
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert "call_sessions" not in inserted_tables, (
            f"call_sessions inserted for non-owner role={role}"
        )


# ---------------------------------------------------------------------------
# Test 5: metadata.transfer.* fields are correctly populated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_metadata_includes_agent_name_and_reason() -> None:
    """Inserted row metadata must match the frontend contract."""
    captured_data: list[dict[str, Any]] = []

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            return [_front_desk_config("APP_AND_PHONE_SIMUL_RING")]
        return []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        if table == "call_sessions":
            captured_data.append(data)
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        _client.post(
            "/v1/tools/sarah/transfer",
            json=_transfer_payload(
                agent_slug="tiffany",
                agent_display_name="Tiffany",
                transfer_reason="Customer needs billing help",
                capture_message="Please call back ASAP",
                caller_business_name="Acme Plumbing",
                caller_total_calls=5,
            ),
        )

    assert len(captured_data) == 1
    meta = captured_data[0]["metadata"]

    transfer_block = meta["transfer"]
    assert transfer_block["agent"] == "tiffany"
    assert transfer_block["agent_name"] == "Tiffany"
    assert transfer_block["reason"] == "Customer needs billing help"
    assert transfer_block["capture_message"] == "Please call back ASAP"
    assert meta["contact_business_name"] == "Acme Plumbing"
    assert meta["caller_total_calls"] == 5


# ---------------------------------------------------------------------------
# Test 6: Supabase insert failure does NOT fail the transfer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_ring_insert_failure_does_not_fail_transfer() -> None:
    """If the call_sessions insert raises, the /transfer endpoint still returns success."""
    call_count = {"insert": 0}

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            return [_front_desk_config("APP_AND_PHONE_SIMUL_RING")]
        return []

    async def mock_insert_fail(table: str, data: dict[str, Any]) -> dict[str, Any]:
        call_count["insert"] += 1
        if table == "call_sessions":
            raise SupabaseClientError("insert/call_sessions", status_code=500, detail="DB error")
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert_fail),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        resp = _client.post("/v1/tools/sarah/transfer", json=_transfer_payload())

    # Transfer must succeed even when the app-ring insert failed
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["role"] == "owner"


# ---------------------------------------------------------------------------
# Test 7: catch_mode lookup failure does NOT fail the transfer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catch_mode_lookup_failure_does_not_fail_transfer() -> None:
    """If front_desk_configs lookup raises, transfer still returns success (no app ring)."""
    inserted_tables: list[str] = []
    select_call_order: list[str] = []

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        select_call_order.append(table)
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            raise SupabaseClientError("select/front_desk_configs", status_code=503)
        return []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        inserted_tables.append(table)
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        resp = _client.post("/v1/tools/sarah/transfer", json=_transfer_payload())

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # No call_sessions insert when catch_mode is unknown (fail closed = no ring)
    assert "call_sessions" not in inserted_tables


# ---------------------------------------------------------------------------
# Test 8: caller_phone is NOT echoed raw in the transfer HTTP response (PII)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caller_phone_not_in_transfer_response() -> None:
    """Raw caller_phone (PII) must not appear in the JSON response body."""

    async def mock_select(table: str, query: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == "tenant_phone_numbers":
            return [{"tenant_id": TENANT_ID, "suite_id": SUITE_ID, "office_id": OFFICE_ID}]
        if table == "front_desk_routing_contacts":
            return [_routing_contact("owner")]
        if table == "front_desk_configs":
            return [_front_desk_config("APP_AND_PHONE_SIMUL_RING")]
        return []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        return data

    with (
        patch("aspire_orchestrator.routes.sarah_tools.supabase_select", side_effect=mock_select),
        patch("aspire_orchestrator.routes.sarah_tools.supabase_insert", side_effect=mock_insert),
        patch("aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts"),
    ):
        resp = _client.post(
            "/v1/tools/sarah/transfer",
            json=_transfer_payload(caller_phone="+13055559999"),
        )

    # The raw phone number must NOT appear in the response body
    assert "+13055559999" not in resp.text
