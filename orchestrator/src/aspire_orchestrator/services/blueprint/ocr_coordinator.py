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

Wave 9n (2026-05-20): Parallel Azure DocIntel via asyncio.gather + Semaphore.
  - The sequential per-sheet for-loop is replaced by a concurrent gather.
  - ASPIRE_DREW_OCR_CONCURRENCY (default 8) limits concurrent Azure requests.
  - Azure S0 tier allows 15 req/s per region; 8 concurrent is safe headroom.
  - Sheet order is preserved: gather returns results in task-submission order.
  - All existing receipt emissions (azure_doc_intel_used, dlp_scanned) are
    preserved inside the per-sheet coroutine.

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

import uuid as _uuid_mod
from datetime import datetime as _datetime_mod, timezone as _timezone_mod

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

    # Wave 9m: ASPIRE_DREW_OCR_PROVIDER controls which provider runs as
    # PRIMARY on every sheet. Default "azure" because Azure Doc Intelligence
    # layout model extracts structured tables/paragraphs that LlamaParse and
    # PyMuPDF miss — critical for REASON to read door/panel schedules.
    # Values: "azure" (default), "llamaparse" (legacy, hoist + cache).
    import os as _os_mod
    _primary_ocr = _os_mod.getenv("ASPIRE_DREW_OCR_PROVIDER", "azure").lower().strip()
    if _primary_ocr not in ("azure", "llamaparse"):
        _primary_ocr = "azure"

    # Wave 9n: per-sheet OCR concurrency limit.
    # Azure S0 allows 15 req/s per region; default 8 is safe headroom.
    _ocr_concurrency = int(_os_mod.getenv("ASPIRE_DREW_OCR_CONCURRENCY", "8"))
    _ocr_concurrency = max(1, _ocr_concurrency)
    _ocr_sem = asyncio.Semaphore(_ocr_concurrency)

    # P2-5 fix: hoist LlamaParse call out of the per-page loop (O(N²) → O(N)).
    # Only relevant when LlamaParse is the primary provider. When Azure is
    # primary the LP hoist is skipped — LP is still available as a fallback
    # per-sheet but parsed lazily inside the loop only when Azure fails.
    _lp_response_cache: dict | None = None
    _has_any_text_layer_page = any(
        len(e.text.strip()) >= _TEXT_LAYER_MIN_CHARS for e in extracts
    )
    if _primary_ocr == "llamaparse" and _has_any_text_layer_page:
        _lp_attempts = 0
        while _lp_attempts < 2:
            try:
                if _llamaparse is None:
                    _llamaparse = get_llamaparse_client()
                _lp_doc_response = await _llamaparse.parse_pdf(
                    pdf_bytes,
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                )
                if _lp_doc_response.success:
                    _lp_response_cache = _lp_doc_response.body
                    logger.info(
                        "ocr_coordinator: llamaparse hoisted — parsed full PDF, pages=%d, corr=%s",
                        len((_lp_response_cache or {}).get("pages", [])),
                        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                    )
                else:
                    logger.warning(
                        "ocr_coordinator: hoisted llamaparse parse failed (%s) — per-page Azure/PyMuPDF fallback, corr=%s",
                        _lp_doc_response.error_code.value if _lp_doc_response.error_code else "unknown",
                        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                    )
                break
            except Exception as _exc:
                if _lp_attempts == 0 and _is_transient_provider_error(_exc):
                    logger.warning(
                        "ocr_coordinator: hoisted llamaparse transient error (%s), retrying once, corr=%s",
                        type(_exc).__name__,
                        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                    )
                    await asyncio.sleep(_TRANSIENT_RETRY_DELAY_S)
                    _lp_attempts += 1
                    continue
                logger.warning(
                    "ocr_coordinator: hoisted llamaparse exception (%s, attempt=%d), per-page Azure/PyMuPDF fallback, corr=%s",
                    type(_exc).__name__,
                    _lp_attempts + 1,
                    correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                )
                break

    # Wave 9n: per-sheet OCR coroutine — runs Azure (or LP cache / PyMuPDF fallback)
    # for a single SheetExtract. Returns OcrSheetResult.
    # All receipt emissions are preserved here; gather order matches extracts order.
    async def _ocr_one_sheet(extract: SheetExtract) -> OcrSheetResult:
        """Process a single sheet through the OCR provider chain.

        Runs inside asyncio.gather with _ocr_sem to bound concurrency.
        Provider chain:
          Path A — LlamaParse hoisted cache (only when _primary_ocr=llamaparse)
          Path B — Azure Doc Intelligence (primary when _primary_ocr=azure)
          Path C — PyMuPDF native text (last resort)
        """
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

        # Wave 9m Path 0 (NEW PRIMARY when ASPIRE_DREW_OCR_PROVIDER=azure):
        # Run Azure Doc Intelligence on every sheet first. LP cache + PyMuPDF
        # below act as fallbacks only when Azure fails for this sheet.
        if _primary_ocr == "azure":
            # Skip Path A entirely — fall directly through to Path B (Azure).
            pass
        # Path A: Text-layer PDF → use hoisted LlamaParse response (P2-5 fix).
        if native_char_count >= _TEXT_LAYER_MIN_CHARS and _lp_response_cache is not None:
            pages_list: list[dict] = _lp_response_cache.get("pages", [])
            page_text = ""
            for p in pages_list:
                if p.get("page_number") == page_num:
                    page_text = p.get("text", "")
                    break
            if not page_text:
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
                "ocr_coordinator: page=%d -> llamaparse (hoisted cache), corr=%s",
                page_num,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
        elif native_char_count >= _TEXT_LAYER_MIN_CHARS:
            # LP cache is None (LP failed) — fall through to Azure below
            logger.info(
                "ocr_coordinator: page=%d — LP cache unavailable, falling to azure, corr=%s",
                page_num,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )

        # Path B: Scanned/image sheet, OR LlamaParse failed → Azure Doc Intelligence
        # Wave 9n: _ocr_sem gates Azure calls so at most _ocr_concurrency are in-flight.
        if ocr_result is None:
            async with _ocr_sem:
                # Re-check inside semaphore: another coroutine may have set the Azure
                # client while this one was waiting — that's fine, the client is stateless.
                _az_client = get_azure_doc_intel_client()
                _az_attempts = 0
                while _az_attempts < 2:  # Fix 3: at most 1 retry (2 attempts total)
                    try:
                        az_response = await _az_client.analyze_layout(
                            extract.image_bytes,
                            correlation_id=correlation_id,
                            suite_id=suite_id,
                            office_id=office_id,
                        )
                        if az_response.success:
                            analyze_result = az_response.body.get("analyzeResult", {})
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
                            # Law #2: emit per-sheet Azure usage receipt for observability.
                            # Write to redacted_outputs (NOT metadata) so the receipt mapper
                            # harvests into receipts.result jsonb and the data is queryable.
                            try:
                                from aspire_orchestrator.services.receipt_store import store_receipts as _sr
                                _sr([{
                                    "id": str(_uuid_mod.uuid4()),
                                    "outcome": "success",
                                    "action_type": "blueprint.ingest.azure_doc_intel_used",
                                    "receipt_type": "blueprint.ingest.azure_doc_intel_used",
                                    "created_at": _datetime_mod.now(_timezone_mod.utc).isoformat(),
                                    "risk_tier": "GREEN",
                                    "actor_type": "WORKER",
                                    "actor_id": "skillpack:drew-blueprint",
                                    "correlation_id": correlation_id,
                                    "suite_id": suite_id,
                                    "redacted_outputs": {
                                        "page_number": page_num,
                                        "page_hash": extract.page_hash[:8],
                                        "native_char_count": native_char_count,
                                        "scanned": native_char_count < 50,
                                        "attempt": _az_attempts + 1,
                                        "azure_char_count": len(azure_text),
                                    },
                                }])
                            except Exception:
                                pass  # Receipt emission is best-effort; never block OCR
                        else:
                            _err_code = az_response.error_code.value if az_response.error_code else "unknown"
                            logger.warning(
                                "ocr_coordinator: page=%d azure failed (%s), using pymupdf native, corr=%s",
                                page_num,
                                _err_code,
                                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
                            )
                            # Law #2: emit FAILED receipt so silent Azure failures are observable.
                            try:
                                from aspire_orchestrator.services.receipt_store import store_receipts as _sr_f
                                _sr_f([{
                                    "id": str(_uuid_mod.uuid4()),
                                    "outcome": "failed",
                                    "action_type": "blueprint.ingest.azure_doc_intel_failed",
                                    "receipt_type": "blueprint.ingest.azure_doc_intel_failed",
                                    "created_at": _datetime_mod.now(_timezone_mod.utc).isoformat(),
                                    "risk_tier": "GREEN",
                                    "actor_type": "WORKER",
                                    "actor_id": "skillpack:drew-blueprint",
                                    "correlation_id": correlation_id,
                                    "suite_id": suite_id,
                                    "reason_code": _err_code,
                                    "error_message": f"azure_doc_intel returned success=False (code={_err_code})",
                                    "redacted_outputs": {
                                        "page_number": page_num,
                                        "page_hash": extract.page_hash[:8],
                                        "native_char_count": native_char_count,
                                        "scanned": native_char_count < 50,
                                        "attempt": _az_attempts + 1,
                                        "failure_mode": "response_success_false",
                                    },
                                }])
                            except Exception:
                                pass
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
                        # Law #2: emit FAILED receipt for exception path too.
                        try:
                            from aspire_orchestrator.services.receipt_store import store_receipts as _sr_e
                            _sr_e([{
                                "id": str(_uuid_mod.uuid4()),
                                "outcome": "failed",
                                "action_type": "blueprint.ingest.azure_doc_intel_failed",
                                "receipt_type": "blueprint.ingest.azure_doc_intel_failed",
                                "created_at": _datetime_mod.now(_timezone_mod.utc).isoformat(),
                                "risk_tier": "GREEN",
                                "actor_type": "WORKER",
                                "actor_id": "skillpack:drew-blueprint",
                                "correlation_id": correlation_id,
                                "suite_id": suite_id,
                                "reason_code": "PROVIDER_EXCEPTION",
                                "error_message": f"azure_doc_intel raised {type(exc).__name__}",
                                "redacted_outputs": {
                                    "page_number": page_num,
                                    "page_hash": extract.page_hash[:8],
                                    "native_char_count": native_char_count,
                                    "scanned": native_char_count < 50,
                                    "attempt": _az_attempts + 1,
                                    "exception_type": type(exc).__name__,
                                    "failure_mode": "provider_exception",
                                },
                            }])
                        except Exception:
                            pass
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

        return ocr_result

    # Wave 9n: run all sheets concurrently, preserving order.
    # return_exceptions=True lets a single sheet failure degrade to PyMuPDF
    # rather than abort the entire corpus.
    _sheet_results = await asyncio.gather(
        *(_ocr_one_sheet(e) for e in extracts),
        return_exceptions=True,
    )

    for _idx, _res in enumerate(_sheet_results):
        if isinstance(_res, BaseException):
            # Unexpected coroutine failure — fall back to PyMuPDF native for this sheet.
            _extract = extracts[_idx]
            logger.warning(
                "ocr_coordinator: page=%d sheet-coroutine raised %s — using pymupdf fallback, corr=%s",
                _extract.page_number,
                type(_res).__name__,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
            corpus.sheets.append(OcrSheetResult(
                page_number=_extract.page_number,
                text=_extract.text,
                provider="pymupdf",
                confidence=0.5,
                page_hash=_extract.page_hash,
                image_bytes=_extract.image_bytes,
            ))
            corpus.record_provider("pymupdf")
        else:
            corpus.sheets.append(_res)

    logger.info(
        "ocr_coordinator: corpus complete, pages=%d, provider_mix=%s, corr=%s",
        len(corpus.sheets),
        corpus.provider_mix,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )
    return corpus
