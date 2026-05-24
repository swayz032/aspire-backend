"""Tests for bls_oews_client — BLS OEWS labor rate API (commit 5.4e)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aspire_orchestrator.models import Outcome

SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
CORR_ID = "test-corr-bls-001"

# Recorded BLS API v2 response fixture for SOC 47-2111 (electrician) national
_BLS_SUCCESS_BODY = {
    "status": "REQUEST_SUCCEEDED",
    "responseTime": 123,
    "message": [],
    "Results": {
        "series": [
            {
                "seriesID": "OEUN000000000000047211103",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "30.54", "footnotes": [{}]}],
            },
            {
                "seriesID": "OEUN000000000000047211108",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "29.02", "footnotes": [{}]}],
            },
            {
                "seriesID": "OEUN000000000000047211111",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "17.88", "footnotes": [{}]}],
            },
            {
                "seriesID": "OEUN000000000000047211112",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "22.89", "footnotes": [{}]}],
            },
            {
                "seriesID": "OEUN000000000000047211113",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "37.14", "footnotes": [{}]}],
            },
            {
                "seriesID": "OEUN000000000000047211114",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "46.52", "footnotes": [{}]}],
            },
            {
                "seriesID": "OEUN000000000000047211101",
                "data": [{"year": "2024", "period": "A01", "periodName": "Annual", "value": "693390", "footnotes": [{}]}],
            },
        ]
    },
}

_BLS_EMPTY_BODY = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": []},
}

_BLS_FAILED_BODY = {
    "status": "REQUEST_FAILED",
    "message": ["Series does not exist"],
    "Results": {},
}


def _make_provider_response(success: bool, body: dict[str, Any], status: int = 200):
    from aspire_orchestrator.providers.base_client import ProviderResponse
    resp = MagicMock(spec=ProviderResponse)
    resp.success = success
    resp.body = body
    resp.status_code = status
    resp.error_message = None if success else "Error"
    resp.error_code = None
    resp.request_id = None
    resp.latency_ms = 80
    return resp


@pytest.fixture(autouse=True)
def reset_client():
    import aspire_orchestrator.providers.bls_oews_client as m
    m._client = None
    yield
    m._client = None


@pytest.fixture(autouse=True)
def mock_receipt_logger(monkeypatch):
    from aspire_orchestrator.services import provider_call_logger as pcl
    s = MagicMock()
    s.log_call = MagicMock(return_value=None)
    monkeypatch.setattr(pcl, "_logger_instance", s)
    monkeypatch.setattr(pcl, "get_provider_call_logger", lambda: s)


# --- Contract: input validation ---

@pytest.mark.asyncio
async def test_missing_soc_and_trade_returns_failed():
    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_unknown_trade_returns_failed():
    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"trade": "astronaut"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert "astronaut" in (result.error or "").lower()
    assert result.receipt_data is not None


# --- Contract: success path ---

@pytest.mark.asyncio
async def test_success_returns_wage_band(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    mock_resp = _make_provider_response(True, _BLS_SUCCESS_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"soc_code": "47-2111"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.SUCCESS
    data = result.data
    assert data["soc_code"] == "47-2111"
    assert data["mean_hourly"] == pytest.approx(30.54)
    assert data["median_hourly"] == pytest.approx(29.02)
    assert data["pct_10_hourly"] == pytest.approx(17.88)
    assert data["pct_25_hourly"] == pytest.approx(22.89)
    assert data["pct_75_hourly"] == pytest.approx(37.14)
    assert data["pct_90_hourly"] == pytest.approx(46.52)
    assert data["employment"] == 693390
    assert data["year"] == "2024"
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_trade_name_resolves_to_soc(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    mock_resp = _make_provider_response(True, _BLS_SUCCESS_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"trade": "electrician"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.SUCCESS
    assert result.data["soc_code"] == "47-2111"


@pytest.mark.asyncio
async def test_msa_code_included_in_series_id(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    captured_requests: list[Any] = []

    async def _capture(req):
        captured_requests.append(req)
        return _make_provider_response(True, _BLS_SUCCESS_BODY)

    monkeypatch.setattr(m._get_client(), "_request", _capture)

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    await execute_bls_oews_get_wage(
        payload={"soc_code": "47-2111", "msa_code": "12420"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert captured_requests
    body = captured_requests[0].body
    assert isinstance(body, dict)
    series_ids = body.get("seriesid", [])
    # MSA area type M + 0012420 padded to 7 digits
    assert any("M0012420" in sid for sid in series_ids)


@pytest.mark.asyncio
async def test_national_fallback_when_no_msa(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    captured_requests: list[Any] = []

    async def _capture(req):
        captured_requests.append(req)
        return _make_provider_response(True, _BLS_SUCCESS_BODY)

    monkeypatch.setattr(m._get_client(), "_request", _capture)

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    await execute_bls_oews_get_wage(
        payload={"soc_code": "47-2152"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    body = captured_requests[0].body
    series_ids = body.get("seriesid", [])
    assert any("N0000000" in sid for sid in series_ids)


# --- Error mapping ---

@pytest.mark.asyncio
async def test_empty_series_returns_resource_not_found(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    mock_resp = _make_provider_response(True, _BLS_EMPTY_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"soc_code": "47-2111"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_bls_request_failed_status(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    mock_resp = _make_provider_response(True, _BLS_FAILED_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"soc_code": "47-2111"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    assert result.outcome == Outcome.FAILED
    assert "does not exist" in (result.error or "").lower()
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_timeout_returns_failed(monkeypatch):
    import asyncio
    import aspire_orchestrator.providers.bls_oews_client as m

    async def _slow(*a, **kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(m._get_client(), "_request", _slow)

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"trade": "hvac"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        timeout=0.001,
    )
    assert result.outcome == Outcome.FAILED
    assert result.receipt_data is not None


@pytest.mark.asyncio
async def test_receipt_no_pii_in_outputs(monkeypatch):
    import aspire_orchestrator.providers.bls_oews_client as m
    mock_resp = _make_provider_response(True, _BLS_SUCCESS_BODY)
    monkeypatch.setattr(m._get_client(), "_request", AsyncMock(return_value=mock_resp))

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"soc_code": "47-2111"},
        correlation_id=CORR_ID,
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    receipt_str = str(result.receipt_data)
    # BLS data is public — no secrets or PII expected; just assert no key names sneak in
    assert "registrationkey" not in receipt_str.lower()


# --- Helpers ---

def test_resolve_soc_known_trades():
    from aspire_orchestrator.providers.bls_oews_client import resolve_soc_code
    assert resolve_soc_code("electrician") == "47-2111"
    assert resolve_soc_code("plumber") == "47-2152"
    assert resolve_soc_code("drywaller") == "47-2081"
    assert resolve_soc_code("painter") == "47-2141"
    assert resolve_soc_code("hvac") == "49-9021"


def test_resolve_soc_unknown_returns_none():
    from aspire_orchestrator.providers.bls_oews_client import resolve_soc_code
    assert resolve_soc_code("astronaut") is None
    assert resolve_soc_code("") is None


def test_build_series_id_format():
    from aspire_orchestrator.providers.bls_oews_client import _build_series_id
    # National electrician median hourly
    sid = _build_series_id("N", "0000000", "47-2111", "08")
    assert sid == "OEUN000000000000047211108"
    assert len(sid) == 25
    # MSA electrician median hourly (Austin TX MSA 12420)
    sid_msa = _build_series_id("M", "0012420", "47-2111", "08")
    assert sid_msa == "OEUM001242000000047211108"
    assert len(sid_msa) == 25


def test_parse_value_handles_suppressed():
    from aspire_orchestrator.providers.bls_oews_client import _parse_value
    assert _parse_value("-") is None
    assert _parse_value("*") is None
    assert _parse_value("#") is None
    assert _parse_value("29.02") == pytest.approx(29.02)
    assert _parse_value("693,390") == pytest.approx(693390.0)


# --- Integration: live BLS API (free, no key needed) ---

@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_live_bls_electrician_national():
    """Live call to BLS OEWS — free, no API key, ~25 req/day unauthenticated."""
    import aspire_orchestrator.providers.bls_oews_client as m
    m._client = None

    from aspire_orchestrator.providers.bls_oews_client import execute_bls_oews_get_wage
    result = await execute_bls_oews_get_wage(
        payload={"trade": "electrician"},
        correlation_id="live-bls-elec-national",
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
    )
    # BLS data always present nationally; accept SUCCESS or FAILED (rate limit)
    assert result.outcome in (Outcome.SUCCESS, Outcome.FAILED)
    assert result.receipt_data is not None
    if result.outcome == Outcome.SUCCESS:
        # At least one wage field should be populated
        data = result.data
        has_wage = any(
            data.get(k) is not None
            for k in ("median_hourly", "mean_hourly", "pct_10_hourly", "employment")
        )
        assert has_wage, f"Expected at least one wage field, got: {data}"
