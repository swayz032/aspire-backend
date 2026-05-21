"""Wave 17b.3 — SEE naming pipeline: kill COCO label leaks + interpretable fallback.

The pre-trained YOLOv11 model used by Drew SEE is COCO-trained — it does NOT
know construction symbols. When it fires it emits one of two things:

  * a real COCO class (e.g. 'person', 'door', 'chair') that is meaningless
    on a blueprint and actively misleads the contractor;
  * a class translated by symbol_class_map.yaml into something Aspire knows
    (e.g. 'circular_callout').

Anything un-mapped is tagged 'unmapped:<coco_class>' by symbol_detector.

This module owns the *write-time* policy that decides which detections
actually reach blueprint_symbols and what name they carry:

  1. SUPPRESS — any 'unmapped:*' class is dropped silently.
                 any known-bad COCO label is dropped silently.
  2. NAME    — surviving symbols are run through:
                 (a) tenant symbol_memory  (deterministic hit by signature)
                 (b) interpretable fallback  (discipline + confidence band)
                 Original class is preserved in bbox.original_class so
                 Wave 18 spatial OCR can dedupe / replace.

The point is: by the time the UI loads blueprint_symbols, every row has
a name a contractor can read. No 'unmapped:door'. No 'person'. No raw
COCO leakage.

Holding-pattern note (Wave 17b.3): the fallback name
('electrical_symbol_high', etc.) is intentionally low-information. Wave 18
(spatial OCR binding) replaces these with real labels lifted from sheet
text adjacent to the bbox. Until that ships, fallback is the floor: every
symbol is interpretable even if not specific.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# COCO classes that occasionally fire on architectural drawings but carry
# zero semantic value. Suppress at write time so the UI never sees them.
# Curated against the 80-class COCO label set; biased toward classes that
# have been seen leaking through on real blueprints (see project 5b0adf45).
COCO_SUPPRESS_SET: frozenset[str] = frozenset({
    # explicit hits seen in production
    "door",
    "person",
    "car",
    "bicycle",
    "motorcycle",
    "dog",
    "cat",
    "bird",
    "horse",
    "sheep",
    "cow",
    # furniture / interior objects that match random rectangles
    "chair",
    "couch",
    "bed",
    "dining table",
    "toilet",
    "potted plant",
    # appliances / wearables / random COCO categories with no blueprint value
    "refrigerator",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "backpack",
    "handbag",
    "tie",
    "suitcase",
    "umbrella",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "remote",
    "keyboard",
    "mouse",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
    # sports / outdoor
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "boat",
    "airplane",
    "bus",
    "train",
    "truck",
    "traffic light",
    "fire hydrant",
    "parking meter",
    "bench",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
})


# Discipline single-letter code → readable prefix for fallback names.
# Matches ASPIRE_DREW_SEE_DISCIPLINES set; unknown codes fall through to "general".
_DISCIPLINE_PREFIX: dict[str, str] = {
    "A": "architectural",
    "S": "structural",
    "M": "mechanical",
    "E": "electrical",
    "P": "plumbing",
    "FP": "fire_protection",
    "C": "civil",
    "L": "landscape",
}


def should_suppress_yolo_class(class_name: str) -> tuple[bool, str]:
    """Decide whether to drop a YOLO detection before it reaches blueprint_symbols.

    Args:
        class_name: The class string already translated by symbol_class_map
            (so it's either a mapped Aspire bucket, 'unmapped:<coco>', or a
            raw COCO label if the map call was skipped).

    Returns:
        (suppress, reason) — suppress=True means do NOT insert this row.
        reason is one of {"unmapped_prefix", "coco_blacklist", ""}.
    """
    if not class_name:
        return True, "empty_class"
    if class_name.startswith("unmapped:"):
        return True, "unmapped_prefix"
    # Strip any provider-injected prefix (defensive)
    bare = class_name.split(":", 1)[-1] if ":" in class_name else class_name
    if bare in COCO_SUPPRESS_SET:
        return True, "coco_blacklist"
    return False, ""


def interpretable_fallback(
    symbol_class: str,
    sheet_discipline: str | None,
    confidence: float,
) -> str:
    """Replace an opaque 'unknown_motif_*' / 'sig:*' class with a human-readable
    fallback name derived from sheet discipline + confidence band.

    Pass-through behavior: anything that is already a real name (does not start
    with the well-known opaque prefixes) is returned unchanged. The caller is
    expected to invoke this AFTER tenant-memory lookup so memory wins.

    Bands:
        >=0.85 → high   (very likely a true symbol; just no name yet)
        >=0.65 → mid    (probable symbol)
        else   → low    (weak evidence — flag for review later)

    Examples:
        ('unknown_motif_abc12345', 'E', 0.91) → 'electrical_symbol_high'
        ('sig:abcd1234',           'P', 0.70) → 'plumbing_symbol_mid'
        ('circular_callout',       'A', 0.80) → 'circular_callout'  (pass-through)
    """
    if not symbol_class:
        return "unknown_symbol"
    # Pass-through for real names. We only rename the opaque buckets.
    opaque_prefixes = ("unknown_motif_", "sig:", "class_")
    if not symbol_class.startswith(opaque_prefixes):
        return symbol_class
    disc = _DISCIPLINE_PREFIX.get((sheet_discipline or "").upper(), "general")
    if confidence >= 0.85:
        band = "high"
    elif confidence >= 0.65:
        band = "mid"
    else:
        band = "low"
    return f"{disc}_symbol_{band}"


def is_opaque_name(name: str) -> bool:
    """True if the name is an opaque placeholder that should be replaced by
    fallback (when memory has no hit). Used by callers to decide which rows
    to bother running the fallback against."""
    if not name:
        return False
    return name.startswith(("unknown_motif_", "sig:", "class_"))
