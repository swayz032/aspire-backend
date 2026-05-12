"""Cache sanitizer — strip heavy/PII fields before writing to Supabase — Pass C.

Strips the following fields from each product record before persisting:
  - thumbnails (can be MBs of base64 / URL arrays)
  - media (array of objects)
  - reviews (array of objects — may contain review text / user names)
  - serpapi_product_api (SerpApi internal URL — contains API key fragments)
  - serpapi_product_api_comparisons (same)
  - serpapi_product_page_url (same)
  - related_products (large array)
  - complementary_products (large array)

Truncates:
  - specifications: keep max 20 scalar pairs
  - description: cap at 500 chars
  - breadcrumbs: keep first 5 entries

Law #9: Reviews may contain user-submitted PII — strip before persistence.
"""

from __future__ import annotations

from typing import Any

_STRIP_FIELDS = frozenset({
    "thumbnails",
    "media",
    "reviews",
    "serpapi_product_api",
    "serpapi_product_api_comparisons",
    "serpapi_product_page_url",
    "related_products",
    "complementary_products",
})

_MAX_SPECS = 20
_MAX_DESC_CHARS = 500
_MAX_BREADCRUMBS = 5


def sanitize_product(product: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized copy of a single product dict.

    Does NOT mutate the original. Returns a shallow copy with heavy/PII fields
    removed and truncations applied.
    """
    out: dict[str, Any] = {}
    for key, value in product.items():
        if key in _STRIP_FIELDS:
            continue
        out[key] = value

    # Truncate specifications to max 20 scalar pairs
    specs = out.get("specifications")
    if isinstance(specs, dict):
        truncated: dict[str, Any] = {}
        for k, v in specs.items():
            if len(truncated) >= _MAX_SPECS:
                break
            # Keep only scalar values (no nested dicts/lists)
            if isinstance(v, (str, int, float, bool, type(None))):
                truncated[k] = v
        out["specifications"] = truncated
    elif isinstance(specs, list):
        out["specifications"] = specs[:_MAX_SPECS]

    # Truncate description
    desc = out.get("description")
    if isinstance(desc, str) and len(desc) > _MAX_DESC_CHARS:
        out["description"] = desc[:_MAX_DESC_CHARS]

    # Truncate breadcrumbs
    crumbs = out.get("breadcrumbs")
    if isinstance(crumbs, list):
        out["breadcrumbs"] = crumbs[:_MAX_BREADCRUMBS]

    return out


def sanitize_product_list(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize a list of product dicts. Returns a new list; originals unchanged."""
    return [sanitize_product(p) for p in products]
