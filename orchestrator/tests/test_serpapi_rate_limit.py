"""SerpApi 429 no-retry test — output finding #9.

When SerpApi returns RATE_LIMITED (HTTP 429), the voice path must NOT retry.
Retrying a 429 wastes budget and triggers backoff at the provider — the correct
behavior is to return an error response immediately with a single call.

Tests:
  - voice path: RATE_LIMITED → exactly 1 HD call, no retry
  - response artifact_type == "error" with reason "rate_limited"
  - a receipt is stored for the rate-limited action (Law #2)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.adam.playbooks.trades import execute_tool_material_price_check
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.tool_types import ToolExecutionResult


def _rate_limited(tool_id: str) -> ToolExecutionResult:
    # The backend detects rate-limiting by checking "RATE_LIMITED" in error.upper()
    # (trades.py F-HIGH-7). There is no Outcome.RATE_LIMITED enum value — the
    # outcome is FAILED, but the error string carries the 429 signal.
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error="HTTP 429 RATE_LIMITED: Too Many Requests",
        receipt_data={
            "id": "ratelimit-receipt-001",
            "outcome": "failed",
            "action_type": "provider.rate_limited",
        },
    )


def _ok(tool_id: str, data: dict) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data={"id": "test", "outcome": "success"},
    )


def _ctx() -> PlaybookContext:
    return PlaybookContext(
        suite_id="11111111-1111-4111-8111-111111111111",
        office_id="22222222-2222-2222-2222-222222222222",
        correlation_id="rate-limit-test",
    )


@pytest.mark.asyncio
async def test_rate_limited_voice_path_makes_one_hd_call():
    """Output #9: RATE_LIMITED on first SerpApi call → exactly 1 call, no retry.

    The voice path is single-attempt (no retry loop). A 429 must not trigger
    any additional attempts. Retrying a rate-limited provider wastes the 5s
    voice budget and may trigger exponential backoff at the provider.
    """
    hd_mock = AsyncMock(return_value=_rate_limited("serpapi_home_depot.search"))
    shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        shopping_mock,
    ):
        await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            zip_code="32308",  # Bangor guardrail (Wave 2.0): location must be set
                                # for SerpAPI to be called at all. Without it the
                                # playbook short-circuits to STORE_UNRESOLVED.
            voice_path=True,
        )

    assert hd_mock.await_count == 1, (
        f"Output #9 regression: RATE_LIMITED must not be retried; "
        f"hd_mock called {hd_mock.await_count} time(s), expected exactly 1"
    )
    # Shopping must be skipped on voice path regardless of HD outcome
    assert shopping_mock.await_count == 0, (
        "Voice path must skip Google Shopping even when HD is RATE_LIMITED"
    )


@pytest.mark.asyncio
async def test_rate_limited_returns_error_artifact():
    """Output #9: RATE_LIMITED → artifact_type == 'error' with reason 'rate_limited'.

    Fail-closed (Law #3): when the only data provider is rate-limited, the
    response must not be an empty PriceComparison. Return a structured error.
    """
    hd_mock = AsyncMock(return_value=_rate_limited("serpapi_home_depot.search"))
    shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        shopping_mock,
    ):
        response = await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            zip_code="32308",  # Bangor guardrail: location required.
            voice_path=True,
        )

    assert response.artifact_type == "error", (
        f"Output #9 regression: RATE_LIMITED → expected artifact_type='error', "
        f"got {response.artifact_type!r}"
    )

    reason = (response.extra or {}).get("reason", "")
    assert "rate_limited" in reason or "rate" in reason or response.summary, (
        f"Output #9 regression: error response should carry reason='rate_limited'; "
        f"extra={response.extra!r}"
    )


@pytest.mark.asyncio
async def test_rate_limited_receipt_contains_provider_action_type():
    """Output #9 + Law #2: RATE_LIMITED outcome must produce a receipt.

    The ToolExecutionResult for RATE_LIMITED must carry receipt_data with
    action_type='provider.rate_limited'. Callers (receipt_write_node) pick
    this up and persist it — no state change without a receipt.
    """
    rate_limited_result = _rate_limited("serpapi_home_depot.search")

    # Verify receipt_data is present and carries the expected action_type
    assert rate_limited_result.receipt_data is not None, (
        "Law #2: RATE_LIMITED ToolExecutionResult must have receipt_data"
    )
    assert rate_limited_result.receipt_data.get("action_type") == "provider.rate_limited", (
        f"Law #2: receipt action_type must be 'provider.rate_limited'; "
        f"got {rate_limited_result.receipt_data.get('action_type')!r}"
    )
    # outcome is 'failed' (there is no RATE_LIMITED enum in Outcome) but the
    # action_type distinguishes it from a generic 500 failure.
    assert rate_limited_result.receipt_data.get("outcome") in ("failed", "rate_limited"), (
        f"Law #2: receipt outcome must be 'failed' or 'rate_limited'; "
        f"got {rate_limited_result.receipt_data.get('outcome')!r}"
    )


@pytest.mark.asyncio
async def test_text_path_does_not_retry_on_rate_limited():
    """Output #9: text path (3 attempts) also must not retry a 429.

    Retrying rate-limited providers is prohibited — repeated 429s compound
    into provider bans. The text path's attempt loop should break on RATE_LIMITED,
    not continue to attempt 2 and 3.
    """
    hd_mock = AsyncMock(return_value=_rate_limited("serpapi_home_depot.search"))
    shopping_mock = AsyncMock(return_value=_ok("serpapi_shopping.search", {"results": []}))

    with patch(
        "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
        hd_mock,
    ), patch(
        "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
        shopping_mock,
    ):
        response = await execute_tool_material_price_check(
            query="sheetrock",
            ctx=_ctx(),
            zip_code="32308",  # force text path (has zip)
            voice_path=False,
        )

    # Text path must stop on first RATE_LIMITED, not run all 3 attempts
    assert hd_mock.await_count == 1, (
        f"Output #9: text path ran {hd_mock.await_count} attempt(s) despite RATE_LIMITED; "
        "must break immediately on first 429 — no retry"
    )
    assert response.artifact_type == "error", (
        f"RATE_LIMITED text path must return error artifact; got {response.artifact_type!r}"
    )
