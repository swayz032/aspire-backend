"""Unwrangle Lowes Provider Client -- local hardware/home improvement supplier discovery (Wave 5.1a).

Provider: Unwrangle (https://data.unwrangle.com) -- Lowes engine
Auth: Query parameter `api_key` -- no auth headers
Risk tier: GREEN (read-only store/product data)
Idempotency: N/A (read-only)

Verified working as of 2026-05-17:
  - lowes_store: 10 real GA stores returned for zip 30297
  - lowes_category: 431 dishwasher results with store-localized inventory
  - lowes_detail: Full Husqvarna spec sheet with variants

Known broken (Unwrangle-side -- Wave 5.2 re-enable):
  - lowes_search -> returns 504 upstream scraper failure (NOT wired here)

PII policy: zipcode is not PII (safe to log). Store addresses NOT logged.

Tools:
  - unwrangle_lowes.find_nearest_store: Find up to 10 nearby Lowes stores by ZIP
  - unwrangle_lowes.browse_category: Browse a Lowes category URL with store inventory
  - unwrangle_lowes.get_product_detail: Fetch full Lowes product detail by URL
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


class UnwrangleLowesClient(BaseProviderClient):
    """Unwrangle Lowes client (store finder + category browse + product detail)."""

    provider_id = "unwrangle_lowes"
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


_client: UnwrangleLowesClient | None = None


def _get_client() -> UnwrangleLowesClient:
    global _client
    if _client is None:
        _client = UnwrangleLowesClient()
    return _client


def _check_api_key(
    client: UnwrangleLowesClient,
    tool_id: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str,
    capability_token_id: str | None,
    capability_token_hash: str | None,
) -> tuple[str | None, ToolExecutionResult | None]:
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
        return None, ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Unwrangle API key not configured (ASPIRE_UNWRANGLE_API_KEY)",
            receipt_data=receipt,
        )
    return api_key, None


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


async def find_nearest_store(
    *,
    zipcode: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Returns up to 10 nearby Lowes stores. Used to get store_no for category browsing.

    Verified 2026-05-17: 10 real GA stores for zip 30297.
    PII: zipcode logged; store addresses NOT logged.
    """
    client = _get_client()
    tool_id = "unwrangle_lowes.find_nearest_store"

    api_key, deny = _check_api_key(
        client, tool_id, correlation_id, suite_id, office_id,
        risk_tier, capability_token_id, capability_token_hash,
    )
    if deny is not None:
        return deny

    if not zipcode or not zipcode.strip():
        receipt = client.make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
            tool_id=tool_id, risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.INPUT_MISSING_REQUIRED.value,
            capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: zipcode", receipt_data=receipt)

    logger.info(
        "unwrangle_lowes.find_nearest_store: zipcode=%s suite=%s corr=%s",
        zipcode, suite_id[:8], correlation_id[:8],
    )

    query_params: dict[str, str] = {
        "platform": "lowes_store",
        "zipcode": zipcode,
        "api_key": api_key,  # type: ignore[arg-type]
    }

    request = ProviderRequest(
        method="GET", path="/api/getter/",
        query_params=query_params,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
    )

    response = await client._request(request)

    if _is_upstream_degraded(response):
        logger.warning(
            "provider.unwrangle.degraded: endpoint=lowes_store "
            "reason=upstream_scraper_504 suite=%s corr=%s",
            suite_id[:8], correlation_id[:8],
        )
        receipt = client.make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
            tool_id=tool_id, risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=_UPSTREAM_DEGRADED,
            capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_inputs"] = {"platform": "lowes_store", "zipcode": zipcode}
        return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id,
            error="Unwrangle lowes_store upstream scraper unavailable (504)", receipt_data=receipt)

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )
    receipt = client.make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        tool_id=tool_id, risk_tier=risk_tier, outcome=outcome, reason_code=reason,
        capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
        provider_response=response,
    )
    receipt["redacted_inputs"] = {"platform": "lowes_store", "zipcode": zipcode}

    if response.success:
        stores: list[dict[str, Any]] = (
            response.body.get("stores")
            or response.body.get("results")
            or []
        )
        store_count = len(stores)
        credits_remaining = response.body.get("remaining_credits")
        logger.info(
            "unwrangle_lowes.find_nearest_store success: store_count=%d zipcode=%s suite=%s",
            store_count, zipcode, suite_id[:8],
        )
        if credits_remaining is not None:
            receipt.setdefault("provider_metadata", {})
            receipt["provider_metadata"]["credits_remaining"] = credits_remaining
        # PII-safe: log count+zipcode, NOT store addresses
        receipt["redacted_outputs"] = {"store_count": store_count, "source_api": "lowes_store", "zipcode": zipcode}
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id=tool_id,
            data={
                "stores": stores, "store_count": store_count,
                "zipcode": zipcode, "source": "unwrangle_lowes",
                "credits_remaining": credits_remaining,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error=response.error_message or f"Unwrangle Lowes store error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def browse_category(
    *,
    category_url: str,
    store_no: int,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    page: int = 1,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Browse a Lowes category page with store-localized inventory.

    Verified 2026-05-17: 431 dishwasher results with store-localized inventory.
    store_no must come from find_nearest_store.
    """
    client = _get_client()
    tool_id = "unwrangle_lowes.browse_category"

    api_key, deny = _check_api_key(
        client, tool_id, correlation_id, suite_id, office_id,
        risk_tier, capability_token_id, capability_token_hash,
    )
    if deny is not None:
        return deny

    if not category_url or not category_url.strip():
        receipt = client.make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
            tool_id=tool_id, risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.INPUT_MISSING_REQUIRED.value,
            capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: category_url", receipt_data=receipt)

    url_safe = category_url[:100]
    logger.info(
        "unwrangle_lowes.browse_category: url=%r store_no=%d page=%d suite=%s corr=%s",
        url_safe, store_no, page, suite_id[:8], correlation_id[:8],
    )

    query_params: dict[str, str] = {
        "platform": "lowes_category",
        "url": category_url,
        "store_no": str(store_no),
        "page": str(page),
        "api_key": api_key,  # type: ignore[arg-type]
    }

    request = ProviderRequest(
        method="GET", path="/api/getter/",
        query_params=query_params,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
    )

    response = await client._request(request)

    if _is_upstream_degraded(response):
        logger.warning(
            "provider.unwrangle.degraded: endpoint=lowes_category "
            "reason=upstream_scraper_504 suite=%s corr=%s",
            suite_id[:8], correlation_id[:8],
        )
        receipt = client.make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
            tool_id=tool_id, risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=_UPSTREAM_DEGRADED,
            capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_inputs"] = {"platform": "lowes_category", "url_normalized": url_safe, "store_no": store_no, "page": page}
        return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id,
            error="Unwrangle lowes_category upstream scraper unavailable (504)", receipt_data=receipt)

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )
    receipt = client.make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        tool_id=tool_id, risk_tier=risk_tier, outcome=outcome, reason_code=reason,
        capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
        provider_response=response,
    )
    receipt["redacted_inputs"] = {"platform": "lowes_category", "url_normalized": url_safe, "store_no": store_no, "page": page}

    if response.success:
        raw_results = response.body.get("results", [])
        result_count = len(raw_results)
        credits_remaining = response.body.get("remaining_credits")
        logger.info(
            "unwrangle_lowes.browse_category success: results=%d store_no=%d suite=%s",
            result_count, store_no, suite_id[:8],
        )
        if credits_remaining is not None:
            receipt.setdefault("provider_metadata", {})
            receipt["provider_metadata"]["credits_remaining"] = credits_remaining
        receipt["redacted_outputs"] = {"result_count": result_count, "store_no": store_no, "source_api": "lowes_category"}
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id=tool_id,
            data={
                "results": raw_results, "result_count": result_count,
                "category_url": category_url, "store_no": store_no,
                "page": page, "source": "unwrangle_lowes",
                "credits_remaining": credits_remaining,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error=response.error_message or f"Unwrangle Lowes category error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def get_product_detail(
    *,
    product_url: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Fetch full Lowes product detail by URL.

    Verified 2026-05-17: full Husqvarna spec sheet with variants.
    """
    client = _get_client()
    tool_id = "unwrangle_lowes.get_product_detail"

    api_key, deny = _check_api_key(
        client, tool_id, correlation_id, suite_id, office_id,
        risk_tier, capability_token_id, capability_token_hash,
    )
    if deny is not None:
        return deny

    if not product_url or not product_url.strip():
        receipt = client.make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
            tool_id=tool_id, risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.INPUT_MISSING_REQUIRED.value,
            capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: product_url", receipt_data=receipt)

    url_safe = product_url[:100]
    logger.info(
        "unwrangle_lowes.get_product_detail: url=%r suite=%s corr=%s",
        url_safe, suite_id[:8], correlation_id[:8],
    )

    query_params: dict[str, str] = {
        "platform": "lowes_detail",
        "url": product_url,
        "api_key": api_key,  # type: ignore[arg-type]
    }

    request = ProviderRequest(
        method="GET", path="/api/getter/",
        query_params=query_params,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
    )

    response = await client._request(request)

    if _is_upstream_degraded(response):
        logger.warning(
            "provider.unwrangle.degraded: endpoint=lowes_detail "
            "reason=upstream_scraper_504 suite=%s corr=%s",
            suite_id[:8], correlation_id[:8],
        )
        receipt = client.make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
            tool_id=tool_id, risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=_UPSTREAM_DEGRADED,
            capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        receipt["redacted_inputs"] = {"platform": "lowes_detail", "url_normalized": url_safe}
        return ToolExecutionResult(outcome=Outcome.FAILED, tool_id=tool_id,
            error="Unwrangle lowes_detail upstream scraper unavailable (504)", receipt_data=receipt)

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )
    receipt = client.make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        tool_id=tool_id, risk_tier=risk_tier, outcome=outcome, reason_code=reason,
        capability_token_id=capability_token_id, capability_token_hash=capability_token_hash,
        provider_response=response,
    )
    receipt["redacted_inputs"] = {"platform": "lowes_detail", "url_normalized": url_safe}

    if response.success:
        product_data = response.body.get("product", response.body)
        credits_remaining = response.body.get("remaining_credits")
        logger.info(
            "unwrangle_lowes.get_product_detail success: credits_remaining=%s suite=%s",
            credits_remaining, suite_id[:8],
        )
        if credits_remaining is not None:
            receipt.setdefault("provider_metadata", {})
            receipt["provider_metadata"]["credits_remaining"] = credits_remaining
        receipt["redacted_outputs"] = {"has_product_data": bool(product_data), "source_api": "lowes_detail"}
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS, tool_id=tool_id,
            data={"product": product_data, "source": "unwrangle_lowes", "credits_remaining": credits_remaining},
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error=response.error_message or f"Unwrangle Lowes detail error: HTTP {response.status_code}",
            receipt_data=receipt,
        )