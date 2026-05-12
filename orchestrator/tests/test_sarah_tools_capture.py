"""Tests for capture_message handler — Fix 1 (data capture + contacts upsert).

Covers:
- CaptureMessageReq schema accepts new fields (category, callback_window, route_to)
- Handler writes all fields into memory_objects.detail (nothing silently dropped)
- Handler UPSERTs frontdesk_contacts on (phone_e164, office_id)
  - New row: contact_id, display_name, category, last_call_summary, total_calls=1
  - Existing row: total_calls incremented, notes appended, summary updated
- Receipt emitted with receipt_type='contact_captured' (Law #2)
- PII: raw phone never in receipt inputs (only redacted prefix)
- RLS evil test: cross-office capture writes to correct office only
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = str(uuid.uuid4())
SUITE_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
OTHER_OFFICE_ID = str(uuid.uuid4())

CALLED_NUMBER = "+14482885386"
CALLER_PHONE = "+19416818610"
CALLER_NAME = "Mike Johnson"
MESSAGE = "Called about exterior painting quote — wants callback before Friday."

FAKE_SCOPE = {
    "tenant_id": TENANT_ID,
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
}


def _fake_memory_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_capture_message_req_accepts_new_fields() -> None:
    """CaptureMessageReq now accepts category, callback_window, route_to."""
    from aspire_orchestrator.routes.sarah_tools import CaptureMessageReq

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message=MESSAGE,
        urgency="urgent",
        reason_category="new_inquiry",
        category="lead",
        callback_window="tomorrow between 9 and 11 AM",
        route_to="owner",
    )
    assert req.category == "lead"
    assert req.callback_window == "tomorrow between 9 and 11 AM"
    assert req.route_to == "owner"


def test_capture_message_req_new_fields_default_none() -> None:
    """New fields default to None — backward compat for existing callers."""
    from aspire_orchestrator.routes.sarah_tools import CaptureMessageReq

    req = CaptureMessageReq(called_number=CALLED_NUMBER)
    assert req.category is None
    assert req.callback_window is None
    assert req.route_to is None


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------


def test_resolve_contact_category_uses_explicit_category() -> None:
    from aspire_orchestrator.routes.sarah_tools import (
        CaptureMessageReq,
        _resolve_contact_category,
    )

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        reason_category="general",
        category="vendor",
    )
    assert _resolve_contact_category(req) == "vendor"


def test_resolve_contact_category_heuristic_lead() -> None:
    from aspire_orchestrator.routes.sarah_tools import (
        CaptureMessageReq,
        _resolve_contact_category,
    )

    for rc in ("quote", "estimate", "new_inquiry"):
        req = CaptureMessageReq(called_number=CALLED_NUMBER, reason_category=rc)
        assert _resolve_contact_category(req) == "lead", f"reason_category={rc}"


def test_resolve_contact_category_heuristic_client() -> None:
    from aspire_orchestrator.routes.sarah_tools import (
        CaptureMessageReq,
        _resolve_contact_category,
    )

    for rc in ("existing_job", "invoice", "followup"):
        req = CaptureMessageReq(called_number=CALLED_NUMBER, reason_category=rc)
        assert _resolve_contact_category(req) == "client", f"reason_category={rc}"


def test_resolve_contact_category_unknown_fallback() -> None:
    from aspire_orchestrator.routes.sarah_tools import (
        CaptureMessageReq,
        _resolve_contact_category,
    )

    req = CaptureMessageReq(called_number=CALLED_NUMBER, reason_category="other")
    assert _resolve_contact_category(req) == "unknown"


# ---------------------------------------------------------------------------
# Handler — memory_objects includes new fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_includes_new_fields_in_detail() -> None:
    """category, callback_window, route_to must appear in memory_objects.detail."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message=MESSAGE,
        urgency="normal",
        reason_category="new_inquiry",
        category="lead",
        callback_window="tomorrow 9-11 AM",
        route_to="owner",
    )

    inserted_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        if table == "memory_objects":
            inserted_rows.append(row)
            return {**row, "memory_id": str(uuid.uuid4())}
        return {**row}

    with (
        patch(
            "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
            new=AsyncMock(return_value=FAKE_SCOPE),
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_insert",
            side_effect=mock_insert,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_select",
            new=AsyncMock(return_value=[]),  # no existing contact
        ),
        patch("aspire_orchestrator.routes.sarah_tools._cut_receipt"),
    ):
        result = await capture_message(req)

    assert result["success"] is True
    assert inserted_rows, "memory_objects insert never called"
    detail = inserted_rows[0].get("detail", {})
    assert detail.get("category") == "lead"
    assert detail.get("callback_window") == "tomorrow 9-11 AM"
    assert detail.get("route_to") == "owner"


