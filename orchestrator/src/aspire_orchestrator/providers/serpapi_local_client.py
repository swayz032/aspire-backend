"""SerpApi Google Local Provider Client — Google Maps local pack results for subcontractor discovery.

Provider: SerpApi (https://serpapi.com) — google_local engine
Auth: Query parameter api_key (via dual-account budget gate)
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Budget note: Shares the same dual-account budget pool (A+B, 240 cap each).
  select_account() → try_increment() → get_api_key(). On HTTP 429 / quota body
  error, mark_account_exhausted() forces the account to cap, then the other
  account is tried once. Adapter never retries autonomously — orchestrator
  owns retry logic (Law #1).

Normalised output shape (one entry per Google local business):
  {
    "id":           str  — position-based stable ID
    "name":         str  — Business display name
    "address":      str  — Full address string
    "phone":        str  — Business phone (public retail data — OK per Law #9)
    "rating":       float | None
    "review_count": int
    "website":      str  — Business website URL
    "gbp_url":      str  — Google Business Profile URL (serpapi_link)
    "type":         str  — Primary category/type
  }

Tools:
  - serpapi_local.search: Search Google Maps local pack via SerpApi google_local engine
"""

from __future__ import annotations

import asyncio
import logging
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


class SerpApiLocalClient(BaseProviderClient):
    """SerpApi google_local engine client — Google Maps local pack results."""

    provider_id = "serpapi_local"
    base_url = "https://serpapi.com"
    timeout_seconds = 5.0
    max_retries = 0  # Adapter never retries; orchestrator owns retry logic (Law #1)
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


_client: SerpApiLocalClient | None = None


def _get_client() -> SerpApiLocalClient:
    global _client
    if _client is None:
        _client = SerpApiLocalClient()
    return _client


def _normalize_local_result(result: dict[str, Any], position: int) -> dict[str, Any]:
    """Normalise a single google_local result to the adapter contract shape.

    SerpApi google_local result keys (verified against engine docs):
      position, title, place_id, data_id, reviews, address, phone,
      type, types, website, thumbnail, serpapi_link, gps_coordinates
    """
    raw_rating = result.get("rating")
    rating: float | None = None
    if isinstance(raw_rating, (int, float)):
        rating = float(raw_rating)
    elif isinstance(raw_rating, str):
        try:
            rating = float(raw_rating)
        except ValueError:
            pass

    raw_reviews = result.get("reviews")
    review_count: int = 0
    if isinstance(raw_reviews, int):
        review_count = raw_reviews
    elif isinstance(raw_reviews, str):
        cleaned = raw_reviews.replace(",", "").strip()
        try:
            review_count = int(cleaned)
        except ValueError:
            pass

    place_id = str(result.get("place_id") or result.get("data_id") or f"local_{position}").strip()

    return {
        "id": place_id,
        "name": str(result.get("title") or "").strip(),
        "address": str(result.get("address") or "").strip(),
        "phone": str(result.get("phone") or "").strip(),
        "rating": rating,
        "review_count": review_count,
        "website": str(result.get("website") or "").strip(),
        "gbp_url": str(result.get("serpapi_link") or "").strip(),
        "type": str(result.get("type") or "").strip(),
    }


async def execute_serpapi_local_search(
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
    """Execute serpapi_local.search — Google Maps local pack via SerpApi google_local engine.

    Required payload:
      - query: str — Search query (e.g. "licensed electrician", "HVAC contractor")

    Optional payload:
      - location: str — Location string (city, state, ZIP). Defaults to SerpApi geolocation.
      - type_hint: str | None — Category filter passed as 'type' param (e.g. "electrician")
      - hl: str — Language code, default "en"
      - gl: str — Country code, default "us"
    """
    client = _get_client()
    tool_id = "serpapi_local.search"

    query = str(payload.get("query") or "").strip()
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

    # --- Dual-account budget gate ---
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
            "engine": "google_local",
            "account_id": None,
            "query_normalized": query[:100],
        }
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
            receipt["redacted_outputs"] = {
                "engine": "google_local",
                "account_id": None,
                "query_normalized": query[:100],
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
            "engine": "google_local",
            "account_id": account_id,
            "query_normalized": query[:100],
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi account {account_id} key not configured",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {
        "engine": "google_local",
        "q": query,
        "api_key": api_key,
        "no_cache": "false",
        "hl": str(payload.get("hl") or "en"),
        "gl": str(payload.get("gl") or "us"),
    }

    location = str(payload.get("location") or "").strip()
    if location:
        query_params["location"] = location

    type_hint = str(payload.get("type_hint") or "").strip()
    if type_hint:
        query_params["type"] = type_hint

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
            "engine": "google_local",
            "account_id": account_id,
            "query_normalized": query[:100],
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi google_local timeout after {timeout}s",
            receipt_data=receipt,
        )

    # 429 / quota-body exhaustion detection
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
        try:
            other_key = get_api_key(other_account)
            if try_increment(other_account):
                query_params["api_key"] = other_key
                request2 = ProviderRequest(
                    method="GET",
                    path="/search",
                    query_params=query_params,
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                )
                try:
                    response = await asyncio.wait_for(
                        client._request(request2), timeout=timeout
                    )
                    account_id = other_account
                except asyncio.TimeoutError:
                    pass
        except (ValueError, KeyError):
            pass

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
        "engine": "google_local",
        "account_id": account_id,
        "budget_remaining_a": max(0, 240 - _post_counts.get("A", 0)),
        "budget_remaining_b": max(0, 240 - _post_counts.get("B", 0)),
        "query_normalized": query[:100],
        "location": location,
    }

    if response.success:
        raw_results = (
            response.body.get("local_results")
            or response.body.get("results")
            or []
        )
        businesses = [
            _normalize_local_result(r, idx)
            for idx, r in enumerate(raw_results)
            if isinstance(r, dict)
        ]
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "businesses": businesses,
                "result_count": len(businesses),
                "query": query,
                "location": location,
            },
            receipt_data=receipt,
        )

    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=response.error_message or f"SerpApi google_local error: HTTP {response.status_code}",
        receipt_data=receipt,
    )
