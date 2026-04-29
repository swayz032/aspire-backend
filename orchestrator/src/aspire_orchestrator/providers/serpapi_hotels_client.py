"""SerpApi Google Hotels Provider Client — single-call hotel search for Adam.

Provider: SerpApi (https://serpapi.com) — Google Hotels engine
Auth: Query parameter api_key
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Why this provider for the live-voice path: Google Hotels via SerpApi returns
name + rating + reviews + photos + amenities + price + gps_coordinates +
hotel_class in a single request, fitting the ~5s Anam ceiling. The
TripAdvisor-based 3-phase enrichment pipeline is retained for the desktop
research mode where latency is unconstrained, but is NOT used for live voice.

Tools:
  - serpapi_google_hotels.search: Search hotels via SerpApi Google Hotels engine
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
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


class SerpApiGoogleHotelsClient(BaseProviderClient):
    """SerpApi Google Hotels search client."""

    provider_id = "serpapi_google_hotels"
    base_url = "https://serpapi.com"
    timeout_seconds = 6.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        if not settings.serpapi_api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="SerpApi API key not configured (ASPIRE_SERPAPI_API_KEY)",
                provider_id=self.provider_id,
            )
        return {}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 400:
            return InternalErrorCode.INPUT_INVALID_FORMAT
        return super()._parse_error(status_code, body)


_client: SerpApiGoogleHotelsClient | None = None


def _get_client() -> SerpApiGoogleHotelsClient:
    global _client
    if _client is None:
        _client = SerpApiGoogleHotelsClient()
    return _client


def _default_dates() -> tuple[str, str]:
    """Default check-in / check-out for live-voice catalog queries.

    Google Hotels REQUIRES dates to return a property list at all. For
    research/discovery (no booking), pick a near-future 1-night window so
    the index returns properties with realistic pricing rather than peak
    holiday rates.
    """
    today = date.today()
    check_in = today + timedelta(days=30)
    check_out = check_in + timedelta(days=1)
    return check_in.isoformat(), check_out.isoformat()


async def execute_serpapi_google_hotels_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute serpapi_google_hotels.search — hotel listings for a location.

    Required payload:
      - query: str — destination string (e.g. "hotels in Tallahassee, FL")

    Optional payload:
      - check_in_date: YYYY-MM-DD (default: 30 days from today)
      - check_out_date: YYYY-MM-DD (default: check_in + 1 day)
      - adults: int (default: 2)
      - currency: str (default: "USD")
      - gl: str — country code (default: "us")
      - hl: str — language (default: "en")
      - sort_by: int — 3 = lowest price, 8 = highest rating (per SerpApi)
      - min_rating: int — guest rating filter
      - hotel_class: list[int] — star ratings (e.g. [3,4,5])
    """
    client = _get_client()
    tool_id = "serpapi_google_hotels.search"

    query = (payload.get("query") or "").strip()
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

    api_key = settings.serpapi_api_key
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
            error="SerpApi API key not configured",
            receipt_data=receipt,
        )

    check_in_default, check_out_default = _default_dates()
    check_in = str(payload.get("check_in_date") or check_in_default)
    check_out = str(payload.get("check_out_date") or check_out_default)

    query_params: dict[str, str] = {
        "engine": "google_hotels",
        "q": query,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "adults": str(payload.get("adults", 2)),
        "currency": str(payload.get("currency", "USD")),
        "gl": str(payload.get("gl", "us")),
        "hl": str(payload.get("hl", "en")),
        "api_key": api_key,
        "no_cache": "false",
    }

    if payload.get("sort_by") is not None:
        query_params["sort_by"] = str(int(payload["sort_by"]))
    if payload.get("min_rating") is not None:
        query_params["rating"] = str(int(payload["min_rating"]))
    hotel_class = payload.get("hotel_class")
    if isinstance(hotel_class, (list, tuple)) and hotel_class:
        query_params["hotel_class"] = ",".join(str(c) for c in hotel_class)
    elif hotel_class is not None:
        query_params["hotel_class"] = str(hotel_class)

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/search",
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
        body = response.body or {}
        properties = body.get("properties", []) or []
        # SerpApi sometimes lists ad-style sponsored hotels in a separate
        # `ads` array. Treat them as additional candidates — they have the
        # same shape (name, rating, gps_coordinates, photos, price).
        ads = body.get("ads", []) or []
        search_metadata = body.get("search_metadata", {}) or {}
        search_information = body.get("search_information", {}) or {}

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "properties": properties,
                "ads": ads,
                "query": query,
                "check_in_date": check_in,
                "check_out_date": check_out,
                "result_count": len(properties),
                "search_metadata": search_metadata,
                "search_information": search_information,
            },
            receipt_data=receipt,
        )

    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=response.error_message or f"SerpApi Google Hotels error: HTTP {response.status_code}",
        receipt_data=receipt,
    )
