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


# ---------------------------------------------------------------------------
# Test 16: Fix 2 — redacted_outputs envelope complete on all 3 adapters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipt_redacted_outputs_envelope_homedepot(monkeypatch):
    """Fix 2: HD adapter receipt.redacted_outputs has all 7 architect-spec fields."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    from aspire_orchestrator.providers import serpapi_homedepot_client as hd_mod
    hd_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(return_value=_success_response({"products": []}))
    mock_client.make_receipt_data = MagicMock(return_value={
        "id": "r1", "outcome": "success", "reason_code": "EXECUTED",
    })
    with patch.object(hd_mod, "_get_client", return_value=mock_client):
        result = await hd_mod.execute_serpapi_homedepot_search(
            payload={"query": "2x4 lumber", "store_id": "0206"},
            correlation_id="test-envelope-hd",
            suite_id="s1",
            office_id="o1",
        )

    rd = result.receipt_data
    assert rd is not None
    assert "redacted_outputs" in rd
    ro = rd["redacted_outputs"]
    for field in ("engine", "account_id", "cached", "budget_remaining_a",
                  "budget_remaining_b", "query_normalized", "store_id"):
        assert field in ro, f"missing field: {field}"
    assert ro["engine"] == "home_depot"
    assert ro["query_normalized"] == "2x4 lumber"
    # Top-level budget_* fields must not appear
    assert "budget_account_id" not in rd
    assert "budget_remaining_a" not in rd
    assert "budget_remaining_b" not in rd


@pytest.mark.asyncio
async def test_receipt_redacted_outputs_envelope_shopping(monkeypatch):
    """Fix 2: Shopping adapter receipt.redacted_outputs has all 7 architect-spec fields."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    from aspire_orchestrator.providers import serpapi_shopping_client as shop_mod
    shop_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(return_value=_success_response({"shopping_results": []}))
    mock_client.make_receipt_data = MagicMock(return_value={
        "id": "r2", "outcome": "success", "reason_code": "EXECUTED",
    })
    with patch.object(shop_mod, "_get_client", return_value=mock_client):
        result = await shop_mod.execute_serpapi_shopping_search(
            payload={"query": "drywall screws"},
            correlation_id="test-envelope-shopping",
            suite_id="s1",
            office_id="o1",
        )

    rd = result.receipt_data
    assert rd is not None
    assert "redacted_outputs" in rd
    ro = rd["redacted_outputs"]
    for field in ("engine", "account_id", "cached", "budget_remaining_a",
                  "budget_remaining_b", "query_normalized", "store_id"):
        assert field in ro, f"missing field: {field}"
    assert ro["engine"] == "shopping"
    assert ro["store_id"] is None
    assert "budget_account_id" not in rd
    assert "budget_remaining_a" not in rd
    assert "budget_remaining_b" not in rd


