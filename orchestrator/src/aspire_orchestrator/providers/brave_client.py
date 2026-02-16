"""Brave Search Provider Client — Web search for Adam (Research) skill pack.

Provider: Brave Search (https://api.search.brave.com)
Auth: API key (X-Subscription-Token header)
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - brave.search: Execute a web search query via Brave Search API
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


class BraveClient(BaseProviderClient):
    """Brave Search API client."""

    provider_id = "brave"
    base_url = "https://api.search.brave.com/res/v1"
    timeout_seconds = 5.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.brave_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Brave API key not configured (ASPIRE_BRAVE_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"X-Subscription-Token": api_key}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


_client: BraveClient | None = None


def _get_client() -> BraveClient:
    global _client
    if _client is None:
        _client = BraveClient()
    return _client


async def execute_brave_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute brave.search — web search via Brave Search API.

    Required payload:
      - query: str — search query

    Optional payload:
      - count: int — number of results (default 10, max 20)
      - freshness: str — "pd" (past day), "pw", "pm", "py"
      - country: str — 2-letter country code
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="brave.search",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="brave.search",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {
        "q": query,
        "count": str(min(payload.get("count", 10), 20)),
    }
    if payload.get("freshness"):
        query_params["freshness"] = payload["freshness"]
    if payload.get("country"):
        query_params["country"] = payload["country"]

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/web/search",
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
        tool_id="brave.search",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        web_results = response.body.get("web", {}).get("results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="brave.search",
            data={
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "description": r.get("description", ""),
                        "age": r.get("age"),
                        "image_url": (r.get("thumbnail", {}) or {}).get("src"),
                        "favicon_url": (r.get("profile", {}) or {}).get("img"),
                    }
                    for r in web_results
                ],
                "query": query,
                "result_count": len(web_results),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="brave.search",
            error=response.error_message or f"Brave API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
