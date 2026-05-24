"""Modal YOLO client — POSTs sheet PNG bytes to the Modal GPU endpoint.

Pairs with `orchestrator/modal/blueprint_yolo_app.py` (deployed as
`aspire-drew-yolo` on Modal). The Modal endpoint is a stateless FastAPI POST
that takes raw PNG bytes and returns a JSON detection list. T4 GPU + warm
container → typical inference latency ~150-400ms per sheet vs ~3-8s on CPU.

The orchestrator keeps `coco_class_map.yaml` translation locally — Modal
returns generic COCO `class_name` values which `symbol_detector.py` re-maps
to Aspire names. This keeps the class map as single source of truth.

Auth: none in v1. The endpoint URL acts as the secret (random Modal subdomain).
Move to a shared-secret header in v2 if needed.

Fail-mode: on any error (timeout, 5xx, network), returns None. The caller
(`symbol_detector.py`) treats None as "Modal unavailable" and falls back to
local CPU inference — fail-soft per sheet (Law #3 compliant because SEE is
advisory; REASON uses OCR text as primary signal).

Env vars:
  ASPIRE_DREW_MODAL_YOLO_URL  — full https URL of the detect_symbols endpoint
  ASPIRE_DREW_MODAL_TIMEOUT_S — per-call timeout in seconds (default 30)
  ASPIRE_DREW_MODAL_CONF      — confidence floor sent to Modal (default 0.5)
  ASPIRE_DREW_MODAL_IMGSZ     — YOLO input resize (default 1280)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


_MODAL_URL_ENV = "ASPIRE_DREW_MODAL_YOLO_URL"
_TIMEOUT_ENV = "ASPIRE_DREW_MODAL_TIMEOUT_S"
_CONF_ENV = "ASPIRE_DREW_MODAL_CONF"
_IMGSZ_ENV = "ASPIRE_DREW_MODAL_IMGSZ"


@dataclass(frozen=True)
class ModalDetection:
    """One raw detection from the Modal endpoint (pre-class-map translation)."""

    class_id: int
    class_name: str  # COCO name from Modal; symbol_detector re-maps to Aspire
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]


@dataclass(frozen=True)
class ModalResult:
    """Full Modal response."""

    detections: list[ModalDetection]
    image_size: tuple[int, int]
    model_version: str
    elapsed_ms: int


def modal_enabled() -> bool:
    """True iff the Modal URL is configured."""
    return bool(os.getenv(_MODAL_URL_ENV, "").strip())


async def detect_symbols_modal(
    sheet_image_bytes: bytes,
    *,
    sheet_id: str,
    correlation_id: str,
    confidence_floor: float | None = None,
) -> ModalResult | None:
    """POST PNG bytes to Modal, parse detections. Returns None on any failure.

    Args:
        sheet_image_bytes: PNG bytes of the 200 DPI sheet render.
        sheet_id: DB UUID (log context only — Modal is stateless).
        correlation_id: Trace ID for log correlation.
        confidence_floor: Optional override (else reads ASPIRE_DREW_MODAL_CONF).

    Returns:
        ModalResult on success, None on any error (caller falls back to CPU).
    """
    url = os.getenv(_MODAL_URL_ENV, "").strip()
    if not url:
        return None
    if not sheet_image_bytes:
        return None

    try:
        timeout_s = float(os.getenv(_TIMEOUT_ENV, "30"))
    except ValueError:
        timeout_s = 30.0

    conf = (
        confidence_floor
        if confidence_floor is not None
        else float(os.getenv(_CONF_ENV, "0.5"))
    )
    try:
        imgsz = int(os.getenv(_IMGSZ_ENV, "1280"))
    except ValueError:
        imgsz = 1280

    short_sheet = sheet_id[:8] if len(sheet_id) > 8 else sheet_id
    short_corr = correlation_id[:8] if len(correlation_id) > 8 else correlation_id

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                url,
                content=sheet_image_bytes,
                headers={"Content-Type": "application/octet-stream"},
                params={"confidence": str(conf), "imgsz": str(imgsz)},
            )
    except httpx.TimeoutException:
        logger.warning(
            "modal_yolo: timeout (>%ss) sheet=%s corr=%s — falling back to CPU",
            timeout_s, short_sheet, short_corr,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "modal_yolo: network error (%s) sheet=%s corr=%s — falling back to CPU",
            type(exc).__name__, short_sheet, short_corr,
        )
        return None

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if resp.status_code != 200:
        logger.warning(
            "modal_yolo: HTTP %d sheet=%s corr=%s (%dms) — falling back to CPU",
            resp.status_code, short_sheet, short_corr, elapsed_ms,
        )
        return None

    try:
        payload: dict[str, Any] = resp.json()
    except Exception:  # noqa: BLE001
        logger.warning(
            "modal_yolo: non-JSON response sheet=%s corr=%s — falling back to CPU",
            short_sheet, short_corr,
        )
        return None

    raw_dets = payload.get("detections") or []
    detections: list[ModalDetection] = []
    for d in raw_dets:
        try:
            bbox = d["bbox_xyxy"]
            detections.append(ModalDetection(
                class_id=int(d["class_id"]),
                class_name=str(d["class_name"]),
                confidence=float(d["confidence"]),
                bbox_xyxy=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            ))
        except (KeyError, TypeError, ValueError, IndexError):
            continue  # skip malformed detection; don't kill the sheet

    image_size = payload.get("image_size") or [0, 0]
    try:
        size_tuple = (int(image_size[0]), int(image_size[1]))
    except (TypeError, ValueError, IndexError):
        size_tuple = (0, 0)

    logger.info(
        "modal_yolo: sheet=%s detections=%d (%dms) corr=%s",
        short_sheet, len(detections), elapsed_ms, short_corr,
    )

    return ModalResult(
        detections=detections,
        image_size=size_tuple,
        model_version=str(payload.get("model_version", "modal-yolo11n")),
        elapsed_ms=elapsed_ms,
    )