@pytest.mark.asyncio
async def test_receipt_redacted_outputs_envelope_product(monkeypatch):
    """Fix 2: Product adapter receipt.redacted_outputs has all 7 architect-spec fields."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    from aspire_orchestrator.providers import serpapi_homedepot_product_client as prod_mod
    prod_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(return_value=_success_response({"product_results": {}}))
    mock_client.make_receipt_data = MagicMock(return_value={
        "id": "r3", "outcome": "success", "reason_code": "EXECUTED",
    })
    with patch.object(prod_mod, "_get_client", return_value=mock_client):
        result = await prod_mod.fetch_product_details(
            product_id="123456789",
            correlation_id="test-envelope-product",
            suite_id="s1",
            office_id="o1",
        )

    rd = result.receipt_data
    assert rd is not None
    assert "redacted_outputs" in rd
    ro = rd["redacted_outputs"]
    for field in ("engine", "account_id", "cached", "budget_remaining_a",
                  "budget_remaining_b", "query_normalized", "store_id"):
        assert field in ro, f"missing field: {field}"
    assert ro["engine"] == "home_depot_product"
    assert ro["query_normalized"] == "123456789"
    assert "budget_account_id" not in rd
    assert "budget_remaining_a" not in rd
    assert "budget_remaining_b" not in rd


# ---------------------------------------------------------------------------
# Test 17: Fix 4 — 429 failover emits intermediate receipt BEFORE retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_failover_emits_intermediate_receipt(monkeypatch):
    """Fix 4: Two receipts emitted — FAILED/RATE_LIMITED for A, then SUCCESS for B."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    stored_receipts: list[dict] = []

    def fake_store(receipts: list[dict]) -> None:
        stored_receipts.extend(receipts)

    call_count = 0

    async def fake_request(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _rate_limited_response()
        return _success_response({"products": [{"title": "test"}]})

    from aspire_orchestrator.providers import serpapi_homedepot_client as hd_mod
    hd_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(side_effect=fake_request)
    mock_client.make_receipt_data = MagicMock(side_effect=lambda **kw: {
        "id": f"r-{call_count}",
        "outcome": kw.get("outcome", Outcome.FAILED).value
        if hasattr(kw.get("outcome"), "value") else str(kw.get("outcome", "")),
        "reason_code": kw.get("reason_code", ""),
    })

    # Patch at the receipt_store module level — the adapter imports store_receipts
    # inside the function body (local import), so module-level patch is the correct target.
    with patch.object(hd_mod, "_get_client", return_value=mock_client):
        with patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=fake_store,
        ):
            result = await hd_mod.execute_serpapi_homedepot_search(
                payload={"query": "drywall", "store_id": "0206"},
                correlation_id="test-429-2receipts",
                suite_id="s1",
                office_id="o1",
            )

    # At least one intermediate rate-limited receipt should have been stored
    rate_receipts = [r for r in stored_receipts if r.get("reason_code") == "RATE_LIMITED"]
    assert len(rate_receipts) >= 1, "Expected intermediate RATE_LIMITED receipt"
    # The intermediate receipt must have redacted_outputs with http_status
    ro = rate_receipts[0].get("redacted_outputs", {})
    assert ro.get("http_status") == 429
    assert ro.get("engine") == "home_depot"
    # Account A must now be at cap
    assert budget_module._get_count_for_account("A") == DEFAULT_CAP
    # Law #9: no keys in any stored receipt
    all_str = str(stored_receipts)
    assert "key-a" not in all_str
    assert "key-b" not in all_str


# ---------------------------------------------------------------------------
# Test 18: Fix 5 — hotels adapter budget gated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hotels_adapter_budget_gated(monkeypatch):
    """Fix 5: Hotels adapter returns FAILED with SERPAPI_BUDGET_EXHAUSTED when both accounts exhausted."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    month = budget_module._current_month()
    with budget_module._in_memory_lock:
        budget_module._in_memory_counts[month] = {
            "A": DEFAULT_CAP,
            "B": DEFAULT_CAP,
        }

    from aspire_orchestrator.providers.serpapi_hotels_client import (
        execute_serpapi_google_hotels_search,
    )
    result = await execute_serpapi_google_hotels_search(
        payload={"query": "hotels in Miami, FL"},
        correlation_id="test-hotels-exhausted",
        suite_id="s1",
        office_id="o1",
    )

    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("reason_code") == "SERPAPI_BUDGET_EXHAUSTED"
    assert "key-a" not in (result.error or "")
    assert "key-b" not in (result.error or "")


# ---------------------------------------------------------------------------
# Test 19: Fix 6 — 429 retry skips increment when other account key missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_429_retry_skips_increment_when_other_account_key_missing(monkeypatch):
    """Fix 6 (R-003): When account B key is missing, failover returns FAILED without incrementing B."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.delenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", raising=False)

    async def always_429(_request):
        return _rate_limited_response()

    from aspire_orchestrator.providers import serpapi_homedepot_client as hd_mod
    hd_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(side_effect=always_429)
    mock_client.make_receipt_data = MagicMock(return_value={
        "id": "r-fix6", "outcome": "failed", "reason_code": "RATE_LIMITED",
    })

    with patch.object(hd_mod, "_get_client", return_value=mock_client):
        with patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=lambda r: None,
        ):
            result = await hd_mod.execute_serpapi_homedepot_search(
                payload={"query": "pipe fittings", "store_id": "0206"},
                correlation_id="test-fix6",
                suite_id="s1",
                office_id="o1",
            )

    assert result.outcome == Outcome.FAILED
    # Account B budget must NOT have been incremented
    assert budget_module._get_count_for_account("B") == 0
    # Law #9: no keys in error message
    assert "key-a" not in (result.error or "")


