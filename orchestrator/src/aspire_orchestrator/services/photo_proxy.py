"""Server-side Google Places photo proxy.

Why this exists (THREAT-004 / receipt #26):
  Google Places photos require an API key as a query parameter on the photo
  media URL — the client cannot fetch them without exposing the key. Embedding
  `&key=...` in client-visible URLs leaked the production GOOGLE_MAPS_API_KEY
  through every store_summary card hero rendered in the desktop UI and the
  voice transcript.

What it does:
  Accepts an opaque Google photo `resource name` of the form
  `places/{PLACE_ID}/photos/{PHOTO_REF}`, signs the upstream request server-side
  with the API key, and streams the JPEG bytes back to the caller — no key
  ever crosses the trust boundary.

  The `resource name` is opaque to the client: it cannot be rebuilt into a
  usable Google URL without the key, and it expires alongside the underlying
  Place.

Risk tier: GREEN (read-only). No receipts emitted in the proxy hot path —
the upstream Place lookup that produced the resource_name already has its
own receipts. Logging is rate-limited at the route layer.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Strict shape: places/<id>/photos/<ref> — exactly two slashes between the
# four segments. Reject anything else so a malicious client cannot inject
# an arbitrary upstream URL fragment via `..` or extra path segments.
_RESOURCE_NAME_RE = re.compile(
    r"^places/[A-Za-z0-9_\-]+/photos/[A-Za-z0-9_\-]+$"
)

_PLACES_PHOTO_MEDIA_URL = "https://places.googleapis.com/v1/{name}/media"

# Defensive caps — Google's own limits are 4800px; we serve store-card heroes
# so 1200x1200 is plenty and prevents unbounded bytes egress per call.
_MAX_HEIGHT_PX = 1200
_MAX_WIDTH_PX = 1200
_DEFAULT_HEIGHT_PX = 400
_DEFAULT_WIDTH_PX = 600

_UPSTREAM_TIMEOUT_SECONDS = 5.0


def is_valid_resource_name(resource_name: str) -> bool:
    """True when the resource name matches the strict places/.../photos/... shape."""
    if not resource_name:
        return False
    if len(resource_name) > 256:
        # No legitimate Google photo resource name approaches this length.
        return False
    return bool(_RESOURCE_NAME_RE.match(resource_name))


def clamp_dim(raw: Any, default: int, max_value: int) -> int:
    """Coerce a query parameter to an int within [1, max_value], or default."""
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if value < 1:
        return default
    return min(value, max_value)


async def fetch_place_photo_bytes(
    *,
    resource_name: str,
    max_height_px: int | None = None,
    max_width_px: int | None = None,
) -> tuple[bytes, str] | None:
    """Fetch the photo bytes from Google Places v1, signed with the server key.

    Returns (image_bytes, content_type) on success, or None when:
      - resource_name fails validation
      - GOOGLE_MAPS_API_KEY is unset
      - upstream returns non-2xx, times out, or response is not an image
    """
    if not is_valid_resource_name(resource_name):
        return None

    from aspire_orchestrator.config.settings import settings

    api_key = (getattr(settings, "google_maps_api_key", "") or "").strip()
    if not api_key:
        return None

    height = clamp_dim(max_height_px, _DEFAULT_HEIGHT_PX, _MAX_HEIGHT_PX)
    width = clamp_dim(max_width_px, _DEFAULT_WIDTH_PX, _MAX_WIDTH_PX)

    upstream = _PLACES_PHOTO_MEDIA_URL.format(name=resource_name)
    params = {
        "maxHeightPx": str(height),
        "maxWidthPx": str(width),
        "key": api_key,
    }

    try:
        async with httpx.AsyncClient(
            timeout=_UPSTREAM_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(upstream, params=params)
    except httpx.HTTPError as exc:
        logger.warning(
            "place photo proxy upstream error for %s: %s",
            _redact_resource(resource_name), exc,
        )
        return None

    if resp.status_code >= 400:
        logger.info(
            "place photo proxy upstream %d for %s",
            resp.status_code, _redact_resource(resource_name),
        )
        return None

    content_type = resp.headers.get("content-type", "image/jpeg")
    if not content_type.startswith("image/"):
        return None

    return resp.content, content_type


def _redact_resource(resource_name: str) -> str:
    """Log a partial resource name so we can correlate without leaking specifics."""
    if not resource_name:
        return ""
    return resource_name[:32] + ("..." if len(resource_name) > 32 else "")
