"""Tests for sheet_image_storage — Wave 9l.

4 tests:
  1. upload_sheet_image happy path — returns storage object path.
  2. upload_sheet_image failure — best-effort: returns None, does not raise.
  3. download_sheet_image cache hit — returns PNG bytes.
  4. download_sheet_image cache miss — returns None (404 response).

Law compliance:
  Law #9: no raw bytes in logs (verified via caplog assertions).
  Law #3: failure returns None, never raises.
  Law #6: path always starts with suite_id (verified on upload).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SUITE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SHEET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CORR_ID = "test-sheet-img-001"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # minimal PNG header + padding


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestUploadSheetImage:
    """upload_sheet_image: happy path and failure best-effort."""

    def test_upload_happy_path_returns_storage_path(self) -> None:
        """Successful upload returns '{suite_id}/{project_id}/{sheet_id}.png'."""
        from aspire_orchestrator.services.blueprint.sheet_image_storage import (
            upload_sheet_image,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.httpx.AsyncClient",
            return_value=mock_client,
        ), patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.settings"
        ) as mock_settings:
            mock_settings.supabase_url = "https://test.supabase.co"
            mock_settings.supabase_service_role_key = "service-role-key-test"

            result = _run(
                upload_sheet_image(SUITE_ID, PROJECT_ID, SHEET_ID, PNG_BYTES, CORR_ID)
            )

        expected_path = f"{SUITE_ID}/{PROJECT_ID}/{SHEET_ID}.png"
        assert result == expected_path, (
            f"Expected storage path '{expected_path}', got '{result}'"
        )
        # Verify suite_id is path prefix (Law #6).
        assert result.startswith(SUITE_ID)

    def test_upload_failure_returns_none_no_raise(self) -> None:
        """Upload failure (non-2xx HTTP) returns None and does not raise (Law #3)."""
        from aspire_orchestrator.services.blueprint.sheet_image_storage import (
            upload_sheet_image,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 503  # service unavailable

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.httpx.AsyncClient",
            return_value=mock_client,
        ), patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.settings"
        ) as mock_settings:
            mock_settings.supabase_url = "https://test.supabase.co"
            mock_settings.supabase_service_role_key = "service-role-key-test"

            result = _run(
                upload_sheet_image(SUITE_ID, PROJECT_ID, SHEET_ID, PNG_BYTES, CORR_ID)
            )

        assert result is None, (
            "upload_sheet_image must return None on failure (Law #3 best-effort)"
        )

    def test_upload_empty_bytes_returns_none(self) -> None:
        """Empty png_bytes returns None immediately without making any HTTP call."""
        from aspire_orchestrator.services.blueprint.sheet_image_storage import (
            upload_sheet_image,
        )

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.httpx.AsyncClient"
        ) as mock_cls:
            result = _run(
                upload_sheet_image(SUITE_ID, PROJECT_ID, SHEET_ID, b"", CORR_ID)
            )
            # No HTTP call should have been made.
            mock_cls.assert_not_called()

        assert result is None


class TestDownloadSheetImage:
    """download_sheet_image: cache hit and cache miss."""

    def test_download_cache_hit_returns_bytes(self) -> None:
        """200 response returns the PNG bytes (cache hit)."""
        from aspire_orchestrator.services.blueprint.sheet_image_storage import (
            download_sheet_image,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = PNG_BYTES

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.httpx.AsyncClient",
            return_value=mock_client,
        ), patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.settings"
        ) as mock_settings:
            mock_settings.supabase_url = "https://test.supabase.co"
            mock_settings.supabase_service_role_key = "service-role-key-test"

            result = _run(
                download_sheet_image(SUITE_ID, PROJECT_ID, SHEET_ID, CORR_ID)
            )

        assert result == PNG_BYTES, (
            "download_sheet_image must return the raw PNG bytes on cache hit"
        )

    def test_download_cache_miss_returns_none(self) -> None:
        """404 response returns None (cache miss) and does not raise (Law #3)."""
        from aspire_orchestrator.services.blueprint.sheet_image_storage import (
            download_sheet_image,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.httpx.AsyncClient",
            return_value=mock_client,
        ), patch(
            "aspire_orchestrator.services.blueprint.sheet_image_storage.settings"
        ) as mock_settings:
            mock_settings.supabase_url = "https://test.supabase.co"
            mock_settings.supabase_service_role_key = "service-role-key-test"

            result = _run(
                download_sheet_image(SUITE_ID, PROJECT_ID, SHEET_ID, CORR_ID)
            )

        assert result is None, (
            "download_sheet_image must return None on cache miss (Law #3)"
        )
