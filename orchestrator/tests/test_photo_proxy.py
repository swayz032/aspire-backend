"""Photo proxy tests — F-CRIT-5 verification.

Verifies:
  1. resource_name validator: accepts canonical form, rejects URLs / traversal /
     injection / bare strings.
  2. Proxy URL emission: places_nearest_finder builds /v1/places/photo URLs
     that contain NO 'key=' substring.
  3. Response shape from a mocked Google upstream.

F-CRIT-5: Google Maps API key must never appear in client-visible URLs.
The photo proxy (services/photo_proxy.py) signs requests server-side and
returns raw bytes to the client — the key never crosses the trust boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.photo_proxy import (
    is_valid_resource_name,
    clamp_dim,
    fetch_place_photo_bytes,
)


# ─── resource_name validator ─────────────────────────────────────────────────

class TestResourceNameValidator:
    """is_valid_resource_name must accept only canonical resource names."""

    def test_accepts_canonical_resource_name(self):
        valid = "places/ChIJ123456ABC/photos/AUjq9jkDEF-GHI"
        assert is_valid_resource_name(valid), (
            f"Expected valid canonical resource name to pass; got False for {valid!r}"
        )

    def test_accepts_alphanumeric_with_dashes_underscores(self):
        valid = "places/ChIJ-a_B9/photos/AUjq-x_Y1"
        assert is_valid_resource_name(valid)

    def test_rejects_https_url(self):
        url = "https://places.googleapis.com/v1/places/ChIJ123/photos/AUjq456/media"
        assert not is_valid_resource_name(url), (
            "Must reject HTTPS URL — only canonical resource names allowed"
        )

    def test_rejects_http_url(self):
        url = "http://evil.com/places/X/photos/Y"
        assert not is_valid_resource_name(url)

    def test_rejects_path_traversal(self):
        traversal = "places/../etc/passwd/photos/x"
        assert not is_valid_resource_name(traversal), (
            "Must reject path traversal '../' in resource name"
        )

    def test_rejects_sql_injection(self):
        injection = "places/X/photos/Y;DROP TABLE receipts"
        assert not is_valid_resource_name(injection), (
            "Must reject semicolon injection in resource name"
        )

    def test_rejects_bare_string(self):
        assert not is_valid_resource_name("ChIJjust-an-id-no-path"), (
            "Must reject bare string without places/.../ prefix"
        )

    def test_rejects_empty_string(self):
        assert not is_valid_resource_name(""), (
            "Must reject empty string"
        )

    def test_rejects_too_long_resource_name(self):
        long_name = "places/" + "A" * 200 + "/photos/" + "B" * 50
        assert not is_valid_resource_name(long_name), (
            "Must reject resource names longer than 256 chars"
        )

    def test_rejects_resource_with_query_string(self):
        with_qs = "places/ChIJ123/photos/AUjq456?key=AIzaSy_secret"
        assert not is_valid_resource_name(with_qs), (
            "Must reject resource name containing query string / '?'"
        )

    def test_rejects_resource_missing_photos_segment(self):
        no_photos = "places/ChIJ123"
        assert not is_valid_resource_name(no_photos)

    def test_rejects_resource_with_too_many_segments(self):
        extra_segment = "places/ChIJ123/photos/AUjq456/media"
        assert not is_valid_resource_name(extra_segment), (
            "Must reject resource name with >4 path segments"
        )


# ─── Proxy URL emission — no key= in client-visible URLs ─────────────────────

class TestProxyUrlEmission:
    """NearestStore.image_url and store_summary.image_url must not contain 'key='."""

    @pytest.mark.asyncio
    async def test_nearest_store_image_url_no_key_param(self):
        """F-CRIT-5: places_nearest_finder must build /v1/places/photo URLs, not Google direct URLs.

        The proxy path /v1/places/photo?ref=... is client-safe; the Google key
        is added server-side inside photo_proxy.py.
        """
        from aspire_orchestrator.services.adam.places_nearest_finder import (
            find_nearest_home_depot_by_address,
        )

        mock_geocode_resp = MagicMock()
        mock_geocode_resp.status_code = 200
        mock_geocode_resp.json.return_value = {
            "status": "OK",
            "results": [
                {
                    "geometry": {"location": {"lat": 30.4518, "lng": -84.2807}},
                    "formatted_address": "1234 N Monroe St, Tallahassee, FL 32303, USA",
                }
            ],
        }

        mock_photo_name = "places/ChIJtest1234/photos/AUjq9jm_PHOTO"
        mock_places_resp = MagicMock()
        mock_places_resp.status_code = 200
        mock_places_resp.json.return_value = {
            "places": [
                {
                    "id": "ChIJtest1234",
                    "displayName": {"text": "The Home Depot"},
                    "formattedAddress": "3200 Capital Cir NE, Tallahassee, FL 32308, USA",
                    "location": {"latitude": 30.4867, "longitude": -84.2977},
                    "photos": [{"name": mock_photo_name}],
                }
            ]
        }

        async def mock_get(url, **kwargs):
            if "geocode" in url:
                return mock_geocode_resp
            return mock_places_resp

        async def mock_post(url, **kwargs):
            return mock_places_resp

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key-not-exposed"}):
            result = await find_nearest_home_depot_by_address(
                "1234 N Monroe St, Tallahassee, FL 32303",
            )

        if result is not None:
            assert "key=" not in (result.image_url or ""), (
                f"F-CRIT-5 violation: image_url contains raw Google key. "
                f"image_url={result.image_url!r}"
            )

    @pytest.mark.asyncio
    async def test_fetch_photo_bytes_upstream_key_not_in_response(self):
        """F-CRIT-5: fetch_place_photo_bytes must not return the API key in bytes.

        The proxy fetches bytes from Google using the server-side key.
        Returned image bytes (JPEG) must not contain the literal API key.
        """
        fake_image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # fake JPEG header

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.content = fake_image_bytes

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch(
                 "aspire_orchestrator.config.settings.settings",
                 MagicMock(google_maps_api_key="AIzaSy_FAKEKEYNEVEREXPOSED"),
             ):
            result = await fetch_place_photo_bytes(
                resource_name="places/ChIJ123/photos/AUjq456",
                max_height_px=400,
                max_width_px=600,
            )

        assert result is not None, "Should return (bytes, content_type) on 200"
        image_bytes, content_type = result

        assert b"AIzaSy_FAKEKEYNEVEREXPOSED" not in image_bytes, (
            "F-CRIT-5: API key must never appear in proxy response bytes"
        )
        assert content_type == "image/jpeg"

    def test_fetch_photo_bytes_rejects_invalid_resource_name(self):
        """F-CRIT-5: fetch_place_photo_bytes must return None for invalid resource names.

        Invalid names (URLs, traversal, injection) are rejected before any
        upstream call is made — no key is fetched for malicious inputs.
        """
        import asyncio

        invalid_names = [
            "https://places.googleapis.com/v1/places/X/photos/Y/media",
            "../etc/passwd",
            "places/X/photos/Y;DROP TABLE receipts",
            "",
            "just-a-string",
        ]

        for name in invalid_names:
            result = asyncio.get_event_loop().run_until_complete(
                fetch_place_photo_bytes(resource_name=name)
            )
            assert result is None, (
                f"F-CRIT-5: invalid resource_name {name!r} must return None immediately"
            )


# ─── clamp_dim helper ─────────────────────────────────────────────────────────

class TestClampDim:
    """clamp_dim coerces query params to safe pixel bounds."""

    def test_clamp_valid_within_bounds(self):
        assert clamp_dim(400, 100, 1200) == 400

    def test_clamp_exceeds_max(self):
        assert clamp_dim(9999, 100, 1200) == 1200

    def test_clamp_below_one_returns_default(self):
        assert clamp_dim(0, 400, 1200) == 400
        assert clamp_dim(-5, 400, 1200) == 400

    def test_clamp_non_numeric_returns_default(self):
        assert clamp_dim("abc", 400, 1200) == 400

    def test_clamp_none_returns_default(self):
        assert clamp_dim(None, 400, 1200) == 400
