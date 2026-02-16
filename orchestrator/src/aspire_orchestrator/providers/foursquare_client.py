"""Foursquare Places Provider Client — Location search for Adam (Research) skill pack.

Provider: Foursquare Places API v3 (https://api.foursquare.com/v3)
Auth: API key in `Authorization` header (no Bearer prefix)
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - foursquare.search: Search for places via Foursquare Places API

Per ecosystem providers.yaml:
  adam_research places routing: google_places -> tomtom -> here -> foursquare (fallback 3) -> osm_overpass
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


class FoursquareClient(BaseProviderClient):
    """Foursquare Places API v3 client.

    Auth: API key in Authorization header (no Bearer prefix, just the key).
    Endpoint: GET /places/search?query=X
    """

    provider_id = "foursquare"
    base_url = "https://api.foursquare.com/v3"
    timeout_seconds = 10.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.foursquare_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Foursquare API key not configured (ASPIRE_FOURSQUARE_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"Authorization": api_key}  # No Bearer prefix per FSQ v3

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.AUTH_SCOPE_INSUFFICIENT
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


# Module-level singleton (lazy)
_client: FoursquareClient | None = None


def _get_client() -> FoursquareClient:
    global _client
    if _client is None:
        _client = FoursquareClient()
    return _client


async def execute_foursquare_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute foursquare.search — place search via Foursquare Places API v3.

    Required payload:
      - query: str — search query

    Optional payload:
      - ll: str — "lat,lng" center point
      - radius: int — search radius in meters (max 100000)
      - categories: str — comma-separated FSQ category IDs
      - limit: int — max results (default 10, max 50)
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="foursquare.search",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="foursquare.search",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {
        "query": query,
        "limit": str(min(payload.get("limit", 10), 50)),
    }
    if payload.get("ll"):
        query_params["ll"] = payload["ll"]
    if payload.get("radius"):
        query_params["radius"] = str(min(int(payload["radius"]), 100000))
    if payload.get("categories"):
        query_params["categories"] = payload["categories"]

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/places/search",
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
        tool_id="foursquare.search",
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
            tool_id="foursquare.search",
            data={
                "results": [
                    {
                        "name": r.get("name", ""),
                        "address": r.get("location", {}).get("formatted_address", ""),
                        "location": {
                            "lat": r.get("geocodes", {}).get("main", {}).get("latitude"),
                            "lng": r.get("geocodes", {}).get("main", {}).get("longitude"),
                        },
                        "categories": [
                            c.get("name", "")
                            for c in r.get("categories", [])
                        ],
                        "fsq_id": r.get("fsq_id", ""),
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
            tool_id="foursquare.search",
            error=response.error_message or f"Foursquare API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
