"""Apify Zillow Provider Client — Property photo subsystem (Phase 3 — Visuals tab).

Provider: Apify Zillow Detail Scraper (https://apify.com/maxcopell/zillow-detail-scraper)
Auth: Apify API token (Bearer header)
Risk tier: GREEN (read-only photo enrichment)
Idempotency: N/A (read-only)

Adam owns property research. ATTOM provides facts (sqft/year/zoning/owner/sales).
This client provides PHOTOS ONLY (interior/exterior/roof) via Apify's Zillow scraper.

Endpoint: POST /v2/acts/maxcopell~zillow-detail-scraper/run-sync-get-dataset-items
Body:    { "addresses": [<address>], "propertyStatus": "FOR_SALE" }
Response: list[dict] — one entry per address with responsivePhotos / photos arrays

Cold-start: Apify actors can take 10s+ to start. Phase B-1 (2026-05-11):
HTTP client timeout is 20s now that the n8n warmer cron ("Adam Apify Zillow
Warmer", */4 * * * *) keeps the actor container hot. 20s comfortably covers
a warm-path round-trip; deeper cold-starts are handled by per-call retries
(resilient_call + RetryPolicy APIFY_RETRY) and the apify_breaker, not by
stretching this per-call timeout.
Free plan: ~1,388 lookups/month at $5/mo (talismatic_labyrinth user).

Fail-closed (Law #3): Apify failure does NOT block ATTOM facts. The playbook
caller catches via asyncio.gather(return_exceptions=True) and degrades to
photos=[] gracefully.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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

# Apify actor identifier for the Zillow detail scraper.
# `~` separates user from actor name in Apify URL paths.
_ZILLOW_ACTOR_PATH = "/acts/maxcopell~zillow-detail-scraper/run-sync-get-dataset-items"

# Per-call wallclock for run-sync-get-dataset-items. Set higher than the
# normal 5-8s provider timeout because Apify actors cold-start in 10s+.
# Also passed as `timeout` query param so Apify itself bounds the run.
_APIFY_RUN_TIMEOUT_SECONDS = 120


class ApifyZillowClient(BaseProviderClient):
    """Apify Zillow Detail Scraper API client (photos only)."""

    provider_id = "apify_zillow"
    base_url = "https://api.apify.com/v2"
    # Phase B-1 (2026-05-11): lowered from 90.0 -> 20.0.
    # Rationale: a 90s HTTP wallclock undermined the playbook outer wrapper
    # (28s after B-1) — a single Apify cold-start could discard 12
    # already-successful ATTOM responses with PLAYBOOK_TIMEOUT. The n8n
    # warmer cron keeps the actor container hot so warm-path p95 ~2-4s and
    # 20s covers it comfortably. Cold-start scenarios are now handled by
    # the resilient_call wrapper (RetryPolicy=APIFY_RETRY) + apify_breaker
    # at the playbook layer (Phase B-2) — not by stretching this timeout.
    # In-client retries removed (max_retries=0); the resilient_call wrapper
    # owns retry policy so the breaker observes every attempt and the
    # retry budget is bounded across the whole playbook, not duplicated
    # at the client layer.
    # NB: _APIFY_RUN_TIMEOUT_SECONDS (Apify actor server-side cap, still
    # 120s) is intentionally not lowered in B-1 — Phase B-2 will tighten
    # it to ~18s alongside the playbook refactor that consumes the breaker.
    timeout_seconds = 20.0
    max_retries = 0
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.apify_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Apify API key not configured (ASPIRE_APIFY_API_KEY)",
                provider_id=self.provider_id,
            )
        return {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def _parse_response(self, raw_body: bytes) -> dict[str, Any]:
        """Apify returns a JSON ARRAY (dataset items). Wrap into a dict so
        ProviderResponse.body's dict[str, Any] contract is preserved.

        Shape: {"items": [<scraped property dict>, ...]}
        """
        if not raw_body:
            return {"items": []}
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            return {"raw": raw_body.decode("utf-8", errors="replace")[:500]}
        if isinstance(parsed, list):
            return {"items": parsed}
        if isinstance(parsed, dict):
            # Some Apify error envelopes are dicts (e.g. {"error": {...}}).
            return parsed
        return {"items": []}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.AUTH_FORBIDDEN
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


_client: ApifyZillowClient | None = None


def _get_client() -> ApifyZillowClient:
    global _client
    if _client is None:
        _client = ApifyZillowClient()
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def execute_apify_zillow_photos(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Call Apify Zillow scraper for an address. Photos-only output.

    Required payload:
      - address: str — full street address (street + city + state, ZIP optional)

    Optional payload:
      - property_status: str — "FOR_SALE" (default) | "FOR_RENT" | "SOLD"

    Returns: ToolExecutionResult wrapping ZillowPhotoResult:
        {
          "status": "ok" | "partial" | "missing" | "api_failure",
          "photos": [{"url": str, "caption": Optional[str], "lane": str}],
          "listing_url": Optional[str],
          "fetched_at": str (ISO8601 Z),
        }

    Photo lane categorization is performed via zillow_photo_normalizer.

    Receipt is emitted for every outcome (Law #2).
    """
    tool_id = "apify_zillow.photos"
    client = _get_client()

    address = (payload.get("address") or "").strip()
    if not address:
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
            error="Missing required parameter: address",
            receipt_data=receipt,
        )

    # Per Apify actor docs: "If [propertyStatus] is provided incorrectly,
    # the Actor will figure out the correct status and use it." Defaulting
    # to FOR_SALE excludes commercial/industrial properties (typically
    # RECENTLY_SOLD or off-market) which is Aspire's primary ICP. We only
    # pass propertyStatus when explicitly requested; otherwise the actor
    # auto-detects across FOR_SALE / RECENTLY_SOLD / FOR_RENT.
    property_status = payload.get("property_status")

    # Apify run-sync-get-dataset-items query params:
    #   timeout=<seconds>  → cap actor wallclock server-side
    query_params = {"timeout": str(_APIFY_RUN_TIMEOUT_SECONDS)}

    body: dict[str, Any] = {
        "addresses": [address],
    }
    if property_status:
        body["propertyStatus"] = property_status.upper()

    response: ProviderResponse = await client._request(
        ProviderRequest(
            method="POST",
            path=_ZILLOW_ACTOR_PATH,
            body=body,
            query_params=query_params,
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

    if not response.success:
        # Never include the API key in error messages (Law #9).
        safe_msg = response.error_message or f"Apify HTTP {response.status_code}"
        logger.warning(
            "Apify Zillow scrape failed: status=%d code=%s",
            response.status_code,
            response.error_code.value if response.error_code else "?",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"Apify Zillow API error: {safe_msg}",
            data={
                "status": "api_failure",
                "photos": [],
                "listing_url": None,
                "fetched_at": _now_iso(),
            },
            receipt_data=receipt,
        )

    # Normalize photos. Import inline to avoid circular imports at module load.
    from aspire_orchestrator.services.adam.normalizers.zillow_photo_normalizer import (
        normalize_apify_photos,
    )

    items = response.body.get("items", []) if isinstance(response.body, dict) else []
    photos = normalize_apify_photos(items)

    # Best-effort listing URL extraction from first item.
    listing_url: str | None = None
    if items and isinstance(items[0], dict):
        first = items[0]
        # Apify Zillow scraper field names vary by actor version. Cover the
        # common variants without throwing on missing keys.
        listing_url = (
            first.get("hdpUrl")
            or first.get("url")
            or first.get("detailUrl")
            or None
        )
        if isinstance(listing_url, str) and listing_url.startswith("/"):
            # Zillow returns relative paths sometimes; prefix with origin.
            listing_url = f"https://www.zillow.com{listing_url}"

    if not items:
        status = "missing"
    elif not photos:
        status = "partial"  # Item returned but no photos extracted.
    else:
        status = "ok"

    data = {
        "status": status,
        "photos": photos,
        "listing_url": listing_url,
        "fetched_at": _now_iso(),
    }

    logger.info(
        "Apify Zillow scrape: status=%s photos=%d address_len=%d",
        status, len(photos), len(address),
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data=data,
        receipt_data=receipt,
    )
