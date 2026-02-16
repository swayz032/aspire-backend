"""Tavily Search Provider Client — Web search fallback for Adam (Research) skill pack.

Provider: Tavily (https://api.tavily.com)
Auth: API key (in POST body per Tavily API spec)
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - tavily.search: Execute a web search query via Tavily Search API

Per ecosystem providers.yaml:
  adam_research search routing: brave (primary) -> tavily (fallback)
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


class TavilyClient(BaseProviderClient):
    """Tavily Search API client."""

    provider_id = "tavily"
    base_url = "https://api.tavily.com"
    timeout_seconds = 10.0
    max_retries = 1  # Search is read-only, one retry is sufficient
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.tavily_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Tavily API key not configured (ASPIRE_TAVILY_API_KEY)",
                provider_id=self.provider_id,
            )
        return {}  # Tavily uses api_key in POST body, not header

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


# Module-level singleton (lazy)
_client: TavilyClient | None = None


def _get_client() -> TavilyClient:
    global _client
    if _client is None:
        _client = TavilyClient()
    return _client


async def execute_tavily_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute tavily.search — web search via Tavily Search API.

    Required payload:
      - query: str — search query

    Optional payload:
      - max_results: int — number of results (default 10, max 20)
      - search_depth: str — "basic" | "advanced" (default "basic")
      - include_answer: bool — include AI-generated answer (default False)
      - include_domains: list[str] — restrict to domains
      - exclude_domains: list[str] — exclude domains
    """
    client = _get_client()

    query = payload.get("query", "")
    if not query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="tavily.search",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="tavily.search",
            error="Missing required parameter: query",
            receipt_data=receipt,
        )

    # Tavily uses POST with api_key in body
    body: dict[str, Any] = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": min(payload.get("max_results", 10), 20),
        "search_depth": payload.get("search_depth", "basic"),
        "include_answer": payload.get("include_answer", False),
    }

    if payload.get("include_domains"):
        body["include_domains"] = payload["include_domains"]
    if payload.get("exclude_domains"):
        body["exclude_domains"] = payload["exclude_domains"]

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/search",
            body=body,
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
        tool_id="tavily.search",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        results = response.body.get("results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="tavily.search",
            data={
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", ""),
                        "score": r.get("score", 0.0),
                        "published_date": r.get("published_date"),
                        "image_url": r.get("image") or r.get("thumbnail"),
                    }
                    for r in results
                ],
                "query": query,
                "result_count": len(results),
                "answer": response.body.get("answer"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="tavily.search",
            error=response.error_message or f"Tavily API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
