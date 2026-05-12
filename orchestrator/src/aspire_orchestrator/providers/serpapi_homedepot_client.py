"""SerpApi Home Depot Provider Client — Hardware/home goods search for Adam (Research) skill pack.

Provider: SerpApi (https://serpapi.com) — Home Depot engine
Auth: Query parameter api_key
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Budget note: SerpApi free tier is 250 searches/month across two accounts (A+B, 240 cap each).
Dual-account budget gate: select_account() → try_increment() → get_api_key(). On HTTP 429 /
quota body error, mark_account_exhausted() forces the account to cap, then the other account
is tried once. Adapter never retries autonomously — orchestrator owns retry logic (Law #1).

Tools:
  - serpapi_home_depot.search: Search Home Depot product catalog via SerpApi
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
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
        # Key is injected into query_params in execute_serpapi_homedepot_search.
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
    timeout: float = 8.0,
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

    # Fix 7 — query length cap (security R-004). Must run BEFORE select_account()
    # so a malformed query never burns a budget slot.
    if len(query) > 500:
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
            error="query exceeds 500 character limit",
            receipt_data=receipt,
        )

    # --- Dual-account budget gate (Pass A) ---
    # State machine: select_account → try_increment → get_api_key.
    # On 429/quota: mark_account_exhausted → retry other account once.
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
        # Race: another request consumed the last slot between select and increment
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
    except KeyError as exc:
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

    request = ProviderRequest(
        method="GET",
        path="/search",
        query_params=query_params,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    # Per-call timeout (voice path uses 4s; default 8s preserves prior behavior).
    try:
        response = await asyncio.wait_for(client._request(request), timeout=timeout)
    except asyncio.TimeoutError:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.NETWORK_TIMEOUT.value,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi Home Depot timeout after {timeout}s",
            receipt_data=receipt,
        )

    # --- 429 / quota-body exhaustion detection (Pass A) ---
    # SerpApi returns HTTP 429 for rate limiting and HTTP 200 with an error
    # body containing "quota" or "plan" when the monthly limit is hit. Either
    # signal means this account is exhausted — mark it and try the other once.
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
        # Fix 4 — emit intermediate receipt for the 429 event on the exhausted account
        # BEFORE attempting failover (Law #2: every failure gets a receipt).
        _pre_counts = current_counts()
        rate_receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id=tool_id,
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=InternalErrorCode.RATE_LIMITED.value,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        rate_receipt["redacted_outputs"] = {
            "engine": "home_depot",
            "account_id": account_id,
            "cached": False,
            "budget_remaining_a": max(0, 240 - _pre_counts.get("A", 0)),
            "budget_remaining_b": max(0, 240 - _pre_counts.get("B", 0)),
            "query_normalized": query[:100],
            "store_id": payload.get("store_id"),
            "http_status": response.status_code,
        }
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([rate_receipt])
        except Exception:
            pass
        mark_account_exhausted(account_id, reason=f"HTTP {response.status_code}")
        # Retry on the other account (single attempt — Law #1: no autonomous retry loops)
        # Fix 6 — validate key BEFORE incrementing to avoid burning a budget slot (R-003).
        other_account = "B" if account_id == "A" else "A"
        try:
            other_key = get_api_key(other_account)
        except (ValueError, KeyError):
            # No key available for the other account — return failure without incrementing.
            _counts_post = current_counts()
            no_key_receipt = client.make_receipt_data(
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
            no_key_receipt["redacted_outputs"] = {
                "engine": "home_depot",
                "account_id": None,
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": query[:100],
                "store_id": payload.get("store_id"),
            }
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=tool_id,
                error=f"SerpApi account {other_account} key not configured",
                receipt_data=no_key_receipt,
            )
        if try_increment(other_account):
            try:
                query_params["api_key"] = other_key
                request2 = ProviderRequest(
                    method="GET",
                    path="/search",
                    query_params=query_params,
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                )
                response = await asyncio.wait_for(
                    client._request(request2), timeout=timeout
                )
                account_id = other_account
            except asyncio.TimeoutError:
                pass  # Fall through to failure path below

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
    # Fix 2 — assemble full redacted_outputs envelope (architect spec).
    # Budget fields moved from top-level into redacted_outputs so _map_receipt_to_row
    # preserves them. Top-level budget_* fields were silently dropped.
    receipt["redacted_outputs"] = {
        "engine": "home_depot",
        "account_id": account_id,
        "cached": False,
        "budget_remaining_a": max(0, 240 - _post_counts.get("A", 0)),
        "budget_remaining_b": max(0, 240 - _post_counts.get("B", 0)),
        "query_normalized": query[:100],
        "store_id": payload.get("store_id"),
    }
    receipt.pop("budget_account_id", None)
    receipt.pop("budget_remaining_a", None)
    receipt.pop("budget_remaining_b", None)

    if response.success:
        raw_products = response.body.get("products", [])
        search_info = response.body.get("search_information", {})
        search_params = response.body.get("search_parameters", {})

        # Bangor default-fallback detection (Wave 2.0).
        # SerpAPI silently injects its account-default store when the caller
        # passes neither `store_id` nor `delivery_zip`. The default for our key
        # is store_id=2414 / delivery_zip=04401 (Bangor, ME). When this happens,
        # `pickup.store_name` on every product reads "Bangor" / "South Loop"
        # regardless of where the user actually is — a poisoning bug that
        # masquerades as a real local result.
        #
        # We mark the response with `default_store_fallback=true` so the
        # playbook can refuse it and ask Ava to clarify the user's location
        # instead of shipping cards anchored to a Maine store.
        requested_store_id = str(payload.get("store_id") or "").strip()
        requested_delivery_zip = str(payload.get("delivery_zip") or "").strip()
        returned_store_id = str(search_info.get("store_id") or "").strip()
        returned_store_name = str(search_info.get("store_name") or "").strip()
        # The 2414/04401 pair is *our* SerpAPI account default. If the caller
        # didn't request them but they're echoed back in search_parameters,
        # the response is a default-fallback (poisoned) result.
        echo_store_id = str(search_params.get("store_id") or "").strip()
        echo_delivery_zip = str(search_params.get("delivery_zip") or "").strip()
        default_store_fallback = (
            (not requested_store_id and not requested_delivery_zip)
            and (echo_store_id == "2414" or echo_delivery_zip == "04401")
        )

        # Stronger detection (Wave 2.1): even when delivery_zip is passed,
        # SerpAPI sometimes still ships Bangor inventory if store_id wasn't
        # also pinned (the May-4 1:01PM transcript: zip=32303 FL but every
        # pickup.store_name = "Bangor"). Detect mismatch when the requested
        # zip is non-Maine (Maine = 039xx-049xx) but pickup.store_id=2414
        # OR pickup.store_name="Bangor" appears on the majority of products.
        product_pickup_poisoning = False
        if requested_delivery_zip and raw_products:
            zip_prefix = requested_delivery_zip[:2]
            is_maine_zip = zip_prefix in ("03", "04")  # Maine ZIP range
            if not is_maine_zip:
                bangor_pickup_count = 0
                checked = 0
                for p in raw_products[:8]:  # sample top-8 to bound cost
                    pickup_obj = p.get("pickup") if isinstance(p.get("pickup"), dict) else {}
                    pickup_store_id = str(pickup_obj.get("store_id") or "").strip()
                    pickup_store_name = str(pickup_obj.get("store_name") or "").strip()
                    if pickup_store_id or pickup_store_name:
                        checked += 1
                        if pickup_store_id == "2414" or pickup_store_name.lower() == "bangor":
                            bangor_pickup_count += 1
                # If majority of pickup-bearing products say Bangor in a non-ME
                # zip request, SerpAPI ignored our zip and shipped its default.
                if checked >= 2 and bangor_pickup_count >= max(2, checked // 2):
                    product_pickup_poisoning = True
                    logger.warning(
                        "SerpAPI HD pickup-poisoning detected: requested zip=%s but "
                        "%d/%d products shipped Bangor pickup data — refusing response",
                        requested_delivery_zip, bangor_pickup_count, checked,
                    )

        # Combined poisoning flag — playbook treats either signal as fatal.
        default_store_fallback = default_store_fallback or product_pickup_poisoning

        store_info = {
            "store_id": returned_store_id,
            "store_name": returned_store_name,
            "requested_store_id": requested_store_id,
            "requested_delivery_zip": requested_delivery_zip,
            "default_store_fallback": default_store_fallback,
            "pickup_poisoning": product_pickup_poisoning,
        }
        def _pick_image(product: dict[str, Any]) -> str:
            def _extract(value: Any) -> str:
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    for key in ("url", "thumbnail", "image", "src", "link"):
                        maybe = value.get(key)
                        if isinstance(maybe, str) and maybe.strip():
                            return maybe.strip()
                    for nested in value.values():
                        nested_url = _extract(nested)
                        if nested_url:
                            return nested_url
                    return ""
                if isinstance(value, list):
                    for item in value:
                        nested_url = _extract(item)
                        if nested_url:
                            return nested_url
                    return ""
                return ""

            # Prefer explicit thumbnail string if present, then complex thumbnails payload.
            return _extract(product.get("thumbnail")) or _extract(product.get("thumbnails"))

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
                        "sku": p.get("sku") or p.get("internet_number") or "",
                        "upc": p.get("upc") or "",
                        "price": p.get("price"),
                        "price_was": p.get("price_was"),
                        "price_saving": p.get("price_saving"),
                        "percentage_off": p.get("percentage_off"),
                        # CA-specific: SerpAPI returns "33%" string here.
                        "percent_off": p.get("percent_off"),
                        # SerpAPI HD price highlight chip ("Special-Buy", "New-Lower-Price").
                        "price_badge": p.get("price_badge"),
                        # Currency: explicit when SerpAPI returns it (CA = "CAD"),
                        # else inferred downstream by the normalizer from URL host.
                        "currency": p.get("currency"),
                        # Pricing unit ("case", "package", "piece") — surfaced as
                        # "$99.97 / case" on the card price line when present.
                        "unit": p.get("unit") or "",
                        "rating": p.get("rating"),
                        "reviews": p.get("reviews"),
                        # Social proof — favorite count from HD ("10,293 saved").
                        "favorite": p.get("favorite"),
                        # Collection page URL (e.g. DEWALT 20V Collection).
                        "collection": p.get("collection") or "",
                        # CA-only stock dict (general_stock, store_stock_status).
                        "stock_information": p.get("stock_information") or {},
                        # Forward the raw nested pickup object (per-product local store).
                        "pickup": p.get("pickup") or {},
                        "delivery": p.get("delivery"),
                        "link": p.get("link"),
                        # Direct lazy-enrich URL — preferred over rebuilt path
                        # because it carries any tier-specific routing flags.
                        "serpapi_link": p.get("serpapi_link") or "",
                        "thumbnail": _pick_image(p),
                        "thumbnails": p.get("thumbnails") or [],
                        "badges": p.get("badges", []),
                        # Extended fields surfaced for richer card rendering.
                        "description": p.get("description") or p.get("highlights") or "",
                        "specifications": p.get("specifications") or {},
                        "dimensions": p.get("dimensions") or {},
                        "weight": p.get("weight") or "",
                        "variants": p.get("variants") or [],
                        "store_availability": p.get("store_availability") or [],
                    }
                    for p in raw_products
                ],
                "query": query,
                "result_count": len(raw_products),
                "store": store_info,
                # Search-level metadata for refinable sessions and breadcrumbs.
                # taxonomy = category trail ("Tools > Power Tools > Drills").
                # filters = facets with hd_filter_tokens for "show only Milwaukee
                # under $200" follow-ups. related_products = query suggestions.
                "taxonomy": response.body.get("taxonomy") or [],
                "filters": response.body.get("filters") or [],
                "related_products": response.body.get("related_products")
                    or response.body.get("related") or [],
                "pagination": response.body.get("serpapi_pagination")
                    or response.body.get("pagination") or {},
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
