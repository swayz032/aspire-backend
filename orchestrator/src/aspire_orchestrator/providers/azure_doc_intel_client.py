"""Azure Document Intelligence Provider Client — OCR fallback for Drew (Blueprint Engine).

Provider: Azure AI Document Intelligence
         (https://{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze)
Auth: API key via Ocp-Apim-Subscription-Key header (ASPIRE_AZURE_DOC_INTEL_KEY)
Risk tier: GREEN (read-only document analysis)
Idempotency: N/A (analysis is idempotent — same bytes → same output)

Drew uses Azure Document Intelligence as the FALLBACK OCR layer for scanned
or image-heavy blueprint sheets where LlamaParse cannot extract structured text.

Wave 2: analyze_layout wired to real Azure prebuilt-layout async pipeline:
  1. POST /documentintelligence/documentModels/prebuilt-layout:analyze
     (api-version=2024-11-30, Content-Type: application/octet-stream)
  2. Response: 202 Accepted + Operation-Location header
  3. Poll GET Operation-Location until status=succeeded (2s cadence, 30s max)
  4. Return analyzeResult from final poll body

Law compliance:
  #2 — Receipt emission via BaseProviderClient.make_receipt_data()
  #3 — Fail-closed on missing API key OR endpoint (raises ProviderError before any call)
  #6 — suite_id/office_id scoped on every ProviderRequest
  #9 — Never logs pdf_bytes content; logs only len(pdf_bytes) + correlation_id

MCP research note: Azure Document Intelligence v4.0 (2024-11-30) endpoint confirmed via
Azure documentation. POST returns 202 + Operation-Location header for async polling.
Poll until {"status": "succeeded"}, result in ["analyzeResult"].
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode

logger = logging.getLogger(__name__)

_API_VERSION = "2024-11-30"
_POLL_INTERVAL_S: float = 2.0
_POLL_MAX_S: float = 30.0


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

        Pipeline:
          1. POST {endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze
             (api-version=2024-11-30, Content-Type: application/octet-stream)
          2. Read 202 response + Operation-Location header
          3. Poll Operation-Location until status=succeeded (2s cadence, 30s max)
          4. Return analyzeResult payload

        PII-safe: only logs byte length, never raw content (Law #9).

        Args:
            pdf_bytes: Raw PDF or image binary content (never logged).
            correlation_id: Trace correlation ID (Law #2).
            suite_id: Tenant suite ID for isolation enforcement (Law #6).
            office_id: Tenant office ID for isolation enforcement (Law #6).

        Returns:
            ProviderResponse with body["analyzeResult"] on success.
            ProviderResponse with success=False and error_code on failure.
        """
        logger.info(
            "azure_doc_intel.analyze_layout called: suite=%s, corr=%s, size_bytes=%d",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            len(pdf_bytes),
        )

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

        analyze_url = (
            f"{self.base_url}/documentintelligence/documentModels/"
            f"prebuilt-layout:analyze?api-version={_API_VERSION}"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/octet-stream",
            "Accept": "application/json",
            "X-Correlation-Id": correlation_id,
        }

        # Step 1: Submit analysis job
        client = await self._get_client()
        try:
            submit_resp = await client.post(
                analyze_url,
                content=pdf_bytes,
                headers=headers,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        except httpx.TimeoutException:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=408,
                body={"error": InternalErrorCode.NETWORK_TIMEOUT.value},
                success=False,
                error_code=InternalErrorCode.NETWORK_TIMEOUT,
                error_message="Azure Doc Intel submit timed out",
            )
        except httpx.ConnectError:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=503,
                body={"error": InternalErrorCode.NETWORK_CONNECTION_REFUSED.value},
                success=False,
                error_code=InternalErrorCode.NETWORK_CONNECTION_REFUSED,
                error_message="Azure Doc Intel submit connection failed",
            )

        if submit_resp.status_code != 202:
            error_code = self._parse_error(submit_resp.status_code, {})
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=submit_resp.status_code,
                body={"error": error_code.value},
                success=False,
                error_code=error_code,
                error_message=f"Azure Doc Intel submit failed: HTTP {submit_resp.status_code}",
            )

        operation_location: str | None = submit_resp.headers.get("Operation-Location")
        if not operation_location:
            # Some Azure deployments use lowercase header key
            operation_location = submit_resp.headers.get("operation-location")
        if not operation_location:
            return ProviderResponse(
                status_code=submit_resp.status_code,
                body={"error": "MISSING_OPERATION_LOCATION"},
                success=False,
                error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                error_message="Azure Doc Intel 202 response missing Operation-Location header",
            )

        logger.info(
            "azure_doc_intel: job submitted, corr=%s",
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        # Step 2: Poll Operation-Location until succeeded
        poll_headers = {
            "Ocp-Apim-Subscription-Key": api_key,
            "Accept": "application/json",
            "X-Correlation-Id": correlation_id,
        }

        elapsed: float = 0.0
        final_status: str = ""
        analyze_result: dict[str, Any] | None = None

        while elapsed < _POLL_MAX_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S

            try:
                poll_resp = await client.get(
                    operation_location,
                    headers=poll_headers,
                    timeout=httpx.Timeout(self.timeout_seconds),
                )
            except httpx.TimeoutException:
                continue  # Transient poll timeout — keep polling

            if poll_resp.status_code not in (200, 201):
                error_code = self._parse_error(poll_resp.status_code, {})
                self._circuit.record_failure()
                return ProviderResponse(
                    status_code=poll_resp.status_code,
                    body={"error": error_code.value},
                    success=False,
                    error_code=error_code,
                    error_message=f"Azure Doc Intel poll failed: HTTP {poll_resp.status_code}",
                )

            try:
                poll_body: dict[str, Any] = poll_resp.json()
            except Exception:
                continue

            final_status = poll_body.get("status", "")
            if final_status == "succeeded":
                analyze_result = poll_body.get("analyzeResult")
                break
            if final_status == "failed":
                self._circuit.record_failure()
                error_detail = poll_body.get("error", {})
                return ProviderResponse(
                    status_code=500,
                    body={"error": "ANALYSIS_FAILED", "status": final_status},
                    success=False,
                    error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                    error_message=f"Azure Doc Intel analysis failed: {error_detail.get('message', final_status)}",
                )
            # running or notStarted — keep polling

        if final_status != "succeeded" or analyze_result is None:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=408,
                body={"error": InternalErrorCode.NETWORK_TIMEOUT.value, "status": final_status},
                success=False,
                error_code=InternalErrorCode.NETWORK_TIMEOUT,
                error_message=f"Azure Doc Intel analysis did not complete within {_POLL_MAX_S}s",
            )

        self._circuit.record_success()
        logger.info(
            "azure_doc_intel: analysis complete, corr=%s",
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        return ProviderResponse(
            status_code=200,
            body={"analyzeResult": analyze_result},
            success=True,
        )


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
