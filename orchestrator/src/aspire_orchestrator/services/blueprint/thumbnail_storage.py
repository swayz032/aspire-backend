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
_UPLOAD_TIMEOUT = 60.0  # 2026-05-26: raised from 30s — large product-cut-sheet renders (~10MB PNG from photo-heavy spec sheets) intermittently exceeded 30s, producing silent thumbnail_upload_failed with no diagnostic info.
_MAX_ATTEMPTS = 3


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


class ThumbnailUploadResult:
    """Outcome of an upload_sheet_thumbnail call.

    Carries either a signed_url (success) or a structured failure reason
    so the caller can emit a Law #2-compliant FAILED receipt with
    diagnostic detail instead of an opaque "upload failed" entry.
    """
    __slots__ = ("signed_url", "reason_code", "error_message", "attempts", "size_bytes", "last_status")

    def __init__(
        self,
        *,
        signed_url: str | None = None,
        reason_code: str | None = None,
        error_message: str | None = None,
        attempts: int = 0,
        size_bytes: int = 0,
        last_status: int | None = None,
    ) -> None:
        self.signed_url = signed_url
        self.reason_code = reason_code
        self.error_message = error_message
        self.attempts = attempts
        self.size_bytes = size_bytes
        self.last_status = last_status


async def upload_sheet_thumbnail(
    *,
    suite_id: str,
    project_id: str,
    sheet_id: str,
    png_bytes: bytes,
    correlation_id: str,
) -> str | None:
    """Upload a PNG thumbnail to Supabase Storage and return a signed URL.

    Back-compat thin wrapper around `upload_sheet_thumbnail_detailed`. Returns
    only the signed URL on success or None on failure. Existing callers that
    only care about success/failure stay unchanged; callers that need the
    failure reason should call `upload_sheet_thumbnail_detailed` directly.
    """
    res = await upload_sheet_thumbnail_detailed(
        suite_id=suite_id,
        project_id=project_id,
        sheet_id=sheet_id,
        png_bytes=png_bytes,
        correlation_id=correlation_id,
    )
    return res.signed_url


