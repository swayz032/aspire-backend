"""Zillow photo normalizer — categorize Apify Zillow scraper photos into lanes.

Adam returns photos categorized into 4 lanes for the Visuals tab:
  - interior   (kitchen, living room, bedroom, bath, ...)
  - exterior   (front, back, yard, porch, driveway, ...)
  - roof       (roof, rooftop, attic, aerial)
  - uncategorized (no caption, or no keyword match)

Categorization is a caption-keyword heuristic. Captions are only as good as
Zillow listing agents' diligence, so a meaningful share end up uncategorized
— that is expected. Roof check runs FIRST so "aerial roof view" lands in roof
not exterior (the ROOF_KEYWORDS share some terms with EXTERIOR_KEYWORDS).

Apify scraper response shape (varies by listing):
  - responsivePhotos: [{"url": "https://...", "caption": "Kitchen"}]   (preferred)
  - photos: [{
        "caption": "Kitchen",
        "mixedSources": {
            "jpeg": [{"url": "...", "width": 1536}, {"url": "...", "width": 768}],
            "webp": [...],
        },
    }]                                                                  (fallback)

When using mixedSources, we pick the WIDEST jpeg variant (best quality for
gallery rendering). Webp is ignored to maximise client compatibility.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

PhotoLane = Literal["interior", "exterior", "roof", "uncategorized"]

# Keywords in priority order: roof > interior > exterior > uncategorized.
ROOF_KEYWORDS: tuple[str, ...] = (
    "roof", "rooftop", "attic", "aerial", "overhead",
)

INTERIOR_KEYWORDS: tuple[str, ...] = (
    "kitchen", "living", "bedroom", "bathroom", "dining", "family",
    "bath", "closet", "master", "guest", "office", "den", "pantry",
    "laundry", "interior", "inside",
)

EXTERIOR_KEYWORDS: tuple[str, ...] = (
    "exterior", "front", "back", "side", "yard", "porch", "patio",
    "deck", "driveway", "pool", "garage", "landscape", "curb",
    "building", "facade", "view",
)


def categorize_photo(caption: str | None) -> PhotoLane:
    """Categorize a single photo caption into one of 4 lanes.

    Roof keywords win over interior/exterior because some captions overlap
    (e.g. "aerial view of the back yard" → roof, since aerial is the more
    informative signal for our hero switcher).
    """
    if not caption:
        return "uncategorized"
    c = caption.lower()
    if any(k in c for k in ROOF_KEYWORDS):
        return "roof"
    if any(k in c for k in INTERIOR_KEYWORDS):
        return "interior"
    if any(k in c for k in EXTERIOR_KEYWORDS):
        return "exterior"
    return "uncategorized"


def _pick_widest_jpeg(mixed_sources: dict[str, Any] | None) -> str | None:
    """From a mixedSources dict, return the URL of the widest JPEG variant.

    Falls back to first URL if widths are missing/non-numeric.
    """
    if not isinstance(mixed_sources, dict):
        return None
    jpeg_list = mixed_sources.get("jpeg")
    if not isinstance(jpeg_list, list) or not jpeg_list:
        return None

    best_url: str | None = None
    best_width: int = -1
    for entry in jpeg_list:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue
        width_raw = entry.get("width")
        try:
            width = int(width_raw) if width_raw is not None else 0
        except (TypeError, ValueError):
            width = 0
        if width > best_width:
            best_width = width
            best_url = url

    return best_url


def _extract_from_responsive_photos(
    responsive_photos: list[Any],
) -> list[dict[str, Any]]:
    """Preferred path: responsivePhotos already has {url, caption}."""
    out: list[dict[str, Any]] = []
    for entry in responsive_photos:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            # Some responsivePhotos entries also use mixedSources.
            url = _pick_widest_jpeg(entry.get("mixedSources"))
        if not isinstance(url, str) or not url:
            continue
        caption = entry.get("caption") if isinstance(entry.get("caption"), str) else None
        out.append({
            "url": url,
            "caption": caption,
            "lane": categorize_photo(caption),
        })
    return out


def _extract_from_photos(photos: list[Any]) -> list[dict[str, Any]]:
    """Fallback path: photos[].mixedSources.jpeg — pick widest jpeg."""
    out: list[dict[str, Any]] = []
    for entry in photos:
        if not isinstance(entry, dict):
            continue
        url = _pick_widest_jpeg(entry.get("mixedSources"))
        if not url:
            # Some entries have a flat url field as last resort.
            flat = entry.get("url")
            url = flat if isinstance(flat, str) and flat else None
        if not url:
            continue
        caption = entry.get("caption") if isinstance(entry.get("caption"), str) else None
        out.append({
            "url": url,
            "caption": caption,
            "lane": categorize_photo(caption),
        })
    return out


def normalize_apify_photos(raw_response: Any) -> list[dict[str, Any]]:
    """Extract photos from an Apify Zillow dataset items response.

    Args:
        raw_response: list[dict] (dataset items) OR a single dict (for
        defensive callers). We tolerate both — most callers pass the list
        of items returned from run-sync-get-dataset-items.

    Strategy:
        1. For each item, prefer `responsivePhotos` (already shaped {url, caption}).
        2. If absent or empty, fall back to `photos[].mixedSources.jpeg` (widest).
        3. Skip items that have neither.
        4. Categorize every photo into a lane.

    Returns:
        list[{"url": str, "caption": Optional[str], "lane": PhotoLane}]
    """
    if isinstance(raw_response, dict):
        # Caller passed a single item; wrap.
        items: list[Any] = [raw_response]
    elif isinstance(raw_response, list):
        items = raw_response
    else:
        return []

    all_photos: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        responsive = item.get("responsivePhotos")
        item_photos: list[dict[str, Any]] = []
        if isinstance(responsive, list) and responsive:
            item_photos = _extract_from_responsive_photos(responsive)

        if not item_photos:
            photos = item.get("photos")
            if isinstance(photos, list) and photos:
                item_photos = _extract_from_photos(photos)

        # Dedupe by URL across items (some scrapes return overlapping sets).
        for p in item_photos:
            if p["url"] in seen_urls:
                continue
            seen_urls.add(p["url"])
            all_photos.append(p)

    return all_photos
