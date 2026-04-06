"""Exa Semantic Search Provider Client — Deep research with structured output + grounding.

Provider: Exa (https://api.exa.ai)
Auth: API key (x-api-key header)
Risk tier: GREEN (read-only search/research)
Idempotency: N/A (read-only)

Full 2026 capabilities:
  - Search modes: auto, instant, fast, deep-lite, deep, deep-reasoning, deep-max
  - Structured output: outputSchema → JSON matching schema with grounding + confidence
  - Categories: company, news, financial_report, people, research_paper
  - Content: text, highlights, summaries per result
  - Subpage crawling, domain filtering (1200 max), date filtering
  - Live crawling via maxAgeHours
  - Content moderation (Law #9)
  - Cost breakdown in response → direct feed into telemetry
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


class ExaClient(BaseProviderClient):
    """Exa AI Search API client (v1.2.0)."""

    provider_id = "exa"
    base_url = "https://api.exa.ai"
    timeout_seconds = 30.0  # Deep search can take longer
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.exa_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Exa API key not configured (ASPIRE_EXA_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"x-api-key": api_key, "Content-Type": "application/json"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 400:
            return InternalErrorCode.INPUT_VALIDATION_FAILED
        return super()._parse_error(status_code, body)


_client: ExaClient | None = None


def _get_client() -> ExaClient:
    global _client
    if _client is None:
        _client = ExaClient()
    return _client


async def execute_exa_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute exa.search — semantic web search with optional deep research + structured output.

    Required payload:
      - query: str — the search query

    Optional payload:
      - type: str — search mode: "auto" (default), "instant", "fast",
                     "deep-lite", "deep", "deep-reasoning", "deep-max"
      - category: str — "company", "news", "financial_report", "people", "research_paper"
      - num_results: int — number of results (default 10, max 100)
      - output_schema: dict — JSON schema for structured output (triggers grounding)
      - system_prompt: str — instructions for synthesized output
      - include_domains: list[str] — only include results from these domains (max 1200)
      - exclude_domains: list[str] — exclude these domains (max 1200)
      - start_published_date: str — ISO 8601 date filter
      - end_published_date: str — ISO 8601 date filter
      - include_text: list[str] — require these strings in results
      - exclude_text: list[str] — exclude these strings
      - max_age_hours: int — freshness control (0 = always live crawl)
      - moderation: bool — enable content moderation (Law #9)
      - contents: dict — content retrieval options:
          - text: bool or dict with maxCharacters
          - highlights: bool or dict with maxCharacters, query
          - summary: dict with query, schema
          - subpages: int — number of subpages to crawl
          - subpage_target: str — keywords for subpage selection
    """
    tool_id = "exa.search"
    client = _get_client()

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

    # Build request body
    body: dict[str, Any] = {"query": query}

    # Search mode
    search_type = payload.get("type", "auto")
    body["type"] = search_type

    # Category filtering
    if payload.get("category"):
        body["category"] = payload["category"]

    # Result count
    body["numResults"] = min(payload.get("num_results", 10), 100)

    # Structured output (the key differentiator)
    if payload.get("output_schema"):
        body["outputSchema"] = payload["output_schema"]
    if payload.get("system_prompt"):
        body["systemPrompt"] = payload["system_prompt"]

    # Additional query variations for deep search
    if payload.get("additional_queries"):
        body["additionalQueries"] = payload["additional_queries"]

    # Domain filtering
    if payload.get("include_domains"):
        body["includeDomains"] = payload["include_domains"][:1200]
    if payload.get("exclude_domains"):
        body["excludeDomains"] = payload["exclude_domains"][:1200]

    # Date filtering
    if payload.get("start_published_date"):
        body["startPublishedDate"] = payload["start_published_date"]
    if payload.get("end_published_date"):
        body["endPublishedDate"] = payload["end_published_date"]

    # Text inclusion/exclusion
    if payload.get("include_text"):
        body["includeText"] = payload["include_text"]
    if payload.get("exclude_text"):
        body["excludeText"] = payload["exclude_text"]

    # Freshness control
    if payload.get("max_age_hours") is not None:
        body["maxAgeHours"] = payload["max_age_hours"]

    # Content moderation (Law #9)
    body["moderation"] = payload.get("moderation", True)

    # Content retrieval options
    contents: dict[str, Any] = {}
    content_opts = payload.get("contents", {})
    if content_opts.get("text") is not None:
        contents["text"] = content_opts["text"]
    elif "text" not in content_opts:
        # Default: return text with reasonable limit
        contents["text"] = {"maxCharacters": 3000}

    if content_opts.get("highlights") is not None:
        contents["highlights"] = content_opts["highlights"]
    else:
        contents["highlights"] = {"maxCharacters": 2000}

    if content_opts.get("summary"):
        contents["summary"] = content_opts["summary"]

    if content_opts.get("subpages"):
        contents["subpages"] = content_opts["subpages"]
    if content_opts.get("subpage_target"):
        contents["subpageTarget"] = content_opts["subpage_target"]

    if contents:
        body["contents"] = contents

    # Adjust timeout for deep search modes
    if search_type in ("deep", "deep-reasoning", "deep-max"):
        client.timeout_seconds = 60.0
    elif search_type == "deep-lite":
        client.timeout_seconds = 30.0
    else:
        client.timeout_seconds = 15.0

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
        tool_id=tool_id,
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        results = response.body.get("results", [])
        parsed_results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "published_date": r.get("publishedDate"),
                "author": r.get("author"),
                "text": r.get("text", ""),
                "highlights": r.get("highlights", []),
                "highlight_scores": r.get("highlightScores", []),
                "summary": r.get("summary"),
                "image": r.get("image"),
            }
            for r in results
        ]

        data: dict[str, Any] = {
            "results": parsed_results,
            "query": query,
            "search_type": response.body.get("searchType", search_type),
            "result_count": len(parsed_results),
        }

        # Structured output with grounding (the key differentiator)
        output = response.body.get("output")
        if output:
            data["output"] = output.get("content")
            data["grounding"] = output.get("grounding", [])

        # Cost breakdown (direct feed into telemetry)
        cost = response.body.get("costDollars")
        if cost:
            data["cost_dollars"] = cost.get("total", 0)
            data["cost_breakdown"] = cost

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data=data,
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"Exa API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
