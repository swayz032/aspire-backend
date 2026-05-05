"""ATTOM Property Intelligence Provider Client — U.S. property data subsystem (ADR-002).

Provider: ATTOM Data Solutions (https://api.gateway.attomdata.com)
Auth: API key (apikey header)
Risk tier: GREEN (read-only property data)
Idempotency: N/A (read-only)

ATTOM is a subsystem, not a thin adapter. It provides:
  - Property detail, snapshot, detail with schools
  - Sales history (detail + expanded)
  - Sales comparables, sales trends
  - AVM (Automated Valuation Model), Rental AVM
  - School/district context, boundary/geography

Entitlement gating: Each endpoint family may require separate entitlements.
Fail-closed: Missing entitlement raises ENTITLEMENT_MISSING (Law #3).
Address normalization: HERE geocoding BEFORE querying ATTOM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


class AttomClient(BaseProviderClient):
    """ATTOM Property Data API client."""

    provider_id = "attom"
    # Gateway root only — the api_root prefix (defaulting to /propertyapi/v1.0.0)
    # is prepended in _attom_request so we can reach Property API v1, Transaction
    # API V3, Area API V4, Community/Location V4, and any other ATTOM surface
    # from the same client instance.
    base_url = "https://api.gateway.attomdata.com"
    timeout_seconds = 8.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.attom_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="ATTOM API key not configured (ASPIRE_ATTOM_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"apikey": api_key, "Accept": "application/json"}

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


_client: AttomClient | None = None


def _get_client() -> AttomClient:
    global _client
    if _client is None:
        _client = AttomClient()
    return _client


def _build_receipt(
    client: AttomClient,
    tool_id: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    outcome: Outcome,
    reason: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
    response: ProviderResponse | None = None,
) -> dict[str, Any]:
    return client.make_receipt_data(
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


async def _attom_request(
    *,
    path: str,
    query_params: dict[str, str],
    tool_id: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
    api_root: str = "/propertyapi/v1.0.0",
    timeout_seconds: float = 8.0,
    cache_ttl_seconds: int | None = 86400,
) -> ToolExecutionResult:
    """Shared ATTOM request handler for all endpoint families.

    api_root selects which ATTOM API surface the request hits. Defaults to
    the Property API v1 (the bulk of our existing wrappers use this). Pass
    "/property/v3" for Transaction V3 endpoints (preforeclosuredetails),
    "/v4" for Area / Community / Location lookup endpoints, "" for absolute
    paths. Final URL is `https://api.gateway.attomdata.com{api_root}{path}`.

    timeout_seconds caps the wallclock per call so one slow ATTOM endpoint
    cannot drag the whole property pull. Defaults to 8s (covers the 95th
    percentile of ATTOM latency observed in production).

    cache_ttl_seconds enables a process-local response cache. ATTOM property
    data updates daily-ish, so a 24h TTL on (path, params) means repeat pulls
    of the same address are sub-second. None = no caching for this call.
    Defaults to None — callers opt in via wrappers.
    """
    client = _get_client()
    full_path = f"{api_root}{path}"

    # Cache lookup BEFORE the network call. Caching is process-local and
    # tenant-shared (ATTOM data is public-records, not tenant-scoped) so the
    # tenant_id passed to cache_get is a fixed sentinel "attom_shared".
    if cache_ttl_seconds and cache_ttl_seconds > 0:
        try:
            from aspire_orchestrator.services.adam.cache import cache_get
            cached = cache_get(
                tenant_id="attom_shared",
                provider="attom",
                playbook=tool_id,
                query=full_path,
                params=query_params,
            )
            if cached is not None:
                logger.debug("ATTOM cache hit: %s %s", tool_id, full_path)
                # Re-emit a fresh receipt for the cached call so receipt
                # coverage stays at 100% (Law #2). The receipt is marked
                # cached=True so dashboards can see hit-rate.
                receipt = _build_receipt(
                    client, tool_id, correlation_id, suite_id, office_id,
                    Outcome.SUCCESS, "EXECUTED_FROM_CACHE",
                    capability_token_id=capability_token_id,
                    capability_token_hash=capability_token_hash,
                )
                return ToolExecutionResult(
                    outcome=Outcome.SUCCESS,
                    tool_id=tool_id,
                    data=cached,
                    receipt_data=receipt,
                )
        except Exception as exc:  # noqa: BLE001 — cache failures are never fatal
            logger.debug("ATTOM cache lookup failed (continuing without): %s", exc)

    request = ProviderRequest(
        method="GET",
        path=full_path,
        query_params=query_params,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    try:
        response = await asyncio.wait_for(
            client._request(request),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "ATTOM %s timed out after %.1fs (path=%s)",
            tool_id, timeout_seconds, full_path,
        )
        timeout_receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "TIMEOUT",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=f"ATTOM call timed out after {timeout_seconds:.1f}s",
            receipt_data=timeout_receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = _build_receipt(
        client, tool_id, correlation_id, suite_id, office_id,
        outcome, reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        response=response,
    )

    # Parse the ATTOM custom status code from the response body. ATTOM
    # uses HTTP 400 even for some success-with-empty cases — the
    # authoritative signal is the "code" / "msg" on the body.status block
    # (different envelope shape per API surface).
    body = response.body or {}
    custom_code: int | None = None
    custom_msg = ""
    if isinstance(body, dict):
        # Property API v1: body.status
        # Area API v4: body.response.result.status
        # Transaction V3 (preforeclosure): body.status (same as v1)
        status_obj = (
            body.get("status")
            or (body.get("response", {}) if isinstance(body.get("response"), dict) else {}).get("status")
            or (body.get("Response", {}) if isinstance(body.get("Response"), dict) else {}).get("status")
            or {}
        )
        if isinstance(status_obj, dict):
            try:
                custom_code = int(status_obj.get("code")) if status_obj.get("code") is not None else None
            except (TypeError, ValueError):
                custom_code = None
            custom_msg = str(status_obj.get("msg") or "")

    # Per ATTOM docs:
    #   code  0  SuccessWithResult       → success
    #   code  1  SuccessWithoutResult    → success-with-empty (not error!)
    #   code -4  Invalid Parameter Combination
    #   code -5  Invalid Parameter
    #   code -6  Missing Required Parameter
    #   code -8  Either GeoID or GeoIDV4 Value Required (mutually exclusive)
    #   code 10  Invalid Date Format    (must be YYYY/MM/DD)
    #   code 11  Invalid Date Range
    #   code 12  Sort Value Exceeded
    #   code 13  Invalid Sort Value
    #   code 15  Invalid AVM Value Range
    SUCCESS_EMPTY_MSGS = {"SuccessWithoutResult", "Success", ""}

    if response.success and (custom_code in (None, 0) or custom_code == 0):
        # Cache the successful response so subsequent pulls of the same
        # address are sub-second. ATTOM data updates daily-ish; 24h TTL is safe.
        if cache_ttl_seconds and cache_ttl_seconds > 0 and response.body is not None:
            try:
                from aspire_orchestrator.services.adam.cache import cache_set
                cache_set(
                    tenant_id="attom_shared",
                    provider="attom",
                    playbook=tool_id,
                    query=full_path,
                    params=query_params,
                    value=response.body,
                    ttl_override=cache_ttl_seconds,
                )
            except Exception as exc:  # noqa: BLE001 — cache failures are never fatal
                logger.debug("ATTOM cache write failed (continuing): %s", exc)
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data=response.body,
            receipt_data=receipt,
        )

    # SuccessWithoutResult shows up across several envelope shapes — treat
    # as success-with-empty so downstream code can branch on `total=0`
    # rather than `outcome=FAILED`.
    if custom_code == 1 or custom_msg == "SuccessWithoutResult":
        logger.info(
            "ATTOM %s: SuccessWithoutResult (no records for inputs)",
            tool_id,
        )
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data=response.body if isinstance(response.body, dict) else {"property": []},
            receipt_data=receipt,
        )

    # Map the ATTOM custom error codes to a clear log line so debugging
    # production failures is no longer guesswork.
    code_to_label = {
        -4: "INVALID_PARAM_COMBINATION",
        -5: "INVALID_PARAM",
        -6: "MISSING_REQUIRED_PARAM",
        -8: "GEOID_GEOIDV4_CONFLICT",
        10: "INVALID_DATE_FORMAT_YYYY_SLASH_MM_SLASH_DD",
        11: "INVALID_DATE_RANGE",
        12: "SORT_VALUE_EXCEEDED",
        13: "INVALID_SORT_VALUE",
        15: "INVALID_AVM_VALUE_RANGE",
    }
    error_label = code_to_label.get(custom_code or 0, "")
    error_detail = ""
    if custom_msg:
        error_detail = custom_msg
    if error_label:
        logger.warning(
            "ATTOM %s failed: code=%s label=%s msg=%r http=%s",
            tool_id, custom_code, error_label, custom_msg, response.status_code,
        )
    elif custom_msg or response.status_code >= 400:
        logger.warning(
            "ATTOM %s failed: code=%s msg=%r http=%s",
            tool_id, custom_code, custom_msg, response.status_code,
        )

    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=(
            response.error_message
            or error_detail
            or f"ATTOM API error: HTTP {response.status_code} (code={custom_code})"
        ),
        receipt_data=receipt,
    )


# Unit suffix detection: APT 4802 / UNIT 12 / STE 200 / #B  (case-insensitive)
# Captures the leading marker word + the unit token. Trailing chars after the
# unit token (e.g. comma, end-of-string) are the boundary.
_UNIT_RE = re.compile(
    # Anchor on start-of-string, whitespace, or comma so the marker is its own
    # token. Cannot use \b before "#" — both whitespace and "#" are non-word
    # characters so \b never matches that transition (e.g. "789 Elm St #B-7"
    # would silently fail without this anchor).
    r"(?:^|[\s,])(?:APT|APARTMENT|UNIT|STE|SUITE|#)\s*([A-Za-z0-9\-]+)\s*$",
    re.IGNORECASE,
)


def _extract_unit_number(address1: str) -> tuple[str, str]:
    """Return (address1_without_unit, unit_token) for APT/UNIT/STE/# suffixes.

    HISTORY: This helper used to split unit suffixes off address1 into a
    separate `unitnumber` query parameter on the assumption that ATTOM's
    unit-aware endpoints required it. Live testing on 2026-05-04 against
    /property/expandedprofile, /valuation/homeequity, /assessment/detail,
    /sale/detail, and /saleshistory/expandedhistory showed ATTOM REJECTS
    `unitnumber` with HTTP 400 "Invalid Parameter(s) in Request -
    UNITNUMBER". Including the unit IN address1 (e.g. "1575 Paul Russell
    Rd APT 4802") resolves successfully on every endpoint and returns the
    correct unit-level record (different attomId from the master parcel).
    Caller logic that consumes the (cleaned, unit) tuple is preserved for
    backward compatibility, but production code paths in _validate_address
    no longer apply the split — they pass address1 unmodified to ATTOM.
    """
    if not isinstance(address1, str) or not address1.strip():
        return address1, ""
    match = _UNIT_RE.search(address1)
    if not match:
        return address1, ""
    unit = match.group(1).strip()
    cleaned = address1[: match.start()].rstrip(" ,")
    return cleaned, unit


def _validate_address(payload: dict[str, Any], client: AttomClient, tool_id: str,
                       correlation_id: str, suite_id: str, office_id: str,
                       capability_token_id: str | None = None,
                       capability_token_hash: str | None = None) -> ToolExecutionResult | dict[str, str]:
    """Validate and extract address params. Returns query_params dict or ToolExecutionResult on error.

    Accepts (in order of precedence):
      1. attomid (direct ATTOM ID lookup)
      2. apn + fips (parcel ID lookup)
      3. address1 + address2 (pre-parsed by parse_us_address — deterministic primary path)
      4. address (free-form string — legacy regex split, kept for callers that
         haven't migrated to address_parser)

    Unit-suffixed addresses (`APT 4802`, `STE 200`, `#B`) are split into a
    cleaned `address1` plus a separate `unitnumber` query parameter so the
    ATTOM endpoint can resolve to the unit-level record instead of the master
    parcel.
    """
    pre_address1 = payload.get("address1", "")
    pre_address2 = payload.get("address2", "")
    address = payload.get("address", "")

    if not (payload.get("attomid") or (payload.get("apn") and payload.get("fips"))
            or (pre_address1 and pre_address2) or address):
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: address or address1+address2 (fail-closed per Law #3)",
            receipt_data=receipt,
        )

    params: dict[str, str] = {}
    # Priority 1-2: direct identifiers
    if payload.get("attomid"):
        params["attomid"] = str(payload["attomid"])
    elif payload.get("apn") and payload.get("fips"):
        params["apn"] = payload["apn"]
        params["fips"] = payload["fips"]
    # Priority 3: pre-parsed address1+address2 (deterministic primary path)
    elif pre_address1 and pre_address2:
        params["address1"] = pre_address1.strip()
        params["address2"] = pre_address2.strip()
        # NOTE: Do NOT split APT/UNIT/STE off into a separate `unitnumber`
        # query param. Live ATTOM testing (2026-05-04) showed unitnumber
        # is REJECTED as an invalid parameter on every property endpoint
        # we use — full address1 including the unit (e.g.
        # "1575 Paul Russell Rd APT 4802") resolves to the correct
        # unit-level record. The previous split behavior caused HTTP 400
        # INPUT_INVALID_FORMAT for every condo/apt/townhouse query —
        # surfacing as the May 4 user-reported "fallback empty card."
    else:
        # Parse address into ATTOM format: address1=street, address2=City, ST ZIP
        parts = address.split(",")
        if len(parts) >= 2:
            params["address1"] = parts[0].strip()
            params["address2"] = ",".join(parts[1:]).strip()
        else:
            # No comma — try to split "123 Main St Lexington KY 40509" intelligently
            # Look for state abbreviation pattern to split street from city/state/zip
            match = re.search(r'\b([A-Z]{2})\s+(\d{5})\b', address)
            if match:
                # Found "KY 40509" — everything before the city is address1
                state_pos = match.start()
                # Walk backwards from state to find city start (after street suffix)
                pre_state = address[:state_pos].rstrip()
                # Split on last word boundary that looks like end of street
                street_suffixes = re.search(
                    r'(.*?\b(?:St|Ave|Rd|Blvd|Dr|Ln|Ct|Way|Pl|Cir|Pkwy|Hwy)\b\.?)\s+(.*)',
                    pre_state, re.IGNORECASE,
                )
                if street_suffixes:
                    params["address1"] = street_suffixes.group(1).strip()
                    city = street_suffixes.group(2).strip()
                    state_zip = address[state_pos:].strip()
                    params["address2"] = f"{city}, {state_zip}"
                else:
                    params["address1"] = pre_state
                    params["address2"] = address[state_pos:].strip()
            else:
                params["address1"] = address
                params["address2"] = ""

        # NOTE: legacy free-form path also leaves the unit IN address1.
        # See pre-parsed branch above for the rationale (ATTOM rejects
        # unitnumber as an invalid parameter — full address1 including
        # APT/UNIT/STE token is what resolves to unit-level records).

    return params


# ---------------------------------------------------------------------------
# Endpoint family wrappers
# ---------------------------------------------------------------------------

async def execute_attom_property_detail(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Property detail — base parcel facts, characteristics, ownership.

    For unit-suffixed addresses (condos/apartments) the request is routed to
    `/property/expandedprofile` instead of `/property/detail`. The plain
    `detail` endpoint is NOT unit-aware and returns master-parcel placeholders
    for unit fields. `expandedprofile` accepts `unitnumber` and returns
    unit-level `livingsize`, `assessed`, `market`, owner, and sale fields.
    """
    tool_id = "attom.property_detail"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    # Unit-aware routing: when unitnumber is present, query the expanded
    # profile endpoint (unit-level) instead of the building-level detail.
    path = "/property/expandedprofile" if result.get("unitnumber") else "/property/detail"

    return await _attom_request(
        path=path,
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_property_snapshot(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Property snapshot — quick scan with key fields."""
    tool_id = "attom.property_snapshot"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    return await _attom_request(
        path="/property/snapshot",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_property_detail_with_schools(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Property detail with schools — full detail + school/district context."""
    tool_id = "attom.property_detail_with_schools"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    # /property/detailwithschools is School API V4 — base path /propertyapi/v4
    # (verified live 2026-05-04). Calling it at v1 base returned silent
    # SuccessWithoutResult on schools.
    return await _attom_request(
        path="/property/detailwithschools",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/propertyapi/v4",
    )


async def execute_attom_detail_mortgage_owner(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Property detail + mortgage + owner — full profile in one call.

    For unit-suffixed addresses, `/property/expandedprofile` is used because
    `/property/detailmortgageowner` is not unit-aware and returns
    building-level placeholders for unit fields.
    """
    tool_id = "attom.detail_mortgage_owner"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    path = (
        "/property/expandedprofile"
        if result.get("unitnumber")
        else "/property/detailmortgageowner"
    )

    return await _attom_request(
        path=path,
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_assessment_detail(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Assessment detail — tax values, market values, annual tax amount."""
    tool_id = "attom.assessment_detail"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/assessment/detail",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_sale_detail(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Sale detail — last sale price, price/sqft, buyer type, arm's length."""
    tool_id = "attom.sale_detail"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/sale/detail",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_home_equity(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Home equity — LTV, available equity, lendable equity, amortized balance."""
    tool_id = "attom.home_equity"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/valuation/homeequity",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_building_permits(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Building permits — permit history, job value, contractors."""
    tool_id = "attom.building_permits"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/property/buildingpermits",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_sale_snapshot_zip(
    *,
    zip_code: str,
    min_price: int = 100000,
    max_price: int = 500000,
    property_type: str = "SFR",
    page_size: int = 10,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Sale snapshot by ZIP — nearby comps workaround (sale/comparables not in entitlement)."""
    tool_id = "attom.sale_snapshot_zip"
    client = _get_client()
    return await _attom_request(
        path="/sale/snapshot",
        query_params={
            "postalcode": zip_code,
            "minsaleamt": str(min_price),
            "maxsaleamt": str(max_price),
            "propertytype": property_type,
            "pagesize": str(page_size),
            "orderby": "saleSearchDate desc",
        },
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_expanded_profile(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Expanded profile — zoning, census, seller name, REO/quit claim flags, attic."""
    tool_id = "attom.expanded_profile"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/property/expandedprofile",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_sales_history(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Sales history — transaction chain for a property."""
    tool_id = "attom.sales_history"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    return await _attom_request(
        path="/saleshistory/detail",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_sales_trends(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Sales trend — macro geo trends by geography (Transaction V4).

    Per ATTOM docs: lives at /v4/transaction/salestrend (NOT
    /salestrend/snapshot). Required params: GeoIdV4 (NOT legacy geoid),
    Interval (yearly/quarterly/monthly), StartYear, EndYear.

    Old wrapper called /salestrend/snapshot at v1 root → 404. Verified
    live 2026-05-04 at /v4/transaction/salestrend.

    Backwards-compat shim: callers passing `postalcode` get a
    ProviderError pointing to the new shape since postalcode is no
    longer supported (Transaction V4 only accepts GeoIdV4).
    """
    tool_id = "attom.sales_trends"

    geo_id_v4 = str(
        payload.get("geoIdV4")
        or payload.get("geo_id_v4")
        or payload.get("geoid")
        or ""
    ).strip()

    if not geo_id_v4:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        msg = (
            "Missing required parameter: geoIdV4. "
            "If you have a ZIP, resolve to geoIdV4 first via "
            "execute_attom_location_lookup(name=ZIP, geography_type='ZI') "
            "or pass an existing geoIdV4 from a prior /property/* call."
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id, error=msg,
            receipt_data=receipt,
        )

    interval = str(payload.get("interval") or "yearly").strip().lower()
    if interval not in {"yearly", "quarterly", "monthly"}:
        interval = "yearly"

    # Default to a 3-year window so callers without explicit dates still
    # get useful data. ATTOM REQUIRES StartYear+EndYear or returns -6.
    from datetime import datetime as _dt
    current_year = _dt.utcnow().year
    start_year = str(payload.get("StartYear") or payload.get("start_year") or (current_year - 3))
    end_year = str(payload.get("EndYear") or payload.get("end_year") or current_year)

    params: dict[str, str] = {
        "GeoIdV4": geo_id_v4,
        "Interval": interval,
        "StartYear": start_year,
        "EndYear": end_year,
    }
    # Optional quarterly/monthly bounds.
    if interval == "quarterly":
        if payload.get("StartQuarter"): params["StartQuarter"] = str(payload["StartQuarter"])
        if payload.get("EndQuarter"): params["EndQuarter"] = str(payload["EndQuarter"])
    elif interval == "monthly":
        if payload.get("StartMonth"): params["StartMonth"] = str(payload["StartMonth"])
        if payload.get("EndMonth"): params["EndMonth"] = str(payload["EndMonth"])
    if payload.get("PropertyType"): params["PropertyType"] = str(payload["PropertyType"])

    return await _attom_request(
        path="/transaction/salestrend",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_valuation_avm(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """AVM (Automated Valuation Model) — estimated property value."""
    tool_id = "attom.valuation_avm"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    # Per ATTOM docs: AVM lives at /attomavm/detail, NOT /valuation/homeequity.
    # The previous wrapper was returning home equity data and labelling it as
    # AVM — silently broken since 2025. /valuation/homeequity is for LTV/equity
    # only; /attomavm/detail returns the cascaded AVM value + confidence score.
    return await _attom_request(
        path="/attomavm/detail",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_rental_avm(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Rental AVM — estimated rent for a property."""
    tool_id = "attom.rental_avm"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    # Per ATTOM docs: rental AVM is at /valuation/rentalavm (NOT /rental).
    # Old path returned 404 silently — every rental valuation lookup since
    # Jan 2025 has been failing. Verified live 2026-05-04.
    return await _attom_request(
        path="/valuation/rentalavm",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_sales_comparables(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Sales comparables — nearby comparable property sales.

    Per ATTOM docs: lives at /property/v2 (not v1). Three URL forms:
      - /salescomparables/propid/{propId}      — by ATTOM ID (preferred)
      - /salescomparables/apn/{apn}/{county}/{state}
      - /salescomparables/address/{street}/{city}/{county}/{state}/{zip}

    Old wrapper called /salescomparables/detail at v1 root → 404 silently.
    Every comp lookup since launch has been failing. Verified live
    2026-05-04: /property/v2/salescomparables/propid/<id> returns full
    comp data in a different envelope (RESPONSE_GROUP top-level).
    """
    tool_id = "attom.sales_comparables"
    client = _get_client()

    # Prefer attomid (propid) when available — cleanest URL, no parsing.
    # Accept all three casing conventions our callers use: attomid (lowercase
    # — this wrapper's own preferred form), attomId (camelCase — what
    # landlord.py and ATTOM docs use), and propid (legacy alias).
    propid = str(
        payload.get("attomid")
        or payload.get("attomId")
        or payload.get("attom_id")
        or payload.get("propid")
        or payload.get("propId")
        or ""
    ).strip()
    apn = str(payload.get("apn") or "").strip()
    county = str(payload.get("county") or "").strip()
    state_code = str(payload.get("state") or "").strip()

    if propid:
        path = f"/salescomparables/propid/{propid}"
        params: dict[str, str] = {}
    elif apn and county and state_code:
        path = f"/salescomparables/apn/{apn}/{county}/{state_code}"
        params = {}
    else:
        # Address-form requires street/city/county/state/zip in path.
        # We don't always have county; fail fast with a clear message
        # so the playbook can resolve attomid first via /property/detail.
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=(
                "salescomparables requires attomid (propid) or apn+county+state. "
                "Resolve attomid via /property/detail first, then re-call comps."
            ),
            receipt_data=receipt,
        )

    if payload.get("miles"):
        params["miles"] = str(payload["miles"])
    if payload.get("minComps"):
        params["minComps"] = str(payload["minComps"])
    if payload.get("maxComps"):
        params["maxComps"] = str(payload["maxComps"])

    return await _attom_request(
        path=path,
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/property/v2",
    )


async def execute_attom_sales_expanded_history(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Sales expanded history — extended transaction history (may be sparse)."""
    tool_id = "attom.sales_expanded_history"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    return await _attom_request(
        path="/saleshistory/expandedhistory",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_school_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """School search — nearby schools and district context."""
    tool_id = "attom.school_search"

    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if not latitude or not longitude:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameters: latitude, longitude",
            receipt_data=receipt,
        )

    params: dict[str, str] = {
        "latitude": str(latitude),
        "longitude": str(longitude),
    }
    if payload.get("radius"):
        params["radius"] = str(payload["radius"])

    # Per ATTOM docs: School V4 lives at /v4/school/search — NOT
    # /school/search at v1 root. Old wrapper silently 404'd. Verified live
    # 2026-05-04: /v4/school/search returns 200 with schools array.
    # Note: /v4/school/search accepts geoIdV4 OR latitude+longitude+radius.
    return await _attom_request(
        path="/school/search",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_boundary_lookup(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Boundary detail — parcel/place geometry for map rendering.

    Per ATTOM docs: Area API V4 lives at /v4/area, the boundary endpoint is
    /boundary/detail (not /boundary/lookup). Accepts geoIdV4 OR legacy
    AreaId, plus optional format=geojson|wkt. Old wrapper hit
    /boundary/lookup at v1 root → 404 silently.
    """
    tool_id = "attom.boundary_detail"

    geo_id_v4 = str(
        payload.get("geoIdV4")
        or payload.get("geo_id_v4")
        or ""
    ).strip()
    legacy_area_id = str(payload.get("AreaId") or payload.get("area_id") or "").strip()
    # Backwards-compat: callers passing geoid + geo_type get the legacy
    # combined form.
    if not geo_id_v4 and not legacy_area_id and payload.get("geoid"):
        legacy_area_id = f"{payload.get('geo_type', 'ZI')}{payload['geoid']}"

    if not geo_id_v4 and not legacy_area_id:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: geoIdV4 or AreaId",
            receipt_data=receipt,
        )

    params: dict[str, str] = {}
    if geo_id_v4:
        params["geoIdV4"] = geo_id_v4
    else:
        params["AreaId"] = legacy_area_id
    params["format"] = str(payload.get("format") or "geojson").lower()

    return await _attom_request(
        path="/boundary/detail",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )


# ─── New wrappers — Transaction V3 + Area/Location V4 (May 4 user audit) ──
# These hit ATTOM API surfaces beyond the Property API v1 we historically
# called. Verified live against the user's account 2026-05-04.
#
#   /property/v3/preforeclosuredetails — Foreclosure / pre-foreclosure
#       returns Default[] (filings, lender, judgment, opening bid) +
#       Auction[] (auctionDate, auctionTime, auctionAddress, courthouse).
#       This is the ATTOM endpoint with ACTUAL auction details.
#
#   /v4/location/lookup — Resolve city/place name to geoIdV4 (the modern
#       geographic identifier ATTOM uses for city-scoped searches).
#       Required: name + geographyTypeAbbreviation (PL=Place, ZI=Zip, etc.)
#
#   /propertyapi/v1.0.0/sale/snapshot?geoIdV4=... — Properties sold in
#       a city/place geography during a date range. The wrapper is
#       distinct from the existing execute_attom_sale_snapshot_zip which
#       takes postalcode.

async def execute_attom_preforeclosure_details(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Foreclosure / pre-foreclosure details for a property.

    Surfaces auction data (auctionDate, auctionTime, opening bid),
    foreclosure filings (lender, trustee, judgment amount, default amount),
    and recording dates. This is the ATTOM Transaction V3 endpoint at
    /property/v3/preforeclosuredetails — separate API root from the v1
    Property endpoints.

    Accepts (in order of precedence):
      1. attomid — direct ATTOM ID lookup
      2. apn + county + state — parcel ID lookup
      3. combined_address — single comma-separated address string
      4. address1 + address2 — pre-parsed pair (assembled into combinedAddress)
    """
    tool_id = "attom.preforeclosure_details"

    params: dict[str, str] = {}
    if payload.get("attomid"):
        params["AttomID"] = str(payload["attomid"])
    elif payload.get("apn") and payload.get("county") and payload.get("state"):
        params["apn"] = str(payload["apn"])
        params["county"] = str(payload["county"])
        params["state"] = str(payload["state"])
    else:
        combined = (payload.get("combined_address") or "").strip()
        if not combined:
            address1 = (payload.get("address1") or "").strip()
            address2 = (payload.get("address2") or "").strip()
            if address1 and address2:
                combined = f"{address1}, {address2}"
            elif payload.get("address"):
                combined = str(payload["address"]).strip()
        if not combined:
            client = _get_client()
            receipt = _build_receipt(
                client, tool_id, correlation_id, suite_id, office_id,
                Outcome.FAILED, "INPUT_MISSING_REQUIRED",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id=tool_id,
                error="Missing required parameter: combined_address, address1+address2, or attomid",
                receipt_data=receipt,
            )
        params["combinedAddress"] = combined

    return await _attom_request(
        path="/preforeclosuredetails",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/property/v3",
    )


async def execute_attom_location_lookup(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Resolve a city/place name to ATTOM's geoIdV4 identifier.

    Required: name (e.g. "Forest Park") + geography_type (PL for Place /
    incorporated city, ZI for ZIP, CO for County, ST for State, etc.).

    Returns a `geographies` list. When the name matches multiple
    locations across states (e.g. "Forest Park" exists in OH, GA, IL),
    callers must filter the response by state from `geographyName`
    (which contains the city + county + state, e.g.
    "Forest Park, Hamilton County, OH").
    """
    tool_id = "attom.location_lookup"

    name = (payload.get("name") or "").strip()
    geography_type = (payload.get("geography_type") or "PL").strip()

    if not name:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: name",
            receipt_data=receipt,
        )

    return await _attom_request(
        path="/location/lookup",
        query_params={"name": name, "geographyTypeAbbreviation": geography_type},
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_sale_snapshot_geoid(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Properties sold within a city/place/zip geography during a date range.

    Required: geoIdV4 (resolved via execute_attom_location_lookup).
    Optional: start_date / end_date (YYYY/MM/DD), pagesize, orderby.

    This is /propertyapi/v1.0.0/sale/snapshot — the same endpoint as
    execute_attom_sale_snapshot_zip uses, but parameterized by geoIdV4
    so we can scan whole cities (or multi-zip places like Atlanta) without
    knowing the ZIP up front.
    """
    tool_id = "attom.sale_snapshot_geoid"

    geo_id_v4 = (payload.get("geoIdV4") or payload.get("geo_id_v4") or "").strip()
    if not geo_id_v4:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: geoIdV4",
            receipt_data=receipt,
        )

    params: dict[str, str] = {"geoIdV4": geo_id_v4}
    if payload.get("start_date"):
        params["startSaleSearchDate"] = str(payload["start_date"])
    if payload.get("end_date"):
        params["endSaleSearchDate"] = str(payload["end_date"])
    pagesize = int(payload.get("pagesize", 50))
    params["pagesize"] = str(min(max(pagesize, 1), 100))
    if payload.get("orderby"):
        params["orderBy"] = str(payload["orderby"])

    return await _attom_request(
        path="/sale/snapshot",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


# ─── Additional high-value ATTOM endpoints (May 4 audit) ──────────────────
# Community demographics, POI, Area lookups, AllEvents snapshot, basic
# property profile + detail-owner, AVM history. All verified live
# 2026-05-04 against the user's account.

async def execute_attom_basic_profile(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Basic property profile — lightweight summary (faster than /detail).

    Use when you only need address, beds, baths, sqft, year built — not
    the full mortgage/owner/transaction stack. Verified 200 live.
    """
    tool_id = "attom.basic_profile"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/property/basicprofile",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_detail_owner(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Property detail + current owner (no mortgage). Lighter than
    detailmortgageowner when only owner identity is needed."""
    tool_id = "attom.detail_owner"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/property/detailowner",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_allevents_snapshot(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Lighter sibling to /allevents/detail — summary of events for a
    property. Use when you don't need the full event payload."""
    tool_id = "attom.allevents_snapshot"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/allevents/snapshot",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_avm_history(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """AVM history — historical AVM values for a property over time."""
    tool_id = "attom.avm_history"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/avmhistory/detail",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_assessment_history(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Historical tax assessments for a property."""
    tool_id = "attom.assessment_history"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/assessmenthistory/detail",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_community_profile(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Community profile — demographics, crime, weather, air quality, income.

    Per ATTOM Community API V4: 600+ attributes per geography (state,
    county, place, county-subdivision, ZCTA, neighborhood). Required:
    geoIdV4. Returns nested community.demographics + community.crime +
    community.weather etc.
    """
    tool_id = "attom.community_profile"
    geo_id_v4 = str(
        payload.get("geoIdV4")
        or payload.get("geo_id_v4")
        or ""
    ).strip()
    if not geo_id_v4:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: geoIdV4",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/neighborhood/community",
        query_params={"geoIdV4": geo_id_v4},
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_poi_search(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Points of Interest near an address or geography.

    Per ATTOM POI API V4: address OR latitude+longitude OR point WKT.
    Optional category/lineOfBusiness/industry filters. ~15M
    establishments. Useful for "what's near this property" enrichment.
    """
    tool_id = "attom.poi_search"
    client = _get_client()
    params: dict[str, str] = {}

    address = (payload.get("address") or "").strip()
    lat = payload.get("latitude")
    lng = payload.get("longitude")
    point = (payload.get("point") or "").strip()
    if address:
        params["address"] = address
    elif point:
        params["point"] = point
    elif lat is not None and lng is not None:
        params["point"] = f"POINT({lng},{lat})"
    else:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: address, point, or latitude+longitude",
            receipt_data=receipt,
        )

    # Per ATTOM POI V4 docs: default recordLimit=20, default search radius
    # 5 sq mi. Both produce a stunted POI list for property cards. We default
    # to recordLimit=50 inside a 5-mile radius so the desktop POI section
    # shows enough variety to be useful.
    params["radius"] = str(payload.get("radius") or 5)
    params["recordLimit"] = str(payload.get("recordLimit") or 50)
    if payload.get("categoryName"):
        params["categoryName"] = str(payload["categoryName"])
    if payload.get("lineOfBusinessName"):
        params["LineOfBusinessName"] = str(payload["lineOfBusinessName"])
    if payload.get("industryName"):
        params["IndustryName"] = str(payload["industryName"])

    return await _attom_request(
        path="/neighborhood/poi",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_area_county_lookup(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Area API V4: list of counties within a state."""
    tool_id = "attom.area_county_lookup"
    state_id = str(payload.get("StateId") or payload.get("state_id") or "").strip()
    if not state_id:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: StateId (e.g. ST12 for Florida)",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/county/lookup",
        query_params={"StateId": state_id},
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )


async def execute_attom_area_hierarchy_lookup(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Area API V4: all geographic boundaries a point falls within.

    Required: WKTString (POINT(lng lat)) or latitude+longitude.
    Returns county, place, ZIP, neighborhood, school district, etc.
    that the point belongs to.
    """
    tool_id = "attom.area_hierarchy"
    client = _get_client()
    params: dict[str, str] = {}
    wkt = (payload.get("WKTString") or payload.get("wkt") or "").strip()
    lat = payload.get("latitude")
    lng = payload.get("longitude")
    if wkt:
        params["WKTString"] = wkt
    elif lat is not None and lng is not None:
        params["WKTString"] = f"POINT({lng} {lat})"
    else:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: WKTString or latitude+longitude",
            receipt_data=receipt,
        )
    if payload.get("geoType"):
        params["geoType"] = str(payload["geoType"])
    return await _attom_request(
        path="/hierarchy/lookup",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )


# ─── Final batch — 100% ATTOM endpoint coverage (May 4 audit) ────────────
# 13 wrappers covering every remaining documented ATTOM endpoint.

async def execute_attom_property_address(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """List properties within a ZIP / radius — address-only payloads."""
    tool_id = "attom.property_address"
    client = _get_client()
    params: dict[str, str] = {}
    if payload.get("postalcode"):
        params["postalcode"] = str(payload["postalcode"])
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        params["latitude"] = str(payload["latitude"])
        params["longitude"] = str(payload["longitude"])
        params["radius"] = str(payload.get("radius", 1))
    else:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: postalcode OR latitude+longitude",
            receipt_data=receipt,
        )
    pagesize = int(payload.get("pagesize", 50))
    params["pagesize"] = str(min(max(pagesize, 1), 100))
    if payload.get("page"):
        params["page"] = str(payload["page"])
    return await _attom_request(
        path="/property/address", query_params=params, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_property_id_search(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Properties matching multi-criteria filters."""
    tool_id = "attom.property_id_search"
    client = _get_client()
    params: dict[str, str] = {}
    if payload.get("postalcode"):
        params["postalcode"] = str(payload["postalcode"])
    elif payload.get("geoIdV4"):
        params["geoIdV4"] = str(payload["geoIdV4"])
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        params["latitude"] = str(payload["latitude"])
        params["longitude"] = str(payload["longitude"])
        params["radius"] = str(payload.get("radius", 1))
    else:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing geographic anchor: postalcode OR geoIdV4 OR latitude+longitude+radius",
            receipt_data=receipt,
        )
    for body_key, attom_key in [
        ("propertytype", "propertytype"),
        ("minBeds", "minBeds"), ("maxBeds", "maxBeds"),
        ("minBathsTotal", "minBathsTotal"), ("maxBathsTotal", "maxBathsTotal"),
        ("minYearBuilt", "minYearBuilt"), ("maxYearBuilt", "maxYearBuilt"),
        ("minUniversalSize", "minUniversalSize"), ("maxUniversalSize", "maxUniversalSize"),
        ("minLotSize1", "minLotSize1"), ("maxLotSize1", "maxLotSize1"),
        ("minSaleAmt", "minSaleAmt"), ("maxSaleAmt", "maxSaleAmt"),
        ("minAssdTtlValue", "minAssdTtlValue"), ("maxAssdTtlValue", "maxAssdTtlValue"),
        ("minAvmValue", "minAvmValue"), ("maxAvmValue", "maxAvmValue"),
        ("minMktTtlValue", "minMktTtlValue"), ("maxMktTtlValue", "maxMktTtlValue"),
        ("orderby", "orderBy"),
    ]:
        if payload.get(body_key) is not None:
            params[attom_key] = str(payload[body_key])
    pagesize = int(payload.get("pagesize", 50))
    params["pagesize"] = str(min(max(pagesize, 1), 100))
    if payload.get("page"):
        params["page"] = str(payload["page"])
    return await _attom_request(
        path="/property/id", query_params=params, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_detail_mortgage(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Property detail + current mortgage (no owner)."""
    tool_id = "attom.detail_mortgage"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/property/detailmortgage", query_params=result, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_saleshistory_snapshot(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Condensed sales history list."""
    tool_id = "attom.saleshistory_snapshot"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/saleshistory/snapshot", query_params=result, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_saleshistory_basichistory(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Deed and mortgage details for a property."""
    tool_id = "attom.saleshistory_basichistory"
    client = _get_client()
    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result
    return await _attom_request(
        path="/saleshistory/basichistory", query_params=result, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_assessment_snapshot(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Bulk assessment data for a geography."""
    tool_id = "attom.assessment_snapshot"
    client = _get_client()
    params: dict[str, str] = {}
    if payload.get("postalcode"):
        params["postalcode"] = str(payload["postalcode"])
    elif payload.get("geoIdV4"):
        params["geoIdV4"] = str(payload["geoIdV4"])
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        params["latitude"] = str(payload["latitude"])
        params["longitude"] = str(payload["longitude"])
        params["radius"] = str(payload.get("radius", 1))
    else:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing geographic anchor: postalcode OR geoIdV4 OR latitude+longitude",
            receipt_data=receipt,
        )
    if payload.get("minAssdTtlValue"):
        params["minAssdTtlValue"] = str(payload["minAssdTtlValue"])
    if payload.get("maxAssdTtlValue"):
        params["maxAssdTtlValue"] = str(payload["maxAssdTtlValue"])
    if payload.get("orderby"):
        params["orderBy"] = str(payload["orderby"])
    pagesize = int(payload.get("pagesize", 50))
    params["pagesize"] = str(min(max(pagesize, 1), 100))
    return await _attom_request(
        path="/assessment/snapshot", query_params=params, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_avm_snapshot(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Bulk AVM data for a geography."""
    tool_id = "attom.avm_snapshot"
    client = _get_client()
    params: dict[str, str] = {}
    if payload.get("postalcode"):
        params["postalcode"] = str(payload["postalcode"])
    elif payload.get("geoIdV4"):
        params["geoIdV4"] = str(payload["geoIdV4"])
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        params["latitude"] = str(payload["latitude"])
        params["longitude"] = str(payload["longitude"])
        params["radius"] = str(payload.get("radius", 1))
    else:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing geographic anchor: postalcode OR geoIdV4 OR latitude+longitude",
            receipt_data=receipt,
        )
    if payload.get("minAvmValue"):
        params["minAvmValue"] = str(payload["minAvmValue"])
    if payload.get("maxAvmValue"):
        params["maxAvmValue"] = str(payload["maxAvmValue"])
    if payload.get("orderby"):
        params["orderBy"] = str(payload["orderby"])
    pagesize = int(payload.get("pagesize", 50))
    params["pagesize"] = str(min(max(pagesize, 1), 100))
    return await _attom_request(
        path="/avm/snapshot", query_params=params, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )


async def execute_attom_school_profile(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Detailed profile for a specific school."""
    tool_id = "attom.school_profile"
    client = _get_client()
    geo_id_v4 = str(payload.get("geoIdV4") or "").strip()
    if not geo_id_v4:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: geoIdV4 (school geography)",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/school/profile", query_params={"geoIdV4": geo_id_v4},
        tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_school_district(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """School district profile."""
    tool_id = "attom.school_district"
    client = _get_client()
    geo_id_v4 = str(payload.get("geoIdV4") or "").strip()
    if not geo_id_v4:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: geoIdV4 (district geography)",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/school/district", query_params={"geoIdV4": geo_id_v4},
        tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4",
    )


async def execute_attom_area_state_lookup(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """All US states with their geoIdV4 + StateId."""
    tool_id = "attom.area_state_lookup"
    return await _attom_request(
        path="/state/lookup", query_params={}, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )


async def execute_attom_area_cbsa_lookup(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """CBSAs (metros) within a state."""
    tool_id = "attom.area_cbsa_lookup"
    state_id = str(payload.get("StateId") or payload.get("state_id") or "").strip()
    if not state_id:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: StateId (e.g. ST12 for Florida)",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/cbsa/lookup", query_params={"StateId": state_id}, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )


async def execute_attom_area_geoid_lookup(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Specific Geo IDs that exist within a parent Geo ID."""
    tool_id = "attom.area_geoid_lookup"
    geo_id = str(payload.get("geoId") or payload.get("geoIdV4") or "").strip()
    geo_type = str(payload.get("geotype") or "ZI").strip()
    if not geo_id:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: geoId (parent geography)",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/geoid/lookup",
        query_params={"geoId": geo_id, "geotype": geo_type},
        tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )


async def execute_attom_area_geoid_legacy_lookup(
    *, payload: dict[str, Any], correlation_id: str, suite_id: str,
    office_id: str, risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Translate between legacy ATTOM geo codes and modern geoIdV4."""
    tool_id = "attom.area_geoid_legacy_lookup"
    params: dict[str, str] = {}
    if payload.get("geoIdV4"):
        params["geoIdV4"] = str(payload["geoIdV4"])
    if payload.get("geoId"):
        params["geoId"] = str(payload["geoId"])
    if not params:
        client = _get_client()
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id=tool_id,
            error="Missing required parameter: geoIdV4 or geoId",
            receipt_data=receipt,
        )
    return await _attom_request(
        path="/geoid/legacyLookup", query_params=params, tool_id=tool_id,
        correlation_id=correlation_id, suite_id=suite_id, office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        api_root="/v4/area",
    )
