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
) -> ToolExecutionResult:
    """Shared ATTOM request handler for all endpoint families.

    api_root selects which ATTOM API surface the request hits. Defaults to
    the Property API v1 (the bulk of our existing wrappers use this). Pass
    "/property/v3" for Transaction V3 endpoints (preforeclosuredetails),
    "/v4" for Area / Community / Location lookup endpoints, "" for absolute
    paths. Final URL is `https://api.gateway.attomdata.com{api_root}{path}`.
    """
    client = _get_client()
    full_path = f"{api_root}{path}"

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=full_path,
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

    receipt = _build_receipt(
        client, tool_id, correlation_id, suite_id, office_id,
        outcome, reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        response=response,
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data=response.body,
            receipt_data=receipt,
        )

    # ATTOM returns HTTP 400 with "SuccessWithoutResult" when address is valid
    # but no data exists — treat as empty success, not error
    body = response.body or {}
    status_msg = ""
    if isinstance(body, dict):
        status_obj = body.get("status", {})
        if isinstance(status_obj, dict):
            status_msg = status_obj.get("msg", "")
    if status_msg == "SuccessWithoutResult":
        logger.info("ATTOM %s: address valid but no results (SuccessWithoutResult)", tool_id)
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={"property": []},  # Empty but valid
            receipt_data=receipt,
        )

    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=response.error_message or f"ATTOM API error: HTTP {response.status_code}",
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

    return await _attom_request(
        path="/property/detailwithschools",
        query_params=result,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
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
    """Sales trend snapshot — macro geo trends by geography."""
    tool_id = "attom.sales_trends"

    geoid = payload.get("geoid", "")
    geo_type = payload.get("geo_type", "ZI")  # ZI=ZIP, CO=county, ST=state
    if not geoid:
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
            error="Missing required parameter: geoid (ZIP/county/state code)",
            receipt_data=receipt,
        )

    return await _attom_request(
        path="/salestrend/snapshot",
        query_params={"geoid": f"{geo_type}{geoid}", "interval": payload.get("interval", "monthly")},
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
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

    return await _attom_request(
        path="/valuation/rental",
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
    """Sales comparables — nearby comparable property sales."""
    tool_id = "attom.sales_comparables"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    params = dict(result)
    if payload.get("searchtype"):
        params["searchtype"] = payload["searchtype"]
    if payload.get("miles"):
        params["miles"] = str(payload["miles"])

    return await _attom_request(
        path="/salescomparables/detail",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
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

    return await _attom_request(
        path="/school/search",
        query_params=params,
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
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
    """Boundary/geography lookup — parcel geometry and hyperlocal context."""
    tool_id = "attom.boundary_lookup"

    geoid = payload.get("geoid", "")
    geo_type = payload.get("geo_type", "ZI")
    if not geoid:
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
            error="Missing required parameter: geoid",
            receipt_data=receipt,
        )

    return await _attom_request(
        path="/boundary/lookup",
        query_params={"geoid": f"{geo_type}{geoid}"},
        tool_id=tool_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
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
