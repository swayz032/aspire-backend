"""LlamaParse Provider Client — PDF parsing for Drew (Blueprint Engine).

Provider: LlamaIndex Cloud / LlamaParse (https://api.cloud.llamaindex.ai)
Auth: Bearer token (ASPIRE_LLAMAPARSE_API_KEY)
Risk tier: GREEN (read-only document parsing)
Idempotency: N/A (parsing is idempotent by nature — same bytes → same output)

Drew uses LlamaParse as the PRIMARY PDF parser for blueprint/sheet ingestion.
Azure Document Intelligence is the FALLBACK for scanned/image-heavy sheets.

Wave 2: parse_pdf wired to real LlamaParse async parse pipeline:
  1. POST /api/parsing/upload  (multipart, result_type=markdown)
  2. Poll GET /api/parsing/job/{job_id}  until status=SUCCESS (2s cadence, 30s max)
  3. GET /api/parsing/job/{job_id}/result/markdown  → pages array

Law compliance:
  #2 — Receipt emission via BaseProviderClient.make_receipt_data()
  #3 — Fail-closed on missing API key (raises ProviderError before any call)
  #6 — suite_id/office_id scoped on every ProviderRequest
  #9 — Never logs pdf_bytes content; logs only len(pdf_bytes) + correlation_id

MCP research note: LlamaParse endpoint shape confirmed via LlamaIndex official docs
(api.cloud.llamaindex.ai). Multipart field name is "file", result_type="markdown",
polling endpoint is /api/parsing/job/{job_id}, result endpoint is
/api/parsing/job/{job_id}/result/markdown with pages[].md property.
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

_POLL_INTERVAL_S: float = 2.0
_POLL_MAX_S: float = 30.0


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
        """Parse a PDF document into structured markdown via LlamaParse.

        Pipeline:
          1. Upload PDF via multipart POST to /api/parsing/upload
          2. Poll /api/parsing/job/{job_id} until status=SUCCESS (2s cadence, 30s)
          3. Fetch /api/parsing/job/{job_id}/result/markdown
          4. Return ProviderResponse with body["pages"] = list of {page: int, text: str}

        PII-safe: only logs byte length and job_id prefix, never raw content (Law #9).

        Args:
            pdf_bytes: Raw PDF binary content (never logged).
            correlation_id: Trace correlation ID (Law #2).
            suite_id: Tenant suite ID for isolation enforcement (Law #6).
            office_id: Tenant office ID for isolation enforcement (Law #6).

        Returns:
            ProviderResponse with body["pages"] on success.
            ProviderResponse with success=False and error_code on failure.
        """
        logger.info(
            "llamaparse.parse_pdf called: suite=%s, corr=%s, size_bytes=%d",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            len(pdf_bytes),
        )

        api_key: str = settings.llamaparse_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="LlamaParse API key not configured (ASPIRE_LLAMAPARSE_API_KEY)",
                provider_id=self.provider_id,
            )

        # Step 1: Upload the PDF via multipart/form-data
        upload_url = f"{self.base_url}/api/parsing/upload"
        try:
            client = await self._get_client()
            upload_resp = await client.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Correlation-Id": correlation_id,
                },
                data={"result_type": "markdown"},
                files={"file": ("blueprint.pdf", pdf_bytes, "application/pdf")},
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        except httpx.TimeoutException:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=408,
                body={"error": InternalErrorCode.NETWORK_TIMEOUT.value},
                success=False,
                error_code=InternalErrorCode.NETWORK_TIMEOUT,
                error_message="LlamaParse upload timed out",
            )
        except httpx.ConnectError:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=503,
                body={"error": InternalErrorCode.NETWORK_CONNECTION_REFUSED.value},
                success=False,
                error_code=InternalErrorCode.NETWORK_CONNECTION_REFUSED,
                error_message="LlamaParse upload connection failed",
            )

        if upload_resp.status_code not in (200, 201):
            error_code = self._parse_error(upload_resp.status_code, {})
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=upload_resp.status_code,
                body={"error": error_code.value},
                success=False,
                error_code=error_code,
                error_message=f"LlamaParse upload failed: HTTP {upload_resp.status_code}",
            )

        try:
            upload_body: dict[str, Any] = upload_resp.json()
        except Exception:
            return ProviderResponse(
                status_code=upload_resp.status_code,
                body={"error": "PARSE_ERROR"},
                success=False,
                error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                error_message="LlamaParse upload response was not valid JSON",
            )

        job_id: str | None = upload_body.get("id")
        if not job_id:
            return ProviderResponse(
                status_code=upload_resp.status_code,
                body={"error": "MISSING_JOB_ID"},
                success=False,
                error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                error_message="LlamaParse upload did not return job id",
            )

        logger.info(
            "llamaparse: upload accepted, job_id=%s, corr=%s",
            job_id[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        # Step 2: Poll until status=SUCCESS
        status_url = f"{self.base_url}/api/parsing/job/{job_id}"
        status_headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Correlation-Id": correlation_id,
        }

        elapsed: float = 0.0
        final_status: str = ""

        while elapsed < _POLL_MAX_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S

            try:
                poll_resp = await client.get(
                    status_url,
                    headers=status_headers,
                    timeout=httpx.Timeout(self.timeout_seconds),
                )
            except httpx.TimeoutException:
                continue  # Don't count poll timeout against the job — keep polling

            if poll_resp.status_code not in (200, 201):
                error_code = self._parse_error(poll_resp.status_code, {})
                self._circuit.record_failure()
                return ProviderResponse(
                    status_code=poll_resp.status_code,
                    body={"error": error_code.value},
                    success=False,
                    error_code=error_code,
                    error_message=f"LlamaParse job poll failed: HTTP {poll_resp.status_code}",
                )

            try:
                poll_body: dict[str, Any] = poll_resp.json()
            except Exception:
                continue

            final_status = poll_body.get("status", "")
            if final_status == "SUCCESS":
                break
            if final_status == "ERROR":
                self._circuit.record_failure()
                return ProviderResponse(
                    status_code=500,
                    body={"error": "JOB_FAILED", "status": final_status},
                    success=False,
                    error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                    error_message=f"LlamaParse job {job_id[:8]} failed: status={final_status}",
                )
            # PENDING or PROCESSING — keep polling

        if final_status != "SUCCESS":
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=408,
                body={"error": InternalErrorCode.NETWORK_TIMEOUT.value, "status": final_status},
                success=False,
                error_code=InternalErrorCode.NETWORK_TIMEOUT,
                error_message=f"LlamaParse job {job_id[:8]} did not complete within {_POLL_MAX_S}s",
            )

        # Step 3: Fetch markdown result
        result_url = f"{self.base_url}/api/parsing/job/{job_id}/result/markdown"
        try:
            result_resp = await client.get(
                result_url,
                headers=status_headers,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        except httpx.TimeoutException:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=408,
                body={"error": InternalErrorCode.NETWORK_TIMEOUT.value},
                success=False,
                error_code=InternalErrorCode.NETWORK_TIMEOUT,
                error_message="LlamaParse result fetch timed out",
            )

        if result_resp.status_code not in (200, 201):
            error_code = self._parse_error(result_resp.status_code, {})
            return ProviderResponse(
                status_code=result_resp.status_code,
                body={"error": error_code.value},
                success=False,
                error_code=error_code,
                error_message=f"LlamaParse result fetch failed: HTTP {result_resp.status_code}",
            )

        try:
            result_body: dict[str, Any] = result_resp.json()
        except Exception:
            return ProviderResponse(
                status_code=result_resp.status_code,
                body={"error": "PARSE_ERROR"},
                success=False,
                error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                error_message="LlamaParse result response was not valid JSON",
            )

        # Normalize pages: LlamaParse returns {"pages": [{"page": 1, "md": "..."}, ...]}
        raw_pages: list[dict[str, Any]] = result_body.get("pages", [])
        normalized_pages: list[dict[str, Any]] = [
            {
                "page_number": p.get("page", idx + 1),
                "text": p.get("md", ""),
            }
            for idx, p in enumerate(raw_pages)
        ]

        self._circuit.record_success()
        logger.info(
            "llamaparse: job %s complete, %d pages, corr=%s",
            job_id[:8],
            len(normalized_pages),
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        return ProviderResponse(
            status_code=200,
            body={"pages": normalized_pages, "job_id": job_id},
            success=True,
            provider_request_id=job_id,
        )


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
