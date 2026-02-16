"""TomTom Search Provider Client — Location search for Adam (Research) skill pack.

Provider: TomTom Search API v2 (https://api.tomtom.com/search/2)
Auth: API key as `key` query parameter
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - tomtom.search: Execute a location search via TomTom Search API

Per ecosystem providers.yaml:
  adam_research places routing: google_places -> tomtom (fallback 1) -> here -> foursquare -> osm_overpass
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


class TomTomClient(BaseProviderClient):
    """TomTom Search API client.

    Auth: API key as `key` query parameter (not header).
    Endpoint: GET /search/{query}.json?key=X&limit=10
    """

    provider_id = "tomtom"
    base_url = "https://api.tomtom.com/search/2"
    timeout_seconds = 10.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.tomtom_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="TomTom API key not configured (ASPIRE_TOMTOM_API_KEY)",
                provider_id=self.provider_id,
            )
        return {}  # Key is passed as query param

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


# Module-level singleton (lazy)
_client: TomTomClient | None = None


def _get_client() -> TomTomClient:
    global _client
    if _client is None:
        _client = TomTomClient()
    return _client


async def execute_tomtom_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute tomtom.search — location search via TomTom Search API.

    Required payload:
      - query: str — search query

    Optional payload:
      - limit: int — max results (default 10, max 100)
      - lat: float — center latitude for biasing
      - lon: float — center longitude for biasing
      - radius: int — search radius in meters
      - language: str — language code (default "en-US")
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="tomtom.search",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="tomtom.search",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    # TomTom uses query in the URL path: /search/{query}.json
    encoded_query = quote(query, safe="")
    query_params: dict[str, str] = {
        "key": settings.tomtom_api_key,
        "limit": str(min(payload.get("limit", 10), 100)),
        "language": payload.get("language", "en-US"),
    }
    if payload.get("lat") is not None and payload.get("lon") is not None:
        query_params["lat"] = str(payload["lat"])
        query_params["lon"] = str(payload["lon"])
    if payload.get("radius"):
        query_params["radius"] = str(payload["radius"])

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/search/{encoded_query}.json",
            query_params=query_params,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="tomtom.search",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        raw_results = response.body.get("results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="tomtom.search",
            data={
                "results": [
                    {
                        "name": r.get("poi", {}).get("name", r.get("address", {}).get("freeformAddress", "")),
                        "address": r.get("address", {}).get("freeformAddress", ""),
                        "location": r.get("position", {}),
                        "score": r.get("score"),
                        "type": r.get("type", ""),
                        "id": r.get("id", ""),
                    }
                    for r in raw_results
                ],
                "query": query,
                "result_count": len(raw_results),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="tomtom.search",
            error=response.error_message or f"TomTom API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
