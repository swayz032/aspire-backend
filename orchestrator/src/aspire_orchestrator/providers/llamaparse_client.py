"""LlamaParse Provider Client — PDF parsing for Drew (Blueprint Engine).

Provider: LlamaIndex Cloud / LlamaParse (https://api.cloud.llamaindex.ai)
Auth: Bearer token (ASPIRE_LLAMAPARSE_API_KEY)
Risk tier: GREEN (read-only document parsing)
Idempotency: N/A (parsing is idempotent by nature — same bytes → same output)

Drew uses LlamaParse as the PRIMARY PDF parser for blueprint/sheet ingestion.
Azure Document Intelligence is the FALLBACK for scanned/image-heavy sheets.

Wave 1: Stub only — method signature + ProviderRequest shape correct so
Wave 2 INGEST only needs to flip the NotImplementedError to real wire-up.

Law compliance:
  #2 — Receipt emission via BaseProviderClient.make_receipt_data()
  #3 — Fail-closed on missing API key (raises ProviderError before any call)
  #6 — suite_id/office_id scoped on every ProviderRequest
  #9 — Never logs pdf_bytes content; logs only len(pdf_bytes) + correlation_id
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode

logger = logging.getLogger(__name__)


class LlamaParseClient(BaseProviderClient):
    """LlamaParse API client for PDF → structured text extraction.

    Primary PDF parser for the Drew Blueprint Engine.
    All real HTTP execution is in BaseProviderClient._request().
    This class only supplies auth headers and error mapping.
    """

    provider_id: str = "llamaparse"
    base_url: str = "https://api.cloud.llamaindex.ai"
    timeout_seconds: float = 12.0
    max_retries: int = 1  # Single retry per no-fallback principle (feedback_no_fallback)
    idempotency_support: bool = False  # LlamaParse has no native idempotency header

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Return Bearer auth header. Fail-closed per Law #3 if key missing."""
        api_key: str = settings.llamaparse_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="LlamaParse API key not configured (ASPIRE_LLAMAPARSE_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"Authorization": f"Bearer {api_key}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map LlamaParse HTTP errors to internal error codes.

        LlamaParse documented error codes (as of 2025-08):
          401 — invalid or missing API key
          429 — rate limit exceeded
          5xx — provider-side errors
        """
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.DOMAIN_FORBIDDEN
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if 500 <= status_code < 600:
            return InternalErrorCode.SERVER_UNAVAILABLE
        return super()._parse_error(status_code, body)

    async def parse_pdf(
        self,
        pdf_bytes: bytes,
        *,
        correlation_id: str,
        suite_id: str,
        office_id: str,
    ) -> ProviderResponse:
        """Parse a PDF document into structured text via LlamaParse.

        PII-safe: only logs byte length, never raw content (Law #9).

        Args:
            pdf_bytes: Raw PDF binary content (never logged).
            correlation_id: Trace correlation ID (Law #2).
            suite_id: Tenant suite ID for isolation enforcement (Law #6).
            office_id: Tenant office ID for isolation enforcement (Law #6).

        Returns:
            ProviderResponse with parsed markdown/text in body["text"] on success.

        Raises:
            NotImplementedError: Wave 1 stub — Wave 2 INGEST wires real call.
        """
        logger.info(
            "llamaparse.parse_pdf called: suite=%s, corr=%s, size_bytes=%d",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            len(pdf_bytes),
        )

        # Build the ProviderRequest shape so Wave 2 only needs to flip
        # NotImplementedError → real self._request() invocation.
        # LlamaParse upload endpoint: POST /api/parsing/upload (multipart/form-data)
        # Wave 2 must override _prepare_body() or pass body=None + use httpx streaming
        # to send the file as multipart. The request shape is documented here.
        _request = ProviderRequest(
            method="POST",
            path="/api/parsing/upload",
            body=None,  # Wave 2: replace with multipart upload via httpx
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            extra_headers={
                # Wave 2: add Content-Type: multipart/form-data with boundary
                # and the actual file bytes as form field "file"
            },
        )

        raise NotImplementedError("Wave 2 wires this")


# ---------------------------------------------------------------------------
# Module-level singleton (matches pattern used by attom_client, brave_client)
# ---------------------------------------------------------------------------

_client: LlamaParseClient | None = None


def get_llamaparse_client() -> LlamaParseClient:
    """Return the module-level LlamaParseClient singleton."""
    global _client
    if _client is None:
        _client = LlamaParseClient()
    return _client
