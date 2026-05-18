"""YOLOv11 symbol detector for Drew Blueprint Engine — Wave 3 SEE.

Wraps the Ultralytics YOLOv11 inference on a single sheet image (200 DPI PNG).
Returns SymbolDetection records above a confidence floor; lower-confidence
candidates are silently dropped.

Honest scope (v1):
  Generic YOLOv11 (COCO-trained) will NOT reliably detect construction
  symbols. We translate a small subset of COCO classes that occasionally
  fire on architectural drawings (e.g. 'clock' on round seal-like elements)
  into Aspire's construction taxonomy via symbol_class_map.yaml. Anything
  un-mapped becomes 'unmapped:<coco_class>' so REASON can choose to ignore.
  Wave 10 fine-tune is the real moat — v1 SEE is a best-effort grid.

Law compliance:
  #2 — Receipt emission happens in Drew.see(), not here.
  #3 — Fails closed when weights are missing; clear bootstrap instructions.
  #9 — Image bytes never logged; only sheet_id prefixes + detection counts.

Free-tier / open-source: Ultralytics YOLOv11 (AGPL-3.0). Weights yolo11m.pt
~50MB downloaded on first run. No paid services. ONNX fallback path
documented below if container image bloat becomes a constraint.
"""

from __future__ import annotations

import io
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from aspire_orchestrator.services.blueprint.schemas_detection import SymbolDetection

logger = logging.getLogger(__name__)

# Confidence floor for inserting a blueprint_symbols row. Below this we drop.
_CONFIDENCE_FLOOR: float = 0.70

# Generic pre-trained weights identifier. Override via env for tests / Wave 10.
_DEFAULT_MODEL: str = os.getenv("ASPIRE_DREW_YOLO_MODEL", "yolo11m.pt")

# Lazy singleton — model load is ~1-2s; reuse across sheets.
_model_singleton: Any = None

_CLASS_MAP_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config"
    / "pack_policies"
    / "drew"
    / "symbol_class_map.yaml"
)
_class_map_cache: dict[str, str] | None = None


class SymbolDetectorError(RuntimeError):
    """Raised when the YOLO model cannot load or run. Fail-closed (Law #3)."""


def _load_class_map() -> dict[str, str]:
    """Load coco_class -> aspire_class translation from YAML. Empty if missing."""
    global _class_map_cache
    if _class_map_cache is not None:
        return _class_map_cache
    if not _CLASS_MAP_PATH.exists():
        logger.warning(
            "symbol_detector: class map missing at %s — all detections will be 'unmapped:*'",
            _CLASS_MAP_PATH,
        )
        _class_map_cache = {}
        return _class_map_cache
    try:
        with open(_CLASS_MAP_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        mapping = data.get("coco_to_aspire", {})
        _class_map_cache = {str(k): str(v) for k, v in mapping.items()}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "symbol_detector: failed to parse class map (%s) — using empty map",
            type(exc).__name__,
        )
        _class_map_cache = {}
    return _class_map_cache


