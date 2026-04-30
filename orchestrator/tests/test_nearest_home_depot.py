"""Round 4 A — Google Places nearest-Home-Depot-by-user-address resolver.

Mode of operation:
  1. Live recording (record-once): set GOOGLE_PLACES_RECORD_LIVE=1 +
     ASPIRE_GOOGLE_MAPS_API_KEY in env. Tests run against real Google
     Geocoding + Places v1 and write cassettes to
     tests/cassettes/nearest_home_depot/.
  2. Replay (default for CI): cassettes are replayed; no network.

Skip behavior:
  - If a cassette is missing AND GOOGLE_PLACES_RECORD_LIVE is not set,
    the case is SKIPPED (so CI can run incrementally).

Why no fallback fail mode:
  - Per Aspire no-fallback-design-principle: tests treat the primary path
    as the contract. The trades-playbook fallback to Wave A.5 is graceful
    degradation when the user has not provided an address — not a
    fallback when the resolver itself fails. Negative tests assert None
    is returned cleanly; integration test asserts trades.py wires the
    resolved zip into SerpApi.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

vcr = pytest.importorskip("vcr", reason="vcrpy required for Places cassette replay")

from aspire_orchestrator.services.adam.places_nearest_finder import (  # noqa: E402
    NearestStore,
    _extract_postal_code,
    _haversine_miles,
    find_nearest_home_depot_by_address,
)

CASSETTE_DIR = Path(__file__).parent / "cassettes" / "nearest_home_depot"
CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

RECORD_LIVE = os.environ.get("GOOGLE_PLACES_RECORD_LIVE", "").lower() in {"1", "true", "yes"}
GOOGLE_API_KEY = os.environ.get("ASPIRE_GOOGLE_MAPS_API_KEY", "") or os.environ.get(
    "GOOGLE_MAPS_API_KEY", ""
)


# --------------------------------------------------------------------------
# Fixtures — 5 real-world addresses across the US
# --------------------------------------------------------------------------

ADDRESS_FIXTURES: list[dict[str, Any]] = [
    {
        "address": "1234 N Monroe St, Tallahassee, FL 32303",
        "expected_state_in_address": "FL",
        "label": "tallahassee_fl",
    },
    {
        "address": "5678 Peachtree St, Atlanta, GA 30309",
        "expected_state_in_address": "GA",
        "label": "atlanta_ga",
    },
    {
        "address": "1010 Main St, Forest Park, GA 30297",
        "expected_state_in_address": "GA",
        "label": "forest_park_ga",
    },
    {
        "address": "999 Bond St, Denver, CO 80202",
        "expected_state_in_address": "CO",
        "label": "denver_co",
    },
    {
        "address": "100 Capitol Ave, Hartford, CT 06106",
        "expected_state_in_address": "CT",
        "label": "hartford_ct",
    },
]


def _cassette_path(label: str) -> Path:
    return CASSETTE_DIR / f"{label}.yaml"


def _vcr_config() -> dict[str, Any]:
    record_mode = "new_episodes" if RECORD_LIVE else "none"
    return {
        "record_mode": record_mode,
        "filter_headers": [
            ("X-Goog-Api-Key", "REDACTED-GOOGLE-KEY"),
            "authorization",
        ],
        "filter_query_parameters": [("key", "REDACTED")],
        "match_on": ["method", "scheme", "host", "path", "query"],
        "decode_compressed_response": True,
    }


@pytest.fixture(scope="session")
def my_vcr() -> Any:
    return vcr.VCR(**_vcr_config())


# --------------------------------------------------------------------------
# Pure unit tests — haversine + postal extraction (no network, always run)
# --------------------------------------------------------------------------


class TestHaversine:
    def test_zero_distance_for_same_point(self) -> None:
        assert _haversine_miles(40.0, -75.0, 40.0, -75.0) == pytest.approx(0.0, abs=0.001)

    def test_known_distance_nyc_to_la(self) -> None:
        # NYC (40.7128, -74.0060) to LA (34.0522, -118.2437) ~ 2451 miles.
        miles = _haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)
        assert 2400 < miles < 2500

    def test_short_distance_within_city(self) -> None:
        # Two points ~1 mile apart in Tallahassee.
        miles = _haversine_miles(30.4383, -84.2807, 30.4527, -84.2807)
        assert 0.9 < miles < 1.1


class TestPostalCodeExtraction:
    def test_standard_us_format(self) -> None:
        assert _extract_postal_code(
            "1100 Capital Cir NE, Tallahassee, FL 32308, USA"
        ) == "32308"

    def test_zip_plus_four(self) -> None:
        assert _extract_postal_code(
            "1100 Capital Cir NE, Tallahassee, FL 32308-1234, USA"
        ) == "32308"

    def test_no_country_tail(self) -> None:
        assert _extract_postal_code("1100 Capital Cir NE, Tallahassee, FL 32308") == "32308"

    def test_empty_string(self) -> None:
        assert _extract_postal_code("") == ""

    def test_no_zip_present(self) -> None:
        assert _extract_postal_code("Some random address, no postal") == ""

    def test_loose_fallback_when_tail_doesnt_match(self) -> None:
        # ZIP is in the middle, not the tail — loose regex catches it.
        assert _extract_postal_code("PO Box 32308 Tallahassee Florida") == "32308"


# --------------------------------------------------------------------------
# Negative path tests — input validation (no network)
# --------------------------------------------------------------------------


class TestNegativePath:
    @pytest.mark.asyncio
    async def test_empty_string_returns_none(self) -> None:
        result = await find_nearest_home_depot_by_address("")
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_none(self) -> None:
        result = await find_nearest_home_depot_by_address("   \n\t  ")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self) -> None:
        # Override settings with empty key — should return None, not raise.
        from aspire_orchestrator.config.settings import settings as _settings
        original_key = getattr(_settings, "google_maps_api_key", "")
        try:
            _settings.google_maps_api_key = ""
            result = await find_nearest_home_depot_by_address(
                "1234 N Monroe St, Tallahassee, FL 32303",
                timeout=2.0,
            )
            assert result is None
        finally:
            _settings.google_maps_api_key = original_key

    @pytest.mark.asyncio
    async def test_geocoding_zero_results_returns_none(self) -> None:
        """Mock geocoding returns ZERO_RESULTS — caller swallows and returns None."""
        from aspire_orchestrator.config.settings import settings as _settings
        original_key = getattr(_settings, "google_maps_api_key", "")
        try:
            _settings.google_maps_api_key = "test-key"
            with patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._geocode",
                new=AsyncMock(return_value=(None, None)),
            ):
                result = await find_nearest_home_depot_by_address(
                    "asdfqwerty zzz nowhere",
                    timeout=2.0,
                )
                assert result is None
        finally:
            _settings.google_maps_api_key = original_key

    @pytest.mark.asyncio
    async def test_no_hd_within_radius_returns_none(self) -> None:
        """Mock searchNearby returns non-HD-only results — no Home Depot match."""
        from aspire_orchestrator.config.settings import settings as _settings
        original_key = getattr(_settings, "google_maps_api_key", "")
        try:
            _settings.google_maps_api_key = "test-key"
            with patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._geocode",
                new=AsyncMock(return_value=(40.0, -75.0)),
            ), patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._search_nearby",
                new=AsyncMock(return_value=[
                    {
                        "id": "PLACE_ABC",
                        "displayName": {"text": "Lowe's"},
                        "formattedAddress": "100 Main St, Anywhere, PA 19000, USA",
                        "location": {"latitude": 40.01, "longitude": -75.01},
                    },
                ]),
            ):
                result = await find_nearest_home_depot_by_address(
                    "100 Main St, Anywhere, PA",
                    timeout=2.0,
                )
                assert result is None
        finally:
            _settings.google_maps_api_key = original_key

    @pytest.mark.asyncio
    async def test_helper_swallows_unexpected_exception(self) -> None:
        """Geocoder raising a non-HTTPError must NOT propagate — return None."""
        from aspire_orchestrator.config.settings import settings as _settings
        original_key = getattr(_settings, "google_maps_api_key", "")
        try:
            _settings.google_maps_api_key = "test-key"
            with patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._resolve_nearest",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                result = await find_nearest_home_depot_by_address(
                    "1234 Main St, Anywhere, US",
                    timeout=2.0,
                )
                assert result is None
        finally:
            _settings.google_maps_api_key = original_key


# --------------------------------------------------------------------------
# Mocked happy-path test — verifies plumbing without network
# --------------------------------------------------------------------------


class TestMockedHappyPath:
    @pytest.mark.asyncio
    async def test_resolves_nearest_hd_with_mocked_apis(self) -> None:
        """End-to-end with mocked geocode + searchNearby. Asserts NearestStore shape."""
        from aspire_orchestrator.config.settings import settings as _settings
        original_key = getattr(_settings, "google_maps_api_key", "")
        try:
            _settings.google_maps_api_key = "test-key-abc"
            with patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._geocode",
                new=AsyncMock(return_value=(30.4383, -84.2807)),
            ), patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._search_nearby",
                new=AsyncMock(return_value=[
                    {
                        "id": "ChIJ_HOME_DEPOT_TLH",
                        "displayName": {"text": "The Home Depot"},
                        "formattedAddress": "1100 Capital Cir NE, Tallahassee, FL 32308, USA",
                        "shortFormattedAddress": "1100 Capital Cir NE, Tallahassee",
                        "location": {"latitude": 30.4527, "longitude": -84.2611},
                        "photos": [{"name": "places/ChIJ_HOME_DEPOT_TLH/photos/PHOTO_REF_123"}],
                    },
                ]),
            ):
                result = await find_nearest_home_depot_by_address(
                    "1234 N Monroe St, Tallahassee, FL 32303",
                    timeout=3.0,
                )

            assert result is not None
            assert isinstance(result, NearestStore)
            assert "Home Depot" in result.name
            assert result.postal_code == "32308"
            assert result.distance_miles < 50
            assert result.distance_miles > 0
            assert result.photo_url.startswith(
                "https://places.googleapis.com/v1/places/ChIJ_HOME_DEPOT_TLH/photos/"
            )
            assert "key=test-key-abc" in result.photo_url
            assert result.place_id == "ChIJ_HOME_DEPOT_TLH"
            assert result.user_lat == 30.4383
            assert result.user_lng == -84.2807
            assert result.lat == 30.4527
            assert result.lng == -84.2611

            d = result.to_dict()
            assert set(d.keys()) >= {
                "place_id", "name", "address", "postal_code",
                "lat", "lng", "distance_miles", "photo_url",
                "user_lat", "user_lng",
            }
        finally:
            _settings.google_maps_api_key = original_key

    @pytest.mark.asyncio
    async def test_filters_out_non_hd_picks_first_hd(self) -> None:
        """searchNearby returned Lowe's first then Home Depot — pick HD."""
        from aspire_orchestrator.config.settings import settings as _settings
        original_key = getattr(_settings, "google_maps_api_key", "")
        try:
            _settings.google_maps_api_key = "test-key"
            with patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._geocode",
                new=AsyncMock(return_value=(33.7490, -84.3880)),
            ), patch(
                "aspire_orchestrator.services.adam.places_nearest_finder._search_nearby",
                new=AsyncMock(return_value=[
                    {
                        "id": "LOWES_PLACE",
                        "displayName": {"text": "Lowe's Home Improvement"},
                        "formattedAddress": "200 Peach St, Atlanta, GA 30309, USA",
                        "location": {"latitude": 33.75, "longitude": -84.39},
                    },
                    {
                        "id": "HD_PLACE",
                        "displayName": {"text": "The Home Depot"},
                        "formattedAddress": "300 Oak Ave, Atlanta, GA 30309, USA",
                        "location": {"latitude": 33.76, "longitude": -84.40},
                        "photos": [],
                    },
                ]),
            ):
                result = await find_nearest_home_depot_by_address(
                    "5678 Peachtree St, Atlanta, GA 30309",
                )

            assert result is not None
            assert result.place_id == "HD_PLACE"
            assert "Home Depot" in result.name
            assert result.photo_url == ""  # No photos in fixture
        finally:
            _settings.google_maps_api_key = original_key


