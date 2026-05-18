"""Unwrangle Home Depot Provider Client -- supplier discovery for Drew (Wave 5.1a).

Provider: Unwrangle (https://data.unwrangle.com)
Auth: Query parameter `api_key` -- no auth headers
Risk tier: GREEN (read-only product search)
Idempotency: N/A (read-only)

Verified working as of 2026-05-17:
  - homedepot_search: 2569 PVC pipe results, full product data including bulk_pricing

Known broken (Unwrangle-side 504 upstreams -- Wave 5.2 re-enable candidates):
  - lowes_search, acehardware_search -> return 504 upstream scraper failure

504 contract: Unwrangle returns HTTP 504 with {"success": false, "error": "..."}
when their upstream scraper is down. This is NOT transient -- it means the scraper
is unavailable. Do NOT retry (waste of credits). Return UPSTREAM_DEGRADED and let
Adam router handle gracefully.

Tools:
  - unwrangle_homedepot.search_products: Search Home Depot via Unwrangle scraper
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

_UPSTREAM_DEGRADED = "UPSTREAM_DEGRADED"


class UnwrangleHomeDepotClient(BaseProviderClient):
    """Unwrangle Home Depot search client."""

    provider_id = "unwrangle_homedepot"
    base_url = "https://data.unwrangle.com"
    timeout_seconds = 12.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        return {}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 402:
            return InternalErrorCode.RATE_QUOTA_EXCEEDED
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 504:
            return InternalErrorCode.SERVER_BAD_GATEWAY
        if 500 <= status_code < 600:
            return InternalErrorCode.SERVER_UNAVAILABLE
        return super()._parse_error(status_code, body)


_client: UnwrangleHomeDepotClient | None = None


def _get_client() -> UnwrangleHomeDepotClient:
    global _client
    if _client is None:
        _client = UnwrangleHomeDepotClient()
    return _client


def _is_upstream_degraded(response: ProviderResponse) -> bool:
    if response.status_code == 504:
        return True
    if (
        response.status_code == 200
        and not response.body.get("success", True)
        and "unable to fetch" in str(response.body.get("error", "")).lower()
    ):
        return True
    return False


async def search_products(
    line_item: str,
    *,
    suite_id: str,
    office_id: str,
    office_zip: str | None,
    correlation_id: str,
    page: int = 1,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Search Home Depot via Unwrangle (failover for SerpAPI HD)."""
    client = _get_client()
    tool_id = "unwrangle_homedepot.search_products"

    api_key = settings.unwrangle_api_key
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
            error="Unwrangle API key not configured (ASPIRE_UNWRANGLE_API_KEY)",
            receipt_data=receipt,
        )

    if not line_item or not line_item.strip():
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.INPUT_MISSING_REQUIRED.value,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: line_item",
            receipt_data=receipt,
        )

    if len(line_item) > 500:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.INPUT_INVALID_FORMAT.value,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="line_item exceeds 500 character limit",
            receipt_data=receipt,
        )

    query_safe = line_item[:100]
    logger.info(
        "unwrangle_homedepot.search_products: query=%r page=%d suite=%s corr=%s",
        query_safe, page, suite_id[:8], correlation_id[:8],
    )

    query_params: dict[str, str] = {
        "platform": "homedepot_search",
        "search": line_item,
        "page": str(page),
        "api_key": api_key,
    }

    request = ProviderRequest(
        method="GET",
        path="/api/getter/",
        query_params=query_params,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    response = await client._request(request)

    if _is_upstream_degraded(response):
        logger.warning(
            "provider.unwrangle.degraded: endpoint=homedepot_search "
            "reason=upstream_scraper_504 suite=%s corr=%s",
            suite_id[:8], correlation_id[:8],
        )
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=_UPSTREAM_DEGRADED,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_inputs"] = {"platform": "homedepot_search", "query_normalized": query_safe, "page": page}
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Unwrangle homedepot_search upstream scraper unavailable (504)",
            receipt_data=receipt,
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
    receipt["redacted_inputs"] = {
        "platform": "homedepot_search",
        "query_normalized": query_safe,
        "page": page,
        "office_zip": office_zip,
    }

    if response.success:
        raw_results = response.body.get("results", [])
        result_count = len(raw_results)
        credits_remaining = response.body.get("remaining_credits")
        logger.info(
            "unwrangle_homedepot success: results=%d credits_remaining=%s suite=%s",
            result_count, credits_remaining, suite_id[:8],
        )
        if credits_remaining is not None:
            receipt.setdefault("provider_metadata", {})
            receipt["provider_metadata"]["credits_remaining"] = credits_remaining
        receipt["redacted_outputs"] = {"result_count": result_count, "source_api": "homedepot_search"}
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "results": raw_results,
                "result_count": result_count,
                "query": line_item,
                "page": page,
                "source": "unwrangle_homedepot",
                "credits_remaining": credits_remaining,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"Unwrangle Home Depot error: HTTP {response.status_code}",
            receipt_data=receipt,
        )