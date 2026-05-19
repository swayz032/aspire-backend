"""Supabase Storage helper for full-resolution blueprint sheet PNG images.

Uploads 200-DPI full-resolution PNG sheets to the `blueprint-sheet-images` bucket.
Used by the SEE stage to retrieve cached renders and skip PDF re-rasterization.
Path scheme: {suite_id}/{project_id}/{sheet_id}.png

Law compliance:
  Law #6: Path is always prefixed with suite_id — no cross-tenant path access.
  Law #9: Image bytes never logged; only sheet_id, size_bytes, duration_ms.
  Law #3: Returns None on failure rather than raising — caller decides whether to
          fail the whole ingest (currently: soft failure, SEE falls back to re-render).

Pattern: mirrors thumbnail_storage.py exactly. Uses the Supabase Storage REST API
directly (consistent with how supabase_client.py uses the PostgREST REST API).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.supabase_client import SupabaseClientError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BUCKET = "blueprint-sheet-images"
_UPLOAD_TIMEOUT = 30.0  # full-res PNG may be several MB


def _storage_base_url() -> str:
    url = settings.supabase_url
    if not url:
        raise SupabaseClientError("storage", detail="Missing ASPIRE_SUPABASE_URL")
    return f"{url.rstrip('/')}/storage/v1"


def _headers() -> dict[str, str]:
    key = settings.supabase_service_role_key
    if not key:
        raise SupabaseClientError("storage", detail="Missing ASPIRE_SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


async def upload_sheet_image(
    suite_id: str,
    project_id: str,
    sheet_id: str,
    png_bytes: bytes,
    correlation_id: str,
) -> str | None:
    """Upload a full-resolution PNG sheet image to Supabase Storage.

    Args:
        suite_id: Tenant UUID — used as path prefix for isolation (Law #6).
        project_id: Blueprint project UUID.
        sheet_id: Blueprint sheet UUID — used as the object filename.
        png_bytes: Raw PNG image bytes (200 DPI rendered page).
        correlation_id: Request correlation ID for log tracing.

    Returns:
        Storage object path string on success (e.g. '{suite_id}/{project_id}/{sheet_id}.png').
        None on any failure (caller handles soft failure — SEE falls back to re-render).

    Law #9: Never logs raw bytes. Logs only sheet_id, size_bytes, duration_ms.
    """
    if not png_bytes:
        logger.warning(
            "sheet_image_storage: empty png_bytes for sheet=%s corr=%s",
            sheet_id[:8],
            correlation_id[:8],
        )
        return None

    object_path = f"{suite_id}/{project_id}/{sheet_id}.png"
    base = _storage_base_url()
    upload_url = f"{base}/object/{_BUCKET}/{object_path}"

    start = time.monotonic()
    try:
        hdrs = _headers()
        hdrs["Content-Type"] = "image/png"
        # upsert=true means a re-ingest of the same sheet overwrites the cached image
        hdrs["x-upsert"] = "true"

        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
            resp = await client.post(upload_url, content=png_bytes, headers=hdrs)

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        if resp.status_code not in (200, 201):
            logger.warning(
                "sheet_image_storage: upload failed sheet=%s status=%d duration_ms=%.1f corr=%s",
                sheet_id[:8],
                resp.status_code,
                duration_ms,
                correlation_id[:8],
            )
            return None

        logger.info(
            "sheet_image_storage: uploaded sheet=%s size_bytes=%d duration_ms=%.1f corr=%s",
            sheet_id[:8],
            len(png_bytes),
            duration_ms,
            correlation_id[:8],
        )
        return object_path

    except httpx.TimeoutException:
        logger.warning(
            "sheet_image_storage: upload timed out sheet=%s corr=%s",
            sheet_id[:8],
            correlation_id[:8],
        )
        return None
    except Exception as exc:
        logger.warning(
            "sheet_image_storage: upload error sheet=%s error=%s corr=%s",
            sheet_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None


async def download_sheet_image(
    suite_id: str,
    project_id: str,
    sheet_id: str,
    correlation_id: str,
) -> bytes | None:
    """Download a full-resolution PNG sheet image from Supabase Storage.

    Args:
        suite_id: Tenant UUID — path prefix for isolation (Law #6).
        project_id: Blueprint project UUID.
        sheet_id: Blueprint sheet UUID.
        correlation_id: Request correlation ID for log tracing.

    Returns:
        Raw PNG bytes on success.
        None if the object does not exist or any error occurs.

    Law #9: Never logs raw bytes. Logs only sheet_id, size_bytes, duration_ms.
    """
    object_path = f"{suite_id}/{project_id}/{sheet_id}.png"
    base = _storage_base_url()
    download_url = f"{base}/object/{_BUCKET}/{object_path}"

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
            resp = await client.get(download_url, headers=_headers())

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        if resp.status_code == 404:
            logger.info(
                "sheet_image_storage: cache miss sheet=%s corr=%s",
                sheet_id[:8],
                correlation_id[:8],
            )
            return None

        if resp.status_code != 200:
            logger.warning(
                "sheet_image_storage: download failed sheet=%s status=%d duration_ms=%.1f corr=%s",
                sheet_id[:8],
                resp.status_code,
                duration_ms,
                correlation_id[:8],
            )
            return None

        png_bytes = resp.content
        logger.info(
            "sheet_image_storage: cache hit sheet=%s size_bytes=%d duration_ms=%.1f corr=%s",
            sheet_id[:8],
            len(png_bytes),
            duration_ms,
            correlation_id[:8],
        )
        return png_bytes

    except httpx.TimeoutException:
        logger.warning(
            "sheet_image_storage: download timed out sheet=%s corr=%s",
            sheet_id[:8],
            correlation_id[:8],
        )
        return None
    except Exception as exc:
        logger.warning(
            "sheet_image_storage: download error sheet=%s error=%s corr=%s",
            sheet_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None
