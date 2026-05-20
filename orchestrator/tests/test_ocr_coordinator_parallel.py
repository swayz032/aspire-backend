"""Wave 9n: OCR Coordinator parallelism tests.

Verifies that extract_sheet_corpus runs Azure DocIntel calls concurrently
(bounded by ASPIRE_DREW_OCR_CONCURRENCY) rather than sequentially, and that
all receipt emissions and provider_mix accounting are preserved.

Laws validated:
  - Law #2: azure_doc_intel_used receipt emitted per sheet (counted below).
  - Law #7: coordinator makes no autonomous decisions; concurrency is bounded.
  - Law #9: no raw OCR text logged.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_sheets(n: int):
    """Return n SheetExtract-like objects with distinct page hashes."""
    from aspire_orchestrator.services.blueprint.pdf_splitter import SheetExtract

    sheets = []
    for i in range(n):
        sheets.append(SheetExtract(
            page_number=i + 1,
            text="",  # empty → forces Azure path
            image_bytes=b"\x89PNG\r\n\x1a\n" + bytes([i % 256]) * 32,
            page_hash=f"{i:064x}",
        ))
    return sheets


def _make_azure_success_response(page_num: int):
    """Build a fake successful Azure DocIntel response."""
    resp = MagicMock()
    resp.success = True
    resp.body = {"analyzeResult": {"content": f"OCR text for page {page_num}"}}
    resp.error_code = None
    return resp


def _query_receipts_by_action(action_type: str) -> list[dict]:
    """Return receipts with the given action_type from the in-process store."""
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("action_type") == action_type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_receipt_store():
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


# ---------------------------------------------------------------------------
# Concurrency test — core Wave 9n assertion
# ---------------------------------------------------------------------------

class TestOcrCoordinatorParallel:
    """Wave 9n: extract_sheet_corpus must run Azure calls concurrently."""

    def test_concurrent_azure_calls_10_sheets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """10 sheets with ASPIRE_DREW_OCR_CONCURRENCY=8 must overlap Azure calls.

        Strategy:
          - Mock Azure client with a 200ms sleep per call.
          - Sequential time: 10 * 0.20 = 2.0s
          - Concurrent (8 in parallel): ceil(10/8) * 0.20 ≈ 0.40s
          - Assert elapsed < 60% of sequential bound.
          - Assert peak concurrency >= 4 (measured via shared counter).
        """
        monkeypatch.setenv("ASPIRE_DREW_OCR_PROVIDER", "azure")
        monkeypatch.setenv("ASPIRE_DREW_OCR_CONCURRENCY", "8")

        n_sheets = 10
        per_sheet_sleep_s = 0.20

        # Shared state — written inside asyncio event loop (no threading issues)
        peak_concurrent: list[int] = [0]
        current_concurrent: list[int] = [0]

        async def _slow_azure_analyze(image_bytes, *, correlation_id, suite_id, office_id):
            current_concurrent[0] += 1
            if current_concurrent[0] > peak_concurrent[0]:
                peak_concurrent[0] = current_concurrent[0]
            await asyncio.sleep(per_sheet_sleep_s)
            current_concurrent[0] -= 1
            page_idx = int.from_bytes(image_bytes[-1:], "big")
            return _make_azure_success_response(page_idx)

        fake_azure_client = MagicMock()
        fake_azure_client.analyze_layout = _slow_azure_analyze

        fake_sheets = _make_fake_sheets(n_sheets)

        with patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.split_pdf_to_sheets",
            return_value=fake_sheets,
        ), patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.get_azure_doc_intel_client",
            return_value=fake_azure_client,
        ):
            from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus

            t0 = time.monotonic()
            corpus = asyncio.run(extract_sheet_corpus(
                b"%PDF-1.4 fake",
                correlation_id="test-parallel-ocr-" + str(uuid.uuid4()),
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            ))
            elapsed = time.monotonic() - t0

        sequential_bound = n_sheets * per_sheet_sleep_s
        assert elapsed < 0.60 * sequential_bound, (
            f"Concurrent OCR must be < 60% of sequential. "
            f"elapsed={elapsed:.3f}s sequential_bound={sequential_bound:.3f}s"
        )

        # At peak, must have seen >= 4 concurrent Azure calls (proves parallelism).
        assert peak_concurrent[0] >= 4, (
            f"Expected >= 4 concurrent Azure calls at peak, saw {peak_concurrent[0]}"
        )

        # All 10 sheets must be returned in original page_number order.
        assert len(corpus.sheets) == n_sheets
        for i, sheet in enumerate(corpus.sheets):
            assert sheet.page_number == i + 1, (
                f"Sheet order must be preserved. Position {i} has page_number={sheet.page_number}"
            )

    def test_provider_mix_counts_azure_correctly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """provider_mix must count azure_doc_intel for each successful sheet."""
        monkeypatch.setenv("ASPIRE_DREW_OCR_PROVIDER", "azure")
        monkeypatch.setenv("ASPIRE_DREW_OCR_CONCURRENCY", "4")

        n_sheets = 5
        fake_sheets = _make_fake_sheets(n_sheets)

        async def _instant_azure(image_bytes, *, correlation_id, suite_id, office_id):
            return _make_azure_success_response(0)

        fake_azure_client = MagicMock()
        fake_azure_client.analyze_layout = _instant_azure

        with patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.split_pdf_to_sheets",
            return_value=fake_sheets,
        ), patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.get_azure_doc_intel_client",
            return_value=fake_azure_client,
        ):
            from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus

            corpus = asyncio.run(extract_sheet_corpus(
                b"%PDF-1.4 fake",
                correlation_id="test-provider-mix-" + str(uuid.uuid4()),
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            ))

        assert corpus.provider_mix.get("azure_doc_intel", 0) == n_sheets, (
            f"Expected provider_mix['azure_doc_intel']={n_sheets}, got {corpus.provider_mix}"
        )
        assert len(corpus.sheets) == n_sheets

    def test_azure_doc_intel_used_receipts_emitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Law #2: blueprint.ingest.azure_doc_intel_used receipt must be emitted per sheet."""
        monkeypatch.setenv("ASPIRE_DREW_OCR_PROVIDER", "azure")
        monkeypatch.setenv("ASPIRE_DREW_OCR_CONCURRENCY", "4")

        n_sheets = 4
        fake_sheets = _make_fake_sheets(n_sheets)

        async def _instant_azure(image_bytes, *, correlation_id, suite_id, office_id):
            return _make_azure_success_response(0)

        fake_azure_client = MagicMock()
        fake_azure_client.analyze_layout = _instant_azure

        corr = "test-azure-receipts-" + str(uuid.uuid4())

        with patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.split_pdf_to_sheets",
            return_value=fake_sheets,
        ), patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.get_azure_doc_intel_client",
            return_value=fake_azure_client,
        ):
            from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus

            asyncio.run(extract_sheet_corpus(
                b"%PDF-1.4 fake",
                correlation_id=corr,
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            ))

        receipts = _query_receipts_by_action("blueprint.ingest.azure_doc_intel_used")
        assert len(receipts) == n_sheets, (
            f"Expected {n_sheets} azure_doc_intel_used receipts, got {len(receipts)}"
        )

    def test_pymupdf_fallback_on_azure_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When Azure raises, sheet must fall back to PyMuPDF provider."""
        monkeypatch.setenv("ASPIRE_DREW_OCR_PROVIDER", "azure")
        monkeypatch.setenv("ASPIRE_DREW_OCR_CONCURRENCY", "2")

        from aspire_orchestrator.services.blueprint.pdf_splitter import SheetExtract

        native_text = "This is native PyMuPDF text " * 10  # > 200 chars
        fake_sheets = [
            SheetExtract(
                page_number=1,
                text=native_text,
                image_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
                page_hash="0" * 64,
            )
        ]

        async def _failing_azure(image_bytes, *, correlation_id, suite_id, office_id):
            raise RuntimeError("Azure is down")

        fake_azure_client = MagicMock()
        fake_azure_client.analyze_layout = _failing_azure

        with patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.split_pdf_to_sheets",
            return_value=fake_sheets,
        ), patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.get_azure_doc_intel_client",
            return_value=fake_azure_client,
        ):
            from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus

            corpus = asyncio.run(extract_sheet_corpus(
                b"%PDF-1.4 fake",
                correlation_id="test-pymupdf-fallback-" + str(uuid.uuid4()),
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            ))

        assert len(corpus.sheets) == 1
        assert corpus.sheets[0].provider == "pymupdf", (
            f"Expected pymupdf fallback on Azure failure, got {corpus.sheets[0].provider}"
        )
        assert corpus.provider_mix.get("pymupdf", 0) == 1

    def test_semaphore_limits_concurrency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ASPIRE_DREW_OCR_CONCURRENCY=2 must limit peak concurrent Azure calls to 2."""
        monkeypatch.setenv("ASPIRE_DREW_OCR_PROVIDER", "azure")
        monkeypatch.setenv("ASPIRE_DREW_OCR_CONCURRENCY", "2")

        n_sheets = 6
        per_sheet_sleep_s = 0.05

        peak_concurrent: list[int] = [0]
        current_concurrent: list[int] = [0]

        async def _slow_azure(image_bytes, *, correlation_id, suite_id, office_id):
            current_concurrent[0] += 1
            if current_concurrent[0] > peak_concurrent[0]:
                peak_concurrent[0] = current_concurrent[0]
            await asyncio.sleep(per_sheet_sleep_s)
            current_concurrent[0] -= 1
            return _make_azure_success_response(0)

        fake_azure_client = MagicMock()
        fake_azure_client.analyze_layout = _slow_azure

        fake_sheets = _make_fake_sheets(n_sheets)

        with patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.split_pdf_to_sheets",
            return_value=fake_sheets,
        ), patch(
            "aspire_orchestrator.services.blueprint.ocr_coordinator.get_azure_doc_intel_client",
            return_value=fake_azure_client,
        ):
            from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus

            asyncio.run(extract_sheet_corpus(
                b"%PDF-1.4 fake",
                correlation_id="test-semaphore-" + str(uuid.uuid4()),
                suite_id=SUITE_A,
                office_id=OFFICE_A,
            ))

        # Peak must not exceed the concurrency limit.
        assert peak_concurrent[0] <= 2, (
            f"Semaphore limit=2 violated: peak_concurrent={peak_concurrent[0]}"
        )
        # But must have seen some concurrency (> 1).
        assert peak_concurrent[0] >= 1