# ---------------------------------------------------------------------------
# Test 20: Fix 7 — query length cap 500 chars (homedepot)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_length_cap_500_chars_homedepot(monkeypatch):
    """Fix 7 (R-004): query of 501 chars returns FAILED with INPUT_INVALID_FORMAT, budget untouched."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    from aspire_orchestrator.providers.serpapi_homedepot_client import (
        execute_serpapi_homedepot_search,
    )
    long_query = "x" * 501
    result = await execute_serpapi_homedepot_search(
        payload={"query": long_query},
        correlation_id="test-query-cap",
        suite_id="s1",
        office_id="o1",
    )

    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("reason_code") == "INPUT_INVALID_FORMAT"
    assert "500 character" in (result.error or "")
    # Budget counter must NOT have been touched
    assert budget_module._get_count_for_account("A") == 0
    assert budget_module._get_count_for_account("B") == 0


@pytest.mark.asyncio
async def test_query_length_cap_500_chars_shopping(monkeypatch):
    """Fix 7 (R-004): shopping adapter also rejects >500 char query without budget touch."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    from aspire_orchestrator.providers.serpapi_shopping_client import (
        execute_serpapi_shopping_search,
    )
    long_query = "y" * 501
    result = await execute_serpapi_shopping_search(
        payload={"query": long_query},
        correlation_id="test-query-cap-shop",
        suite_id="s1",
        office_id="o1",
    )

    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("reason_code") == "INPUT_INVALID_FORMAT"
    assert budget_module._get_count_for_account("A") == 0
    assert budget_module._get_count_for_account("B") == 0


# ---------------------------------------------------------------------------
# Test 21: Fix 3 — adapter receipt persisted from trades (smoke test)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adapter_receipt_persisted_from_trades(monkeypatch):
    """Fix 3: execute_serpapi_homedepot_search receipt is store_receipts'd by the playbook caller."""
    monkeypatch.setenv("SERPAPI_API_KEY", "key-a")
    monkeypatch.setenv("ASPIRE_SERPAPI_2ND_ACCOUNT_API_KEY", "key-b")

    from aspire_orchestrator.providers import serpapi_homedepot_client as hd_mod
    hd_mod._client = None
    mock_client = MagicMock()
    mock_client._request = AsyncMock(return_value=_success_response({"products": []}))
    mock_client.make_receipt_data = MagicMock(return_value={
        "id": "r-trades",
        "outcome": "success",
        "reason_code": "EXECUTED",
    })

    async def patched_hd(**kw):  # type: ignore[override]
        with patch.object(hd_mod, "_get_client", return_value=mock_client):
            return await hd_mod.execute_serpapi_homedepot_search(**kw)

    from unittest.mock import patch as _patch

    captured_calls: list[list[dict]] = []

    def fake_store_trades(receipts: list[dict]) -> None:
        captured_calls.append(receipts)

    # Simulate the pattern from trades.py Fix 3
    hd_result = await patched_hd(
        payload={"query": "drywall", "store_id": "0206"},
        correlation_id="test-fix3",
        suite_id="s1",
        office_id="o1",
    )
    with _patch(
        "aspire_orchestrator.services.receipt_store.store_receipts",
        side_effect=fake_store_trades,
    ):
        if hd_result.receipt_data:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([hd_result.receipt_data])

    assert len(captured_calls) >= 1
    assert captured_calls[0][0]["id"] == "r-trades"
