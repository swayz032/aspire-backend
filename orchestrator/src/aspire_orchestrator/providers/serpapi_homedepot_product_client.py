"""SerpApi Home Depot Product Provider Client — Lazy per-product enrichment for Adam.

Provider: SerpApi (https://serpapi.com) — `home_depot_product` engine
Auth: Query parameter api_key
Risk tier: GREEN (read-only product detail)
Idempotency: N/A (read-only)

Cost model: 1 SerpApi unit per call. Called ONLY when a user opens the product
detail modal — never during voice search. This is intentional, not a fallback:
the basic `home_depot` search already returns enough fields to render carousel
cards. Detail enrichment (full image gallery, specs, bay/aisle, descriptions)
is on-demand.

Tools:
  - serpapi_home_depot_product.fetch: Lazy product detail fetch via SerpApi
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.adam.serpapi_budget import (
    BudgetExhaustedError,
    current_counts,
    get_api_key,
    mark_account_exhausted,
    select_account,
    try_increment,
)
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

_MAX_IMAGES = 12


class SerpApiHomeDepotProductClient(BaseProviderClient):
    """SerpApi Home Depot product detail client."""

    provider_id = "serpapi_home_depot_product"
    base_url = "https://serpapi.com"
    timeout_seconds = 4.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # SerpApi authenticates via query param, not headers.
        # Key is injected into query_params in fetch_product_details.
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


_client: SerpApiHomeDepotProductClient | None = None


def _get_client() -> SerpApiHomeDepotProductClient:
    global _client
    if _client is None:
        _client = SerpApiHomeDepotProductClient()
    return _client


def _normalize_image(value: Any) -> str:
    """Extract a usable image URL from SerpApi's nested image shapes."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "thumbnail", "image", "src", "link", "high_res"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _collect_images(payload: dict[str, Any]) -> list[str]:
    """Gather image URLs from media/images blocks, capped at _MAX_IMAGES."""
    images: list[str] = []
    seen: set[str] = set()

    candidates: list[Any] = []
    for key in ("media", "images", "thumbnails", "product_images"):
        block = payload.get(key)
        if isinstance(block, list):
            candidates.extend(block)
        elif isinstance(block, dict):
            for inner in block.values():
                if isinstance(inner, list):
                    candidates.extend(inner)
                else:
                    candidates.append(inner)

    for item in candidates:
        url = _normalize_image(item)
        if url and url not in seen:
            seen.add(url)
            images.append(url)
            if len(images) >= _MAX_IMAGES:
                break
    return images


async def fetch_product_details(
    *,
    product_id: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
    store_id: str | None = None,
) -> ToolExecutionResult:
    """Fetch full product details from SerpApi `home_depot_product` engine.

    Required:
      - product_id: Home Depot internet/product identifier

    Optional:
      - store_id: Home Depot store_id for bay/aisle/local stock fields

    Returns ToolExecutionResult with `data` containing enriched product fields:
      images (capped at 12), bullets, specifications, description (short/full),
      stock_quantity, bay, aisle, fulfillment_pickup, fulfillment_delivery,
      purchasing_limit_notes.
    """
    client = _get_client()
    tool_id = "serpapi_home_depot_product.fetch"

    if not product_id:
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
            error="Missing required parameter: product_id",
            receipt_data=receipt,
        )

    # --- Dual-account budget gate (Pass A) ---
    _counts = current_counts()
    account_id = select_account()
    if account_id is None:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="SERPAPI_BUDGET_EXHAUSTED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=str(BudgetExhaustedError(_counts)),
            receipt_data=receipt,
        )

    if not try_increment(account_id):
        other = "B" if account_id == "A" else "A"
        if not try_increment(other):
            _counts = current_counts()
            receipt = client.make_receipt_data(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id=tool_id,
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code="SERPAPI_BUDGET_EXHAUSTED",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=tool_id,
                error=str(BudgetExhaustedError(_counts)),
                receipt_data=receipt,
            )
        account_id = other

    try:
        api_key = get_api_key(account_id)
    except KeyError:
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
            error=f"SerpApi account {account_id} key not configured",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {
        "engine": "home_depot_product",
        "product_id": str(product_id),
        "api_key": api_key,
    }
    if store_id:
        query_params["store_id"] = str(store_id)

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

    # --- 429 / quota-body exhaustion detection (Pass A) ---
    _is_quota = (
        response.status_code == 429
        or (
            not response.success
            and response.error_message is not None
            and any(
                kw in response.error_message.lower()
                for kw in ("quota", "plan", "limit exceeded", "searches/month")
            )
        )
    )
    if _is_quota:
        mark_account_exhausted(account_id, reason=f"HTTP {response.status_code}")
        other_account = "B" if account_id == "A" else "A"
        if try_increment(other_account):
            try:
                other_key = get_api_key(other_account)
                query_params["api_key"] = other_key
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
                account_id = other_account
            except KeyError:
                pass

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    _post_counts = current_counts()
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
    # Augment receipt with budget state for audit trail (Law #2)
    receipt["budget_account_id"] = account_id
    receipt["budget_remaining_a"] = max(0, 240 - _post_counts.get("A", 0))
    receipt["budget_remaining_b"] = max(0, 240 - _post_counts.get("B", 0))

    if not response.success:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message
            or f"SerpApi Home Depot product error: HTTP {response.status_code}",
            receipt_data=receipt,
        )

    body = response.body or {}
    product = body.get("product_results") or body.get("product") or body
    pickup = product.get("fulfillment", {}).get("pickup") or product.get("pickup") or {}
    delivery = product.get("fulfillment", {}).get("delivery") or product.get("delivery") or {}
    inventory = product.get("inventory") or {}

    bullets = product.get("highlights") or product.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [b.strip() for b in bullets.split("\n") if b.strip()]

    description_full = product.get("description") or product.get("product_overview") or ""
    description_short = product.get("subtitle") or product.get("short_description") or ""

    enriched = {
        "product_id": product.get("product_id") or str(product_id),
        "title": product.get("title", ""),
        "brand": product.get("brand", ""),
        "model": product.get("model_number") or product.get("model") or "",
        "price": product.get("price"),
        "price_was": product.get("price_was"),
        "rating": product.get("rating"),
        "review_count": product.get("reviews"),
        "images": _collect_images(product),
        "bullets": list(bullets) if isinstance(bullets, list) else [],
        "specifications": product.get("specifications") or {},
        "description_short": description_short,
        "description_full": description_full,
        "stock_quantity": pickup.get("quantity") or inventory.get("quantity"),
        "bay": pickup.get("bay") or inventory.get("bay"),
        "aisle": pickup.get("aisle") or inventory.get("aisle"),
        "fulfillment_pickup": pickup,
        "fulfillment_delivery": delivery,
        "purchasing_limit_notes": product.get("purchasing_limit_notes")
        or product.get("limit_per_order")
        or "",
        "link": product.get("link", ""),
    }

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=enriched,
        receipt_data=receipt,
    )
