"""Supabase Storage helper for blueprint sheet thumbnails.

Uploads 200-DPI PNG snapshots to the `blueprint-thumbnails` bucket and returns
a 7-day signed URL. Path scheme: {suite_id}/{project_id}/{sheet_id}.png

Law compliance:
  Law #6: Path is always prefixed with suite_id — no cross-tenant path access.
  Law #9: Image bytes never logged; only sheet_id + size_bytes + upload_duration_ms.
  Law #3: Returns None on failure rather than raising — caller decides whether to
          fail the whole ingest (currently: soft failure with receipt).

Pattern: uses the Supabase Storage REST API directly (consistent with how
supabase_client.py uses the PostgREST REST API — no SDK dependency).
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

_BUCKET = "blueprint-thumbnails"
_SIGNED_URL_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days
_UPLOAD_TIMEOUT = 30.0  # large PNG may be several MB


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


async def upload_sheet_thumbnail(
    *,
    suite_id: str,
    project_id: str,
    sheet_id: str,
    png_bytes: bytes,
    correlation_id: str,
) -> str | None:
    """Upload a PNG thumbnail to Supabase Storage and return a signed URL.

    Args:
        suite_id: Tenant UUID — used as path prefix for isolation (Law #6).
        project_id: Blueprint project UUID.
        sheet_id: Blueprint sheet UUID — used as the object filename.
        png_bytes: Raw PNG image bytes (200 DPI rendered page).
        correlation_id: Request correlation ID for log tracing.

    Returns:
        Signed URL string (7-day expiry) on success.
        None on any failure (caller handles soft failure).

    Law #9: Never logs raw bytes. Logs only sheet_id, size_bytes,
            and upload_duration_ms.
    """
    if not png_bytes:
        logger.warning(
            "thumbnail_storage: empty png_bytes for sheet=%s corr=%s",
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
        # upsert=true means a re-ingest of the same sheet overwrites the thumbnail
        hdrs["x-upsert"] = "true"

        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
            resp = await client.post(upload_url, content=png_bytes, headers=hdrs)

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        if resp.status_code not in (200, 201):
            logger.warning(
                "thumbnail_storage: upload failed sheet=%s status=%d duration_ms=%.1f corr=%s",
                sheet_id[:8],
                resp.status_code,
                duration_ms,
                correlation_id[:8],
            )
            return None

        logger.info(
            "thumbnail_storage: uploaded sheet=%s size_bytes=%d duration_ms=%.1f corr=%s",
            sheet_id[:8],
            len(png_bytes),
            duration_ms,
            correlation_id[:8],
        )
    except httpx.TimeoutException:
        logger.warning(
            "thumbnail_storage: upload timed out sheet=%s corr=%s",
            sheet_id[:8],
            correlation_id[:8],
        )
        return None
    except Exception as exc:
        logger.warning(
            "thumbnail_storage: upload error sheet=%s error=%s corr=%s",
            sheet_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None

    # Create signed URL
    return await _create_signed_url(
        object_path=object_path,
        sheet_id=sheet_id,
        correlation_id=correlation_id,
    )


async def _create_signed_url(
    *,
    object_path: str,
    sheet_id: str,
    correlation_id: str,
) -> str | None:
    """Request a signed URL for an already-uploaded object."""
    base = _storage_base_url()
    sign_url = f"{base}/object/sign/{_BUCKET}/{object_path}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                sign_url,
                json={"expiresIn": _SIGNED_URL_EXPIRY_SECONDS},
                headers=_headers(),
            )

        if resp.status_code not in (200, 201):
            logger.warning(
                "thumbnail_storage: sign failed sheet=%s status=%d corr=%s",
                sheet_id[:8],
                resp.status_code,
                correlation_id[:8],
            )
            return None

        body = resp.json()
        signed_url: str | None = body.get("signedURL") or body.get("signedUrl")
        if not signed_url:
            logger.warning(
                "thumbnail_storage: sign response missing signedURL sheet=%s corr=%s",
                sheet_id[:8],
                correlation_id[:8],
            )
            return None

        # Supabase returns a relative path — prepend base origin
        if signed_url.startswith("/"):
            origin = settings.supabase_url.rstrip("/") if settings.supabase_url else ""
            signed_url = f"{origin}{signed_url}"

        return signed_url

    except Exception as exc:
        logger.warning(
            "thumbnail_storage: sign error sheet=%s error=%s corr=%s",
            sheet_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None
