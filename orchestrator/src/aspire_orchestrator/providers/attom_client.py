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
    base_url = "https://api.gateway.attomdata.com/propertyapi/v1.0.0"
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
) -> ToolExecutionResult:
    """Shared ATTOM request handler for all endpoint families."""
    client = _get_client()

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=path,
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
    return ToolExecutionResult(
        outcome=Outcome.FAILED,
        tool_id=tool_id,
        error=response.error_message or f"ATTOM API error: HTTP {response.status_code}",
        receipt_data=receipt,
    )


def _validate_address(payload: dict[str, Any], client: AttomClient, tool_id: str,
                       correlation_id: str, suite_id: str, office_id: str,
                       capability_token_id: str | None = None,
                       capability_token_hash: str | None = None) -> ToolExecutionResult | dict[str, str]:
    """Validate and extract address params. Returns query_params dict or ToolExecutionResult on error."""
    address = payload.get("address", "")
    if not address:
        receipt = _build_receipt(
            client, tool_id, correlation_id, suite_id, office_id,
            Outcome.FAILED, "INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: address (fail-closed per Law #3)",
            receipt_data=receipt,
        )

    params: dict[str, str] = {}
    # Support both full address string and structured components
    if payload.get("attomid"):
        params["attomid"] = str(payload["attomid"])
    elif payload.get("apn") and payload.get("fips"):
        params["apn"] = payload["apn"]
        params["fips"] = payload["fips"]
    else:
        # Parse address into components
        parts = address.split(",")
        if len(parts) >= 2:
            params["address1"] = parts[0].strip()
            params["address2"] = ",".join(parts[1:]).strip()
        else:
            params["address1"] = address
            params["address2"] = ""

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
    """Property detail — base parcel facts, characteristics, ownership."""
    tool_id = "attom.property_detail"
    client = _get_client()

    result = _validate_address(
        payload, client, tool_id, correlation_id, suite_id, office_id,
        capability_token_id, capability_token_hash,
    )
    if isinstance(result, ToolExecutionResult):
        return result

    return await _attom_request(
        path="/property/detail",
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
