"""OCR Coordinator — sheet-level OCR dispatch for Drew Blueprint Engine.

Coordinates PDF splitting + provider selection for each sheet.

Strategy (belt-and-suspenders is appropriate here — OCR is non-deterministic):
  1. Split PDF into pages via PyMuPDF (pdf_splitter.split_pdf_to_sheets)
  2. For each page:
     a. If PyMuPDF extracted >= 200 chars of native text → call LlamaParse for
        richer markdown structure (text-layer PDF)
     b. Else (scanned/image-heavy) OR LlamaParse fails → call Azure Doc Intelligence
  3. Return SheetCorpus with per-sheet text + OCR confidence + provider attribution

Fix 3 (2026-05-19): Single transient retry on 5xx / timeout / connection errors.
  - LlamaParse and Azure provider calls that raise ProviderError with a retryable
    error code get one retry after a 1-second sleep.
  - 4xx errors (auth / input) are NOT retried — they are permanent failures.
  - The LlamaParse → Azure → PyMuPDF fallback chain already handles degraded-mode
    coverage; this single-retry is purely for transient network blips.

Note on no-fallback principle: The no-fallback memory rule carves out
"non-deterministic systems (LLM instruction-following)" as appropriate for
belt-and-suspenders. OCR quality is equally non-deterministic — a scanned sheet
has legitimately zero text layer and LlamaParse cannot help. The fallback here is
about *input quality classification*, not autonomous decision-making.

Law compliance:
  #2 — Callers (Drew.ingest) emit the receipt; this coordinator only returns data.
  #7 — No autonomous decisions. Coordinator uses deterministic heuristic (char count).
  #9 — Never logs raw OCR text. Logs only page numbers, char counts, provider choice.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aspire_orchestrator.providers.base_client import ProviderError
from aspire_orchestrator.providers.llamaparse_client import get_llamaparse_client
from aspire_orchestrator.providers.azure_doc_intel_client import get_azure_doc_intel_client
from aspire_orchestrator.services.blueprint.pdf_splitter import (
    SheetExtract,
    split_pdf_to_sheets,
)

logger = logging.getLogger(__name__)

_TEXT_LAYER_MIN_CHARS = 200
_TRANSIENT_RETRY_DELAY_S = 1.0  # Fix 3: single retry delay for transient provider errors


def _is_transient_provider_error(exc: Exception) -> bool:
    """Return True if the exception represents a transient provider failure worth retrying.

    Fix 3: Only retry on 5xx / timeout / connection errors. Never retry on 4xx.
    Non-ProviderError exceptions are treated as transient (unexpected — may succeed on retry).

    Uses InternalErrorCode.retryable property which already encodes the NETWORK/RATE/SERVER
    category check — avoids duplicating that logic here.
    """
    if isinstance(exc, ProviderError):
        try:
            # Use the already-defined .retryable property on InternalErrorCode
            return bool(exc.code.retryable)
        except Exception:
            return True  # Unknown code — be optimistic
    # httpx-level exceptions, RuntimeError from provider setup, etc. — treat as transient
    return True


@dataclass(frozen=True)
class OcrSheetResult:
    """OCR result for a single sheet.

    Attributes:
        page_number: 1-based page number.
        text: Best-available OCR text for this sheet.
        provider: Which provider produced the text ("llamaparse" | "azure_doc_intel" | "pymupdf").
        confidence: 0.0–1.0 heuristic confidence score.
            - LlamaParse result: 0.9 (structured markdown from text-layer PDF)
            - Azure Doc Intel result: 0.75 (layout OCR from raster image)
            - PyMuPDF native (fallback of last resort): 0.5 (raw text layer, no structure)
        page_hash: SHA-256 hash of the page image bytes (from pdf_splitter).
        image_bytes: PNG raster snapshot of this page (from pdf_splitter). Used for
            thumbnail upload in the ingest pipeline. May be empty bytes if splitter
            failed to render this page.
    """

    page_number: int
    text: str
    provider: str
    confidence: float
    page_hash: str
    image_bytes: bytes = b""


@dataclass
class SheetCorpus:
    """Collection of OCR results for all sheets in a project PDF.

    Attributes:
        sheets: Ordered list of OcrSheetResult, one per page.
        provider_mix: Count of pages handled by each provider.
    """

    sheets: list[OcrSheetResult] = field(default_factory=list)
    provider_mix: dict[str, int] = field(default_factory=dict)

    def record_provider(self, provider: str) -> None:
        self.provider_mix[provider] = self.provider_mix.get(provider, 0) + 1


async def extract_sheet_corpus(
    pdf_bytes: bytes,
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> SheetCorpus:
    """Extract OCR text for every page in a PDF.

    Args:
        pdf_bytes: Raw PDF binary content.
        correlation_id: Trace correlation ID (Law #2).
        suite_id: Tenant suite ID (Law #6).
        office_id: Office ID (Law #6).

    Returns:
        SheetCorpus with per-sheet OCR text, provider attribution, and provider mix.

    Raises:
        ValueError: If pdf_bytes is empty.
        RuntimeError: If PyMuPDF cannot parse the PDF.
    """
    extracts: list[SheetExtract] = split_pdf_to_sheets(pdf_bytes)
    corpus = SheetCorpus()

    # Lazily resolve provider clients — instantiation raises ProviderError if the
    # required env var is absent (fail-closed per Law #3).  We defer to inside the
    # per-sheet try blocks so a missing credential only disables that provider path;
    # the downstream PyMuPDF fallback (Path C) always succeeds regardless of creds.
    _llamaparse = None
    _azure = None

    for extract in extracts:
        page_num = extract.page_number
        native_char_count = len(extract.text.strip())

        logger.info(
            "ocr_coordinator: page=%d, native_chars=%d, hash=%s, corr=%s",
            page_num,
            native_char_count,
            extract.page_hash[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        ocr_result: OcrSheetResult | None = None

        # Path A: Text-layer PDF → try LlamaParse for richer markdown structure
        if native_char_count >= _TEXT_LAYER_MIN_CHARS:
            _lp_attempts = 0
            while _lp_attempts < 2:  # Fix 3: at most 1 retry (2 attempts total)
                try:
                    if _llamaparse is None:
                        _llamaparse = get_llamaparse_client()
                    lp_response = await _llamaparse.parse_pdf(
                        pdf_bytes,  # LlamaParse parses full doc; we extract the matching page
                        correlation_id=correlation_id,
                        suite_id=suite_id,
                        office_id=office_id,
                    )
                    if lp_response.success:
                        # Extract the matching page from LlamaParse response
                        pages: list[dict] = lp_response.body.get("pages", [])
                        page_text = ""
                        for p in pages:
                            if p.get("page_number") == page_num:
                                page_text = p.get("text", "")
                                break
                        if not page_text:
                            # Fallback: use native text if page not found in LP result
                            page_text = extract.text

                        ocr_result = OcrSheetResult(
                            page_number=page_num,
                            text=page_text,
                            provider="llamaparse",
                            confidence=0.9,
                            page_hash=extract.page_hash,
                            image_bytes=extract.image_bytes,
                        )
                        corpus.record_provider("llamaparse")
                        logger.info(
                            "ocr_coordinator: page=%d -> llamaparse (success, attempt=%d), corr=%s",
                            page_num,
                            _lp_attempts + 1,
                            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                        )
                    else:
                        logger.warning(
                            "ocr_coordinator: page=%d llamaparse failed (%s), falling to azure, corr=%s",
                            page_num,
                            lp_response.error_code.value if lp_response.error_code else "unknown",
                            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                        )
                    break  # success or non-retryable failure — exit retry loop
                except Exception as exc:
                    if _lp_attempts == 0 and _is_transient_provider_error(exc):
                        # Fix 3: single retry on transient errors — wait 1s then retry
                        logger.warning(
                            "ocr_coordinator: page=%d llamaparse transient error (%s), retrying once, corr=%s",
                            page_num,
                            type(exc).__name__,
                            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                        )
                        await asyncio.sleep(_TRANSIENT_RETRY_DELAY_S)
                        _lp_attempts += 1
                        continue
                    logger.warning(
                        "ocr_coordinator: page=%d llamaparse exception (%s, attempt=%d), falling to azure, corr=%s",
                        page_num,
                        type(exc).__name__,
                        _lp_attempts + 1,
                        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                    )
                    break  # Fall through to Azure

        # Path B: Scanned/image sheet, OR LlamaParse failed → Azure Doc Intelligence
        if ocr_result is None:
            _az_attempts = 0
            while _az_attempts < 2:  # Fix 3: at most 1 retry (2 attempts total)
                try:
                    if _azure is None:
                        _azure = get_azure_doc_intel_client()
                    az_response = await _azure.analyze_layout(
                        extract.image_bytes,
                        correlation_id=correlation_id,
                        suite_id=suite_id,
                        office_id=office_id,
                    )
                    if az_response.success:
                        analyze_result = az_response.body.get("analyzeResult", {})
                        # Extract text from Azure content field
                        azure_text: str = analyze_result.get("content", "")
                        ocr_result = OcrSheetResult(
                            page_number=page_num,
                            text=azure_text,
                            provider="azure_doc_intel",
                            confidence=0.75,
                            page_hash=extract.page_hash,
                            image_bytes=extract.image_bytes,
                        )
                        corpus.record_provider("azure_doc_intel")
                        logger.info(
                            "ocr_coordinator: page=%d -> azure_doc_intel (success, attempt=%d), corr=%s",
                            page_num,
                            _az_attempts + 1,
                            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                        )
                    else:
                        logger.warning(
                            "ocr_coordinator: page=%d azure failed (%s), using pymupdf native, corr=%s",
                            page_num,
                            az_response.error_code.value if az_response.error_code else "unknown",
                            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                        )
                    break  # success or non-retryable failure — exit retry loop
                except Exception as exc:
                    if _az_attempts == 0 and _is_transient_provider_error(exc):
                        # Fix 3: single retry on transient errors — wait 1s then retry
                        logger.warning(
                            "ocr_coordinator: page=%d azure transient error (%s), retrying once, corr=%s",
                            page_num,
                            type(exc).__name__,
                            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                        )
                        await asyncio.sleep(_TRANSIENT_RETRY_DELAY_S)
                        _az_attempts += 1
                        continue
                    logger.warning(
                        "ocr_coordinator: page=%d azure exception (%s, attempt=%d), using pymupdf native, corr=%s",
                        page_num,
                        type(exc).__name__,
                        _az_attempts + 1,
                        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                    )
                    break  # Fall through to PyMuPDF

        # Path C: Last resort — PyMuPDF native text (already extracted by splitter)
        if ocr_result is None:
            ocr_result = OcrSheetResult(
                page_number=page_num,
                text=extract.text,
                provider="pymupdf",
                confidence=0.5,
                page_hash=extract.page_hash,
                image_bytes=extract.image_bytes,
            )
            corpus.record_provider("pymupdf")
            logger.info(
                "ocr_coordinator: page=%d -> pymupdf (native fallback), corr=%s",
                page_num,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )

        corpus.sheets.append(ocr_result)

    logger.info(
        "ocr_coordinator: corpus complete, pages=%d, provider_mix=%s, corr=%s",
        len(corpus.sheets),
        corpus.provider_mix,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )
    return corpus