# --------------------------------------------------------------------------
# Live cassette tests — record once with GOOGLE_PLACES_RECORD_LIVE=1
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    ADDRESS_FIXTURES,
    ids=[f["label"] for f in ADDRESS_FIXTURES],
)
@pytest.mark.asyncio
async def test_nearest_home_depot_real_address(
    fixture: dict[str, Any],
    my_vcr: Any,
) -> None:
    """For each fixture: resolve nearest HD via real Google APIs (recorded)."""
    cassette = _cassette_path(fixture["label"])

    if not RECORD_LIVE and not cassette.exists():
        pytest.skip(
            f"cassette not yet recorded: {cassette.name}. "
            "Set GOOGLE_PLACES_RECORD_LIVE=1 + ASPIRE_GOOGLE_MAPS_API_KEY to seed."
        )
    if RECORD_LIVE and not GOOGLE_API_KEY:
        pytest.fail(
            "GOOGLE_PLACES_RECORD_LIVE=1 but ASPIRE_GOOGLE_MAPS_API_KEY is empty. "
            "Set the API key to record cassettes."
        )

    if RECORD_LIVE:
        # Inject the live key into settings for the resolver to read.
        from aspire_orchestrator.config.settings import settings as _settings
        _settings.google_maps_api_key = GOOGLE_API_KEY

    with my_vcr.use_cassette(str(cassette)):
        result = await find_nearest_home_depot_by_address(
            fixture["address"],
            timeout=10.0,  # Live calls can be slower than the production 3s budget.
        )

    assert result is not None, (
        f"Expected NearestStore for {fixture['address']}, got None. "
        "If recording, check API key + quota; if replaying, re-record."
    )
    assert isinstance(result, NearestStore)
    assert "home depot" in result.name.lower(), (
        f"Result name does not contain 'Home Depot': {result.name!r}"
    )
    assert len(result.postal_code) == 5, (
        f"postal_code is not 5 digits: {result.postal_code!r}"
    )
    assert result.postal_code.isdigit()
    assert 0 < result.distance_miles < 50, (
        f"distance_miles out of expected range: {result.distance_miles}"
    )
    assert result.photo_url.startswith("https://places.googleapis.com/v1/places/"), (
        f"photo_url shape unexpected: {result.photo_url!r}"
    )
    assert "/media" in result.photo_url
    assert result.place_id, "place_id must be non-empty"
    # Address should mention the state we asked about — best-effort.
    assert fixture["expected_state_in_address"] in result.address, (
        f"Expected state {fixture['expected_state_in_address']} in {result.address!r}"
    )


