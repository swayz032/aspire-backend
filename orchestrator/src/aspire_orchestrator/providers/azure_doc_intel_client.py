"""Azure Document Intelligence Provider Client — OCR fallback for Drew (Blueprint Engine).

Provider: Azure AI Document Intelligence
         (https://{endpoint}/formrecognizer/documentModels/prebuilt-layout:analyze)
Auth: API key via Ocp-Apim-Subscription-Key header (ASPIRE_AZURE_DOC_INTEL_KEY)
Risk tier: GREEN (read-only document analysis)
Idempotency: N/A (analysis is idempotent — same bytes → same output)

Drew uses Azure Document Intelligence as the FALLBACK OCR layer for scanned
or image-heavy blueprint sheets where LlamaParse cannot extract structured text.

Wave 1: Stub only — method signature + ProviderRequest shape correct so
Wave 2 INGEST only needs to flip the NotImplementedError to real wire-up.

Law compliance:
  #2 — Receipt emission via BaseProviderClient.make_receipt_data()
  #3 — Fail-closed on missing API key OR endpoint (raises ProviderError before any call)
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


class AzureDocIntelClient(BaseProviderClient):
    """Azure AI Document Intelligence client for scanned PDF OCR.

    Fallback OCR provider for the Drew Blueprint Engine.
    The base_url is dynamic (tenant-specific Azure endpoint), so it is resolved
    from settings at instantiation time rather than as a class attribute.

    All real HTTP execution is in BaseProviderClient._request().
    This class only supplies auth headers and error mapping.
    """

    provider_id: str = "azure_doc_intel"
    # base_url is overridden at __init__ from env (endpoint is tenant-specific)
    base_url: str = ""
    timeout_seconds: float = 15.0  # Azure can be slower than LlamaParse — 15s budget
    max_retries: int = 2
    idempotency_support: bool = False

    def __init__(self) -> None:
        """Initialize client and resolve base_url from env. Fail-closed if missing."""
        super().__init__()
        endpoint: str = settings.azure_doc_intel_endpoint
        if not endpoint:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message=(
                    "Azure Document Intelligence endpoint not configured "
                    "(ASPIRE_AZURE_DOC_INTEL_ENDPOINT)"
                ),
                provider_id=self.provider_id,
            )
        # Normalise endpoint: strip trailing slash so path joins are clean
        self.base_url = endpoint.rstrip("/")

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Return Azure subscription key header. Fail-closed per Law #3 if key missing."""
        api_key: str = settings.azure_doc_intel_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message=(
                    "Azure Document Intelligence API key not configured "
                    "(ASPIRE_AZURE_DOC_INTEL_KEY)"
                ),
                provider_id=self.provider_id,
            )
        return {"Ocp-Apim-Subscription-Key": api_key}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map Azure Document Intelligence HTTP errors to internal error codes.

        Azure Cognitive Services documented error codes (as of 2025-08):
          401 — invalid subscription key
          403 — subscription plan access denied or quota exceeded
          429 — rate limit exceeded (Retry-After header present)
          5xx — transient service errors
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

    async def analyze_layout(
        self,
        pdf_bytes: bytes,
        *,
        correlation_id: str,
        suite_id: str,
        office_id: str,
    ) -> ProviderResponse:
        """Analyze a PDF/image document using the Azure prebuilt-layout model.

        PII-safe: only logs byte length, never raw content (Law #9).

        Args:
            pdf_bytes: Raw PDF or image binary content (never logged).
            correlation_id: Trace correlation ID (Law #2).
            suite_id: Tenant suite ID for isolation enforcement (Law #6).
            office_id: Tenant office ID for isolation enforcement (Law #6).

        Returns:
            ProviderResponse with layout analysis in body["analyzeResult"] on success.
            Azure returns an Operation-Location header for async polling — Wave 2
            must implement the poll loop (GET Operation-Location until status=succeeded).

        Raises:
            NotImplementedError: Wave 1 stub — Wave 2 INGEST wires real call.
        """
        logger.info(
            "azure_doc_intel.analyze_layout called: suite=%s, corr=%s, size_bytes=%d",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            len(pdf_bytes),
        )

        # Build the ProviderRequest shape so Wave 2 only needs to flip
        # NotImplementedError → real self._request() + async poll.
        #
        # Azure Document Intelligence Layout endpoint:
        #   POST {endpoint}/formrecognizer/documentModels/prebuilt-layout:analyze
        #   ?api-version=2023-07-31
        #
        # Wave 2 notes:
        #   - Content-Type must be "application/octet-stream" with raw PDF bytes
        #   - Response is 202 Accepted + Operation-Location header
        #   - Poll GET Operation-Location until {"status": "succeeded"}
        #   - Final result in polled body["analyzeResult"]
        _request = ProviderRequest(
            method="POST",
            path="/formrecognizer/documentModels/prebuilt-layout:analyze",
            body=None,  # Wave 2: send pdf_bytes as raw octet-stream content
            query_params={"api-version": "2023-07-31"},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            extra_headers={
                "Content-Type": "application/octet-stream",
                # Wave 2: also needs Accept: application/json (already set by base)
            },
        )

        raise NotImplementedError("Wave 2 wires this")


# ---------------------------------------------------------------------------
# Module-level singleton (matches pattern used by attom_client, brave_client)
# ---------------------------------------------------------------------------

_client: AzureDocIntelClient | None = None


def get_azure_doc_intel_client() -> AzureDocIntelClient:
    """Return the module-level AzureDocIntelClient singleton.

    Note: instantiation raises ProviderError if ASPIRE_AZURE_DOC_INTEL_ENDPOINT
    is not set (fail-closed per Law #3). Call from application startup after
    env vars are loaded.
    """
    global _client
    if _client is None:
        _client = AzureDocIntelClient()
    return _client
