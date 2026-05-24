"""Tests for serpapi_local_client — Google Maps local pack (commit 5.4d)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.error_codes import InternalErrorCode

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-local-001"

_SAMPLE_LOCAL_RESULT = {
    "position": 1,
    "title": "ABC Electric LLC",
    "place_id": "ChIJplace123",
    "address": "123 Main St, Austin, TX 78701",
    "phone": "(512) 555-0100",
    "rating": 4.7,
    "reviews": 82,
    "website": "https://abcelectric.example.com",
    "serpapi_link": "https://serpapi.com/search?...gbp=ChIJplace123",
    "type": "Electrician",
}

_SAMPLE_RESPONSE_BODY = {
    "search_metadata": {"status": "Success"},
    "local_results": [_SAMPLE_LOCAL_RESULT],
}


def _make_provider_response(success: bool, body: dict[str, Any], status: int = 200):
    """Build a ProviderResponse-like mock."""
    from aspire_orchestrator.providers.base_client import ProviderResponse
    resp = MagicMock(spec=ProviderResponse)
    resp.success = success
    resp.body = body
    resp.status_code = status
    resp.error_message = None if success else body.get("error", "Error")
    resp.error_code = None
    resp.request_id = None
    resp.latency_ms = 50
    return resp


@pytest.fixture(autouse=True)
def reset_client():
    import aspire_orchestrator.providers.serpapi_local_client as m
    m._client = None
    yield
    m._client = None


@pytest.fixture(autouse=True)
def mock_budget(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_client as m
    monkeypatch.setattr(m, "select_account", lambda: "A")
    monkeypatch.setattr(m, "try_increment", lambda acct: True)
    monkeypatch.setattr(m, "get_api_key", lambda acct: "fake-serpapi-key")
    monkeypatch.setattr(m, "current_counts", lambda: {"A": 10, "B": 0})
    monkeypatch.setattr(m, "mark_account_exhausted", lambda acct, reason="": None)


@pytest.fixture(autouse=True)
def mock_receipt_logger(monkeypatch):
    from aspire_orchestrator.services import provider_call_logger as pcl
    s = MagicMock()
    s.log_call = MagicMock(return_value=None)
    monkeypatch.setattr(pcl, "_logger_instance", s)
    monkeypatch.setattr(pcl, "get_provider_call_logger", lambda: s)


# --- Contract: input validation ---

@pytest.mark.asyncio
async def test_missing_query_returns_failed():
    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert "query" in (result.error or "").lower()
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_query_too_long_returns_failed():
    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "x" * 501},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert "500" in (result.error or "")
    assert result.receipt_data is not None


# --- Contract: success path ---

@pytest.mark.asyncio
async def test_success_returns_businesses(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_client as m
    mock_resp = _make_provider_response(True, _SAMPLE_RESPONSE_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "electrician", "location": "Austin TX"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.SUCCESS
    assert isinstance(result.data.get("businesses"), list)
    assert len(result.data["businesses"]) == 1
    biz = result.data["businesses"][0]
    assert biz["name"] == "ABC Electric LLC"
    assert biz["phone"] == "(512) 555-0100"
    assert biz["rating"] == 4.7
    assert biz["review_count"] == 82
    assert result.receipt_data is not None
    assert result.receipt_data.get("redacted_outputs", {}).get("engine") == "google_local"


@pytest.mark.asyncio
async def test_success_receipt_does_not_contain_api_key(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_client as m
    mock_resp = _make_provider_response(True, _SAMPLE_RESPONSE_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "plumber"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    receipt_str = str(result.receipt_data)
    assert "fake-serpapi-key" not in receipt_str


# --- Error mapping: timeout ---

@pytest.mark.asyncio
async def test_timeout_returns_failed_with_receipt(monkeypatch):
    import asyncio
    import aspire_orchestrator.providers.serpapi_local_client as m

    async def _slow(*a, **kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(m._get_client(), "_request", _slow)

    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "hvac contractor"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        timeout=0.001,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None
    assert result.receipt_data.get("redacted_outputs", {}).get("engine") == "google_local"


# --- Error mapping: budget exhausted ---

@pytest.mark.asyncio
async def test_budget_exhausted_returns_failed(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_client as m
    monkeypatch.setattr(m, "select_account", lambda: None)

    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "painter"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None


# --- Error mapping: provider 429 ---

@pytest.mark.asyncio
async def test_rate_limited_marks_account_exhausted(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_client as m

    exhausted_calls: list[str] = []
    monkeypatch.setattr(m, "mark_account_exhausted", lambda acct, reason="": exhausted_calls.append(acct))

    mock_resp_429 = _make_provider_response(False, {"error": "quota exceeded"}, status=429)
    mock_resp_429.status_code = 429

    other_resp = _make_provider_response(True, _SAMPLE_RESPONSE_BODY)

    call_count = 0

    async def _side_effect(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_resp_429
        return other_resp

    monkeypatch.setattr(m._get_client(), "_request", _side_effect)
    monkeypatch.setattr(m, "try_increment", lambda acct: True)
    monkeypatch.setattr(m, "get_api_key", lambda acct: "fake-key-B")

    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "drywall contractor"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert "A" in exhausted_calls


# --- Normaliser: result shape ---

def test_normalize_handles_missing_fields():
    from aspire_orchestrator.providers.serpapi_local_client import _normalize_local_result
    result = _normalize_local_result({}, 0)
    assert result["name"] == ""
    assert result["rating"] is None
    assert result["review_count"] == 0
    assert result["id"] == "local_0"


def test_normalize_rating_string():
    from aspire_orchestrator.providers.serpapi_local_client import _normalize_local_result
    result = _normalize_local_result({"rating": "4.5", "title": "Test Co"}, 1)
    assert result["rating"] == 4.5


# --- Integration: live call (gated on ASPIRE_SERPAPI_KEY env var) ---

@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_live_local_search():
    import os
    api_key = os.environ.get("ASPIRE_SERPAPI_KEY") or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        pytest.skip("ASPIRE_SERPAPI_KEY not set")

    # Reset patches so real budget gate runs
    import aspire_orchestrator.providers.serpapi_local_client as m
    m._client = None

    from aspire_orchestrator.providers.serpapi_local_client import execute_serpapi_local_search
    result = await execute_serpapi_local_search(
        payload={"query": "licensed electrician", "location": "Austin TX 78701"},
        correlation_id="live-test-local",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome in (Outcome.SUCCESS, Outcome.FAILED)
    assert result.receipt_data is not None
