"""Wave 3 SEE detection schemas.

In-flight (non-persisted) result types produced by the SEE stage and consumed
by Drew.see() before being written to DB.

- SymbolDetection — single YOLO detection on a sheet.
- ScaleCalibration — combined text+bar scale resolution for a sheet.
- SealDetection — engineer P.E. seal presence on a sheet.

These are frozen dataclasses (not Pydantic BaseModel) because they are
in-memory only; the DB-bound Pydantic model lives in schemas/symbol.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SymbolDetection:
    """A single symbol detected by YOLOv11 on a sheet image.

    Attributes:
        sheet_id: DB UUID of the source blueprint_sheets row.
        class_name: Detected class label (e.g. 'door', 'outlet_duplex', 'unmapped:tv').
        confidence: Model confidence in [0.0, 1.0].
        bbox: Axis-aligned box {'x': float, 'y': float, 'w': float, 'h': float} in pixels.
        model_version: Identifier of the YOLO weights used (e.g. 'yolo11m.pt').
    """

    sheet_id: str
    class_name: str
    confidence: float
    bbox: dict[str, float]
    model_version: str


ScaleMethod = Literal["text", "bar", "both", "none"]


@dataclass(frozen=True)
class ScaleCalibration:
    """Cross-checked scale calibration for a single sheet.

    Attributes:
        scale_factor: Real-world units per pixel at the 200 DPI render. 0.0 if unresolved.
        units: 'inch' or 'mm' or 'unknown'.
        method: How the calibration was determined.
        confidence: 0.95 when text+bar agree (±2%), 0.70 single-source, 0.40 disagreement, 0.0 unresolved.
        text_match: Raw scale notation matched in title block, if any.
        bar_pixels: Pixel length of detected graphic scale bar, if any.
    """

    scale_factor: float
    units: str
    method: ScaleMethod
    confidence: float
    text_match: str | None = None
    bar_pixels: float | None = None


@dataclass(frozen=True)
class SealDetection:
    """Engineer P.E. seal presence on a sheet image.

    Attributes:
        seal_detected: Whether a seal-like circular feature was found.
        confidence: Heuristic confidence in [0.0, 1.0].
        bbox: Bounding box of the candidate seal {'x','y','w','h'} or None.
    """

    seal_detected: bool
    confidence: float
    bbox: dict[str, float] | None = None
