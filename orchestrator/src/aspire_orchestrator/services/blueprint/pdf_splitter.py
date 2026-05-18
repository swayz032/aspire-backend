"""PDF Splitter — per-page extraction for Drew Blueprint Engine.

Splits a multi-page PDF into per-page SheetExtract records using PyMuPDF.
Each page yields:
  - Embedded text (native text layer, if any)
  - Raster snapshot at 200 DPI (PNG bytes, for vision pass or OCR fallback)
  - SHA-256 hash of the raw page bytes (for deduplication)

Law compliance:
  #9 — Never logs or stores raw page content. Only logs page count and hashes.
  #3 — Raises if pdf_bytes is empty or not parseable.

Pattern: stateless pure function — no I/O, no DB, no external calls.
Callers: ocr_coordinator.py
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DPI = 200


@dataclass(frozen=True)
class SheetExtract:
    """Per-page extract from a PDF.

    Attributes:
        page_number: 1-based page number.
        text: Embedded text layer content (empty string if none).
        image_bytes: PNG raster snapshot at 200 DPI (bytes).
        page_hash: SHA-256 hex digest of raw page render bytes (for dedup).
    """

    page_number: int
    text: str
    image_bytes: bytes
    page_hash: str


def split_pdf_to_sheets(pdf_bytes: bytes) -> list[SheetExtract]:
    """Split a PDF into one SheetExtract per page.

    Args:
        pdf_bytes: Raw PDF binary content. Must be non-empty.

    Returns:
        List of SheetExtract, one per page, ordered by page number (1-based).

    Raises:
        ValueError: If pdf_bytes is empty.
        RuntimeError: If PyMuPDF cannot open/parse the PDF.

    PII-safe: Never logs or returns raw text content. Logs only page count.
    """
    if not pdf_bytes:
        raise ValueError("pdf_bytes must be non-empty — cannot split empty PDF")

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for PDF splitting. "
            "Add 'pymupdf>=1.24.0' to pyproject.toml dependencies."
        ) from exc

    try:
        doc: fitz.Document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # fitz raises generic Exception on parse errors
        raise RuntimeError(f"PyMuPDF failed to open PDF: {type(exc).__name__}") from exc

    sheets: list[SheetExtract] = []

    try:
        page_count = len(doc)
        logger.info("pdf_splitter: opened PDF with %d pages", page_count)

        for i in range(page_count):
            page: fitz.Page = doc[i]

            # Extract embedded text (preserves layout whitespace)
            text: str = page.get_text("text")  # type: ignore[attr-defined]

            # Render page to PNG at 200 DPI
            # zoom = DPI / 72 (PDF points are 72 DPI)
            zoom: float = _DPI / 72.0
            mat: fitz.Matrix = fitz.Matrix(zoom, zoom)
            pix: fitz.Pixmap = page.get_pixmap(matrix=mat, alpha=False)
            image_bytes: bytes = pix.tobytes("png")
            pix = None  # release VRAM-like memory

            # Hash raw image bytes for dedup (not the text — avoids encoding quirks)
            page_hash: str = hashlib.sha256(image_bytes).hexdigest()

            sheets.append(
                SheetExtract(
                    page_number=i + 1,  # 1-based
                    text=text,
                    image_bytes=image_bytes,
                    page_hash=page_hash,
                )
            )

        logger.info(
            "pdf_splitter: extracted %d sheets (hashes: %s)",
            len(sheets),
            [s.page_hash[:8] for s in sheets],
        )
    finally:
        doc.close()

    return sheets
