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
    """Safely extract first thumbnail URL from nested list."""
    if isinstance(thumbnails, str):
        return thumbnails.strip()
    if not thumbnails or not isinstance(thumbnails, list):
        return ""
    first = thumbnails[0]
    if isinstance(first, list) and first:
        return str(first[0])
    if isinstance(first, str):
        return first
    return ""


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
    stock = data.get("pickup_quantity")
    store_name = data.get("pickup_store", "")
    store_id = data.get("pickup_store_id", "") or data.get("store_id", "")

    delivery_str = ""
    if isinstance(delivery, dict):
        delivery_str = "Free delivery" if delivery.get("free") else str(delivery)
    elif delivery:
        delivery_str = str(delivery)

    badges = data.get("badges", [])
    image_url = _safe_thumbnail(data.get("thumbnails") or data.get("thumbnail"))

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
