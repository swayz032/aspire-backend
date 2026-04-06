"""Parallel AI Provider Client — Deep research for Adam (Research) skill pack.

Provider: Parallel AI (https://api.parallel.ai/v1)
Auth: Bearer token header
Risk tier: GREEN (read-only deep research)
Idempotency: N/A (read-only)
Timeout: 15s (longer than standard — Parallel AI performs multi-source deep research)

Tools:
  - parallel.search:  Multi-source deep web research with an objective
  - parallel.findall: Extract structured data from a list of target URLs
  - parallel.task:    Agentic research task with full result synthesis
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

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


class ParallelClient(BaseProviderClient):
    """Parallel AI deep research client."""

    provider_id = "parallel"
    base_url = "https://api.parallel.ai/v1"
    # Extended timeout — Parallel AI performs multi-source research, not a simple API call
    timeout_seconds = 15.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.parallel_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Parallel AI API key not configured (ASPIRE_PARALLEL_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"Authorization": f"Bearer {api_key}"}

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
        if status_code == 422:
            return InternalErrorCode.INPUT_CONSTRAINT_VIOLATED
        return super()._parse_error(status_code, body)


_client: ParallelClient | None = None


def _get_client() -> ParallelClient:
    global _client
    if _client is None:
        _client = ParallelClient()
    return _client


async def execute_parallel_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute parallel.search — multi-source deep research via Parallel AI.

    Required payload:
      - objective: str — research objective describing what to find

    Optional payload:
      - search_queries: list[str] — explicit search queries to execute
      - mode: str — "fast" (default) | "one-shot"
      - max_results: int — 1-20, default 10
      - source_domains: list[str] — restrict results to these domains
      - exclude_domains: list[str] — exclude results from these domains
      - after_date: str — ISO8601 date, only results published after this date
      - max_chars_per_result: int — max characters per result excerpt
      - max_chars_total: int — max total characters across all results
      - max_age_seconds: int — maximum age of results in seconds
    """
    client = _get_client()
    tool_id = "parallel.search"

    objective = payload.get("objective", "")
    if not objective:
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
            error="Missing required parameter: objective",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "objective": objective,
        "mode": payload.get("mode", "fast"),
        "max_results": min(max(int(payload.get("max_results", 10)), 1), 20),
    }

    if payload.get("search_queries"):
        body["search_queries"] = payload["search_queries"]
    if payload.get("source_domains"):
        body["source_domains"] = payload["source_domains"]
    if payload.get("exclude_domains"):
        body["exclude_domains"] = payload["exclude_domains"]
    if payload.get("after_date"):
        body["after_date"] = payload["after_date"]
    if payload.get("max_chars_per_result") is not None:
        body["max_chars_per_result"] = int(payload["max_chars_per_result"])
    if payload.get("max_chars_total") is not None:
        body["max_chars_total"] = int(payload["max_chars_total"])
    if payload.get("max_age_seconds") is not None:
        body["max_age_seconds"] = int(payload["max_age_seconds"])

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
        raw_results = response.body.get("results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "results": [
                    {
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "excerpt": r.get("excerpt", ""),
                        "source_domain": r.get("source_domain", ""),
                        "published_date": r.get("published_date"),
                    }
                    for r in raw_results
                ],
                "objective": objective,
                "result_count": len(raw_results),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"Parallel AI search error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_parallel_findall(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute parallel.findall — extract structured data from target URLs via Parallel AI.

    Required payload:
      - objective: str — what information to extract from the provided URLs
      - urls: list[str] — list of URLs to crawl and extract data from

    Optional payload:
      - max_results: int — maximum number of results to return
    """
    client = _get_client()
    tool_id = "parallel.findall"

    objective = payload.get("objective", "")
    urls = payload.get("urls", [])

    if not objective:
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
            error="Missing required parameter: objective",
            receipt_data=receipt,
        )

    if not urls:
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
            error="Missing required parameter: urls (must be a non-empty list)",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "objective": objective,
        "urls": urls,
    }

    if payload.get("max_results") is not None:
        body["max_results"] = int(payload["max_results"])

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/findall",
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
        raw_results = response.body.get("results", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "results": raw_results,
                "objective": objective,
                "urls_searched": len(urls),
                "result_count": len(raw_results),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"Parallel AI findall error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_parallel_task(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute parallel.task — agentic research task with full result synthesis via Parallel AI.

    Required payload:
      - objective: str — research objective for the agentic task

    Optional payload:
      - search_queries: list[str] — seed queries to bootstrap the task
      - max_results: int — maximum number of results to synthesize
    """
    client = _get_client()
    tool_id = "parallel.task"

    objective = payload.get("objective", "")
    if not objective:
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
            error="Missing required parameter: objective",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "objective": objective,
    }

    if payload.get("search_queries"):
        body["search_queries"] = payload["search_queries"]
    if payload.get("max_results") is not None:
        body["max_results"] = int(payload["max_results"])

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/tasks",
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
        # Parallel tasks return the full synthesized result body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "result": response.body,
                "objective": objective,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"Parallel AI task error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_parallel_extract(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Extract structured content from URLs via Parallel Extract (v1beta).

    Uses separate API key (ASPIRE_PARALLEL_EXTRACT_API_KEY) and x-api-key auth header.
    Endpoint: POST https://api.parallel.ai/v1beta/extract

    Required payload:
      - urls: list[str] — URLs to extract content from

    Optional payload:
      - objective: str — focuses extraction on specific information
      - excerpts: bool — include focused excerpts (default True)
      - full_content: bool — include full page content (default False)
      - max_chars_per_result: int — limit excerpt chars per URL
    """
    tool_id = "parallel.extract"

    api_key = settings.parallel_extract_api_key
    if not api_key:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Parallel Extract API key not configured (ASPIRE_PARALLEL_EXTRACT_API_KEY)",
        )

    urls = payload.get("urls", [])
    if not urls:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: urls",
        )

    body: dict[str, Any] = {"urls": urls}
    if payload.get("objective"):
        body["objective"] = payload["objective"]
    body["excerpts"] = payload.get("excerpts", True)
    body["full_content"] = payload.get("full_content", False)
    if payload.get("max_chars_per_result"):
        body["excerpts"] = {"max_chars_per_result": int(payload["max_chars_per_result"])}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.parallel.ai/v1beta/extract",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )

        if resp.status_code == 200:
            data = resp.json()
            return ToolExecutionResult(
                outcome=Outcome.SUCCESS,
                tool_id=tool_id,
                data={
                    "results": data.get("results", []),
                    "errors": data.get("errors", []),
                    "extract_id": data.get("extract_id", ""),
                },
            )
        else:
            error_body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = error_body.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=tool_id,
                error=f"Parallel Extract error: {error_msg}",
            )
    except Exception as exc:
        logger.warning("parallel.extract failed: %s", exc)
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"Parallel Extract request failed: {exc}",
        )
