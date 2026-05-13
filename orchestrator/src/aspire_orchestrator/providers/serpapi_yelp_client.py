"""SerpApi Yelp Provider Client — Local supplier search for Materials Supplier mode.

Provider: SerpApi (https://serpapi.com) — Yelp engine
Auth: Query parameter api_key
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Budget note: Shares the same dual-account budget pool as the Home Depot adapter.
  select_account() → try_increment() → get_api_key(). On HTTP 429 / quota body
  error, mark_account_exhausted() forces the account to cap, then the other
  account is tried once. Adapter never retries autonomously — orchestrator
  owns retry logic (Law #1).

Normalised output shape (one entry per Yelp business):
  {
    "id":            str  — Yelp business identifier (from 'serpapi_link' hash or position)
    "name":          str  — Business display name
    "address":       str  — Street address line
    "city":          str
    "state":         str
    "zip":           str
    "phone":         str  — Formatted phone (public retail data — OK to cache per Law #9)
    "website":       str  — Business website URL
    "rating":        float | None
    "review_count":  int  — Total Yelp reviews
    "distance_miles": float | None
    "hours_open_now": bool | None
    "categories":    list[str]  — Yelp category labels
  }

Tools:
  - serpapi_yelp.search: Search Yelp for local trade suppliers via SerpApi
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
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

# ---------------------------------------------------------------------------
# Yelp category hints for trade suppliers (used as default find_desc prefix
# when the query is very short and likely a category, not a brand/product).
# These are soft hints — the caller sets find_desc from the normalised query.
# ---------------------------------------------------------------------------
_SUPPLIER_CATEGORIES = [
    "building supplies",
    "lumber",
    "concrete",
    "plumbing supply",
    "hvac",
    "electrical supply",
    "industrial supply",
    "roofing",
    "masonry",
]


class SerpApiYelpClient(BaseProviderClient):
    """SerpApi Yelp search client — local trade supplier discovery."""

    provider_id = "serpapi_yelp"
    base_url = "https://serpapi.com"
    timeout_seconds = 5.0  # Hard 5s cap per adapter contract
    max_retries = 0         # Adapter never retries; orchestrator owns retry logic (Law #1)
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # SerpApi authenticates via query param, not headers.
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


_client: SerpApiYelpClient | None = None


def _get_client() -> SerpApiYelpClient:
    global _client
    if _client is None:
        _client = SerpApiYelpClient()
    return _client


def _normalize_business(
    biz: dict[str, Any],
    position: int,
) -> dict[str, Any]:
    """Normalise a single Yelp business result to the adapter contract shape.

    SerpApi Yelp result keys (verified against engine docs 2026-05):
      place_id, title, price, rating, reviews, phone, address, city, state,
      zip_code, hours, categories (list[dict[str, str]] with 'title'),
      website, service_options, thumbnail, serpapi_link, links.website
    """
    # Extract categories — SerpApi returns [{title: "..."}] lists
    raw_cats = biz.get("categories") or []
    categories: list[str] = []
    if isinstance(raw_cats, list):
        for c in raw_cats:
            if isinstance(c, dict):
                title = c.get("title") or c.get("name") or ""
                if title:
                    categories.append(str(title))
            elif isinstance(c, str):
                categories.append(c)

    # Hours open now — SerpApi returns {"is_open_now": bool, ...} or None
    hours_obj = biz.get("hours")
    hours_open_now: bool | None = None
    if isinstance(hours_obj, dict):
        val = hours_obj.get("is_open_now")
        if isinstance(val, bool):
            hours_open_now = val

    # Distance — SerpApi may return "0.4 mi" string or numeric
    raw_dist = biz.get("distance")
    distance_miles: float | None = None
    if isinstance(raw_dist, (int, float)):
        distance_miles = float(raw_dist)
    elif isinstance(raw_dist, str):
        raw_dist = raw_dist.strip().lower().replace(" mi", "").replace("mi", "")
        try:
            distance_miles = float(raw_dist)
        except ValueError:
            pass

    # Rating — SerpApi returns float or string
    raw_rating = biz.get("rating")
    rating: float | None = None
    if isinstance(raw_rating, (int, float)):
        rating = float(raw_rating)
    elif isinstance(raw_rating, str):
        try:
            rating = float(raw_rating)
        except ValueError:
            pass

    # Review count
    raw_reviews = biz.get("reviews")
    review_count: int = 0
    if isinstance(raw_reviews, int):
        review_count = raw_reviews
    elif isinstance(raw_reviews, str):
        cleaned = raw_reviews.replace(",", "").replace(" reviews", "").strip()
        try:
            review_count = int(cleaned)
        except ValueError:
            pass

    # Phone — public retail data (OK to include per Law #9 note in scope doc)
    phone = str(biz.get("phone") or "").strip()

    # Website — prefer explicit 'website', fall back to 'links.website'
    website = str(biz.get("website") or "").strip()
    if not website:
        links = biz.get("links") or {}
        if isinstance(links, dict):
            website = str(links.get("website") or "").strip()

    # Stable ID: Yelp place_id preferred, else positional fallback
    biz_id = str(biz.get("place_id") or biz.get("serpapi_link") or f"yelp_{position}").strip()

    return {
        "id": biz_id,
        "name": str(biz.get("title") or "").strip(),
        "address": str(biz.get("address") or "").strip(),
        "city": str(biz.get("city") or "").strip(),
        "state": str(biz.get("state") or "").strip(),
        "zip": str(biz.get("zip_code") or "").strip(),
        "phone": phone,
        "website": website,
        "rating": rating,
        "review_count": review_count,
        "distance_miles": distance_miles,
        "hours_open_now": hours_open_now,
        "categories": categories,
    }


async def execute_serpapi_yelp_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
    timeout: float = 5.0,
) -> ToolExecutionResult:
    """Execute serpapi_yelp.search — local supplier search via SerpApi Yelp engine.

    Required payload:
      - find_desc: str — What to search for (e.g. "concrete supplier", "lumber yard")

    Optional payload:
      - find_loc: str  — Location string (city, state, ZIP, address)
      - ll:       str  — Lat/lng override: "ll=@37.7,-122.4,10z"
                         (used when caller has coordinates instead of address)
      - start:    int  — Pagination offset (0, 10, 20, …)

    Mutually exclusive: pass either find_loc OR ll, not both.
    If both are absent, SerpApi uses its own geolocation — prefer find_loc.
    """
    client = _get_client()
    tool_id = "serpapi_yelp.search"

    find_desc = str(payload.get("find_desc") or "").strip()
    if not find_desc:
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
            error="Missing required parameter: find_desc",
            receipt_data=receipt,
        )

    # Query length cap — same 500 char limit as Home Depot adapter (security parity).
    if len(find_desc) > 500:
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
            error="find_desc exceeds 500 character limit",
            receipt_data=receipt,
        )

    # --- Dual-account budget gate (shared pool with Home Depot) ---
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
        receipt["redacted_outputs"] = {
            "engine": "yelp",
            "account_id": None,
            "cached": False,
            "budget_remaining_a": 0,
            "budget_remaining_b": 0,
            "query_normalized": find_desc[:100],
        }
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
            receipt["redacted_outputs"] = {
                "engine": "yelp",
                "account_id": None,
                "cached": False,
                "budget_remaining_a": 0,
                "budget_remaining_b": 0,
                "query_normalized": find_desc[:100],
            }
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
        receipt["redacted_outputs"] = {
            "engine": "yelp",
            "account_id": account_id,
            "cached": False,
            "budget_remaining_a": max(0, 240 - _counts.get("A", 0)),
            "budget_remaining_b": max(0, 240 - _counts.get("B", 0)),
            "query_normalized": find_desc[:100],
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi account {account_id} key not configured",
            receipt_data=receipt,
        )

    # Build Yelp query params
    query_params: dict[str, str] = {
        "engine": "yelp",
        "find_desc": find_desc,
        "api_key": api_key,
        "no_cache": "false",
    }

    find_loc = str(payload.get("find_loc") or "").strip()
    ll = str(payload.get("ll") or "").strip()
    if find_loc:
        query_params["find_loc"] = find_loc
    elif ll:
        query_params["ll"] = ll

    start = payload.get("start")
    if isinstance(start, int) and start > 0:
        query_params["start"] = str(start)

    request = ProviderRequest(
        method="GET",
        path="/search",
        query_params=query_params,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    try:
        response = await asyncio.wait_for(
            client._request(request), timeout=timeout
        )
    except asyncio.TimeoutError:
        _counts_post = current_counts()
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
        receipt["redacted_outputs"] = {
            "engine": "yelp",
            "account_id": account_id,
            "cached": False,
            "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
            "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
            "query_normalized": find_desc[:100],
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi Yelp timeout after {timeout}s",
            receipt_data=receipt,
        )

    # --- 429 / quota-body exhaustion detection (mirrors HD adapter pattern) ---
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
            "engine": "yelp",
            "account_id": account_id,
            "cached": False,
            "budget_remaining_a": max(0, 240 - _pre_counts.get("A", 0)),
            "budget_remaining_b": max(0, 240 - _pre_counts.get("B", 0)),
            "query_normalized": find_desc[:100],
            "http_status": response.status_code,
        }
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([rate_receipt])
        except Exception:
            pass
        mark_account_exhausted(account_id, reason=f"HTTP {response.status_code}")
        # Single failover attempt (Law #1: no autonomous retry loops)
        other_account = "B" if account_id == "A" else "A"
        try:
            other_key = get_api_key(other_account)
        except (ValueError, KeyError):
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
                "engine": "yelp",
                "account_id": None,
                "cached": False,
                "budget_remaining_a": max(0, 240 - _counts_post.get("A", 0)),
                "budget_remaining_b": max(0, 240 - _counts_post.get("B", 0)),
                "query_normalized": find_desc[:100],
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

    _post_counts = current_counts()
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
    receipt["redacted_outputs"] = {
        "engine": "yelp",
        "account_id": account_id,
        "cached": False,
        "budget_remaining_a": max(0, 240 - _post_counts.get("A", 0)),
        "budget_remaining_b": max(0, 240 - _post_counts.get("B", 0)),
        "query_normalized": find_desc[:100],
        "find_loc": find_loc or ll,
    }

    if response.success:
        raw_results = response.body.get("organic_results") or response.body.get("results") or []
        suppliers = [
            _normalize_business(biz, idx)
            for idx, biz in enumerate(raw_results)
            if isinstance(biz, dict)
        ]
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "suppliers": suppliers,
                "result_count": len(suppliers),
                "query": find_desc,
                "find_loc": find_loc or ll or "",
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=response.error_message or f"SerpApi Yelp error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
