"""Engineer seal detector for Drew Blueprint Engine — Wave 3 SEE.

Heuristic v1 — detects circular/oval embossed regions in the title-block area
of a sheet that match P.E. seal geometry (≥1.5 inch diameter at 200 DPI =
≥300 px). Uses OpenCV HoughCircles. No ML, no fine-tune — pure shape geometry.

When a seal is found on ANY sheet, Drew.see() flags `blueprint_sheets.seal_detected`
so Stage 4 REASON can upgrade the project's trust class (engineer-stamped =
permit-confirmed-adjacent).

Honest limitations:
  - False positives on circular site-plan elements (manholes, columns).
    Mitigated by restricting search to the rightmost 35% of the sheet
    where seals live.
  - False negatives on faint scans / low-contrast seals.
  - Does not READ the seal — only detects its presence.

Law compliance:
  #3 — Fails soft. Missing OpenCV → returns seal_detected=False with
       confidence=0.0, never raises.
  #9 — Image bytes never logged.
"""

from __future__ import annotations

import io
import logging

from aspire_orchestrator.services.blueprint.schemas_detection import SealDetection

logger = logging.getLogger(__name__)

# Geometry constants (tuned for 200 DPI renders).
_MIN_SEAL_DIAMETER_PX: int = 300   # 1.5" @ 200 DPI
_MAX_SEAL_DIAMETER_PX: int = 700   # 3.5" @ 200 DPI (P.E. seals are usually ~2")
_RIGHT_REGION_FRACTION: float = 0.35   # Search rightmost 35% of sheet only


def detect_engineer_seal(sheet_image_bytes: bytes) -> SealDetection:
    """Detect a P.E. seal on the sheet image. Pure geometric heuristic.

    Args:
        sheet_image_bytes: PNG bytes (200 DPI render of the sheet).

    Returns:
        SealDetection. Never raises — missing deps degrade to seal_detected=False.
    """
    if not sheet_image_bytes:
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np
    except ImportError:
        logger.debug("seal_detector: opencv-python-headless not installed; cannot detect seals")
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    try:
        from PIL import Image
    except ImportError:
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    try:
        img = Image.open(io.BytesIO(sheet_image_bytes)).convert("L")
    except Exception as exc:  # noqa: BLE001
        logger.debug("seal_detector: image decode failed (%s)", type(exc).__name__)
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    arr = np.array(img)
    h, w = arr.shape[:2]
    if h < 200 or w < 200:
        # Too small to host a 300px-diameter seal.
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    # Restrict to right edge of sheet (title-block region).
    x_offset = int(w * (1.0 - _RIGHT_REGION_FRACTION))
    crop = arr[:, x_offset:]
    if crop.size == 0:
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    # Blur to suppress text noise, then Hough circles.
    blurred = cv2.GaussianBlur(crop, (9, 9), 2)

    min_r = _MIN_SEAL_DIAMETER_PX // 2
    max_r = _MAX_SEAL_DIAMETER_PX // 2

    try:
        circles = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(50, min_r),
            param1=80,
            param2=50,
            minRadius=min_r,
            maxRadius=max_r,
        )
    except cv2.error as exc:
        logger.debug("seal_detector: HoughCircles failed (%s)", exc)
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    if circles is None:
        return SealDetection(seal_detected=False, confidence=0.0, bbox=None)

    # Take the highest-confidence (first) circle. HoughCircles returns sorted
    # by accumulator score with our parameter choice.
    best = circles[0][0]
    cx, cy, r = float(best[0]), float(best[1]), float(best[2])

    # Confidence: anchored to diameter falling cleanly in expected band.
    diameter = 2.0 * r
    if _MIN_SEAL_DIAMETER_PX <= diameter <= _MAX_SEAL_DIAMETER_PX:
        # Inner-band confidence; we still flag this as a heuristic.
        confidence = 0.75
    else:
        confidence = 0.55

    # Translate back to full-sheet coordinates.
    bbox = {
        "x": (cx - r) + x_offset,
        "y": cy - r,
        "w": 2.0 * r,
        "h": 2.0 * r,
    }

    return SealDetection(
        seal_detected=True,
        confidence=confidence,
        bbox=bbox,
    )


__all__ = ["detect_engineer_seal", "SealDetection"]
