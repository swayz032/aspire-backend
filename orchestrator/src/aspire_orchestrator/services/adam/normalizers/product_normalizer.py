"""Normalize SerpApi responses to ProductRecord.

Handles: SerpApi Google Shopping, SerpApi Home Depot
STRICT dedup: identical SKU/model required, never merge by name alone (ADR-003).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.product_record import ProductRecord


def _safe_thumbnail(thumbnails: Any) -> str:
    """Safely extract a thumbnail URL from mixed provider payload shapes."""

    def _extract(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            # Common thumbnail keys across SerpApi variants.
            for key in ("url", "thumbnail", "image", "src", "link"):
                maybe = value.get(key)
                if isinstance(maybe, str) and maybe.strip():
                    return maybe.strip()
            # Some payloads nest image size buckets under a dict.
            for nested in value.values():
                nested_url = _extract(nested)
                if nested_url:
                    return nested_url
            return ""
        if isinstance(value, (list, tuple)):
            for item in value:
                nested_url = _extract(item)
                if nested_url:
                    return nested_url
            return ""
        return ""

    return _extract(thumbnails)


def normalize_from_serpapi_shopping(data: dict[str, Any]) -> ProductRecord:
    """Normalize a SerpApi Google Shopping result to ProductRecord."""
    # Google Shopping: 'source' = retailer, brand may be in extensions or title
    extensions = data.get("extensions", [])
    brand = ""
    for ext in (extensions if isinstance(extensions, list) else []):
        if isinstance(ext, str) and ext not in ("Free shipping", "Sale"):
            brand = ext
            break

    image_url = data.get("thumbnail", "")
    return ProductRecord(
        product_name=data.get("title", ""),
        brand=brand,
        model="",
        sku="",
        retailer=data.get("source", ""),
        price=data.get("extracted_price"),
        currency="USD",
        availability="",
        url=data.get("product_link", "") or data.get("link", ""),
        image_url=image_url,
        rating=data.get("rating"),
        reviews=data.get("reviews"),
        delivery_info=data.get("delivery", ""),
        sources=[SourceAttribution(provider="serpapi_shopping", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra={"thumbnail": image_url} if image_url else {},
    )


def normalize_from_serpapi_homedepot(data: dict[str, Any]) -> ProductRecord:
    """Normalize a SerpApi Home Depot result to ProductRecord."""
    delivery = data.get("delivery") or {}
    pickup = data.get("pickup") or {}
    stock = data.get("pickup_quantity")
    if stock is None and isinstance(pickup, dict):
        stock = pickup.get("quantity")
    store_name = data.get("pickup_store", "")
    if not store_name and isinstance(pickup, dict):
        store_name = pickup.get("store_name", "")
    store_id = data.get("pickup_store_id", "") or data.get("store_id", "")
    if not store_id and isinstance(pickup, dict):
        store_id = pickup.get("store_id", "")

    delivery_str = ""
    if isinstance(delivery, dict):
        delivery_str = "Free delivery" if delivery.get("free") else str(delivery)
    elif delivery:
        delivery_str = str(delivery)

    badges = data.get("badges", [])
    # Prefer the explicit thumbnail field first; fall back to thumbnails list/object.
    image_url = _safe_thumbnail(data.get("thumbnail"))
    if not image_url:
        image_url = _safe_thumbnail(data.get("thumbnails"))

    return ProductRecord(
        product_name=data.get("title", ""),
        brand=data.get("brand", ""),
        model=data.get("model_number", ""),
        sku=str(data.get("product_id", "")),
        retailer="Home Depot",
        price=data.get("price"),
        price_was=data.get("price_was"),
        price_saving=data.get("price_saving"),
        percentage_off=data.get("percentage_off"),
        currency="USD",
        availability="in_stock" if stock and stock > 0 else "check_store",
        in_store_stock=stock,
        store_id=str(store_id or ""),
        delivery_info=delivery_str,
        url=data.get("link", ""),
        image_url=image_url,
        rating=data.get("rating"),
        reviews=data.get("reviews"),
        sources=[SourceAttribution(provider="serpapi_home_depot", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra={
            "thumbnail": image_url,
            "pickup_store": store_name,
            "delivery": delivery_str,
            "badges": badges if isinstance(badges, list) else [],
            "availability_text": "In stock" if stock and stock > 0 else "Check store",
        },
    )
