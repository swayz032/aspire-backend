"""Aspire upload pipeline ingestion — user-uploaded files → `memory_objects`
of type `document`.

Pass 14 expansion adapter. Documents are uploaded by users through Aspire's own
upload API (NOT a third-party webhook). This adapter is called from an
authenticated internal route after the upload is complete.

Security boundary:
  The route layer enforces JWT auth + capability token validation BEFORE calling
  this adapter. The adapter trusts the pre-authenticated payload from the route.
  `verify_signature` returns True — this is intentional and documented.

Idempotency: `upload-{tenant_id}-{sha256_or_filename}-{file_size_bytes}` — same
file re-uploaded (same SHA-256 + size) deduplicates via MemoryService's
idempotency_key uniqueness constraint. All uploads are terminal (status='executed').

memory_type = 'document' per migration 103 / plan §14 expansion.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
    ThreadOut,
)
from aspire_orchestrator.services.ingestion.base import (
    BaseIngestionAdapter,
    IngestionError,
)

logger = logging.getLogger(__name__)


class DocumentIngestionAdapter(BaseIngestionAdapter):
    """Aspire upload pipeline → `document` memory_object."""

    provider_name = "aspire_upload"
    memory_type = "document"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """No external HMAC — security boundary is the authenticated route layer.

        The /v1/ingest/document route requires a valid JWT + capability token
        before this adapter is invoked. Trust is established there, not here.
        """
        _ = (body, headers)
        return True

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Scope is provided directly in the authenticated payload (already verified by route)."""
        tenant_id_raw: str | None = payload.get("tenant_id")
        suite_id_raw: str | None = payload.get("suite_id")
        office_id_raw: str | None = payload.get("office_id")

        if not tenant_id_raw or not suite_id_raw or not office_id_raw:
            raise IngestionError(
                "Document upload payload missing tenant_id/suite_id/office_id",
                code="MISSING_SCOPE_FIELDS",
                status_code=422,
            )
        try:
            return ScopedIdentity(
                tenant_id=UUID(tenant_id_raw),
                suite_id=UUID(suite_id_raw),
                office_id=UUID(office_id_raw),
            )
        except ValueError as exc:
            raise IngestionError(
                f"Invalid scope UUID in document upload payload: {exc}",
                code="INVALID_SCOPE_UUID",
                status_code=422,
            ) from exc

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build a memory_objects row of type='document'."""
        file_name: str = payload.get("file_name") or ""
        if not file_name:
            raise IngestionError(
                "Document upload payload missing 'file_name'",
                code="MISSING_FILE_NAME",
                status_code=422,
            )

        s3_url: str = payload.get("s3_url") or ""
        if not s3_url:
            raise IngestionError(
                "Document upload payload missing 's3_url'",
                code="MISSING_S3_URL",
                status_code=422,
            )

        file_size_bytes: int = int(payload.get("file_size_bytes") or 0)
        mime_type: str = payload.get("mime_type") or "application/octet-stream"
        uploaded_by_user_id: str = payload.get("uploaded_by_user_id") or ""
        uploaded_at_raw: str | None = payload.get("uploaded_at")
        sha256: str | None = payload.get("sha256")
        page_count: int | None = payload.get("page_count")
        version_no: int = int(payload.get("version_no") or 1)
        original_filename: str = payload.get("original_filename") or file_name
        original_thread_id: str | None = payload.get("original_thread_id")
        tags: list[str] = payload.get("tags") or []
        client_idempotency_key: str | None = payload.get("idempotency_key")

        # Idempotency: sha256+size > filename+size > client key
        dedup_token = sha256 or file_name
        idempotency_key = (
            client_idempotency_key
            or f"upload-{scope.tenant_id}-{dedup_token}-{file_size_bytes}"
        )

        # Deterministic trace IDs
        ns = uuid.NAMESPACE_URL
        trace_id = uuid.uuid5(ns, f"aspire-upload:trace:{idempotency_key}")
        correlation_id = uuid.uuid5(ns, f"aspire-upload:corr:{idempotency_key}")

        # Human-readable size
        size_mb = file_size_bytes / (1024 * 1024)
        size_str = f"{size_mb:.1f}MB" if size_mb >= 0.1 else f"{file_size_bytes / 1024:.1f}KB"

        # Upload time
        uploaded_at = _parse_iso(uploaded_at_raw) or datetime.now(timezone.utc)
        uploaded_at_display = uploaded_at.strftime("%Y-%m-%d %H:%M UTC")

        # Summary: "PDF, 2.4MB, uploaded 2026-04-29 12:00 UTC"
        ext = _mime_to_ext(mime_type)
        user_display = uploaded_by_user_id[:8] if uploaded_by_user_id else "you"
        summary = f"{ext}, {size_str}, uploaded {uploaded_at_display}"

        # Title is the file name (first 80 chars per Law #9 — no content logging)
        title = file_name[:80]

        detail: dict[str, Any] = {
            "file_name": file_name,
            "file_size_bytes": file_size_bytes,
            "mime_type": mime_type,
            "s3_url": s3_url,
            "uploaded_by_user_id": uploaded_by_user_id,
            "uploaded_at": uploaded_at.isoformat(),
            "sha256": sha256,
            "page_count": page_count,
            "version_no": version_no,
            "original_filename": original_filename,
            "tags": tags,
        }
        if original_thread_id:
            detail["original_thread_id"] = original_thread_id

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="tec_documents",
                runtime_family="ui",
                channel="ui",
                source_record_id=idempotency_key,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="document",
            entity_type=None,
            entity_id=None,
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail=detail,
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=uploaded_at,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/gif": "GIF",
    "image/webp": "WebP",
    "application/msword": "DOC",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.ms-excel": "XLS",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
    "text/plain": "TXT",
    "text/csv": "CSV",
}


def _mime_to_ext(mime_type: str) -> str:
    return _MIME_TO_EXT.get(mime_type, mime_type.split("/")[-1].upper())


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


__all__ = ["DocumentIngestionAdapter"]
