"""SerpApi Google Local Services Ads Provider Client — Google-vetted contractor discovery.

Provider: SerpApi (https://serpapi.com) — google_local_services engine
Auth: Query parameter api_key (via dual-account budget gate)
Risk tier: GREEN (read-only search)
Idempotency: N/A (read-only)

Why this provider: Google Local Services Ads contractors have passed Google's
background check and license verification. The "Google Guaranteed" / "Google
Screened" badge is a strong signal for subcontractor vetting in the Drew
PROCURE stage. LSA data complements google_local results (which may include
unverified listings).

Budget note: Shares the same dual-account budget pool (A+B, 240 cap each).

Normalised output shape (one entry per LSA result):
  {
    "id":             str  — position-based stable ID
    "name":           str  — Business display name
    "google_vetted":  bool — True if badge present (Google Guaranteed or Screened)
    "phone":          str  — Business phone (public retail data — OK per Law #9)
    "rating":         float | None
    "review_count":   int
    "hours":          str  — Hours summary (e.g. "Open 24 hours")
    "years_in_business": int | None
  }

Tools:
  - serpapi_local_service_ads.search: Search Google LSA via SerpApi google_local_services engine
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


class SerpApiLocalServiceAdsClient(BaseProviderClient):
    """SerpApi google_local_services engine client — Google-vetted contractor results."""

    provider_id = "serpapi_local_service_ads"
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


_client: SerpApiLocalServiceAdsClient | None = None


def _get_client() -> SerpApiLocalServiceAdsClient:
    global _client
    if _client is None:
        _client = SerpApiLocalServiceAdsClient()
    return _client


def _normalize_lsa_result(result: dict[str, Any], position: int) -> dict[str, Any]:
    """Normalise a single google_local_services result to the adapter contract shape.

    SerpApi google_local_services result keys (verified against engine docs):
      position, name, rating, reviews, phone, badge (google_guaranteed/google_screened),
      years_in_business, hours, thumbnail, serpapi_link
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

    # Google vetting — badge may be a string ("google_guaranteed", "google_screened")
    # or a dict. Any non-empty badge value means vetted.
    badge = result.get("badge") or result.get("google_guarantee") or result.get("verified")
    google_vetted = bool(badge)

    raw_years = result.get("years_in_business")
    years_in_business: int | None = None
    if isinstance(raw_years, int):
        years_in_business = raw_years
    elif isinstance(raw_years, str):
        try:
            years_in_business = int(raw_years.split()[0])
        except (ValueError, IndexError):
            pass

    hours_obj = result.get("hours") or result.get("opening_hours") or ""
    if isinstance(hours_obj, dict):
        hours = str(hours_obj.get("summary") or hours_obj.get("status") or "")
    else:
        hours = str(hours_obj).strip()

    biz_id = str(result.get("place_id") or f"lsa_{position}").strip()

    return {
        "id": biz_id,
        "name": str(result.get("name") or "").strip(),
        "google_vetted": google_vetted,
        "phone": str(result.get("phone") or "").strip(),
        "rating": rating,
        "review_count": review_count,
        "hours": hours,
        "years_in_business": years_in_business,
    }


async def execute_serpapi_local_service_ads_search(
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
    """Execute serpapi_local_service_ads.search — Google LSA via SerpApi google_local_services.

    Required payload:
      - service: str — Service type (e.g. "electrician", "plumber", "HVAC")

    Optional payload:
      - location: str — Location string (city, state, ZIP)
      - hl: str — Language code, default "en"
      - gl: str — Country code, default "us"
    """
    client = _get_client()
    tool_id = "serpapi_local_service_ads.search"

    service = str(payload.get("service") or "").strip()
    if not service:
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
            error="Missing required parameter: service",
            receipt_data=receipt,
        )

    if len(service) > 200:
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
            error="service exceeds 200 character limit",
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
            "engine": "google_local_services",
            "account_id": None,
            "service_normalized": service[:100],
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
                "engine": "google_local_services",
                "account_id": None,
                "service_normalized": service[:100],
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
            "engine": "google_local_services",
            "account_id": account_id,
            "service_normalized": service[:100],
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi account {account_id} key not configured",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {
        "engine": "google_local_services",
        "q": service,
        "api_key": api_key,
        "no_cache": "false",
        "hl": str(payload.get("hl") or "en"),
        "gl": str(payload.get("gl") or "us"),
    }

    location = str(payload.get("location") or "").strip()
    if location:
        query_params["location"] = location

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
            "engine": "google_local_services",
            "account_id": account_id,
            "service_normalized": service[:100],
        }
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"SerpApi google_local_services timeout after {timeout}s",
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
        "engine": "google_local_services",
        "account_id": account_id,
        "budget_remaining_a": max(0, 240 - _post_counts.get("A", 0)),
        "budget_remaining_b": max(0, 240 - _post_counts.get("B", 0)),
        "service_normalized": service[:100],
        "location": location,
    }

    if response.success:
        raw_results = (
            response.body.get("local_ads")
            or response.body.get("local_results")
            or response.body.get("results")
            or []
        )
        contractors = [
            _normalize_lsa_result(r, idx)
            for idx, r in enumerate(raw_results)
            if isinstance(r, dict)
        ]
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "contractors": contractors,
                "result_count": len(contractors),
                "service": service,
                "location": location,
            },
            receipt_data=receipt,
        )

    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=response.error_message or f"SerpApi google_local_services error: HTTP {response.status_code}",
        receipt_data=receipt,
    )
