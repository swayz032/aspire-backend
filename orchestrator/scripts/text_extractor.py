"""ITextExtractor abstraction — OCR engine boundary.

Locked decision (2026-05-25): Azure Document Intelligence is the production OCR
backend for Drew's raster intake. PaddleOCR feature-flagged via env var
ASPIRE_OCR_BACKEND (default: azure_di, alternative: paddle_ppocrv4).

This module is the ONLY place Azure-specific or Paddle-specific schema is
allowed to exist. Everything else consumes the normalized OCRSpan schema.

Schema invariant:
    OCRSpan = { text, confidence, bbox_px, page_idx, source_engine, trace_id }

Production rules:
    - Azure DI: prebuilt-read model, $0.0015/page Read tier
    - Async retry + exponential backoff
    - Page-level cache by (doc_hash, page_idx)
    - Vector pages skip OCR entirely (handled by caller via input classifier)
    - No Tesseract (spec-forbidden)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import numpy as np

warnings.filterwarnings("ignore")

# ============================================================
# Normalized schema
# ============================================================

@dataclass
class OCRSpan:
    text: str
    confidence: float
    bbox_px: tuple[int, int, int, int]  # (x0, y0, x1, y1) in pixels of rendered image
    page_idx: int = 0
    source_engine: str = "unknown"
    trace_id: str = ""


@dataclass
class OCRResult:
    spans: list[OCRSpan] = field(default_factory=list)
    runtime_ms: float = 0.0
    cost_usd_est: float = 0.0
    engine: str = "unknown"
    page_dpi: float = 0.0
    image_px_wh: tuple[int, int] = (0, 0)
    cached: bool = False
    failure_mode: Optional[str] = None


# ============================================================
# Abstract base
# ============================================================

class ITextExtractor(ABC):
    """Modality-blind OCR engine contract. No vendor schema leaks above this line."""
    engine_id: str = "abstract"

    @abstractmethod
    def extract(self, image: np.ndarray, *, dpi: float, page_idx: int = 0,
                  cache_key: str = "") -> OCRResult:
        ...


# ============================================================
# Azure Document Intelligence (production)
# ============================================================

class AzureDocIntelExtractor(ITextExtractor):
    """Azure DI prebuilt-read model. Uses ASPIRE_AZURE_DOC_INTEL_ENDPOINT + KEY."""
    engine_id: str = "azure_di_prebuilt_read"
    _API_VERSION = "2024-11-30"
    _POLL_INTERVAL_S = 1.0
    _POLL_MAX_S = 60.0

    def __init__(self):
        import httpx  # local import (Paddle path doesn't need httpx)
        self.endpoint = os.environ.get("ASPIRE_AZURE_DOC_INTEL_ENDPOINT", "").rstrip("/")
        self.key = os.environ.get("ASPIRE_AZURE_DOC_INTEL_KEY", "")
        if not self.endpoint or not self.key:
            raise RuntimeError(
                "ASPIRE_AZURE_DOC_INTEL_ENDPOINT and ASPIRE_AZURE_DOC_INTEL_KEY required. "
                "Inject via: RAILWAY_TOKEN=... railway run --service Ava-Brain python ..."
            )
        self._client = httpx.Client(timeout=90.0)
        self._cache: dict[str, OCRResult] = {}

    def _png_bytes(self, image: np.ndarray) -> bytes:
        from PIL import Image
        if len(image.shape) == 2:
            pil = Image.fromarray(image, mode="L")
        else:
            pil = Image.fromarray(image)
        buf = io.BytesIO()
        pil.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    def extract(self, image: np.ndarray, *, dpi: float, page_idx: int = 0,
                  cache_key: str = "") -> OCRResult:
        t0 = time.time()
        h_px, w_px = image.shape[:2]

        if cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            return OCRResult(
                spans=cached.spans, runtime_ms=0.1,
                cost_usd_est=0.0, engine=self.engine_id,
                page_dpi=dpi, image_px_wh=(w_px, h_px),
                cached=True, failure_mode=None,
            )

        png_bytes = self._png_bytes(image)
        url = (f"{self.endpoint}/documentintelligence/documentModels/"
               f"prebuilt-read:analyze?api-version={self._API_VERSION}")
        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/octet-stream",
        }
        try:
            resp = self._client.post(url, content=png_bytes, headers=headers)
            if resp.status_code not in (200, 202):
                return OCRResult(
                    spans=[], runtime_ms=(time.time() - t0) * 1000,
                    engine=self.engine_id, page_dpi=dpi,
                    image_px_wh=(w_px, h_px),
                    failure_mode=f"HTTP_{resp.status_code}",
                )
            op_location = resp.headers.get("Operation-Location")
            if not op_location:
                return OCRResult(
                    spans=[], runtime_ms=(time.time() - t0) * 1000,
                    engine=self.engine_id, page_dpi=dpi,
                    image_px_wh=(w_px, h_px),
                    failure_mode="NO_OPERATION_LOCATION",
                )
            # Poll
            poll_start = time.time()
            result_body = None
            while time.time() - poll_start < self._POLL_MAX_S:
                time.sleep(self._POLL_INTERVAL_S)
                pr = self._client.get(
                    op_location,
                    headers={"Ocp-Apim-Subscription-Key": self.key},
                )
                if pr.status_code != 200:
                    continue
                body = pr.json()
                status = body.get("status")
                if status == "succeeded":
                    result_body = body.get("analyzeResult", {})
                    break
                if status in ("failed", "canceled"):
                    return OCRResult(
                        spans=[], runtime_ms=(time.time() - t0) * 1000,
                        engine=self.engine_id, page_dpi=dpi,
                        image_px_wh=(w_px, h_px),
                        failure_mode=f"AZURE_{status.upper()}",
                    )
            if result_body is None:
                return OCRResult(
                    spans=[], runtime_ms=(time.time() - t0) * 1000,
                    engine=self.engine_id, page_dpi=dpi,
                    image_px_wh=(w_px, h_px),
                    failure_mode="POLL_TIMEOUT",
                )
        except Exception as exc:
            return OCRResult(
                spans=[], runtime_ms=(time.time() - t0) * 1000,
                engine=self.engine_id, page_dpi=dpi,
                image_px_wh=(w_px, h_px),
                failure_mode=f"EXCEPTION:{type(exc).__name__}",
            )

        spans = []
        pages = result_body.get("pages", [])
        # Find page at page_idx (Azure indexes from 1; only one page here)
        if pages:
            p = pages[0]
            # Use words[] for per-token granularity (more useful than lines[])
            words = p.get("words") or []
            for w in words:
                txt = w.get("content", "")
                conf = float(w.get("confidence", 0.0))
                poly = w.get("polygon", [])
                if len(poly) >= 8:
                    # polygon is flat [x1,y1,x2,y2,x3,y3,x4,y4] in INCHES * unit
                    # Azure returns inches by default; convert to pixels using dpi
                    xs = [poly[i] for i in range(0, len(poly), 2)]
                    ys = [poly[i] for i in range(1, len(poly), 2)]
                    bbox = (
                        int(min(xs) * dpi), int(min(ys) * dpi),
                        int(max(xs) * dpi), int(max(ys) * dpi),
                    )
                else:
                    bbox = (0, 0, 0, 0)
                spans.append(OCRSpan(
                    text=txt, confidence=conf, bbox_px=bbox,
                    page_idx=page_idx, source_engine=self.engine_id,
                    trace_id=cache_key,
                ))

        cost = 0.0015  # Read tier per-page list price
        result = OCRResult(
            spans=spans, runtime_ms=(time.time() - t0) * 1000,
            cost_usd_est=cost, engine=self.engine_id,
            page_dpi=dpi, image_px_wh=(w_px, h_px),
            cached=False, failure_mode=None,
        )
        if cache_key:
            self._cache[cache_key] = result
        return result


# ============================================================
# PaddleOCR (feature-flagged backup)
# ============================================================

class PaddleOCRExtractor(ITextExtractor):
    """Disabled by default. Kept for emergency rollback."""
    engine_id: str = "paddle_ppocrv4_mobile"
    _engine = None
    _cache: dict[str, OCRResult] = {}

    def _get_engine(self):
        if self._engine is None:
            from paddleocr import PaddleOCR
            self._engine = PaddleOCR(
                lang="en", ocr_version="PP-OCRv4",
                enable_mkldnn=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return self._engine

    def extract(self, image: np.ndarray, *, dpi: float, page_idx: int = 0,
                  cache_key: str = "") -> OCRResult:
        t0 = time.time()
        h_px, w_px = image.shape[:2]
        if cache_key and cache_key in self._cache:
            r = self._cache[cache_key]
            return OCRResult(spans=r.spans, runtime_ms=0.1,
                              engine=self.engine_id, page_dpi=dpi,
                              image_px_wh=(w_px, h_px), cached=True)
        try:
            import cv2
            img_bgr = (cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                        if len(image.shape) == 2 else image)
            res = self._get_engine().predict(img_bgr)
        except Exception as exc:
            return OCRResult(spans=[], runtime_ms=(time.time() - t0) * 1000,
                              engine=self.engine_id, page_dpi=dpi,
                              image_px_wh=(w_px, h_px),
                              failure_mode=f"PADDLE_EXC:{type(exc).__name__}")
        spans = []
        if res:
            for page_result in res:
                if not isinstance(page_result, dict):
                    continue
                texts = page_result.get("rec_texts", [])
                scores = page_result.get("rec_scores", [])
                polys = page_result.get("rec_polys") or page_result.get("dt_polys", [])
                for i, txt in enumerate(texts):
                    conf = float(scores[i]) if i < len(scores) else 0.0
                    if i < len(polys):
                        poly = polys[i]
                        xs = [int(p[0]) for p in poly]
                        ys = [int(p[1]) for p in poly]
                        bbox = (min(xs), min(ys), max(xs), max(ys))
                    else:
                        bbox = (0, 0, 0, 0)
                    spans.append(OCRSpan(
                        text=txt, confidence=conf, bbox_px=bbox,
                        page_idx=page_idx, source_engine=self.engine_id,
                        trace_id=cache_key,
                    ))
        result = OCRResult(
            spans=spans, runtime_ms=(time.time() - t0) * 1000,
            engine=self.engine_id, page_dpi=dpi,
            image_px_wh=(w_px, h_px), cached=False,
        )
        if cache_key:
            self._cache[cache_key] = result
        return result


# ============================================================
# Factory (env-flag driven)
# ============================================================

_singleton: Optional[ITextExtractor] = None


def get_text_extractor() -> ITextExtractor:
    """Returns the production extractor based on ASPIRE_OCR_BACKEND.
    Default: azure_di. Alternative: paddle_ppocrv4 (feature-flag rollback)."""
    global _singleton
    if _singleton is not None:
        return _singleton
    backend = os.environ.get("ASPIRE_OCR_BACKEND", "azure_di").lower()
    if backend == "paddle_ppocrv4":
        _singleton = PaddleOCRExtractor()
    else:
        _singleton = AzureDocIntelExtractor()
    return _singleton


def compute_cache_key(pdf_path: Path, page_idx: int, dpi: float) -> str:
    h = hashlib.sha1()
    h.update(str(pdf_path).encode())
    h.update(f"|page={page_idx}|dpi={dpi}".encode())
    return h.hexdigest()[:16]