def _load_model() -> Any:
    """Lazy-load the YOLO model. Fail-closed if ultralytics/weights missing."""
    global _model_singleton
    if _model_singleton is not None:
        return _model_singleton

    try:
        from ultralytics import YOLO  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SymbolDetectorError(
            "Ultralytics package missing. Install with: "
            "pip install 'ultralytics>=8.3.0'. "
            "(YOLOv11 inference for Drew SEE stage.)"
        ) from exc

    try:
        # Ultralytics will auto-download weights to ~/.cache/Ultralytics/ on first run.
        # In production environments where outbound network is blocked, pre-stage
        # the weights file via: python -c "from ultralytics import YOLO; YOLO('yolo11m.pt')"
        _model_singleton = YOLO(_DEFAULT_MODEL)
        logger.info("symbol_detector: loaded YOLO model %s", _DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001
        raise SymbolDetectorError(
            f"Failed to load YOLO weights '{_DEFAULT_MODEL}' ({type(exc).__name__}). "
            "Bootstrap: python -c \"from ultralytics import YOLO; YOLO('yolo11m.pt')\""
        ) from exc

    return _model_singleton


async def detect_symbols(
    sheet_image_bytes: bytes,
    *,
    sheet_id: str,
    correlation_id: str,
    suite_id: str,
    office_id: str | None = None,
    confidence_floor: float = _CONFIDENCE_FLOOR,
) -> list[SymbolDetection]:
    """Run YOLOv11 on a sheet image and return filtered detections.

    Args:
        sheet_image_bytes: PNG bytes of the 200 DPI sheet render.
        sheet_id: DB UUID of the blueprint_sheets row this image came from.
        correlation_id: Trace ID for logging.
        suite_id: Tenant ID (for log context; persistence is Drew.see's job).
        office_id: Optional office ID.
        confidence_floor: Minimum confidence to include a detection. Default 0.70.

    Returns:
        List of SymbolDetection records with confidence >= floor. Empty list
        on inference failure (we fail-soft per sheet — Stage 4 REASON uses
        OCR text as primary signal anyway).

    Raises:
        SymbolDetectorError: When weights cannot be loaded (Law #3 fail-closed).
    """
    if not sheet_image_bytes:
        logger.warning(
            "symbol_detector: empty image bytes for sheet=%s corr=%s",
            sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        return []

    model = _load_model()
    class_map = _load_class_map()

    # Wrap bytes as a PIL-compatible source. Ultralytics accepts a path, URL,
    # numpy array, PIL Image, or BytesIO. We use PIL.Image to avoid touching disk.
    try:
        from PIL import Image
    except ImportError as exc:
        raise SymbolDetectorError(
            "Pillow missing — Ultralytics pulls it transitively. "
            "Install with: pip install Pillow"
        ) from exc

    try:
        img = Image.open(io.BytesIO(sheet_image_bytes)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "symbol_detector: failed to decode PNG bytes for sheet=%s (%s)",
            sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
            type(exc).__name__,
        )
        return []

    t0 = time.monotonic()
    try:
        # verbose=False keeps Ultralytics quiet; results is a list of Results objects.
        results = model.predict(img, conf=max(0.1, confidence_floor - 0.1), verbose=False)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "symbol_detector: inference failed for sheet=%s (%s) — fail-soft",
            sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
            type(exc).__name__,
        )
        return []

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    detections: list[SymbolDetection] = []
    if not results:
        return detections

    result = results[0]
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        logger.info(
            "symbol_detector: 0 detections for sheet=%s (%dms)",
            sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
            elapsed_ms,
        )
        return detections

    names: dict[int, str] = getattr(result, "names", {}) or getattr(model, "names", {}) or {}

    for box in boxes:
        try:
            conf = float(box.conf.item()) if hasattr(box.conf, "item") else float(box.conf[0])
            if conf < confidence_floor:
                continue
            cls_id = int(box.cls.item()) if hasattr(box.cls, "item") else int(box.cls[0])
            coco_name = names.get(cls_id, f"class_{cls_id}")
            aspire_name = class_map.get(coco_name, f"unmapped:{coco_name}")

            xyxy = box.xyxy[0].tolist() if hasattr(box.xyxy, "tolist") else list(box.xyxy[0])
            x1, y1, x2, y2 = float(xyxy[0]), float(xyxy[1]), float(xyxy[2]), float(xyxy[3])
            bbox = {
                "x": x1,
                "y": y1,
                "w": max(0.0, x2 - x1),
                "h": max(0.0, y2 - y1),
            }

            detections.append(
                SymbolDetection(
                    sheet_id=sheet_id,
                    class_name=aspire_name,
                    confidence=conf,
                    bbox=bbox,
                    model_version=_DEFAULT_MODEL,
                )
            )
        except Exception as exc:  # noqa: BLE001 — single-box failure mustn't kill batch
            logger.warning(
                "symbol_detector: skipped a box on sheet=%s (%s)",
                sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
                type(exc).__name__,
            )
            continue

    logger.info(
        "symbol_detector: sheet=%s detections=%d (%dms) corr=%s",
        sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
        len(detections),
        elapsed_ms,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )
    return detections
