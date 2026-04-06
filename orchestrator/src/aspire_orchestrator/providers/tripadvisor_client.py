"""TripAdvisor Content API Provider Client — Location/hotel search for Adam (Research) skill pack.

Provider: TripAdvisor Content API (https://api.content.tripadvisor.com/api/v1)
Auth: Query parameter key
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Tools:
  - tripadvisor.search: Search for hotels, restaurants, and attractions via TripAdvisor
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


class TripAdvisorClient(BaseProviderClient):
    """TripAdvisor Content API location search client."""

    provider_id = "tripadvisor"
    base_url = "https://api.content.tripadvisor.com/api/v1"
    timeout_seconds = 8.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # TripAdvisor Content API authenticates via query param `key`.
        # Validate key is present; injection happens in query_params.
        if not settings.tripadvisor_api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="TripAdvisor API key not configured (ASPIRE_TRIPADVISOR_API_KEY)",
                provider_id=self.provider_id,
            )
        return {}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.DOMAIN_FORBIDDEN
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 400:
            return InternalErrorCode.INPUT_INVALID_FORMAT
        return super()._parse_error(status_code, body)


_client: TripAdvisorClient | None = None


def _get_client() -> TripAdvisorClient:
    global _client
    if _client is None:
        _client = TripAdvisorClient()
    return _client


async def execute_tripadvisor_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute tripadvisor.search — location search via TripAdvisor Content API.

    Required payload:
      - query: str — search term (e.g. "hotels in Austin TX")
      - category: str — location category (default: "hotels")
                        options: "hotels" | "attractions" | "restaurants" | "geos"

    Optional payload:
      - latLong: str — "lat,lng" string for geographic anchoring (e.g. "30.2672,-97.7431")
      - language: str — language code (default: "en")
      - radius: int — search radius (requires radiusUnit)
      - radiusUnit: str — "mi" (default) | "km" | "m"
    """
    client = _get_client()
    tool_id = "tripadvisor.search"

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    api_key = settings.tripadvisor_api_key
    if not api_key:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.AUTH_INVALID_KEY.value,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="TripAdvisor API key not configured",
            receipt_data=receipt,
        )

    category = payload.get("category", "hotels")
    language = payload.get("language", "en")

    query_params: dict[str, str] = {
        "searchQuery": query,
        "category": category,
        "language": language,
        "key": api_key,
    }

    if payload.get("latLong"):
        query_params["latLong"] = str(payload["latLong"])
    if payload.get("radius") is not None:
        query_params["radius"] = str(int(payload["radius"]))
    if payload.get("radiusUnit"):
        query_params["radiusUnit"] = str(payload["radiusUnit"])

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/location/search",
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
        tool_id=tool_id,
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        raw_data = response.body.get("data", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "results": [
                    {
                        "location_id": loc.get("location_id", ""),
                        "name": loc.get("name", ""),
                        "address_string": (loc.get("address_obj") or {}).get("address_string", ""),
                        "latitude": loc.get("latitude"),
                        "longitude": loc.get("longitude"),
                        "rating": loc.get("rating"),
                        "num_reviews": loc.get("num_reviews"),
                        "category": (loc.get("category") or {}).get("name"),
                    }
                    for loc in raw_data
                ],
                "query": query,
                "category": category,
                "result_count": len(raw_data),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"TripAdvisor API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_tripadvisor_location_details(
    *,
    location_id: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Get full details for a TripAdvisor location by ID.

    Returns: name, address, rating, num_reviews, price_level, phone, website,
    ranking, hotel_class, amenities, etc.
    Endpoint: GET /location/{locationId}/details
    """
    client = _get_client()
    tool_id = "tripadvisor.location_details"

    api_key = settings.tripadvisor_api_key
    if not api_key:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="TripAdvisor API key not configured",
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/location/{location_id}/details",
            query_params={"key": api_key, "language": "en", "currency": "USD"},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data=response.body,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"TripAdvisor details error: HTTP {response.status_code}",
        )
