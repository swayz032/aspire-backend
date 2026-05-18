"""Supabase Storage helper for raw blueprint PDFs.

Uploads raw PDF bytes to the `blueprint-raw-pdfs` bucket and returns the
storage path (not a signed URL — callers only need the path for dedup/audit).

Path scheme: {suite_id}/{project_id}/raw.pdf

Mirrors thumbnail_storage.py pattern with identical Law compliance:
  Law #6: Path is always prefixed with suite_id — no cross-tenant path access.
  Law #9: PDF bytes NEVER logged. Logs only project_id, size_bytes, duration_ms.
  Law #3: Returns None on failure rather than raising — caller decides whether
          to fail the whole dispatch (currently: soft failure with receipt).
"""

from __future__ import annotations

import logging
import time

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.supabase_client import SupabaseClientError

logger = logging.getLogger(__name__)

_BUCKET = "blueprint-raw-pdfs"
_UPLOAD_TIMEOUT = 60.0  # raw PDFs can be 20MB+
_MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50MB hard limit


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


async def upload_raw_pdf(
    *,
    suite_id: str,
    project_id: str,
    pdf_bytes: bytes,
    correlation_id: str,
) -> str | None:
    """Upload a raw PDF to Supabase Storage and return the storage path.

    Args:
        suite_id: Tenant UUID — used as path prefix for isolation (Law #6).
        project_id: Blueprint project UUID.
        pdf_bytes: Raw PDF binary bytes.
        correlation_id: Request correlation ID for log tracing.

    Returns:
        Storage path string on success (e.g. "{suite_id}/{project_id}/raw.pdf").
        None on any failure (caller handles soft failure with receipt).

    Law #9: Never logs raw bytes. Logs only project_id, size_bytes,
            and upload_duration_ms. No PDF content ever in log output.
    """
    if not pdf_bytes:
        logger.warning(
            "pdf_raw_storage: empty pdf_bytes for project=%s corr=%s",
            project_id[:8],
            correlation_id[:8],
        )
        return None

    if len(pdf_bytes) > _MAX_PDF_SIZE_BYTES:
        logger.warning(
            "pdf_raw_storage: pdf too large project=%s size_bytes=%d max=%d corr=%s",
            project_id[:8],
            len(pdf_bytes),
            _MAX_PDF_SIZE_BYTES,
            correlation_id[:8],
        )
        return None

    object_path = f"{suite_id}/{project_id}/raw.pdf"
    base = _storage_base_url()
    upload_url = f"{base}/object/{_BUCKET}/{object_path}"

    start = time.monotonic()
    try:
        hdrs = _headers()
        hdrs["Content-Type"] = "application/pdf"
        # upsert=true: re-dispatch of same project overwrites (dedup already checked)
        hdrs["x-upsert"] = "true"

        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
            resp = await client.post(upload_url, content=pdf_bytes, headers=hdrs)

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        if resp.status_code not in (200, 201):
            logger.warning(
                "pdf_raw_storage: upload failed project=%s status=%d duration_ms=%.1f corr=%s",
                project_id[:8],
                resp.status_code,
                duration_ms,
                correlation_id[:8],
            )
            return None

        logger.info(
            "pdf_raw_storage: uploaded project=%s size_bytes=%d duration_ms=%.1f corr=%s",
            project_id[:8],
            len(pdf_bytes),
            duration_ms,
            correlation_id[:8],
        )
        return object_path

    except httpx.TimeoutException:
        logger.warning(
            "pdf_raw_storage: upload timed out project=%s corr=%s",
            project_id[:8],
            correlation_id[:8],
        )
        return None
    except Exception as exc:
        logger.warning(
            "pdf_raw_storage: upload error project=%s error=%s corr=%s",
            project_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None
