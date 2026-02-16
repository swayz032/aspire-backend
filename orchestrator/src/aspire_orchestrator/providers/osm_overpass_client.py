"""OpenStreetMap Overpass Provider Client — Free location search for Adam (Research) skill pack.

Provider: OpenStreetMap Overpass API (https://overpass-api.de/api)
Auth: NONE (public API, no key required)
Risk tier: GREEN (search is read-only)
Idempotency: N/A (read-only)

Tools:
  - osm_overpass.query: Query OSM data via Overpass API

Per ecosystem providers.yaml:
  adam_research places routing: google_places -> tomtom -> here -> foursquare -> osm_overpass (last resort)

Note: Overpass API is slow (30s timeout) and rate-limited by the public server.
No retries to avoid overloading the public endpoint.
"""

from __future__ import annotations

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
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


class OsmOverpassClient(BaseProviderClient):
    """OpenStreetMap Overpass API client.

    No authentication required — this is a public API.
    Uses POST with form-encoded Overpass QL query.
    """

    provider_id = "osm_overpass"
    base_url = "https://overpass-api.de/api"
    timeout_seconds = 30.0  # Overpass is slow
    max_retries = 0  # No retries for public API (avoid hammering)
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        # No authentication required — public API
        return {}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 504:
            return InternalErrorCode.NETWORK_TIMEOUT
        return super()._parse_error(status_code, body)

    def _parse_response(self, raw_body: bytes) -> dict[str, Any]:
        """Override to handle Overpass JSON output format."""
        import json
        try:
            return json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, ValueError):
            # Overpass may return plain text errors
            return {"raw": raw_body.decode("utf-8", errors="replace")[:500]}


# Module-level singleton (lazy)
_client: OsmOverpassClient | None = None


def _get_client() -> OsmOverpassClient:
    global _client
    if _client is None:
        _client = OsmOverpassClient()
    return _client


async def execute_osm_overpass_query(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute osm_overpass.query — query OSM data via Overpass API.

    Required payload:
      - query: str — search term for name matching

    Optional payload:
      - lat: float — center latitude (default 40.7128 — NYC)
      - lon: float — center longitude (default -74.0060 — NYC)
      - radius: int — search radius in meters (default 5000, max 50000)
      - raw_query: str — raw Overpass QL (overrides auto-generated query)
    """
    client = _get_client()

    query = payload.get("query", "")
    raw_query = payload.get("raw_query", "")

    if not query and not raw_query:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="osm_overpass.query",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="osm_overpass.query",
            error="Missing required parameter: query (or raw_query)",
            receipt_data=receipt,
        )

    # Build Overpass QL query
    if raw_query:
        overpass_ql = raw_query
    else:
        lat = payload.get("lat", 40.7128)
        lon = payload.get("lon", -74.0060)
        radius = min(payload.get("radius", 5000), 50000)
        # Escape double quotes in query for Overpass QL
        safe_query = query.replace('"', '\\"')
        overpass_ql = (
            f'[out:json][timeout:25];'
            f'node["name"~"{safe_query}"](around:{radius},{lat},{lon});'
            f'out body;'
        )

    # Overpass API uses POST with form data
    # Note: We send as body dict and let the base client serialize as JSON,
    # but Overpass expects form-encoded. We override by sending raw body.
    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/interpreter",
            body={"data": overpass_ql},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
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
        tool_id="osm_overpass.query",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        elements = response.body.get("elements", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="osm_overpass.query",
            data={
                "results": [
                    {
                        "name": elem.get("tags", {}).get("name", ""),
                        "type": elem.get("type", ""),
                        "osm_id": elem.get("id"),
                        "location": {
                            "lat": elem.get("lat"),
                            "lng": elem.get("lon"),
                        },
                        "tags": elem.get("tags", {}),
                    }
                    for elem in elements
                    if elem.get("tags", {}).get("name")
                ],
                "query": query or "(raw_query)",
                "result_count": len(elements),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="osm_overpass.query",
            error=response.error_message or f"Overpass API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