async def upload_sheet_thumbnail_detailed(
    *,
    suite_id: str,
    project_id: str,
    sheet_id: str,
    png_bytes: bytes,
    correlation_id: str,
) -> ThumbnailUploadResult:
    """Upload a PNG thumbnail with retries + structured failure detail.

    2026-05-26: previously a one-shot upload that returned None on any
    failure. Production showed product-cut-sheet uploads silently failing
    with no diagnostic info (Mangonia Electrical #4: 0/1 thumbnails,
    Gulfstream MEP baseline: 2/39 missing). Two changes:
      1. Retry on transient failures: timeout, 5xx, network errors.
         Three attempts total with exponential backoff (0.5s, 1.5s).
         4xx errors are non-transient and skip retry.
      2. Return ThumbnailUploadResult carrying reason_code + error_message
         so the caller can emit a diagnostic-rich FAILED receipt.
    """
    size_bytes = len(png_bytes) if png_bytes else 0
    if not png_bytes:
        logger.warning(
            "thumbnail_storage: empty png_bytes for sheet=%s corr=%s",
            sheet_id[:8],
            correlation_id[:8],
        )
        return ThumbnailUploadResult(
            reason_code="EMPTY_PNG_BYTES",
            error_message="upstream pdf_splitter returned 0 image_bytes for this page",
            attempts=0,
            size_bytes=0,
        )

    object_path = f"{suite_id}/{project_id}/{sheet_id}.png"
    base = _storage_base_url()
    upload_url = f"{base}/object/{_BUCKET}/{object_path}"

    last_status: int | None = None
    last_error_message: str | None = None
    last_reason: str | None = None
    uploaded = False

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        start = time.monotonic()
        try:
            hdrs = _headers()
            hdrs["Content-Type"] = "image/png"
            # upsert=true means a re-ingest of the same sheet overwrites the thumbnail
            hdrs["x-upsert"] = "true"

            async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
                resp = await client.post(upload_url, content=png_bytes, headers=hdrs)

            duration_ms = round((time.monotonic() - start) * 1000, 1)
            last_status = resp.status_code

            if resp.status_code in (200, 201):
                logger.info(
                    "thumbnail_storage: uploaded sheet=%s size_bytes=%d duration_ms=%.1f attempt=%d corr=%s",
                    sheet_id[:8],
                    size_bytes,
                    duration_ms,
                    attempt,
                    correlation_id[:8],
                )
                uploaded = True
                break

            # 4xx is non-transient — fail fast, don't burn retries.
            transient = 500 <= resp.status_code < 600
            last_reason = f"STORAGE_HTTP_{resp.status_code}"
            try:
                body_text = resp.text[:200]
            except Exception:
                body_text = ""
            last_error_message = (
                f"storage upload returned {resp.status_code} "
                f"(attempt={attempt}/{_MAX_ATTEMPTS}, duration_ms={duration_ms}, body={body_text!r})"
            )
            logger.warning(
                "thumbnail_storage: upload failed sheet=%s status=%d duration_ms=%.1f attempt=%d transient=%s corr=%s",
                sheet_id[:8],
                resp.status_code,
                duration_ms,
                attempt,
                transient,
                correlation_id[:8],
            )
            if not transient:
                break
        except httpx.TimeoutException:
            last_reason = "UPLOAD_TIMEOUT"
            last_error_message = (
                f"upload exceeded {_UPLOAD_TIMEOUT}s timeout "
                f"(attempt={attempt}/{_MAX_ATTEMPTS}, size_bytes={size_bytes})"
            )
            logger.warning(
                "thumbnail_storage: upload timed out sheet=%s attempt=%d corr=%s",
                sheet_id[:8],
                attempt,
                correlation_id[:8],
            )
        except Exception as exc:
            last_reason = "PROVIDER_EXCEPTION"
            last_error_message = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.warning(
                "thumbnail_storage: upload error sheet=%s error=%s attempt=%d corr=%s",
                sheet_id[:8],
                type(exc).__name__,
                attempt,
                correlation_id[:8],
            )

        # Back off before retry, but only between attempts (skip after last).
        if attempt < _MAX_ATTEMPTS:
            import asyncio as _asyncio
            await _asyncio.sleep(0.5 * (attempt ** 2))  # 0.5s, 2s

    if not uploaded:
        return ThumbnailUploadResult(
            reason_code=last_reason or "UPLOAD_FAILED_UNKNOWN",
            error_message=last_error_message or "thumbnail upload exhausted retries",
            attempts=_MAX_ATTEMPTS,
            size_bytes=size_bytes,
            last_status=last_status,
        )

    # Create signed URL (this can also fail — propagate detail)
    signed_url = await _create_signed_url(
        object_path=object_path,
        sheet_id=sheet_id,
        correlation_id=correlation_id,
    )
    if not signed_url:
        return ThumbnailUploadResult(
            reason_code="SIGN_FAILED",
            error_message="storage upload succeeded but signed-URL request returned no URL",
            attempts=_MAX_ATTEMPTS,
            size_bytes=size_bytes,
            last_status=last_status,
        )
    return ThumbnailUploadResult(
        signed_url=signed_url,
        attempts=_MAX_ATTEMPTS,
        size_bytes=size_bytes,
        last_status=last_status,
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

        # Supabase returns a relative path like "/object/sign/<bucket>/<path>?token=..."
        # WITHOUT the "/storage/v1" prefix. We must add the full storage prefix
        # or the URL 404s in browsers (cause of every blank thumbnail in
        # Plans & Photos). Defensive: don't double-prepend if a future SDK
        # version starts including /storage/v1 in the response.
        if signed_url.startswith("/"):
            origin = settings.supabase_url.rstrip("/") if settings.supabase_url else ""
            prefix = "" if signed_url.startswith("/storage/v1") else "/storage/v1"
            signed_url = f"{origin}{prefix}{signed_url}"

        return signed_url

    except Exception as exc:
        logger.warning(
            "thumbnail_storage: sign error sheet=%s error=%s corr=%s",
            sheet_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None
