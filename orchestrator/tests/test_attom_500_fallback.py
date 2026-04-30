"""ATTOM 500 fallback test — output finding #10.

When execute_attom_detail_mortgage_owner returns outcome=FAILED (HTTP 500
or timeout), execute_property_facts_and_permits must:
  1. Return artifact_type == 'error' (not an empty PropertyFactPack)
  2. Set extra.reason == 'attom_unavailable'
  3. Set extra.suggested_retry_after_seconds == 30
  4. Store a failure receipt (Law #2: every outcome including failures)

Prior behavior (bug): ATTOM 500 was swallowed → empty PropertyFactPack
returned → Ava said "I found nothing" with no retry hint (F-HIGH-8 + F-MED-1).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks.trades import execute_property_facts_and_permits
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


def _attom_failed() -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id="attom.property_detail_mortgage_owner",
        error="HTTP 500: Internal Server Error",
        receipt_data={
            "id": "attom-fail-receipt-001",
            "outcome": "failed",
            "action_type": "provider.failed",
            "reason_code": "provider_500",
        },
    )


def _ctx() -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-4111-8111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id="attom-500-test",
    )


@pytest.mark.asyncio
async def test_attom_500_returns_error_artifact():
    """Output #10: ATTOM 500 → artifact_type == 'error', not empty PropertyFactPack.

    F-HIGH-8 fix: swallowed errors now surface as structured error responses.
    """
    with patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner",
        AsyncMock(return_value=_attom_failed()),
    ):
        response = await execute_property_facts_and_permits(
            query="Pull property facts for 123 Main St, Lexington, KY 40509",
            ctx=_ctx(),
        )

    assert response.artifact_type == "error", (
        f"Output #10 regression: ATTOM 500 → expected artifact_type='error', "
        f"got {response.artifact_type!r}"
    )


@pytest.mark.asyncio
async def test_attom_500_reason_is_attom_unavailable():
    """Output #10: ATTOM 500 → extra.reason == 'attom_unavailable'.

    F-MED-1 fix: the reason field tells Ava to surface a meaningful message
    rather than silently returning no results.
    """
    with patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner",
        AsyncMock(return_value=_attom_failed()),
    ):
        response = await execute_property_facts_and_permits(
            query="Pull property facts for 123 Main St, Lexington, KY 40509",
            ctx=_ctx(),
        )

    assert response.extra is not None, "error response must include 'extra' metadata"
    reason = response.extra.get("reason", "")
    assert reason == "attom_unavailable", (
        f"Output #10: extra.reason must be 'attom_unavailable'; got {reason!r}"
    )


@pytest.mark.asyncio
async def test_attom_500_suggested_retry_is_30s():
    """Output #10: ATTOM 500 → extra.suggested_retry_after_seconds == 30.

    Retry hint tells the client (and Ava's summary) to wait 30s before
    re-querying the property service.
    """
    with patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner",
        AsyncMock(return_value=_attom_failed()),
    ):
        response = await execute_property_facts_and_permits(
            query="Pull property facts for 123 Main St, Lexington, KY 40509",
            ctx=_ctx(),
        )

    retry_s = (response.extra or {}).get("suggested_retry_after_seconds")
    assert retry_s == 30, (
        f"Output #10: suggested_retry_after_seconds must be 30; got {retry_s!r}"
    )


@pytest.mark.asyncio
async def test_attom_500_failure_receipt_stored():
    """Output #10 + Law #2: ATTOM failure receipt must carry provider outcome.

    The ToolExecutionResult from execute_attom_detail_mortgage_owner must
    carry receipt_data — the receipt_write_node persists this as the failure
    record (Law #2: no outcome without a receipt, including failures).
    """
    failed_result = _attom_failed()

    assert failed_result.receipt_data is not None, (
        "Law #2: ATTOM FAILED ToolExecutionResult must carry receipt_data"
    )
    assert failed_result.receipt_data.get("outcome") == "failed", (
        f"Law #2: receipt outcome must be 'failed'; got {failed_result.receipt_data.get('outcome')!r}"
    )
    assert failed_result.receipt_data.get("action_type") == "provider.failed", (
        f"Law #2: receipt action_type must be 'provider.failed'"
    )


@pytest.mark.asyncio
async def test_attom_500_providers_called_includes_attom():
    """Output #10: providers_called must include 'attom' even when it fails.

    Audit trail requirement: even failed provider calls must appear in
    providers_called so the receipt chain is complete.
    """
    with patch(
        "aspire_orchestrator.providers.attom_client.execute_attom_detail_mortgage_owner",
        AsyncMock(return_value=_attom_failed()),
    ):
        response = await execute_property_facts_and_permits(
            query="Pull property facts for 123 Main St, Lexington, KY 40509",
            ctx=_ctx(),
        )

    assert "attom" in (response.providers_called or []), (
        f"Output #10: providers_called must include 'attom' even on failure; "
        f"got {response.providers_called!r}"
    )
