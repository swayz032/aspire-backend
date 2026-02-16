"""HERE Search Provider Client — Location search for Adam (Research) skill pack.

Provider: HERE Geocoding and Search v1 (https://discover.search.hereapi.com/v1)
Auth: API key as `apiKey` query parameter
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - here.search: Search for places via HERE Discover API

Per ecosystem providers.yaml:
  adam_research places routing: google_places -> tomtom -> here (fallback 2) -> foursquare -> osm_overpass
"""

from __future__ import annotations

import logging
from typing import Any

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


class HereClient(BaseProviderClient):
    """HERE Geocoding and Search API client.

    Auth: API key as `apiKey` query parameter.
    Endpoint: GET /discover?q=X&apiKey=Y&limit=10
    """

    provider_id = "here"
    base_url = "https://discover.search.hereapi.com/v1"
    timeout_seconds = 10.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.here_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="HERE API key not configured (ASPIRE_HERE_API_KEY)",
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
_client: HereClient | None = None


def _get_client() -> HereClient:
    global _client
    if _client is None:
        _client = HereClient()
    return _client


async def execute_here_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute here.search — place search via HERE Discover API.

    Required payload:
      - query: str — search query

    Optional payload:
      - at: str — "lat,lng" reference point (required by HERE for relevance)
      - limit: int — max results (default 10, max 100)
      - language: str — language code (default "en")
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="here.search",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="here.search",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {
        "apiKey": settings.here_api_key,
        "q": query,
        "limit": str(min(payload.get("limit", 10), 100)),
        "lang": payload.get("language", "en"),
    }
    if payload.get("at"):
        query_params["at"] = payload["at"]

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/discover",
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
        tool_id="here.search",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        raw_items = response.body.get("items", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="here.search",
            data={
                "results": [
                    {
                        "name": item.get("title", ""),
                        "address": item.get("address", {}).get("label", ""),
                        "location": item.get("position", {}),
                        "categories": [
                            c.get("name", "")
                            for c in item.get("categories", [])
                        ],
                        "id": item.get("id", ""),
                    }
                    for item in raw_items
                ],
                "query": query,
                "result_count": len(raw_items),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="here.search",
            error=response.error_message or f"HERE API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
