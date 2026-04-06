"""SerpApi Home Depot Provider Client — Hardware/home goods search for Adam (Research) skill pack.

Provider: SerpApi (https://serpapi.com) — Home Depot engine
Auth: Query parameter api_key
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Budget note: SerpApi free tier is 250 searches/month. no_cache defaults to False so
cached results are returned when available — cached searches do not count against the
monthly quota.

Tools:
  - serpapi_home_depot.search: Search Home Depot product catalog via SerpApi
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


class SerpApiHomeDepotClient(BaseProviderClient):
    """SerpApi Home Depot search client."""

    provider_id = "serpapi_home_depot"
    base_url = "https://serpapi.com"
    timeout_seconds = 8.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # SerpApi authenticates via query param, not headers.
        # Validate key is present; injection happens in query_params.
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


_client: SerpApiHomeDepotClient | None = None


def _get_client() -> SerpApiHomeDepotClient:
    global _client
    if _client is None:
        _client = SerpApiHomeDepotClient()
    return _client


async def execute_serpapi_homedepot_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute serpapi_home_depot.search — product search via SerpApi Home Depot engine.

    Required payload:
      - query: str — product search query

    Optional payload:
      - store_id: str — Home Depot store number for local inventory
      - delivery_zip: str — ZIP code for delivery availability check
      - hd_sort: str — "top_sellers" | "price_low_to_high" | "price_high_to_low" |
                       "top_rated" | "best_match"
      - lowerbound: int — minimum price filter (USD)
      - upperbound: int — maximum price filter (USD)
      - hd_filter_tokens: str — category/department filter tokens from Home Depot
    """
    client = _get_client()
    tool_id = "serpapi_home_depot.search"

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

    query_params: dict[str, str] = {
        "engine": "home_depot",
        "q": query,
        "api_key": api_key,
        "no_cache": "false",
    }

    if payload.get("store_id"):
        query_params["store_id"] = str(payload["store_id"])
    if payload.get("delivery_zip"):
        query_params["delivery_zip"] = str(payload["delivery_zip"])
    if payload.get("hd_sort"):
        query_params["hd_sort"] = str(payload["hd_sort"])
    if payload.get("lowerbound") is not None:
        query_params["lowerbound"] = str(int(payload["lowerbound"]))
    if payload.get("upperbound") is not None:
        query_params["upperbound"] = str(int(payload["upperbound"]))
    if payload.get("hd_filter_tokens"):
        query_params["hd_filter_tokens"] = str(payload["hd_filter_tokens"])

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
        raw_products = response.body.get("products", [])
        search_info = response.body.get("search_information", {})
        store_info = {
            "store_id": search_info.get("store_id", ""),
            "store_name": search_info.get("store_name", ""),
        }
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "results": [
                    {
                        "title": p.get("title", ""),
                        "brand": p.get("brand"),
                        "model_number": p.get("model_number"),
                        "product_id": p.get("product_id"),
                        "price": p.get("price"),
                        "price_was": p.get("price_was"),
                        "price_saving": p.get("price_saving"),
                        "percentage_off": p.get("percentage_off"),
                        "rating": p.get("rating"),
                        "reviews": p.get("reviews"),
                        "pickup_quantity": (p.get("pickup") or {}).get("quantity"),
                        "pickup_store": (p.get("pickup") or {}).get("store_name", ""),
                        "delivery": p.get("delivery"),
                        "link": p.get("link"),
                        "thumbnail": p.get("thumbnail"),
                        "badges": p.get("badges", []),
                    }
                    for p in raw_products
                ],
                "query": query,
                "result_count": len(raw_products),
                "store": store_info,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"SerpApi Home Depot error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
