"""Mapbox Geocoding Provider Client — Geocoding for Adam (Research) skill pack.

Provider: Mapbox Geocoding API v5 (https://api.mapbox.com/geocoding/v5/mapbox.places)
Auth: access_token as query parameter
Risk tier: GREEN (geocoding is read-only)
Idempotency: N/A (read-only)

Tools:
  - mapbox.geocode: Forward geocode an address/place name via Mapbox
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


class MapboxClient(BaseProviderClient):
    """Mapbox Geocoding API v5 client.

    Auth: access_token as query parameter.
    Endpoint: GET /{query}.json?access_token=X&limit=5
    """

    provider_id = "mapbox"
    base_url = "https://api.mapbox.com/geocoding/v5/mapbox.places"
    timeout_seconds = 10.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        access_token = settings.mapbox_access_token
        if not access_token:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Mapbox access token not configured (ASPIRE_MAPBOX_ACCESS_TOKEN)",
                provider_id=self.provider_id,
            )
        return {}  # Token is passed as query param

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
_client: MapboxClient | None = None


def _get_client() -> MapboxClient:
    global _client
    if _client is None:
        _client = MapboxClient()
    return _client


async def execute_mapbox_geocode(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute mapbox.geocode — forward geocoding via Mapbox Geocoding API v5.

    Required payload:
      - query: str — address or place name to geocode

    Optional payload:
      - limit: int — max results (default 5, max 10)
      - types: str — comma-separated place types (e.g., "address,poi")
      - proximity: str — "lng,lat" for biasing (note: Mapbox uses lng,lat order)
      - language: str — language code (default "en")
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="mapbox.geocode",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="mapbox.geocode",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    # Mapbox uses query in URL path: /{query}.json
    encoded_query = quote(query, safe="")
    query_params: dict[str, str] = {
        "access_token": settings.mapbox_access_token,
        "limit": str(min(payload.get("limit", 5), 10)),
        "language": payload.get("language", "en"),
    }
    if payload.get("types"):
        query_params["types"] = payload["types"]
    if payload.get("proximity"):
        query_params["proximity"] = payload["proximity"]

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/{encoded_query}.json",
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
        tool_id="mapbox.geocode",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        features = response.body.get("features", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="mapbox.geocode",
            data={
                "results": [
                    {
                        "place_name": f.get("place_name", ""),
                        "location": {
                            "lng": f.get("center", [None, None])[0],
                            "lat": f.get("center", [None, None])[1],
                        },
                        "place_type": f.get("place_type", []),
                        "relevance": f.get("relevance"),
                        "id": f.get("id", ""),
                    }
                    for f in features
                ],
                "query": query,
                "result_count": len(features),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="mapbox.geocode",
            error=response.error_message or f"Mapbox API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