# --------------------------------------------------------------------------
# End-to-end integration with trades.py — verifies the resolver pins delivery_zip
# --------------------------------------------------------------------------


class TestTradesIntegration:
    """Verify trades.py wires nearest_store.postal_code into SerpApi delivery_zip
    and passes nearest_store.photo_url + distance_miles into store_summary.

    We mock the resolver itself + SerpApi clients so the test runs without
    network and without needing the static directory entry to match.
    """

    @pytest.mark.asyncio
    async def test_user_address_pins_delivery_zip_and_photo(self) -> None:
        from aspire_orchestrator.services.adam.places_nearest_finder import NearestStore
        from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
        from aspire_orchestrator.services.adam.playbooks.trades import (
            execute_tool_material_price_check,
        )
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult

        ctx = PlaybookContext(
            suite_id="suite-test",
            office_id="office-test",
            correlation_id="corr-r4-test",
        )

        nearest = NearestStore(
            place_id="ChIJ_HOME_DEPOT_TLH",
            name="The Home Depot",
            address="1100 Capital Cir NE, Tallahassee, FL 32308, USA",
            postal_code="32308",
            lat=30.4527,
            lng=-84.2611,
            distance_miles=2.3,
            photo_url="https://places.googleapis.com/v1/places/ChIJ_HOME_DEPOT_TLH/photos/X/media?maxHeightPx=400&maxWidthPx=600&key=k",
            user_lat=30.4383,
            user_lng=-84.2807,
        )

        # Capture the delivery_zip that the trades playbook hands to SerpApi.
        captured_payloads: list[dict[str, Any]] = []

        async def _fake_serpapi_homedepot(*, payload: dict[str, Any], **_: Any) -> Any:
            captured_payloads.append(dict(payload))
            return ToolExecutionResult(
                outcome=Outcome.SUCCESS,
                tool_id="serpapi.home_depot.search",
                data={
                    "results": [
                        {
                            "title": "DEWALT 20V MAX Drill",
                            "price": 99.99,
                            "link": "https://homedepot.com/p/123",
                            "thumbnail": "https://homedepot.com/img/123.jpg",
                            "product_id": "123456",
                            "pickup": {"store_id": "999999"},
                        },
                    ],
                    "store": {"store_name": "Home Depot Tallahassee", "store_id": "999999"},
                },
                receipt_data={},
            )

        async def _fake_serpapi_shopping(*_: Any, **__: Any) -> Any:
            return ToolExecutionResult(
                outcome=Outcome.SUCCESS,
                tool_id="serpapi.shopping.search",
                data={"results": []},
                receipt_data={},
            )

        with patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            new=AsyncMock(return_value=nearest),
        ), patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            new=AsyncMock(side_effect=_fake_serpapi_homedepot),
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            new=AsyncMock(side_effect=_fake_serpapi_shopping),
        ), patch(
            "aspire_orchestrator.services.adam.hd_store_resolver.resolve_store_async",
            new=AsyncMock(return_value=None),  # Not consulted on Round-4 path.
        ):
            response = await execute_tool_material_price_check(
                query="DEWALT 20V MAX drill",
                ctx=ctx,
                user_address="1234 N Monroe St, Tallahassee, FL 32303",
                voice_path=False,  # Force text path so all 3 attempts can run if needed.
            )

        # SerpApi must have received delivery_zip from nearest_store.postal_code.
        assert captured_payloads, "execute_serpapi_homedepot_search was not called"
        assert captured_payloads[0]["delivery_zip"] == "32308"

        # Provider tracking must reflect the Round-4 call.
        assert "google_places_nearest" in response.providers_called

        # store_summary must carry the Google address + photo + distance.
        store_summaries = [r for r in response.records if r.get("card_kind") == "store_summary"]
        assert len(store_summaries) == 1, "Expected exactly one store_summary record"
        ss = store_summaries[0]
        # Google's formatted address overrides whatever the static dir gave us.
        assert ss["address"] == nearest.address
        assert ss["image_url"] == nearest.photo_url
        assert ss["distance_miles"] == round(nearest.distance_miles, 1)

    @pytest.mark.asyncio
    async def test_user_address_resolution_failure_falls_through(self) -> None:
        """When find_nearest returns None, trades falls back to Wave A.5 — providers
        list shows the failure marker but execution continues.
        """
        from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
        from aspire_orchestrator.services.adam.playbooks.trades import (
            execute_tool_material_price_check,
        )
        from aspire_orchestrator.models import Outcome
        from aspire_orchestrator.services.tool_types import ToolExecutionResult

        ctx = PlaybookContext(
            suite_id="suite-test",
            office_id="office-test",
            correlation_id="corr-r4-fallback",
        )

        async def _fake_serpapi_homedepot(*_: Any, **__: Any) -> Any:
            return ToolExecutionResult(
                outcome=Outcome.SUCCESS,
                tool_id="serpapi.home_depot.search",
                data={"results": [], "store": {}},
                receipt_data={},
            )

        async def _fake_serpapi_shopping(*_: Any, **__: Any) -> Any:
            return ToolExecutionResult(
                outcome=Outcome.SUCCESS,
                tool_id="serpapi.shopping.search",
                data={"results": []},
                receipt_data={},
            )

        with patch(
            "aspire_orchestrator.services.adam.places_nearest_finder.find_nearest_home_depot_by_address",
            new=AsyncMock(return_value=None),
        ), patch(
            "aspire_orchestrator.providers.serpapi_homedepot_client.execute_serpapi_homedepot_search",
            new=AsyncMock(side_effect=_fake_serpapi_homedepot),
        ), patch(
            "aspire_orchestrator.providers.serpapi_shopping_client.execute_serpapi_shopping_search",
            new=AsyncMock(side_effect=_fake_serpapi_shopping),
        ), patch(
            "aspire_orchestrator.services.adam.hd_store_resolver.resolve_store_async",
            new=AsyncMock(return_value=None),
        ):
            response = await execute_tool_material_price_check(
                query="paint roller",
                ctx=ctx,
                user_address="not a real address xxxx",
                voice_path=False,
            )

        # Failure marker present (Law #2 evidence trail).
        assert "google_places_nearest_failed" in response.providers_called
        # Did not throw — we either got an error response or empty card pack.
        assert response.playbook == "TOOL_MATERIAL_PRICE_CHECK"
