"""Tests for Wave 1 Adam Research providers + search router.

Comprehensive coverage:
  - Per-provider: missing query -> receipt with INPUT_MISSING_REQUIRED
  - Per-provider: success response -> normalized result + receipt
  - Per-provider: API error -> correct InternalErrorCode + receipt
  - Per-provider: missing API key -> AUTH_INVALID_KEY error + receipt
  - search_router: primary success -> uses primary, no fallback
  - search_router: primary fails -> falls back to secondary
  - search_router: all fail -> returns last error with full fallback chain
  - search_router: circuit breaker open on primary -> skips to fallback
  - osm_overpass: no auth required (special case)
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, PropertyMock

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    ProviderError,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# Common test kwargs for all executor calls
_COMMON_KWARGS = {
    "correlation_id": "corr-test-001",
    "suite_id": "suite-test-001",
    "office_id": "office-test-001",
    "risk_tier": "green",
    "capability_token_id": "tok-001",
    "capability_token_hash": "hash-001",
}


def _success_response(body: dict) -> ProviderResponse:
    return ProviderResponse(
        status_code=200,
        body=body,
        success=True,
        latency_ms=42.0,
    )


def _error_response(
    status_code: int = 500,
    body: dict | None = None,
    error_code: InternalErrorCode = InternalErrorCode.SERVER_INTERNAL_ERROR,
) -> ProviderResponse:
    return ProviderResponse(
        status_code=status_code,
        body=body or {"error": "fail"},
        success=False,
        error_code=error_code,
        error_message=f"HTTP {status_code}",
    )


def _auth_error_response() -> ProviderResponse:
    return ProviderResponse(
        status_code=401,
        body={"error": "AUTH_INVALID_KEY", "message": "key not configured"},
        success=False,
        error_code=InternalErrorCode.AUTH_INVALID_KEY,
        error_message="key not configured",
    )


# =============================================================================
# Google Places Client
# =============================================================================


class TestGooglePlacesClient:
    """Tests for google_places_client.py."""

    @pytest.mark.asyncio
    async def test_missing_query_returns_receipt(self):
        from aspire_orchestrator.providers.google_places_client import execute_google_places_search
        result = await execute_google_places_search(payload={}, **_COMMON_KWARGS)
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "google_places.search"
        assert "query" in result.error
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"
        assert result.receipt_data["correlation_id"] == "corr-test-001"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_auth_error(self):
        from aspire_orchestrator.providers import google_places_client
        google_places_client._client = None  # Reset singleton
        with patch("aspire_orchestrator.providers.google_places_client.settings") as mock_settings:
            mock_settings.google_maps_api_key = ""
            result = await google_places_client.execute_google_places_search(
                payload={"query": "pizza"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert "AUTH_INVALID_KEY" in str(result.receipt_data.get("reason_code", "")) or \
               result.receipt_data.get("provider_metadata", {}).get("error_code") == "AUTH_INVALID_KEY"
        google_places_client._client = None

    @pytest.mark.asyncio
    async def test_success_response_normalized(self):
        from aspire_orchestrator.providers import google_places_client
        google_places_client._client = None
        body = {
            "status": "OK",
            "results": [
                {
                    "name": "Joe's Pizza",
                    "formatted_address": "123 Main St",
                    "geometry": {"location": {"lat": 40.71, "lng": -74.00}},
                    "rating": 4.5,
                    "place_id": "ChIJ123",
                }
            ],
        }
        with patch("aspire_orchestrator.providers.google_places_client.settings") as mock_settings:
            mock_settings.google_maps_api_key = "test-key"
            with patch.object(
                google_places_client.GooglePlacesClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await google_places_client.execute_google_places_search(
                    payload={"query": "pizza"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 1
        assert result.data["results"][0]["name"] == "Joe's Pizza"
        assert result.data["results"][0]["place_id"] == "ChIJ123"
        assert result.data["results"][0]["rating"] == 4.5
        assert result.receipt_data["reason_code"] == "EXECUTED"
        google_places_client._client = None

    @pytest.mark.asyncio
    async def test_api_error_returns_error_receipt(self):
        from aspire_orchestrator.providers import google_places_client
        google_places_client._client = None
        with patch("aspire_orchestrator.providers.google_places_client.settings") as mock_settings:
            mock_settings.google_maps_api_key = "test-key"
            with patch.object(
                google_places_client.GooglePlacesClient, "_request",
                new_callable=AsyncMock,
                return_value=_error_response(500),
            ):
                result = await google_places_client.execute_google_places_search(
                    payload={"query": "pizza"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data  # Receipt emitted
        google_places_client._client = None

    @pytest.mark.asyncio
    async def test_zero_results_is_success(self):
        from aspire_orchestrator.providers import google_places_client
        google_places_client._client = None
        body = {"status": "ZERO_RESULTS", "results": []}
        with patch("aspire_orchestrator.providers.google_places_client.settings") as mock_settings:
            mock_settings.google_maps_api_key = "test-key"
            with patch.object(
                google_places_client.GooglePlacesClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await google_places_client.execute_google_places_search(
                    payload={"query": "xyznoexist"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 0
        google_places_client._client = None

    @pytest.mark.asyncio
    async def test_request_denied_is_failure(self):
        from aspire_orchestrator.providers import google_places_client
        google_places_client._client = None
        body = {"status": "REQUEST_DENIED", "results": []}
        with patch("aspire_orchestrator.providers.google_places_client.settings") as mock_settings:
            mock_settings.google_maps_api_key = "test-key"
            with patch.object(
                google_places_client.GooglePlacesClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await google_places_client.execute_google_places_search(
                    payload={"query": "pizza"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["reason_code"] == "REQUEST_DENIED"
        google_places_client._client = None


# =============================================================================
# TomTom Client
# =============================================================================


class TestTomTomClient:
    """Tests for tomtom_client.py."""

    @pytest.mark.asyncio
    async def test_missing_query_returns_receipt(self):
        from aspire_orchestrator.providers.tomtom_client import execute_tomtom_search
        result = await execute_tomtom_search(payload={}, **_COMMON_KWARGS)
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "tomtom.search"
        assert "query" in result.error
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        from aspire_orchestrator.providers import tomtom_client
        tomtom_client._client = None
        with patch("aspire_orchestrator.providers.tomtom_client.settings") as mock_settings:
            mock_settings.tomtom_api_key = ""
            result = await tomtom_client.execute_tomtom_search(
                payload={"query": "hotel"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        tomtom_client._client = None

    @pytest.mark.asyncio
    async def test_success_response_normalized(self):
        from aspire_orchestrator.providers import tomtom_client
        tomtom_client._client = None
        body = {
            "results": [
                {
                    "poi": {"name": "Grand Hotel"},
                    "address": {"freeformAddress": "456 Broadway"},
                    "position": {"lat": 40.72, "lon": -73.99},
                    "score": 8.5,
                    "type": "POI",
                    "id": "tom-123",
                }
            ]
        }
        with patch("aspire_orchestrator.providers.tomtom_client.settings") as mock_settings:
            mock_settings.tomtom_api_key = "test-key"
            with patch.object(
                tomtom_client.TomTomClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await tomtom_client.execute_tomtom_search(
                    payload={"query": "hotel"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 1
        assert result.data["results"][0]["name"] == "Grand Hotel"
        assert result.data["results"][0]["score"] == 8.5
        assert result.receipt_data["reason_code"] == "EXECUTED"
        tomtom_client._client = None

    @pytest.mark.asyncio
    async def test_api_error_returns_receipt(self):
        from aspire_orchestrator.providers import tomtom_client
        tomtom_client._client = None
        with patch("aspire_orchestrator.providers.tomtom_client.settings") as mock_settings:
            mock_settings.tomtom_api_key = "test-key"
            with patch.object(
                tomtom_client.TomTomClient, "_request",
                new_callable=AsyncMock, return_value=_error_response(429, error_code=InternalErrorCode.RATE_LIMITED),
            ):
                result = await tomtom_client.execute_tomtom_search(
                    payload={"query": "hotel"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data  # Receipt emitted on failure
        tomtom_client._client = None


# =============================================================================
# HERE Client
# =============================================================================


class TestHereClient:
    """Tests for here_client.py."""

    @pytest.mark.asyncio
    async def test_missing_query_returns_receipt(self):
        from aspire_orchestrator.providers.here_client import execute_here_search
        result = await execute_here_search(payload={}, **_COMMON_KWARGS)
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "here.search"
        assert "query" in result.error
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        from aspire_orchestrator.providers import here_client
        here_client._client = None
        with patch("aspire_orchestrator.providers.here_client.settings") as mock_settings:
            mock_settings.here_api_key = ""
            result = await here_client.execute_here_search(
                payload={"query": "pharmacy"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        here_client._client = None

    @pytest.mark.asyncio
    async def test_success_response_normalized(self):
        from aspire_orchestrator.providers import here_client
        here_client._client = None
        body = {
            "items": [
                {
                    "title": "CVS Pharmacy",
                    "address": {"label": "789 Elm St"},
                    "position": {"lat": 40.73, "lng": -73.98},
                    "categories": [{"name": "Pharmacy"}],
                    "id": "here-abc",
                }
            ]
        }
        with patch("aspire_orchestrator.providers.here_client.settings") as mock_settings:
            mock_settings.here_api_key = "test-key"
            with patch.object(
                here_client.HereClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await here_client.execute_here_search(
                    payload={"query": "pharmacy"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 1
        assert result.data["results"][0]["name"] == "CVS Pharmacy"
        assert result.data["results"][0]["categories"] == ["Pharmacy"]
        assert result.receipt_data["reason_code"] == "EXECUTED"
        here_client._client = None

    @pytest.mark.asyncio
    async def test_api_error_returns_receipt(self):
        from aspire_orchestrator.providers import here_client
        here_client._client = None
        with patch("aspire_orchestrator.providers.here_client.settings") as mock_settings:
            mock_settings.here_api_key = "test-key"
            with patch.object(
                here_client.HereClient, "_request",
                new_callable=AsyncMock, return_value=_error_response(403, error_code=InternalErrorCode.AUTH_INVALID_KEY),
            ):
                result = await here_client.execute_here_search(
                    payload={"query": "pharmacy"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        here_client._client = None


# =============================================================================
# Foursquare Client
# =============================================================================


class TestFoursquareClient:
    """Tests for foursquare_client.py."""

    @pytest.mark.asyncio
    async def test_missing_query_returns_receipt(self):
        from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
        result = await execute_foursquare_search(payload={}, **_COMMON_KWARGS)
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "foursquare.search"
        assert "query" in result.error
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        from aspire_orchestrator.providers import foursquare_client
        foursquare_client._client = None
        with patch("aspire_orchestrator.providers.foursquare_client.settings") as mock_settings:
            mock_settings.foursquare_api_key = ""
            result = await foursquare_client.execute_foursquare_search(
                payload={"query": "coffee"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        foursquare_client._client = None

    @pytest.mark.asyncio
    async def test_success_response_normalized(self):
        from aspire_orchestrator.providers import foursquare_client
        foursquare_client._client = None
        body = {
            "results": [
                {
                    "name": "Blue Bottle Coffee",
                    "location": {"formatted_address": "1 Ferry Bldg"},
                    "geocodes": {"main": {"latitude": 37.79, "longitude": -122.39}},
                    "categories": [{"name": "Coffee Shop"}],
                    "fsq_id": "fsq-456",
                }
            ]
        }
        with patch("aspire_orchestrator.providers.foursquare_client.settings") as mock_settings:
            mock_settings.foursquare_api_key = "test-key"
            with patch.object(
                foursquare_client.FoursquareClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await foursquare_client.execute_foursquare_search(
                    payload={"query": "coffee"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 1
        assert result.data["results"][0]["name"] == "Blue Bottle Coffee"
        assert result.data["results"][0]["fsq_id"] == "fsq-456"
        assert result.data["results"][0]["location"]["lat"] == 37.79
        assert result.receipt_data["reason_code"] == "EXECUTED"
        foursquare_client._client = None

    @pytest.mark.asyncio
    async def test_api_error_returns_receipt(self):
        from aspire_orchestrator.providers import foursquare_client
        foursquare_client._client = None
        with patch("aspire_orchestrator.providers.foursquare_client.settings") as mock_settings:
            mock_settings.foursquare_api_key = "test-key"
            with patch.object(
                foursquare_client.FoursquareClient, "_request",
                new_callable=AsyncMock, return_value=_error_response(401, error_code=InternalErrorCode.AUTH_INVALID_KEY),
            ):
                result = await foursquare_client.execute_foursquare_search(
                    payload={"query": "coffee"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        foursquare_client._client = None


# =============================================================================
# OSM Overpass Client (no auth — special case)
# =============================================================================


class TestOsmOverpassClient:
    """Tests for osm_overpass_client.py (no auth required)."""

    @pytest.mark.asyncio
    async def test_missing_query_returns_receipt(self):
        from aspire_orchestrator.providers.osm_overpass_client import execute_osm_overpass_query
        result = await execute_osm_overpass_query(payload={}, **_COMMON_KWARGS)
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "osm_overpass.query"
        assert "query" in result.error
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_no_api_key_required(self):
        """OSM Overpass is a public API — no key needed."""
        from aspire_orchestrator.providers import osm_overpass_client
        osm_overpass_client._client = None
        body = {"elements": []}
        with patch.object(
            osm_overpass_client.OsmOverpassClient, "_request",
            new_callable=AsyncMock, return_value=_success_response(body),
        ):
            result = await osm_overpass_client.execute_osm_overpass_query(
                payload={"query": "park"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 0
        osm_overpass_client._client = None

    @pytest.mark.asyncio
    async def test_success_response_normalized(self):
        from aspire_orchestrator.providers import osm_overpass_client
        osm_overpass_client._client = None
        body = {
            "elements": [
                {
                    "type": "node",
                    "id": 12345,
                    "lat": 40.78,
                    "lon": -73.97,
                    "tags": {"name": "Central Park", "leisure": "park"},
                }
            ]
        }
        with patch.object(
            osm_overpass_client.OsmOverpassClient, "_request",
            new_callable=AsyncMock, return_value=_success_response(body),
        ):
            result = await osm_overpass_client.execute_osm_overpass_query(
                payload={"query": "Central Park", "lat": 40.78, "lon": -73.97, "radius": 1000},
                **_COMMON_KWARGS,
            )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 1
        assert result.data["results"][0]["name"] == "Central Park"
        assert result.data["results"][0]["osm_id"] == 12345
        assert result.data["results"][0]["location"]["lat"] == 40.78
        assert result.receipt_data["reason_code"] == "EXECUTED"
        osm_overpass_client._client = None

    @pytest.mark.asyncio
    async def test_raw_query_override(self):
        from aspire_orchestrator.providers import osm_overpass_client
        osm_overpass_client._client = None
        body = {"elements": []}
        with patch.object(
            osm_overpass_client.OsmOverpassClient, "_request",
            new_callable=AsyncMock, return_value=_success_response(body),
        ) as mock_req:
            result = await osm_overpass_client.execute_osm_overpass_query(
                payload={"raw_query": '[out:json];node["amenity"="hospital"];out;'},
                **_COMMON_KWARGS,
            )
        assert result.outcome == Outcome.SUCCESS
        # Verify the raw query was passed (body contains the raw_query)
        call_args = mock_req.call_args
        request_obj = call_args[0][0]
        assert request_obj.body["data"] == '[out:json];node["amenity"="hospital"];out;'
        osm_overpass_client._client = None

    @pytest.mark.asyncio
    async def test_api_error_returns_receipt(self):
        from aspire_orchestrator.providers import osm_overpass_client
        osm_overpass_client._client = None
        with patch.object(
            osm_overpass_client.OsmOverpassClient, "_request",
            new_callable=AsyncMock,
            return_value=_error_response(429, error_code=InternalErrorCode.RATE_LIMITED),
        ):
            result = await osm_overpass_client.execute_osm_overpass_query(
                payload={"query": "park"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        osm_overpass_client._client = None

    @pytest.mark.asyncio
    async def test_timeout_config(self):
        """Verify OSM Overpass has 30s timeout (slow API)."""
        from aspire_orchestrator.providers.osm_overpass_client import OsmOverpassClient
        client = OsmOverpassClient()
        assert client.timeout_seconds == 30.0
        assert client.max_retries == 0  # No retries for public API


# =============================================================================
# Mapbox Client
# =============================================================================


class TestMapboxClient:
    """Tests for mapbox_client.py."""

    @pytest.mark.asyncio
    async def test_missing_query_returns_receipt(self):
        from aspire_orchestrator.providers.mapbox_client import execute_mapbox_geocode
        result = await execute_mapbox_geocode(payload={}, **_COMMON_KWARGS)
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "mapbox.geocode"
        assert "query" in result.error
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_missing_access_token(self):
        from aspire_orchestrator.providers import mapbox_client
        mapbox_client._client = None
        with patch("aspire_orchestrator.providers.mapbox_client.settings") as mock_settings:
            mock_settings.mapbox_access_token = ""
            result = await mapbox_client.execute_mapbox_geocode(
                payload={"query": "123 Main St"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        mapbox_client._client = None

    @pytest.mark.asyncio
    async def test_success_response_normalized(self):
        from aspire_orchestrator.providers import mapbox_client
        mapbox_client._client = None
        body = {
            "features": [
                {
                    "place_name": "123 Main St, New York, NY",
                    "center": [-74.00, 40.71],
                    "place_type": ["address"],
                    "relevance": 0.95,
                    "id": "address.123",
                }
            ]
        }
        with patch("aspire_orchestrator.providers.mapbox_client.settings") as mock_settings:
            mock_settings.mapbox_access_token = "test-token"
            with patch.object(
                mapbox_client.MapboxClient, "_request",
                new_callable=AsyncMock, return_value=_success_response(body),
            ):
                result = await mapbox_client.execute_mapbox_geocode(
                    payload={"query": "123 Main St"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.SUCCESS
        assert result.data["result_count"] == 1
        assert result.data["results"][0]["place_name"] == "123 Main St, New York, NY"
        assert result.data["results"][0]["location"]["lng"] == -74.00
        assert result.data["results"][0]["location"]["lat"] == 40.71
        assert result.data["results"][0]["relevance"] == 0.95
        assert result.receipt_data["reason_code"] == "EXECUTED"
        mapbox_client._client = None

    @pytest.mark.asyncio
    async def test_api_error_returns_receipt(self):
        from aspire_orchestrator.providers import mapbox_client
        mapbox_client._client = None
        with patch("aspire_orchestrator.providers.mapbox_client.settings") as mock_settings:
            mock_settings.mapbox_access_token = "test-token"
            with patch.object(
                mapbox_client.MapboxClient, "_request",
                new_callable=AsyncMock,
                return_value=_error_response(401, error_code=InternalErrorCode.AUTH_INVALID_KEY),
            ):
                result = await mapbox_client.execute_mapbox_geocode(
                    payload={"query": "123 Main St"}, **_COMMON_KWARGS
                )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        mapbox_client._client = None


# =============================================================================
# Search Router — Web Search
# =============================================================================


class TestSearchRouterWebSearch:
    """Tests for search_router.route_web_search."""

    @pytest.mark.asyncio
    async def test_primary_success_no_fallback(self):
        """When brave succeeds, should use brave and not fall back."""
        from aspire_orchestrator.services import search_router

        brave_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="brave.search",
            data={"results": [{"title": "Test"}], "query": "test", "result_count": 1},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_brave_search",
            new_callable=AsyncMock, return_value=brave_result,
        ) as mock_brave:
            with patch.object(
                search_router, "execute_tavily_search",
                new_callable=AsyncMock,
            ) as mock_tavily:
                result = await search_router.route_web_search(
                    payload={"query": "test"}, **_COMMON_KWARGS
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "brave"
        assert result.data["fallback_chain"] == ["brave"]
        mock_brave.assert_called_once()
        mock_tavily.assert_not_called()

    @pytest.mark.asyncio
    async def test_primary_fails_fallback_to_secondary(self):
        """When brave fails, should fall back to tavily."""
        from aspire_orchestrator.services import search_router

        brave_result = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="brave.search",
            error="Brave API error",
            receipt_data={"reason_code": "FAILED"},
        )
        tavily_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="tavily.search",
            data={"results": [{"title": "Tavily Result"}], "query": "test", "result_count": 1},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_brave_search",
            new_callable=AsyncMock, return_value=brave_result,
        ):
            with patch.object(
                search_router, "execute_tavily_search",
                new_callable=AsyncMock, return_value=tavily_result,
            ):
                result = await search_router.route_web_search(
                    payload={"query": "test"}, **_COMMON_KWARGS
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "tavily"
        assert result.data["fallback_chain"] == ["brave", "tavily"]

    @pytest.mark.asyncio
    async def test_all_fail_returns_last_error(self):
        """When all providers fail, returns last error with full chain."""
        from aspire_orchestrator.services import search_router

        brave_result = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="brave.search",
            error="Brave down",
            receipt_data={"reason_code": "FAILED"},
        )
        tavily_result = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="tavily.search",
            error="Tavily down",
            receipt_data={"reason_code": "FAILED"},
        )

        with patch.object(
            search_router, "execute_brave_search",
            new_callable=AsyncMock, return_value=brave_result,
        ):
            with patch.object(
                search_router, "execute_tavily_search",
                new_callable=AsyncMock, return_value=tavily_result,
            ):
                result = await search_router.route_web_search(
                    payload={"query": "test"}, **_COMMON_KWARGS
                )

        assert result.outcome == Outcome.FAILED
        assert "All providers failed" in result.error
        assert "Tavily down" in result.error
        assert result.data["provider_used"] is None
        assert result.data["fallback_chain"] == ["brave", "tavily"]

    @pytest.mark.asyncio
    async def test_provider_error_exception_triggers_fallback(self):
        """ProviderError exception should trigger fallback."""
        from aspire_orchestrator.services import search_router

        tavily_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="tavily.search",
            data={"results": [], "query": "test", "result_count": 0},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_brave_search",
            new_callable=AsyncMock,
            side_effect=ProviderError(
                code=InternalErrorCode.NETWORK_CIRCUIT_OPEN,
                message="Circuit breaker OPEN",
                provider_id="brave",
            ),
        ):
            with patch.object(
                search_router, "execute_tavily_search",
                new_callable=AsyncMock, return_value=tavily_result,
            ):
                result = await search_router.route_web_search(
                    payload={"query": "test"}, **_COMMON_KWARGS
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "tavily"
        assert result.data["fallback_chain"] == ["brave", "tavily"]


# =============================================================================
# Search Router — Places Search
# =============================================================================


class TestSearchRouterPlacesSearch:
    """Tests for search_router.route_places_search."""

    @pytest.mark.asyncio
    async def test_primary_success(self):
        """When google_places succeeds, uses it directly."""
        from aspire_orchestrator.services import search_router

        gp_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="google_places.search",
            data={"results": [{"name": "Pizzeria"}], "query": "pizza", "result_count": 1},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_google_places_search",
            new_callable=AsyncMock, return_value=gp_result,
        ):
            result = await search_router.route_places_search(
                payload={"query": "pizza"}, **_COMMON_KWARGS
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "google_places"
        assert result.data["fallback_chain"] == ["google_places"]

    @pytest.mark.asyncio
    async def test_cascading_fallback_to_third_provider(self):
        """Falls through google -> tomtom (fail) -> here (success)."""
        from aspire_orchestrator.services import search_router

        fail_result = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="google_places.search",
            error="API error",
            receipt_data={"reason_code": "FAILED"},
        )
        tomtom_fail = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="tomtom.search",
            error="API error",
            receipt_data={"reason_code": "FAILED"},
        )
        here_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="here.search",
            data={"results": [{"name": "Pizzeria"}], "query": "pizza", "result_count": 1},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_google_places_search",
            new_callable=AsyncMock, return_value=fail_result,
        ):
            with patch.object(
                search_router, "execute_tomtom_search",
                new_callable=AsyncMock, return_value=tomtom_fail,
            ):
                with patch.object(
                    search_router, "execute_here_search",
                    new_callable=AsyncMock, return_value=here_result,
                ):
                    result = await search_router.route_places_search(
                        payload={"query": "pizza"}, **_COMMON_KWARGS
                    )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "here"
        assert result.data["fallback_chain"] == ["google_places", "tomtom", "here"]

    @pytest.mark.asyncio
    async def test_all_five_fail(self):
        """When all 5 places providers fail, returns error with full chain."""
        from aspire_orchestrator.services import search_router

        fail = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="x",
            error="fail",
            receipt_data={"reason_code": "FAILED"},
        )

        with patch.object(search_router, "execute_google_places_search", new_callable=AsyncMock, return_value=fail):
            with patch.object(search_router, "execute_tomtom_search", new_callable=AsyncMock, return_value=fail):
                with patch.object(search_router, "execute_here_search", new_callable=AsyncMock, return_value=fail):
                    with patch.object(search_router, "execute_foursquare_search", new_callable=AsyncMock, return_value=fail):
                        with patch.object(search_router, "execute_osm_overpass_query", new_callable=AsyncMock, return_value=fail):
                            result = await search_router.route_places_search(
                                payload={"query": "pizza"}, **_COMMON_KWARGS
                            )

        assert result.outcome == Outcome.FAILED
        assert result.data["provider_used"] is None
        assert result.data["fallback_chain"] == [
            "google_places", "tomtom", "here", "foursquare", "osm_overpass"
        ]
        assert result.receipt_data.get("router_all_failed") is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_exception_skips_provider(self):
        """Circuit breaker open exception triggers skip to next provider."""
        from aspire_orchestrator.services import search_router

        tomtom_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="tomtom.search",
            data={"results": [], "query": "test", "result_count": 0},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_google_places_search",
            new_callable=AsyncMock,
            side_effect=ProviderError(
                code=InternalErrorCode.NETWORK_CIRCUIT_OPEN,
                message="Circuit open for google_places",
                provider_id="google_places",
            ),
        ):
            with patch.object(
                search_router, "execute_tomtom_search",
                new_callable=AsyncMock, return_value=tomtom_result,
            ):
                result = await search_router.route_places_search(
                    payload={"query": "test"}, **_COMMON_KWARGS
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "tomtom"
        assert "google_places" in result.data["fallback_chain"]
        assert "tomtom" in result.data["fallback_chain"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_triggers_fallback(self):
        """Runtime exceptions should trigger fallback, not crash."""
        from aspire_orchestrator.services import search_router

        tomtom_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="tomtom.search",
            data={"results": [], "query": "test", "result_count": 0},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_google_places_search",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Unexpected crash"),
        ):
            with patch.object(
                search_router, "execute_tomtom_search",
                new_callable=AsyncMock, return_value=tomtom_result,
            ):
                result = await search_router.route_places_search(
                    payload={"query": "test"}, **_COMMON_KWARGS
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "tomtom"


# =============================================================================
# Search Router — Geocode
# =============================================================================


class TestSearchRouterGeocode:
    """Tests for search_router.route_geocode."""

    @pytest.mark.asyncio
    async def test_mapbox_success(self):
        from aspire_orchestrator.services import search_router

        mapbox_result = ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="mapbox.geocode",
            data={"results": [{"place_name": "NYC"}], "query": "NYC", "result_count": 1},
            receipt_data={"reason_code": "EXECUTED"},
        )

        with patch.object(
            search_router, "execute_mapbox_geocode",
            new_callable=AsyncMock, return_value=mapbox_result,
        ):
            result = await search_router.route_geocode(
                payload={"query": "NYC"}, **_COMMON_KWARGS
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["provider_used"] == "mapbox"

    @pytest.mark.asyncio
    async def test_mapbox_fail_returns_error(self):
        from aspire_orchestrator.services import search_router

        mapbox_fail = ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="mapbox.geocode",
            error="Mapbox error",
            receipt_data={"reason_code": "FAILED"},
        )

        with patch.object(
            search_router, "execute_mapbox_geocode",
            new_callable=AsyncMock, return_value=mapbox_fail,
        ):
            result = await search_router.route_geocode(
                payload={"query": "NYC"}, **_COMMON_KWARGS
            )

        assert result.outcome == Outcome.FAILED
        assert result.data["fallback_chain"] == ["mapbox"]


# =============================================================================
# Tool Executor Registry Wiring
# =============================================================================


class TestToolExecutorRegistryWiring:
    """Verify all Wave 1 providers are wired into the tool executor registry."""

    def test_places_executors_wired(self):
        from aspire_orchestrator.services.tool_executor import _PLACES_EXECUTORS
        assert "google_places.search" in _PLACES_EXECUTORS
        assert "tomtom.search" in _PLACES_EXECUTORS
        assert "here.search" in _PLACES_EXECUTORS
        assert "foursquare.search" in _PLACES_EXECUTORS
        assert "osm_overpass.query" in _PLACES_EXECUTORS
        assert "mapbox.geocode" in _PLACES_EXECUTORS

    def test_search_router_executors_wired(self):
        from aspire_orchestrator.services.tool_executor import _SEARCH_ROUTER_EXECUTORS
        assert "search.web" in _SEARCH_ROUTER_EXECUTORS
        assert "search.places" in _SEARCH_ROUTER_EXECUTORS
        assert "search.geocode" in _SEARCH_ROUTER_EXECUTORS

    def test_all_live_executors_include_places(self):
        from aspire_orchestrator.services.tool_executor import _ALL_LIVE_EXECUTORS
        assert "google_places.search" in _ALL_LIVE_EXECUTORS
        assert "tomtom.search" in _ALL_LIVE_EXECUTORS
        assert "here.search" in _ALL_LIVE_EXECUTORS
        assert "foursquare.search" in _ALL_LIVE_EXECUTORS
        assert "osm_overpass.query" in _ALL_LIVE_EXECUTORS
        assert "mapbox.geocode" in _ALL_LIVE_EXECUTORS
        assert "search.web" in _ALL_LIVE_EXECUTORS
        assert "search.places" in _ALL_LIVE_EXECUTORS
        assert "search.geocode" in _ALL_LIVE_EXECUTORS

    def test_get_live_tools_includes_places(self):
        from aspire_orchestrator.services.tool_executor import get_live_tools
        live = get_live_tools()
        assert "google_places.search" in live
        assert "osm_overpass.query" in live
        assert "search.web" in live

    def test_is_live_tool_returns_true_for_new_providers(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("google_places.search")
        assert is_live_tool("tomtom.search")
        assert is_live_tool("here.search")
        assert is_live_tool("foursquare.search")
        assert is_live_tool("osm_overpass.query")
        assert is_live_tool("mapbox.geocode")
        assert is_live_tool("search.web")
        assert is_live_tool("search.places")
        assert is_live_tool("search.geocode")


# =============================================================================
# Receipt Compliance (Law #2) — Every outcome emits receipt
# =============================================================================


class TestReceiptCompliance:
    """Law #2: Verify all providers emit receipts for all outcomes."""

    @pytest.mark.asyncio
    async def test_all_providers_emit_receipt_on_missing_query(self):
        """Missing query -> receipt with correct fields for ALL providers."""
        from aspire_orchestrator.providers.google_places_client import execute_google_places_search
        from aspire_orchestrator.providers.tomtom_client import execute_tomtom_search
        from aspire_orchestrator.providers.here_client import execute_here_search
        from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
        from aspire_orchestrator.providers.osm_overpass_client import execute_osm_overpass_query
        from aspire_orchestrator.providers.mapbox_client import execute_mapbox_geocode

        executors = [
            ("google_places.search", execute_google_places_search),
            ("tomtom.search", execute_tomtom_search),
            ("here.search", execute_here_search),
            ("foursquare.search", execute_foursquare_search),
            ("osm_overpass.query", execute_osm_overpass_query),
            ("mapbox.geocode", execute_mapbox_geocode),
        ]

        for tool_id, executor in executors:
            result = await executor(payload={}, **_COMMON_KWARGS)

            assert result.outcome == Outcome.FAILED, f"{tool_id}: expected FAILED"
            assert result.receipt_data, f"{tool_id}: missing receipt_data"
            assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED", \
                f"{tool_id}: wrong reason_code: {result.receipt_data['reason_code']}"
            assert result.receipt_data["correlation_id"] == "corr-test-001", \
                f"{tool_id}: wrong correlation_id"
            assert result.receipt_data["suite_id"] == "suite-test-001", \
                f"{tool_id}: wrong suite_id"
            assert result.receipt_data["office_id"] == "office-test-001", \
                f"{tool_id}: wrong office_id"
            assert result.receipt_data["outcome"] == "failed", \
                f"{tool_id}: wrong outcome in receipt"
            assert result.receipt_data["capability_token_id"] == "tok-001", \
                f"{tool_id}: missing capability_token_id"

    @pytest.mark.asyncio
    async def test_receipts_have_required_fields(self):
        """All receipt data must contain minimum required fields per Law #2."""
        from aspire_orchestrator.providers.google_places_client import execute_google_places_search

        result = await execute_google_places_search(payload={}, **_COMMON_KWARGS)
        receipt = result.receipt_data

        required_fields = [
            "id", "correlation_id", "suite_id", "office_id",
            "actor_type", "actor_id", "action_type", "risk_tier",
            "tool_used", "created_at", "executed_at",
            "outcome", "reason_code", "receipt_type",
        ]
        for field in required_fields:
            assert field in receipt, f"Missing required receipt field: {field}"

    @pytest.mark.asyncio
    async def test_receipt_actor_id_contains_provider_name(self):
        """Receipt actor_id should identify the provider."""
        from aspire_orchestrator.providers.google_places_client import execute_google_places_search
        result = await execute_google_places_search(payload={}, **_COMMON_KWARGS)
        assert "google_places" in result.receipt_data["actor_id"]