# ---------------------------------------------------------------------------
# Handler — frontdesk_contacts upsert (new contact)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_inserts_new_contact() -> None:
    """When no existing contact row, a new frontdesk_contacts row is inserted."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message=MESSAGE,
        urgency="normal",
        reason_category="new_inquiry",
        category="lead",
    )

    contact_inserts: list[dict[str, Any]] = []

    async def mock_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        if table == "frontdesk_contacts":
            contact_inserts.append(row)
        return {**row, "memory_id": str(uuid.uuid4()), "contact_id": str(uuid.uuid4())}

    async def mock_select(
        table: str, filters: Any, *, order_by: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        # No existing contact
        if table == "frontdesk_contacts":
            return []
        if table == "tenant_phone_numbers":
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        return []

    with (
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_insert",
            side_effect=mock_insert,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_select",
            side_effect=mock_select,
        ),
        patch("aspire_orchestrator.routes.sarah_tools._cut_receipt"),
        patch(
            "aspire_orchestrator.middleware.correlation.get_correlation_id",
            return_value=str(uuid.uuid4()),
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_trace_id",
            return_value=str(uuid.uuid4()),
        ),
    ):
        result = await capture_message(req)

    assert result["success"] is True
    assert contact_inserts, "frontdesk_contacts insert never called"
    ci = contact_inserts[0]
    assert ci["phone_e164"] == CALLER_PHONE
    assert ci["office_id"] == OFFICE_ID
    assert ci["display_name"] == CALLER_NAME
    assert ci["category"] == "lead"
    assert ci["total_calls"] == 1
    assert ci["status"] == "active"
    assert MESSAGE[:300] == ci["last_call_summary"]


# ---------------------------------------------------------------------------
# Handler — frontdesk_contacts upsert (existing contact)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_updates_existing_contact() -> None:
    """When existing contact found, total_calls increments and notes appended."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    existing_contact_id = str(uuid.uuid4())
    existing_contact = {
        "contact_id": existing_contact_id,
        "phone_e164": CALLER_PHONE,
        "office_id": OFFICE_ID,
        "display_name": CALLER_NAME,
        "total_calls": 3,
        "notes": "[2026-05-10] First call about paint.",
        "category": "lead",
    }

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message="Second call — confirmed quote request.",
        urgency="normal",
        reason_category="quote",
    )

    update_calls: list[tuple[str, str, dict]] = []

    async def mock_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        # Only memory_objects insert expected
        return {**row, "memory_id": str(uuid.uuid4())}

    async def mock_update(table: str, filters: str, data: dict[str, Any]) -> dict[str, Any]:
        update_calls.append((table, filters, data))
        return data

    async def mock_select(
        table: str, filters: Any, *, order_by: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if table == "frontdesk_contacts":
            return [existing_contact]
        if table == "tenant_phone_numbers":
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        return []

    with (
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_insert",
            side_effect=mock_insert,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_select",
            side_effect=mock_select,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_update",
            side_effect=mock_update,
        ),
        patch("aspire_orchestrator.routes.sarah_tools._cut_receipt"),
        patch(
            "aspire_orchestrator.middleware.correlation.get_correlation_id",
            return_value=str(uuid.uuid4()),
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_trace_id",
            return_value=str(uuid.uuid4()),
        ),
    ):
        result = await capture_message(req)

    assert result["success"] is True
    assert update_calls, "supabase_update never called for existing contact"
    _table, _filters, update_data = update_calls[0]
    assert _table == "frontdesk_contacts"
    assert f"contact_id=eq.{existing_contact_id}" in _filters
    # total_calls incremented from 3 → 4
    assert update_data["total_calls"] == 4
    # notes appended (not overwritten)
    assert "[2026-05-10] First call" in update_data["notes"]
    assert "Second call" in update_data["notes"]


