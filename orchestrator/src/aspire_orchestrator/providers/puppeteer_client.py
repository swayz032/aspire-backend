"""Puppeteer Provider Client — PDF generation for Tec (Documents) skill pack.

Provider: Local headless Chrome (Puppeteer)
Auth: NONE (local service)
Risk tier: GREEN (document generation is non-destructive)
Idempotency: N/A (stateless generation)

NOTE: This is a STUB implementation. Actual Puppeteer/Playwright integration
requires a headless browser runtime. The stub follows the full provider client
pattern (receipts, error handling) for correctness.

Tools:
  - puppeteer.pdf.generate: Generate a PDF from HTML content
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


class PuppeteerClient(BaseProviderClient):
    """Puppeteer (headless Chrome) PDF generation client.

    This is a local provider — no external API calls. The base class
    HTTP machinery is bypassed via _request override.
    """

    provider_id = "puppeteer"
    base_url = ""  # Not used — local provider
    timeout_seconds = 30.0
    max_retries = 0  # No retries for local operations
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """No authentication required for local service."""
        return {}

    async def _request(self, request: ProviderRequest) -> ProviderResponse:
        """Override: skip HTTP, return stub result.

        In production, this would invoke headless Chrome via subprocess
        or a local HTTP sidecar. For now, returns a stub success.
        """
        logger.info(
            "Puppeteer stub request: %s %s (suite=%s, corr=%s)",
            request.method,
            request.path,
            (request.suite_id[:8] if len(request.suite_id) > 8 else request.suite_id),
            (request.correlation_id[:8] if len(request.correlation_id) > 8 else request.correlation_id),
        )

        return ProviderResponse(
            status_code=200,
            body={
                "stub": True,
                "pdf_generated": True,
                "format": request.body.get("options", {}).get("format", "A4") if request.body else "A4",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            success=True,
            latency_ms=0.1,
        )


_client: PuppeteerClient | None = None


def _get_client() -> PuppeteerClient:
    global _client
    if _client is None:
        _client = PuppeteerClient()
    return _client


async def execute_puppeteer_pdf_generate(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute puppeteer.pdf.generate — generate a PDF from HTML.

    Required payload:
      - html: str — HTML content to convert to PDF

    Optional payload:
      - options: dict — PDF generation options
        - format: str — page format (default "A4")
        - margin: dict — margins {top, right, bottom, left}
        - landscape: bool — landscape orientation (default false)
    """
    client = _get_client()

    html = payload.get("html", "")
    if not html:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="puppeteer.pdf.generate",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="puppeteer.pdf.generate",
            error="Missing required parameter: html",
            receipt_data=receipt,
        )

    options = payload.get("options", {})

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/pdf/generate",
            body={"html": html, "options": options},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="puppeteer.pdf.generate",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="puppeteer.pdf.generate",
            data={
                "pdf_generated": True,
                "format": options.get("format", "A4"),
                "html_length": len(html),
                "stub": response.body.get("stub", False),
                "generated_at": response.body.get("generated_at", ""),
            },
            receipt_data=receipt,
            is_stub=True,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="puppeteer.pdf.generate",
            error=response.error_message or "PDF generation failed",
            receipt_data=receipt,
        )