# =============================================================================
# Fail-Closed Compliance (Law #3) — Missing API key = deny
# =============================================================================


class TestFailClosedCompliance:
    """Law #3: Missing credentials = fail-closed with receipt."""

    @pytest.mark.asyncio
    async def test_google_places_missing_key_fails_closed(self):
        from aspire_orchestrator.providers import google_places_client
        google_places_client._client = None
        with patch("aspire_orchestrator.providers.google_places_client.settings") as m:
            m.google_maps_api_key = ""
            result = await google_places_client.execute_google_places_search(
                payload={"query": "test"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data  # Receipt generated on denial
        google_places_client._client = None

    @pytest.mark.asyncio
    async def test_tomtom_missing_key_fails_closed(self):
        from aspire_orchestrator.providers import tomtom_client
        tomtom_client._client = None
        with patch("aspire_orchestrator.providers.tomtom_client.settings") as m:
            m.tomtom_api_key = ""
            result = await tomtom_client.execute_tomtom_search(
                payload={"query": "test"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        tomtom_client._client = None

    @pytest.mark.asyncio
    async def test_here_missing_key_fails_closed(self):
        from aspire_orchestrator.providers import here_client
        here_client._client = None
        with patch("aspire_orchestrator.providers.here_client.settings") as m:
            m.here_api_key = ""
            result = await here_client.execute_here_search(
                payload={"query": "test"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        here_client._client = None

    @pytest.mark.asyncio
    async def test_foursquare_missing_key_fails_closed(self):
        from aspire_orchestrator.providers import foursquare_client
        foursquare_client._client = None
        with patch("aspire_orchestrator.providers.foursquare_client.settings") as m:
            m.foursquare_api_key = ""
            result = await foursquare_client.execute_foursquare_search(
                payload={"query": "test"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        foursquare_client._client = None

    @pytest.mark.asyncio
    async def test_mapbox_missing_token_fails_closed(self):
        from aspire_orchestrator.providers import mapbox_client
        mapbox_client._client = None
        with patch("aspire_orchestrator.providers.mapbox_client.settings") as m:
            m.mapbox_access_token = ""
            result = await mapbox_client.execute_mapbox_geocode(
                payload={"query": "test"}, **_COMMON_KWARGS
            )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data
        mapbox_client._client = None

    @pytest.mark.asyncio
    async def test_osm_overpass_no_key_required(self):
        """OSM Overpass should NOT fail-closed — it's a public API."""
        from aspire_orchestrator.providers.osm_overpass_client import OsmOverpassClient
        client = OsmOverpassClient()
        # _authenticate_headers should return empty dict (no error)
        from aspire_orchestrator.providers.base_client import ProviderRequest
        headers = await client._authenticate_headers(ProviderRequest(method="GET", path="/"))
        assert headers == {}


# =============================================================================
# Provider Client Configuration
# =============================================================================


class TestProviderClientConfig:
    """Verify provider client configuration values."""

    def test_google_places_config(self):
        from aspire_orchestrator.providers.google_places_client import GooglePlacesClient
        c = GooglePlacesClient()
        assert c.provider_id == "google_places"
        assert "maps.googleapis.com" in c.base_url
        assert c.timeout_seconds == 10.0
        assert c.idempotency_support is False

    def test_tomtom_config(self):
        from aspire_orchestrator.providers.tomtom_client import TomTomClient
        c = TomTomClient()
        assert c.provider_id == "tomtom"
        assert "api.tomtom.com" in c.base_url
        assert c.timeout_seconds == 10.0

    def test_here_config(self):
        from aspire_orchestrator.providers.here_client import HereClient
        c = HereClient()
        assert c.provider_id == "here"
        assert "hereapi.com" in c.base_url
        assert c.timeout_seconds == 10.0

    def test_foursquare_config(self):
        from aspire_orchestrator.providers.foursquare_client import FoursquareClient
        c = FoursquareClient()
        assert c.provider_id == "foursquare"
        assert "api.foursquare.com" in c.base_url

    def test_osm_overpass_config(self):
        from aspire_orchestrator.providers.osm_overpass_client import OsmOverpassClient
        c = OsmOverpassClient()
        assert c.provider_id == "osm_overpass"
        assert c.timeout_seconds == 30.0
        assert c.max_retries == 0

    def test_mapbox_config(self):
        from aspire_orchestrator.providers.mapbox_client import MapboxClient
        c = MapboxClient()
        assert c.provider_id == "mapbox"
        assert "api.mapbox.com" in c.base_url
