"""Google Places Provider Client — Location search for Adam (Research) skill pack.

Provider: Google Places API (https://maps.googleapis.com/maps/api/place)
Auth: API key as `key` query parameter (NOT header)
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - google_places.search: Search for places via Google Places Text Search API

Per ecosystem providers.yaml:
  adam_research places routing: google_places (primary) -> tomtom -> here -> foursquare -> osm_overpass
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


class GooglePlacesClient(BaseProviderClient):
    """Google Places API client.

    Auth note: Google Maps APIs use `key` as a query parameter,
    not a header. The _authenticate_headers method validates the key
    exists but returns empty headers — the key is appended to query params
    at the executor level.
    """

    provider_id = "google_places"
    base_url = "https://maps.googleapis.com/maps/api/place"
    timeout_seconds = 10.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.google_maps_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Google Maps API key not configured (ASPIRE_GOOGLE_MAPS_API_KEY)",
                provider_id=self.provider_id,
            )
        return {}  # Key is passed as query param, not header

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 403:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        # Google Places returns 200 with status field for API-level errors
        api_status = body.get("status", "")
        if api_status == "REQUEST_DENIED":
            return InternalErrorCode.AUTH_INVALID_KEY
        if api_status == "OVER_QUERY_LIMIT":
            return InternalErrorCode.RATE_LIMITED
        if api_status == "INVALID_REQUEST":
            return InternalErrorCode.INPUT_INVALID_FORMAT
        return super()._parse_error(status_code, body)


# Module-level singleton (lazy)
_client: GooglePlacesClient | None = None


def _get_client() -> GooglePlacesClient:
    global _client
    if _client is None:
        _client = GooglePlacesClient()
    return _client


async def execute_google_places_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute google_places.search — place search via Google Places Text Search API.

    Required payload:
      - query: str — search query (e.g., "pizza near Times Square")

    Optional payload:
      - location: str — "lat,lng" center point for biasing
      - radius: int — search radius in meters (max 50000)
      - type: str — place type filter (e.g., "restaurant", "pharmacy")
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="google_places.search",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="google_places.search",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    # Google Places uses API key as query parameter
    query_params: dict[str, str] = {
        "key": settings.google_maps_api_key,
        "query": query,
    }
    if payload.get("location"):
        query_params["location"] = payload["location"]
    if payload.get("radius"):
        query_params["radius"] = str(min(int(payload["radius"]), 50000))
    if payload.get("type"):
        query_params["type"] = payload["type"]

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/textsearch/json",
            query_params=query_params,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    # Google Places returns 200 even for API errors — check `status` field
    api_status = response.body.get("status", "") if response.success else ""
    api_success = response.success and api_status in ("OK", "ZERO_RESULTS")

    outcome = Outcome.SUCCESS if api_success else Outcome.FAILED
    if api_success:
        reason = "EXECUTED"
    elif api_status:
        reason = api_status
    elif response.error_code:
        reason = response.error_code.value
    else:
        reason = "FAILED"

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="google_places.search",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if api_success:
        raw_results = response.body.get("results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="google_places.search",
            data={
                "results": [
                    {
                        "name": r.get("name", ""),
                        "formatted_address": r.get("formatted_address", ""),
                        "location": r.get("geometry", {}).get("location", {}),
                        "rating": r.get("rating"),
                        "place_id": r.get("place_id", ""),
                    }
                    for r in raw_results
                ],
                "query": query,
                "result_count": len(raw_results),
            },
            receipt_data=receipt,
        )
    else:
        error_msg = (
            response.error_message
            or api_status
            or f"Google Places API error: HTTP {response.status_code}"
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="google_places.search",
            error=error_msg,
            receipt_data=receipt,
        )
