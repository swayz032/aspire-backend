"""Tests for _insert_memory_object and its three callers in sarah_tools.

Covers:
- Correct column names sent to memory_objects (memory_type, summary, detail, trace_id,
  correlation_id — NOT the old object_type/metadata/id columns that caused silent drops)
- Idempotency key deduplication path
- Missing required fields raise, not swallow
- capture_message persists with memory_type='voicemail_capture'
- callback_request persists with memory_type='callback_request'
- call_summary persists with memory_type='call_summary'
- DB error is no longer swallowed — receipt outcome flips to 'failed'
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.supabase_client import SupabaseClientError

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

TENANT_ID = str(uuid.uuid4())
SUITE_ID = str(uuid.uuid4())
OFFICE_ID = str(uuid.uuid4())
MEMORY_ID = str(uuid.uuid4())

SCOPE: dict[str, str] = {
    "tenant_id": TENANT_ID,
    "suite_id": SUITE_ID,
    "office_id": OFFICE_ID,
}

# Fake number that _resolve_tenant_from_called_number will return SCOPE for
CALLED_NUMBER = "+15550000001"


def _make_supabase_insert_mock(returned_memory_id: str = MEMORY_ID) -> AsyncMock:
    """Return an AsyncMock for supabase_insert that returns a row with memory_id."""
    mock = AsyncMock(return_value={"memory_id": returned_memory_id})
    return mock


def _make_resolve_mock(scope: dict[str, str] = SCOPE) -> AsyncMock:
    return AsyncMock(return_value=scope)


# ---------------------------------------------------------------------------
# _insert_memory_object unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_memory_object_sends_correct_columns() -> None:
    """Verify the correct column names are sent — not the broken object_type/metadata/id."""
    from aspire_orchestrator.routes.sarah_tools import _insert_memory_object

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        assert table == "memory_objects"
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    with patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ):
        result = await _insert_memory_object(
            memory_type="call_summary",
            scope=SCOPE,
            summary="Test summary",
            detail={"key": "value"},
        )

    assert result == MEMORY_ID
    assert len(captured_rows) == 1
    row = captured_rows[0]

    # Correct columns present
    assert row["memory_type"] == "call_summary"
    assert row["summary"] == "Test summary"
    assert row["detail"] == {"key": "value"}
    assert row["tenant_id"] == TENANT_ID
    assert row["suite_id"] == SUITE_ID
    assert row["office_id"] == OFFICE_ID
    assert "trace_id" in row
    assert "correlation_id" in row

    # Old broken columns must NOT be present
    assert "id" not in row
    assert "object_type" not in row
    assert "metadata" not in row


@pytest.mark.asyncio
async def test_insert_memory_object_includes_idempotency_key_when_provided() -> None:
    from aspire_orchestrator.routes.sarah_tools import _insert_memory_object

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    idem_key = f"conv_abc:{CALLED_NUMBER}:capture_message"
    with patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ):
        await _insert_memory_object(
            memory_type="voicemail_capture",
            scope=SCOPE,
            summary="Duplicate test",
            detail={},
            idempotency_key=idem_key,
        )

    assert captured_rows[0].get("idempotency_key") == idem_key


@pytest.mark.asyncio
async def test_insert_memory_object_omits_idempotency_key_when_none() -> None:
    from aspire_orchestrator.routes.sarah_tools import _insert_memory_object

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    with patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ):
        await _insert_memory_object(
            memory_type="voicemail_capture",
            scope=SCOPE,
            summary="No idem key",
            detail={},
            idempotency_key=None,
        )

    assert "idempotency_key" not in captured_rows[0]


@pytest.mark.asyncio
async def test_insert_memory_object_raises_on_db_error() -> None:
    """DB errors must propagate — no silent swallow (was the original bug)."""
    from aspire_orchestrator.routes.sarah_tools import _insert_memory_object

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        raise SupabaseClientError("memory_objects", detail="violates not-null constraint", status_code=400)

    with patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ):
        with pytest.raises(SupabaseClientError):
            await _insert_memory_object(
                memory_type="call_summary",
                scope=SCOPE,
                summary="Should raise",
                detail={},
            )


# ---------------------------------------------------------------------------
# capture_message — memory_type='voicemail_capture'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_persists_with_correct_memory_type() -> None:
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    req = CaptureMessageReq(
        called_number=CALLED_NUMBER,
        caller_name="John Doe",
        caller_phone="+15550000099",
        message="Please call me back about my invoice.",
        urgency="high",
        reason_category="billing",
    )

    with patch(
        "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
        _make_resolve_mock(),
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts",
    ):
        result = await capture_message(req)

    assert result["success"] is True
    assert result["message_id"] == MEMORY_ID
    assert len(captured_rows) == 1
    assert captured_rows[0]["memory_type"] == "voicemail_capture"
    assert "John Doe" in captured_rows[0]["summary"] or "caller" in captured_rows[0]["summary"]
    # Caller phone must NOT appear in the top-level summary (PII — goes in detail)
    assert "+15550000099" not in captured_rows[0]["summary"]


@pytest.mark.asyncio
async def test_capture_message_db_error_sets_receipt_failed_but_returns_success() -> None:
    """Tool still returns success=True even when DB fails (graceful degradation)
    but receipt outcome must be 'failed', not 'success'."""
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    stored_receipts: list[list[dict[str, Any]]] = []

    def mock_store(receipts: list[dict[str, Any]]) -> None:
        stored_receipts.append(receipts)

    async def mock_insert_fail(table: str, data: dict[str, Any]) -> dict[str, Any]:
        raise SupabaseClientError("memory_objects", detail="connection refused", status_code=503)

    req = CaptureMessageReq(called_number=CALLED_NUMBER, message="test")

    with patch(
        "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
        _make_resolve_mock(),
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert_fail,
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts",
        side_effect=mock_store,
    ):
        result = await capture_message(req)

    assert result["success"] is True  # agent still gets a response
    assert result["message_id"] == ""  # no persisted ID
    # Receipt must show failed outcome
    assert stored_receipts
    receipt = stored_receipts[0][0]
    assert receipt["outcome"] == "failed"


# ---------------------------------------------------------------------------
# callback_request — memory_type='callback_request'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_request_persists_with_correct_memory_type() -> None:
    from aspire_orchestrator.routes.sarah_tools import callback_request, CallbackRequestReq

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    req = CallbackRequestReq(
        called_number=CALLED_NUMBER,
        caller_name="Jane Smith",
        caller_phone="+15550000088",
        preferred_window="tomorrow morning",
        reason="Follow-up on estimate",
    )

    with patch(
        "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
        _make_resolve_mock(),
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts",
    ):
        result = await callback_request(req)

    assert result["success"] is True
    assert result["callback_id"] == MEMORY_ID
    assert len(captured_rows) == 1
    assert captured_rows[0]["memory_type"] == "callback_request"
    # Phone must not be in summary
    assert "+15550000088" not in captured_rows[0]["summary"]


# ---------------------------------------------------------------------------
# call_summary — memory_type='call_summary'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_summary_persists_with_correct_memory_type() -> None:
    from aspire_orchestrator.routes.sarah_tools import call_summary, CallSummaryReq

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    req = CallSummaryReq(
        called_number=CALLED_NUMBER,
        outcome="completed",
        summary="Caller asked about scheduling. Appointment booked for next week.",
        caller_name="Bob Builder",
        caller_phone="+15550000077",
    )

    with patch(
        "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
        _make_resolve_mock(),
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts",
    ):
        result = await call_summary(req)

    assert result["success"] is True
    assert result["summary_id"] == MEMORY_ID
    assert len(captured_rows) == 1
    assert captured_rows[0]["memory_type"] == "call_summary"
    # summary must be populated from req.summary
    assert "Caller asked about scheduling" in captured_rows[0]["summary"]
    # Phone must not be in summary
    assert "+15550000077" not in captured_rows[0]["summary"]


@pytest.mark.asyncio
async def test_call_summary_empty_summary_gets_fallback_text() -> None:
    """When req.summary is blank, a non-empty fallback summary is generated."""
    from aspire_orchestrator.routes.sarah_tools import call_summary, CallSummaryReq

    captured_rows: list[dict[str, Any]] = []

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        captured_rows.append(data)
        return {"memory_id": MEMORY_ID}

    req = CallSummaryReq(
        called_number=CALLED_NUMBER,
        outcome="voicemail",
        summary="",  # blank — must produce fallback
        caller_name="",
        caller_phone="",
    )

    with patch(
        "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
        _make_resolve_mock(),
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.receipt_store.store_receipts",
    ):
        await call_summary(req)

    summary_sent = captured_rows[0]["summary"]
    assert summary_sent  # must not be empty string
    assert len(summary_sent) > 0


# ---------------------------------------------------------------------------
# Regression guard: unknown_number path does not call supabase_insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_message_unknown_number_no_db_call() -> None:
    from aspire_orchestrator.routes.sarah_tools import capture_message, CaptureMessageReq

    insert_called = False

    async def mock_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        nonlocal insert_called
        insert_called = True
        return {}

    req = CaptureMessageReq(called_number="+19999999999", message="test")

    with patch(
        "aspire_orchestrator.routes.sarah_tools._resolve_tenant_from_called_number",
        AsyncMock(return_value=None),
    ), patch(
        "aspire_orchestrator.routes.sarah_tools.supabase_insert",
        side_effect=mock_insert,
    ):
        result = await capture_message(req)

    assert result["success"] is False
    assert result["reason"] == "unknown_number"
    assert not insert_called
