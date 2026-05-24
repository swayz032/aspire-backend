"""Tests for serpapi_local_service_ads_client — Google Local Services Ads (commit 5.4d)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aspire_orchestrator.models import Outcome

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-lsa-001"

_SAMPLE_LSA_RESULT = {
    "position": 1,
    "name": "Premier Electrical Services",
    "badge": "google_guaranteed",
    "phone": "(512) 555-0200",
    "rating": 4.9,
    "reviews": 214,
    "hours": "Open 24 hours",
    "years_in_business": 12,
}

_SAMPLE_RESPONSE_BODY = {
    "search_metadata": {"status": "Success"},
    "local_ads": [_SAMPLE_LSA_RESULT],
}


def _make_provider_response(success: bool, body: dict[str, Any], status: int = 200):
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
    import aspire_orchestrator.providers.serpapi_local_service_ads_client as m
    m._client = None
    yield
    m._client = None


@pytest.fixture(autouse=True)
def mock_budget(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_service_ads_client as m
    monkeypatch.setattr(m, "select_account", lambda: "A")
    monkeypatch.setattr(m, "try_increment", lambda acct: True)
    monkeypatch.setattr(m, "get_api_key", lambda acct: "fake-serpapi-key")
    monkeypatch.setattr(m, "current_counts", lambda: {"A": 5, "B": 0})
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
async def test_missing_service_returns_failed():
    from aspire_orchestrator.providers.serpapi_local_service_ads_client import execute_serpapi_local_service_ads_search
    result = await execute_serpapi_local_service_ads_search(
        payload={},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert "service" in (result.error or "").lower()
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_service_too_long_returns_failed():
    from aspire_orchestrator.providers.serpapi_local_service_ads_client import execute_serpapi_local_service_ads_search
    result = await execute_serpapi_local_service_ads_search(
        payload={"service": "x" * 201},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None


# --- Contract: success path ---

@pytest.mark.asyncio
async def test_success_returns_contractors_with_vetted_flag(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_service_ads_client as m
    mock_resp = _make_provider_response(True, _SAMPLE_RESPONSE_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.serpapi_local_service_ads_client import execute_serpapi_local_service_ads_search
    result = await execute_serpapi_local_service_ads_search(
        payload={"service": "electrician", "location": "Austin TX"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.SUCCESS
    contractors = result.data.get("contractors", [])
    assert len(contractors) == 1
    c = contractors[0]
    assert c["name"] == "Premier Electrical Services"
    assert c["google_vetted"] is True
    assert c["rating"] == 4.9
    assert c["review_count"] == 214
    assert c["years_in_business"] == 12
    assert result.receipt_data is not None
    assert result.receipt_data.get("redacted_outputs", {}).get("engine") == "google_local_services"


@pytest.mark.asyncio
async def test_success_receipt_no_api_key_leak(monkeypatch):
    import aspire_orchestrator.providers.serpapi_local_service_ads_client as m
    mock_resp = _make_provider_response(True, _SAMPLE_RESPONSE_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.serpapi_local_service_ads_client import execute_serpapi_local_service_ads_search
    result = await execute_serpapi_local_service_ads_search(
        payload={"service": "plumber"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert "fake-serpapi-key" not in str(result.receipt_data)


# --- Normaliser ---

def test_normalize_no_badge_returns_not_vetted():
    from aspire_orchestrator.providers.serpapi_local_service_ads_client import _normalize_lsa_result
    result = _normalize_lsa_result({"name": "Joe's Plumbing", "rating": 4.0, "reviews": 10}, 0)
    assert result["google_vetted"] is False
    assert result["name"] == "Joe's Plumbing"


def test_normalize_google_guaranteed_badge():
    from aspire_orchestrator.providers.serpapi_local_service_ads_client import _normalize_lsa_result
    result = _normalize_lsa_result({"name": "ProHVAC", "badge": "google_guaranteed"}, 0)
    assert result["google_vetted"] is True


def test_normalize_years_in_business_string():
    from aspire_orchestrator.providers.serpapi_local_service_ads_client import _normalize_lsa_result
    result = _normalize_lsa_result({"years_in_business": "8 years in business"}, 0)
    assert result["years_in_business"] == 8


def test_normalize_hours_dict():
    from aspire_orchestrator.providers.serpapi_local_service_ads_client import _normalize_lsa_result
    result = _normalize_lsa_result({"hours": {"summary": "Mon–Fri 8am–5pm"}}, 0)
    assert result["hours"] == "Mon–Fri 8am–5pm"


# --- Error mapping: timeout ---

@pytest.mark.asyncio
async def test_timeout_returns_failed(monkeypatch):
    import asyncio
    import aspire_orchestrator.providers.serpapi_local_service_ads_client as m

    async def _slow(*a, **kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(m._get_client(), "_request", _slow)

    from aspire_orchestrator.providers.serpapi_local_service_ads_client import execute_serpapi_local_service_ads_search
    result = await execute_serpapi_local_service_ads_search(
        payload={"service": "HVAC"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        timeout=0.001,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None


# --- Integration ---

@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_live_lsa_search():
    import os
    api_key = os.environ.get("ASPIRE_SERPAPI_KEY") or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        pytest.skip("ASPIRE_SERPAPI_KEY not set")

    import aspire_orchestrator.providers.serpapi_local_service_ads_client as m
    m._client = None

    from aspire_orchestrator.providers.serpapi_local_service_ads_client import execute_serpapi_local_service_ads_search
    result = await execute_serpapi_local_service_ads_search(
        payload={"service": "electrician", "location": "Austin TX 78701"},
        correlation_id="live-test-lsa",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome in (Outcome.SUCCESS, Outcome.FAILED)
    assert result.receipt_data is not None
