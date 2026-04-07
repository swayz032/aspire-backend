"""SerpApi Google Shopping Provider Client — Product search for Adam (Research) skill pack.

Provider: SerpApi (https://serpapi.com)
Auth: Query parameter api_key
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Budget note: SerpApi free tier is 250 searches/month. no_cache defaults to False so
cached results are returned when available — cached searches do not count against the
monthly quota.

Tools:
  - serpapi_shopping.search: Execute a Google Shopping search via SerpApi
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


class SerpApiShoppingClient(BaseProviderClient):
    """SerpApi Google Shopping search client."""

    provider_id = "serpapi_shopping"
    base_url = "https://serpapi.com"
    timeout_seconds = 12.0
    max_retries = 0  # No retry — runs parallel with HD, fail fast if slow
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # SerpApi authenticates via query param, not headers.
        # _authenticate_headers must return a dict — return empty here.
        # The api_key is injected directly into query_params in execute_.
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


_client: SerpApiShoppingClient | None = None


def _get_client() -> SerpApiShoppingClient:
    global _client
    if _client is None:
        _client = SerpApiShoppingClient()
    return _client


async def execute_serpapi_shopping_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute serpapi_shopping.search — product search via SerpApi Google Shopping.

    Required payload:
      - query: str — product search query

    Optional payload:
      - location: str — e.g. "Austin, Texas, United States"
      - gl: str — country code (e.g. "us")
      - hl: str — language code (e.g. "en")
      - min_price: int — minimum price filter (USD)
      - max_price: int — maximum price filter (USD)
      - sort_by: int — 1 = price low to high, 2 = price high to low
      - on_sale: bool — show only items on sale
      - free_shipping: bool — show only items with free shipping
      - small_business: bool — show only small business items
    """
    client = _get_client()
    tool_id = "serpapi_shopping.search"

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

    # Build query params — api_key is a query param (SerpApi auth pattern)
    query_params: dict[str, str] = {
        "engine": "google_shopping",
        "q": query,
        "api_key": api_key,
        # no_cache=false means SerpApi returns cached results (free, doesn't count against quota)
        "no_cache": "false",
    }

    if payload.get("location"):
        query_params["location"] = str(payload["location"])
    if payload.get("gl"):
        query_params["gl"] = str(payload["gl"])
    if payload.get("hl"):
        query_params["hl"] = str(payload["hl"])
    if payload.get("min_price") is not None:
        query_params["min_price"] = str(int(payload["min_price"]))
    if payload.get("max_price") is not None:
        query_params["max_price"] = str(int(payload["max_price"]))
    if payload.get("sort_by") is not None:
        query_params["tbs"] = f"p_ord:{int(payload['sort_by'])}"
    if payload.get("on_sale"):
        query_params["tbs"] = query_params.get("tbs", "") + ",sale:1"
    if payload.get("free_shipping"):
        query_params["tbs"] = query_params.get("tbs", "") + ",mr:1,merchagg:g8000000000&tbs=shipping:1"
    if payload.get("small_business"):
        query_params["tbs"] = query_params.get("tbs", "") + ",small_business:1"

    # Strip leading comma from tbs if it got one from concatenation
    if "tbs" in query_params:
        query_params["tbs"] = query_params["tbs"].lstrip(",")

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
        raw_results = response.body.get("shopping_results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "results": [
                    {
                        "title": r.get("title", ""),
                        "price": r.get("price", ""),
                        "extracted_price": r.get("extracted_price"),
                        "source": r.get("source", ""),
                        "rating": r.get("rating"),
                        "reviews": r.get("reviews"),
                        "product_id": r.get("product_id"),
                        "product_link": r.get("product_link"),
                        "delivery": r.get("delivery"),
                        "thumbnail": r.get("thumbnail"),
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
            tool_id=tool_id,
            error=response.error_message or f"SerpApi Shopping error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