# ---------------------------------------------------------------------------
# Receipt — Law #2 compliance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_emits_contact_captured_receipt() -> None:
    """Every capture path must emit receipt_type='contact_captured'."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message=MESSAGE,
        urgency="normal",
        reason_category="new_inquiry",
    )

    receipt_calls: list[dict[str, Any]] = []

    def mock_cut_receipt(**kwargs: Any) -> str:
        receipt_calls.append(kwargs)
        return str(uuid.uuid4())

    async def mock_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "memory_id": str(uuid.uuid4())}

    async def mock_select(
        table: str, filters: Any, *, order_by: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if table == "frontdesk_contacts":
            return []
        if table == "tenant_phone_numbers":
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        return []

    with (
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_insert",
            side_effect=mock_insert,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_select",
            side_effect=mock_select,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools._cut_receipt",
            side_effect=mock_cut_receipt,
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_correlation_id",
            return_value=str(uuid.uuid4()),
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_trace_id",
            return_value=str(uuid.uuid4()),
        ),
    ):
        await capture_message(req)

    assert receipt_calls, "No receipt emitted"
    receipt_types = [r.get("receipt_type") for r in receipt_calls]
    assert "contact_captured" in receipt_types


# ---------------------------------------------------------------------------
# PII — Law #9: raw phone never in receipt inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_raw_phone_not_in_receipt() -> None:
    """Redacted phone prefix (not full number) must appear in receipt inputs."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message=MESSAGE,
        urgency="normal",
        reason_category="new_inquiry",
    )

    receipt_calls: list[dict[str, Any]] = []

    def mock_cut_receipt(**kwargs: Any) -> str:
        receipt_calls.append(kwargs)
        return str(uuid.uuid4())

    async def mock_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "memory_id": str(uuid.uuid4())}

    async def mock_select(
        table: str, filters: Any, *, order_by: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if table == "frontdesk_contacts":
            return []
        if table == "tenant_phone_numbers":
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        return []

    with (
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_insert",
            side_effect=mock_insert,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_select",
            side_effect=mock_select,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools._cut_receipt",
            side_effect=mock_cut_receipt,
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_correlation_id",
            return_value=str(uuid.uuid4()),
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_trace_id",
            return_value=str(uuid.uuid4()),
        ),
    ):
        await capture_message(req)

    for receipt in receipt_calls:
        inputs_str = str(receipt.get("redacted_inputs", {}))
        # Full phone must not appear verbatim
        assert CALLER_PHONE not in inputs_str, (
            f"Full caller phone leaked into receipt inputs: {inputs_str}"
        )
        # Only truncated prefix may appear (e.g., "+19416...")
        assert MESSAGE not in inputs_str, (
            f"Raw message leaked into receipt inputs: {inputs_str}"
        )


# ---------------------------------------------------------------------------
# RLS evil test — cross-office isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_contact_scoped_to_correct_office() -> None:
    """Contact upsert must use the scope's office_id, not any caller-supplied value."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name=CALLER_NAME,
        caller_phone=CALLER_PHONE,
        message=MESSAGE,
        urgency="normal",
        reason_category="new_inquiry",
    )

    contact_inserts: list[dict[str, Any]] = []

    async def mock_insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
        if table == "frontdesk_contacts":
            contact_inserts.append(row)
        return {**row, "memory_id": str(uuid.uuid4())}

    async def mock_select(
        table: str, filters: Any, *, order_by: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if table == "frontdesk_contacts":
            return []  # no existing contact
        if table == "tenant_phone_numbers":
            # Scope resolves to OFFICE_ID — the server dictates, not the caller
            return [
                {
                    "tenant_id": TENANT_ID,
                    "suite_id": SUITE_ID,
                    "office_id": OFFICE_ID,
                    "phone_number": CALLED_NUMBER,
                    "status": "active",
                }
            ]
        return []

    with (
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_insert",
            side_effect=mock_insert,
        ),
        patch(
            "aspire_orchestrator.routes.sarah_tools.supabase_select",
            side_effect=mock_select,
        ),
        patch("aspire_orchestrator.routes.sarah_tools._cut_receipt"),
        patch(
            "aspire_orchestrator.middleware.correlation.get_correlation_id",
            return_value=str(uuid.uuid4()),
        ),
        patch(
            "aspire_orchestrator.middleware.correlation.get_trace_id",
            return_value=str(uuid.uuid4()),
        ),
    ):
        await capture_message(req)

    assert contact_inserts, "frontdesk_contacts insert not called"
    # Must ALWAYS use the server-resolved office_id (Law #6)
    assert contact_inserts[0]["office_id"] == OFFICE_ID
    # Must NOT bleed into OTHER_OFFICE_ID
    assert contact_inserts[0]["office_id"] != OTHER_OFFICE_ID
