"""Storage lifecycle helpers for blueprint raw-PDF bucket.

Provides cleanup primitives used by the orphan sweeper in server.py:

  - delete_orphaned_storage_object(suite_id, upload_id, correlation_id)
      Best-effort DELETE of {suite_id}/{upload_id}/raw.pdf in the
      blueprint-raw-pdfs bucket. Returns True on success, False otherwise.

  - list_storage_orphans(older_than_minutes)
      Heavier scan that lists objects in the bucket that are older than
      the given threshold AND have no matching blueprint_projects row.
      Returns a list of (suite_id, upload_id, object_path) tuples.

Law #2: Each successful delete emits a blueprint.storage.orphan_deleted receipt
        (callers in server.py handle receipt emission to keep this module
        side-effect-free except for the network call).
Law #6: All paths are scoped under {suite_id}/... — no cross-tenant access.
Law #9: Service-role key is never logged. Only object paths, sizes, durations.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.supabase_client import SupabaseClientError

logger = logging.getLogger(__name__)

_BUCKET = "blueprint-raw-pdfs"
_DELETE_TIMEOUT = 10.0
_LIST_TIMEOUT = 30.0


def _storage_base_url() -> str:
    """Return the Supabase Storage v1 base URL."""
    url = settings.supabase_url
    if not url:
        raise SupabaseClientError("storage", detail="Missing ASPIRE_SUPABASE_URL")
    return f"{url.rstrip('/')}/storage/v1"


def _service_headers() -> dict[str, str]:
    """Build service-role auth headers (Law #9: key never logged)."""
    key = settings.supabase_service_role_key
    if not key:
        raise SupabaseClientError("storage", detail="Missing ASPIRE_SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


async def delete_orphaned_storage_object(
    suite_id: str,
    upload_id: str,
    correlation_id: str,
) -> bool:
    """Best-effort DELETE of an orphaned PDF blob from Storage.

    Issues `DELETE /storage/v1/object/blueprint-raw-pdfs/{suite_id}/{upload_id}/raw.pdf`
    using the service-role key. Returns True on success (200/204) or when the
    object is already gone (404). Returns False on auth or network failure.

    Never raises — failures are logged and swallowed so a single bad delete
    cannot crash the sweeper loop.
    """
    object_path = f"{suite_id}/{upload_id}/raw.pdf"
    try:
        base = _storage_base_url()
        del_url = f"{base}/object/{_BUCKET}/{object_path}"
        hdrs = _service_headers()
    except SupabaseClientError as exc:
        logger.warning(
            "storage_lifecycle: delete skipped (missing creds) upload_id=%s corr=%s detail=%s",
            upload_id[:8],
            correlation_id[:8],
            type(exc).__name__,
        )
        return False

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT) as client:
            resp = await client.delete(del_url, headers=hdrs)
        duration_ms = round((time.monotonic() - start) * 1000, 1)

        # 200/204 = deleted, 404 = already gone (idempotent success)
        if resp.status_code in (200, 204, 404):
            logger.info(
                "storage_lifecycle: deleted orphan upload_id=%s status=%d duration_ms=%.1f corr=%s",
                upload_id[:8],
                resp.status_code,
                duration_ms,
                correlation_id[:8],
            )
            return True

        logger.warning(
            "storage_lifecycle: delete failed upload_id=%s status=%d duration_ms=%.1f corr=%s",
            upload_id[:8],
            resp.status_code,
            duration_ms,
            correlation_id[:8],
        )
        return False

    except httpx.TimeoutException:
        logger.warning(
            "storage_lifecycle: delete timed out upload_id=%s corr=%s",
            upload_id[:8],
            correlation_id[:8],
        )
        return False
    except Exception as exc:
        logger.warning(
            "storage_lifecycle: delete error upload_id=%s error=%s corr=%s",
            upload_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return False


async def list_storage_orphans(
    older_than_minutes: int = 60,
) -> list[dict[str, Any]]:
    """List Storage objects with no matching blueprint_projects row.

    Heavier scan than the per-project delete. Intended to run on a slower
    cadence (every 30 minutes) to catch the case where /upload-init minted a
    signed URL but /upload-complete was never called, so no DB row exists
    at all — leaving the blob orphaned.

    Strategy:
      1. POST /storage/v1/object/list/{bucket} with empty prefix to enumerate
         the bucket (paginated). Filter to objects with `created_at` older
         than the threshold.
      2. For each candidate object, parse the path as `{suite_id}/{upload_id}/raw.pdf`
         and check if any blueprint_projects row references that upload_id.
      3. Return the list of orphan descriptors. Callers issue the actual
         deletes via delete_orphaned_storage_object().

    Returns:
        List of dicts: [{"suite_id": ..., "upload_id": ..., "object_path": ...,
                         "created_at": ..., "size_bytes": ...}, ...]
        Empty list on any failure (best-effort).
    """
    from datetime import datetime, timedelta, timezone

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError as _SCErr,
        supabase_select,
    )

    try:
        base = _storage_base_url()
        list_url = f"{base}/object/list/{_BUCKET}"
        hdrs = _service_headers()
        hdrs["Content-Type"] = "application/json"
    except SupabaseClientError as exc:
        logger.warning(
            "storage_lifecycle: list_orphans skipped (missing creds) detail=%s",
            type(exc).__name__,
        )
        return []

    threshold = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    candidates: list[dict[str, Any]] = []

    try:
        # Storage list endpoint returns at most 100 objects per call.
        # Paginate by offset for buckets larger than 100 objects.
        offset = 0
        page_size = 100
        max_pages = 50  # hard cap — 5,000 objects per sweep should be plenty
        async with httpx.AsyncClient(timeout=_LIST_TIMEOUT) as client:
            for _ in range(max_pages):
                payload = {
                    "prefix": "",
                    "limit": page_size,
                    "offset": offset,
                    "sortBy": {"column": "created_at", "order": "asc"},
                }
                resp = await client.post(list_url, json=payload, headers=hdrs)
                if resp.status_code != 200:
                    logger.warning(
                        "storage_lifecycle: list failed status=%d offset=%d",
                        resp.status_code,
                        offset,
                    )
                    break
                page = resp.json() or []
                if not isinstance(page, list) or not page:
                    break

                for obj in page:
                    name = obj.get("name") or ""
                    created_at_raw = obj.get("created_at")
                    if not name or not created_at_raw:
                        continue
                    try:
                        created_dt = datetime.fromisoformat(
                            str(created_at_raw).replace("Z", "+00:00")
                        )
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        continue
                    if created_dt >= threshold:
                        continue  # too fresh

                    # Path shape: {suite_id}/{upload_id}/raw.pdf
                    parts = name.split("/")
                    if len(parts) < 3 or parts[-1] != "raw.pdf":
                        continue
                    suite_id = parts[0]
                    upload_id = parts[1]
                    candidates.append({
                        "suite_id": suite_id,
                        "upload_id": upload_id,
                        "object_path": name,
                        "created_at": created_at_raw,
                        "size_bytes": int(obj.get("metadata", {}).get("size", 0) or 0),
                    })

                if len(page) < page_size:
                    break
                offset += page_size
    except Exception as exc:
        logger.warning(
            "storage_lifecycle: list_orphans error=%s",
            type(exc).__name__,
        )
        return []

    if not candidates:
        return []

    # For each candidate, verify NO blueprint_projects row references this object path.
    # The schema does NOT have an upload_id column — the canonical FK is pdf_storage_path,
    # which is set to "{suite_id}/{upload_id}/raw.pdf" at project insert time.
    # Wave 9f fix: was incorrectly querying upload_id=eq.{c['upload_id']} which always
    # produced SupabaseClientError → continue → zero orphans ever confirmed.
    confirmed_orphans: list[dict[str, Any]] = []
    for c in candidates:
        from urllib.parse import quote as _urlencode
        object_path_val = f"{c['suite_id']}/{c['upload_id']}/raw.pdf"
        try:
            rows = await supabase_select(
                "blueprint_projects",
                filters=f"pdf_storage_path=eq.{_urlencode(object_path_val, safe='')}",
                limit=1,
            )
        except _SCErr:
            continue  # skip this candidate, try the next
        if not rows:
            confirmed_orphans.append(c)

    return confirmed_orphans
