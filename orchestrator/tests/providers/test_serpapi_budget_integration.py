"""Tests 12-15: SerpApi adapter integration tests — budget gate in adapter layer.

These tests verify that the three SerpApi adapter functions (homedepot, shopping,
product) correctly wire through the dual-account budget gate and handle exhaustion,
429 detection, and account-A-to-B failover.

All provider HTTP calls are mocked. Budget state uses in-memory (no Supabase).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import aspire_orchestrator.services.adam.serpapi_budget as budget_module
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import ProviderResponse
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.adam.serpapi_budget import DEFAULT_CAP


@pytest.fixture(autouse=True)
def reset_budget():
    budget_module._reset_for_tests()
    yield
    budget_module._reset_for_tests()


def _success_response(body: dict | None = None) -> ProviderResponse:
    return ProviderResponse(
        success=True,
        status_code=200,
        body=body or {"products": [], "shopping_results": [], "product_results": {}},
        error_code=None,
        error_message=None,
    )


def _rate_limited_response() -> ProviderResponse:
    return ProviderResponse(
        success=False,
        status_code=429,
        body={},
        error_code=InternalErrorCode.RATE_LIMITED,
        error_message="HTTP 429 RATE_LIMITED",
    )


def _quota_body_response() -> ProviderResponse:
    return ProviderResponse(
        success=False,
        status_code=200,
        body={},
        error_code=InternalErrorCode.RATE_QUOTA_EXCEEDED,
        error_message="searches/month plan limit exceeded",
    )


@pytest.mark.asyncio
async def test_homedepot_adapter_returns_failed_when_budget_exhausted(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {"A": DEFAULT_CAP, "B": DEFAULT_CAP}
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    result = await execute_serpapi_homedepot_search(
        payload={"query": "drywall screws"},
        correlation_id="test-exhausted",
        suite_id="suite-1",
        office_id="office-1",
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("reason_code") == "SERPAPI_BUDGET_EXHAUSTED"
    assert "key-a" not in (result.error or "")
    assert "key-b" not in (result.error or "")


@pytest.mark.asyncio
async def test_homedepot_adapter_failover_on_429(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")
    call_count = 0

    async def fake_request(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _rate_limited_response()
        return _success_response({"products": [{"title": "test product"}]})

    from aspire_orchestrator.providers import serpapi_homedepot_client as hd_mod
    hd_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(side_effect=fake_request)
    mock_client.make_receipt_data = MagicMock(return_value={
        "id": "test-receipt", "outcome": "success", "reason_code": "EXECUTED",
    })
    with patch.object(hd_mod, "_get_client", return_value=mock_client):
        result = await hd_mod.execute_serpapi_homedepot_search(
            payload={"query": "drywall screws", "store_id": "0206"},
            correlation_id="test-429-failover",
            suite_id="suite-1",
            office_id="office-1",
        )
    assert budget_module._get_count_for_account("A") == DEFAULT_CAP
    assert mock_client._request.await_count == 2
    receipt_str = str(result.receipt_data or {})
    assert "key-a" not in receipt_str
    assert "key-b" not in receipt_str


@pytest.mark.asyncio
async def test_shopping_adapter_returns_failed_when_budget_exhausted(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {"A": DEFAULT_CAP, "B": DEFAULT_CAP}
    from aspire_orchestrator.providers.serpapi_shopping_client import execute_serpapi_shopping_search
    result = await execute_serpapi_shopping_search(
        payload={"query": "drywall 5/8 inch"},
        correlation_id="test-shopping-exhausted",
        suite_id="suite-1",
        office_id="office-1",
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("reason_code") == "SERPAPI_BUDGET_EXHAUSTED"
    assert "key-a" not in (result.error or "")
    assert "key-b" not in (result.error or "")


@pytest.mark.asyncio
async def test_product_adapter_returns_failed_when_budget_exhausted(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {"A": DEFAULT_CAP, "B": DEFAULT_CAP}
    from aspire_orchestrator.providers.serpapi_homedepot_product_client import fetch_product_details
    result = await fetch_product_details(
        product_id="123456789",
        correlation_id="test-product-exhausted",
        suite_id="suite-1",
        office_id="office-1",
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("reason_code") == "SERPAPI_BUDGET_EXHAUSTED"
    assert "key-a" not in (result.error or "")
    assert "key-b" not in (result.error or "")


@pytest.mark.asyncio
async def test_receipt_always_generated_on_budget_exhaustion(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")
    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {"A": DEFAULT_CAP, "B": DEFAULT_CAP}
    from aspire_orchestrator.providers.serpapi_homedepot_client import execute_serpapi_homedepot_search
    result = await execute_serpapi_homedepot_search(
        payload={"query": "lumber 2x4"},
        correlation_id="test-law2",
        suite_id="suite-2",
        office_id="office-2",
    )
    assert result.receipt_data is not None, "Law #2: receipt_data missing on budget exhaustion"
    receipt_str = str(result.receipt_data)
    assert "key-a" not in receipt_str
    assert "key-b" not in receipt_str
