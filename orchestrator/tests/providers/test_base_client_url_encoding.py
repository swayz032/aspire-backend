"""Regression tests for base_client.py URL encoding.

The bug this guards against: prior to commit fix/base-client-url-encoding,
`BaseProviderClient._request()` built query strings via raw f-string
concatenation, producing URLs with unencoded spaces / commas / reserved
chars. ATTOM (and most strict gateways) reject those with HTTP 400
INPUT_INVALID_FORMAT.

Verified end-to-end against ATTOM `assessment/detail`:
  - unencoded: HTTP 400 "Invalid Parameter(s) in Request"
  - encoded:   HTTP 200 with full property record
The fix uses `urllib.parse.urlencode(sorted(params.items()))`.

These tests inspect the constructed URL via a stub httpx client and assert
proper percent-encoding for the canonical failure inputs.
"""
from __future__ import annotations

import httpx
import pytest

from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderRequest,
)


class _StubClient(BaseProviderClient):
    """Minimal concrete subclass for URL-construction tests."""

    provider_id = "stub"
    base_url = "https://example.test"
    timeout_seconds = 5.0
    max_retries = 0

    async def _authenticate_headers(self, _request: ProviderRequest) -> dict[str, str]:
        return {"X-Stub-Auth": "1"}


def _build_request(query_params: dict[str, str]) -> ProviderRequest:
    return ProviderRequest(
        method="GET",
        path="/v1/property",
        query_params=query_params,
        correlation_id="test-corr-1",
        suite_id="94b89098-c4bf-4419-a154-e18d9d53f993",
        office_id="94b89098-c4bf-4419-a154-e18d9d53f993",
    )


@pytest.mark.asyncio
async def test_query_string_encodes_spaces(monkeypatch):
    """Address with spaces must percent-encode (was: raw spaces → 400)."""
    captured: dict[str, str] = {}

    async def fake_get(self, url, **kwargs):  # noqa: ANN001 — test stub
        captured["url"] = url
        return httpx.Response(200, json={"status": {"code": 0, "msg": "ok"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = _StubClient()
    req = _build_request({"address1": "4863 Price St", "address2": "Forest Park, GA 30297"})
    await client._request(req)

    assert "url" in captured, "stubbed GET was never invoked"
    # urlencode emits + for spaces (preferred for application/x-www-form-urlencoded
    # style query strings) and %2C for comma.
    assert "address1=4863+Price+St" in captured["url"]
    assert "address2=Forest+Park%2C+GA+30297" in captured["url"]
    assert " " not in captured["url"], "raw space leaked into URL"


@pytest.mark.asyncio
async def test_query_string_encodes_ampersand(monkeypatch):
    """Ampersand in value must be %26 to avoid being parsed as param separator."""
    captured: dict[str, str] = {}

    async def fake_get(self, url, **kwargs):  # noqa: ANN001 — test stub
        captured["url"] = url
        return httpx.Response(200, json={"status": {"code": 0, "msg": "ok"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = _StubClient()
    req = _build_request({"owner": "Smith & Sons LLC"})
    await client._request(req)

    assert "owner=Smith+%26+Sons+LLC" in captured["url"]


@pytest.mark.asyncio
async def test_query_string_sorted_deterministic(monkeypatch):
    """Params sorted alphabetically — guarantees deterministic cache keys."""
    captured: dict[str, str] = {}

    async def fake_get(self, url, **kwargs):  # noqa: ANN001 — test stub
        captured["url"] = url
        return httpx.Response(200, json={"status": {"code": 0, "msg": "ok"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = _StubClient()
    # Insertion order intentionally NOT alphabetical.
    req = _build_request({"zip": "30297", "address1": "4863 Price St"})
    await client._request(req)

    qs = captured["url"].split("?", 1)[1]
    # Alphabetical: address1 before zip.
    assert qs.index("address1=") < qs.index("zip=")


@pytest.mark.asyncio
async def test_empty_query_params_no_question_mark(monkeypatch):
    """No query params → no trailing `?` in URL."""
    captured: dict[str, str] = {}

    async def fake_get(self, url, **kwargs):  # noqa: ANN001 — test stub
        captured["url"] = url
        return httpx.Response(200, json={"status": {"code": 0, "msg": "ok"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = _StubClient()
    req = _build_request({})
    await client._request(req)

    assert "?" not in captured["url"]
