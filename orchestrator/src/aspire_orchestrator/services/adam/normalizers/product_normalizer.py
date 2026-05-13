"""Normalize SerpApi responses to ProductRecord.

Handles: SerpApi Google Shopping, SerpApi Home Depot
STRICT dedup: identical SKU/model required, never merge by name alone (ADR-003).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.product_record import ProductRecord

logger = logging.getLogger(__name__)


# Match thdstatic CDN size suffixes. Real Home Depot CDN URL shape is
#   <base>-<sku>-<asset>_<size>.jpg
# where <asset> is numeric/alphanumeric (e.g. "64", "e4") and <size> is one of
# 65 / 100 / 145 / 300 / 400 / 600 / 1000. Verified live via paint-sprayer
# probes — earlier `_(?:64_65|100|145|300|400|600)\.jpg$` only fired when the
# asset prefix was the constant "64" AND was preceded by an underscore, so
# real product URLs (which use a hyphen between SKU and asset) never matched.
# Match the size token only; the asset prefix is preserved by the substitution.
_THD_SIZE_RE = re.compile(
    r"_(?:65|100|145|300|400|600)\.jpg(\?.*)?$",
    re.IGNORECASE,
)
# Already-high-res URL — no rewrite needed, no warning.
_THD_HIRES_RE = re.compile(r"_1000\.jpg(\?.*)?$", re.IGNORECASE)


def upgrade_thd_image(url: str) -> str:
    """Rewrite Home Depot thdstatic CDN URLs to the high-resolution _1000.jpg variant.

    Home Depot's CDN supports interchangeable size suffixes on the same asset
    path. Backend produces the high-res URL once, at the source — UI does not
    re-rewrite. Non-thdstatic URLs and already-high-res URLs ship unchanged.
    Truly unrecognized patterns are logged as a data-contract anomaly.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    cleaned = url.strip()
    if "thdstatic.com" not in cleaned:
        return cleaned

    # Already at _1000.jpg — pass through silently.
    if _THD_HIRES_RE.search(cleaned):
        return cleaned

    match = _THD_SIZE_RE.search(cleaned)
    if match:
        # Preserve any trailing query string after .jpg
        query_suffix = match.group(1) or ""
        rewritten = _THD_SIZE_RE.sub("", cleaned) + f"_1000.jpg{query_suffix}"
        # Sanity check: thdstatic asset must end with _1000.jpg(query?)
        if not rewritten.split("?", 1)[0].endswith("_1000.jpg"):
            logger.warning(
                "upgrade_thd_image produced unexpected URL shape: %s -> %s",
                cleaned, rewritten,
            )
            return cleaned
        return rewritten

    # Unexpected pattern (no size suffix detected). Ship original; log so we can
    # detect data-contract drift in production.
    if cleaned.split("?", 1)[0].endswith(".jpg"):
        logger.info(
            "upgrade_thd_image: thdstatic URL has no recognized size suffix: %s",
            cleaned,
        )
    return cleaned


def _upgrade_thumbnails(thumbnails: Any) -> list[str]:
    """Normalize and high-res-upgrade an arbitrary thumbnails payload.

    Accepts list/dict/string shapes from SerpApi and returns a flat list of
    upgraded URLs. Empty/invalid entries are dropped.
    """
    urls: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                urls.append(upgrade_thd_image(stripped))
            return
        if isinstance(value, dict):
            # Common SerpApi keys for image payloads.
            for key in ("url", "original", "large", "medium", "small", "thumbnail", "image", "src", "link"):
                maybe = value.get(key)
                if isinstance(maybe, str) and maybe.strip():
                    urls.append(upgrade_thd_image(maybe.strip()))
                    return
            for nested in value.values():
                _walk(nested)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(thumbnails)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


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


def _resolve_in_stock(raw: dict[str, Any]) -> bool:
    """Derive a boolean in-stock signal from the raw SerpApi product dict.

    Priority order (most reliable first):
      1. fulfillment_pickup.available / fulfillment_pickup.eligible
      2. pickup.quantity > 0 (already extracted by normalizer as in_store_stock)
      3. extra["availability_text"] — parse "in stock" / "available" text
      4. Default: False (fail-closed)

    This is the single source of truth for pickup.in_stock emitted into the
    serialized product dict that the frontend reads via p.pickup.in_stock.
    """
    # 1. fulfillment_pickup flags (most explicit)
    fp = raw.get("fulfillment_pickup") or raw.get("pickup") or {}
    if isinstance(fp, dict):
        for flag in ("available", "eligible", "in_store"):
            val = fp.get(flag)
            if isinstance(val, bool):
                return val
            if isinstance(val, int):
                return bool(val)
        # quantity > 0 also means in stock
        qty = fp.get("quantity")
        if isinstance(qty, (int, float)) and qty > 0:
            return True

    # 2. in_store_stock field on the record (already-extracted int from pickup.quantity)
    stock = raw.get("in_store_stock")
    if isinstance(stock, (int, float)) and stock > 0:
        return True

    # 3. availability_text / availability string
    avail_text = (
        str(raw.get("availability_text") or "")
        + " "
        + str(raw.get("availability") or "")
    ).lower()
    if any(kw in avail_text for kw in ("in stock", "in-stock", "available", "pick up today")):
        return True
    if any(kw in avail_text for kw in ("out of stock", "not available", "unavailable", "sold out")):
        return False

    # 4. Default false
    return False


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
    """Normalize a SerpApi Home Depot result to ProductRecord.

    Reads the per-product LOCAL store from `pickup.store_name` only — the
    flat `pickup_store` key from earlier provider shapes was mis-mapped and is
    no longer trusted. Image URLs are high-res-upgraded at the source so the
    UI does not need to rewrite them.

    Bug A fix (2026-05-13): emit a top-level `pickup` dict in the `extra`
    field so that ProductRecord.to_dict() includes it in the serialized
    output. The frontend reads `p.pickup.in_stock`, `p.pickup.store_id`,
    `p.pickup.store_name`, and `p.pickup.drive_minutes`. Without this wrapper,
    those reads always returned undefined, causing every product to display
    "OUT" and "0 MIN".
    """
    delivery = data.get("delivery") or {}
    pickup = data.get("pickup") if isinstance(data.get("pickup"), dict) else {}

    stock = pickup.get("quantity")
    # Read the per-product local store name strictly from pickup.store_name.
    store_name = pickup.get("store_name", "") or ""
    store_id = pickup.get("store_id", "") or data.get("store_id", "") or ""
    # Map remaining SerpAPI pickup fields. SerpAPI Home Depot returns
    # aisle + bay when store_id + delivery_zip are passed. Keep these as
    # top-level fields the UI can render directly (they're already in
    # ProductRecord schema).
    aisle = str(pickup.get("aisle") or "").strip()
    bay = str(pickup.get("bay") or "").strip()
    pickup_store_address = str(pickup.get("store_address") or "").strip()

    # Delivery state -> human label. SerpAPI ships one of four flag shapes
    # in the `delivery` dict. Stringifying the dict produces ugly UI like
    # "{'schedule_delivery': True}", so we map each flag explicitly. Order
    # matters: free wins over scheduled wins over out-of-stock/unavailable.
    delivery_str = ""
    if isinstance(delivery, dict):
        if delivery.get("free"):
            delivery_str = "Free delivery"
        elif delivery.get("schedule_delivery"):
            delivery_str = "Schedule delivery"
        elif delivery.get("not_available_for_delivery"):
            delivery_str = "Delivery unavailable"
        elif delivery.get("out_of_stock"):
            delivery_str = "Delivery out of stock"
    elif isinstance(delivery, str) and delivery.strip():
        # Legacy string shape (older SerpAPI variant).
        delivery_str = delivery.strip()

    # Keep the full pickup + delivery dicts as fulfillment_* — UI renders
    # rich pickup/delivery options when populated. SerpAPI ships these as
    # nested objects; preserving them lets the card show "Pickup at <store>",
    # "Delivery <date>", "Free delivery threshold", etc.
    fulfillment_pickup = pickup if isinstance(pickup, dict) else {}
    fulfillment_delivery = delivery if isinstance(delivery, dict) else {}

    badges = data.get("badges", [])
    raw_thumbnail = _safe_thumbnail(data.get("thumbnail"))
    if not raw_thumbnail:
        raw_thumbnail = _safe_thumbnail(data.get("thumbnails"))
    image_url = upgrade_thd_image(raw_thumbnail) if raw_thumbnail else ""
    thumbnails_full = _upgrade_thumbnails(data.get("thumbnails") or [])
    # Make sure the hero image is in the gallery as the first entry.
    if image_url:
        if image_url in thumbnails_full:
            thumbnails_full = [image_url] + [t for t in thumbnails_full if t != image_url]
        else:
            thumbnails_full = [image_url] + thumbnails_full

    spec_raw = data.get("specifications") or {}
    if isinstance(spec_raw, list):
        # SerpApi sometimes ships specifications as [{name, value}, ...]
        specifications: dict[str, Any] = {}
        for entry in spec_raw:
            if isinstance(entry, dict):
                k = entry.get("name") or entry.get("key")
                v = entry.get("value") or entry.get("val")
                if isinstance(k, str) and v is not None:
                    specifications[k] = v
    elif isinstance(spec_raw, dict):
        specifications = spec_raw
    else:
        specifications = {}

    dimensions_raw = data.get("dimensions") or {}
    dimensions = dimensions_raw if isinstance(dimensions_raw, dict) else {}

    variants_raw = data.get("variants") or []
    variants = variants_raw if isinstance(variants_raw, list) else []

    store_avail_raw = data.get("store_availability") or []
    store_availability = store_avail_raw if isinstance(store_avail_raw, list) else []

    description = ""
    desc_candidate = data.get("description") or data.get("highlights") or ""
    if isinstance(desc_candidate, str):
        description = desc_candidate.strip()
    elif isinstance(desc_candidate, list):
        description = " ".join(str(x).strip() for x in desc_candidate if x)

    sku = str(data.get("sku") or data.get("product_id") or "").strip()
    upc = str(data.get("upc") or "").strip()
    product_id = str(data.get("product_id") or "").strip()

    # Wave 2.0 fields — pricing unit, social proof, lazy-enrich URL.
    unit = str(data.get("unit") or "").strip()
    favorite_raw = data.get("favorite")
    favorite_count: int | None = None
    if isinstance(favorite_raw, (int, float)) and favorite_raw >= 0:
        favorite_count = int(favorite_raw)
    collection_url = str(data.get("collection") or "").strip()
    serpapi_link = str(data.get("serpapi_link") or "").strip()

    # Currency — explicit when SerpAPI returns it (CA = "CAD"), inferred
    # from URL host otherwise. Default USD for backward compat.
    currency = str(data.get("currency") or "").strip().upper()
    if not currency:
        link = str(data.get("link") or "")
        currency = "CAD" if ".homedepot.ca" in link else "USD"

    # CA fallback: SerpAPI Canada uses string "33%" for percent_off and a
    # `stock_information` dict with general_stock + store_stock_status.
    percentage_off = data.get("percentage_off")
    if percentage_off is None:
        percent_off_raw = data.get("percent_off")
        if isinstance(percent_off_raw, str) and percent_off_raw.strip():
            try:
                percentage_off = float(percent_off_raw.strip().rstrip("%"))
            except ValueError:
                percentage_off = None
        elif isinstance(percent_off_raw, (int, float)):
            percentage_off = float(percent_off_raw)

    # CA stock_information — surfaces general_stock + store_stock_status when
    # pickup.quantity is unavailable. We promote store_stock_status into
    # availability_text downstream.
    stock_info = data.get("stock_information") if isinstance(data.get("stock_information"), dict) else {}
    if stock is None and isinstance(stock_info, dict):
        ca_general = stock_info.get("general_stock")
        if isinstance(ca_general, (int, float)) and ca_general > 0:
            stock = int(ca_general)

    # Surface a one-line description for the card UI (description_short).
    # SerpAPI Home Depot search ships either `description` (string) or
    # `highlights` (list/string). The full product detail comes from the
    # home_depot_product enrich endpoint; the search response carries a
    # short blurb at best, so we cap at 200 chars to fit the card layout.
    description_short = description[:200] if description else ""
    description_full = description if description and len(description) > 200 else ""

    # ------------------------------------------------------------------
    # Bug A fix: derive in_stock bool and build the `pickup` wrapper dict
    # that the frontend reads as p.pickup.in_stock / p.pickup.store_id etc.
    #
    # We build a temporary record-like dict so _resolve_in_stock() can
    # read the fields it needs (in_store_stock, availability_text, etc.)
    # from the same raw data that the ProductRecord fields below will use.
    # drive_minutes is left None here; the materials route fills it in
    # after calling Distance Matrix (Bug B/C fix).
    # ------------------------------------------------------------------
    _in_stock_probe: dict[str, Any] = {
        "fulfillment_pickup": fulfillment_pickup,
        "in_store_stock": int(stock) if isinstance(stock, (int, float)) and stock > 0 else None,
        "availability_text": (
            str(stock_info.get("store_stock_status") or "").strip()
            or ("In stock" if stock and stock > 0 else "Check store")
        ),
        "availability": "in_stock" if stock and stock > 0 else "check_store",
    }
    in_stock: bool = _resolve_in_stock(_in_stock_probe)

    # The `pickup` wrapper emitted in `extra` is what the frontend reads.
    # Mirroring the shape the frontend's BackendProduct interface declares:
    #   pickup?: { in_stock?: bool; store_id?: str; store_name?: str;
    #              quantity?: int; drive_minutes?: int | null }
    pickup_wrapper: dict[str, Any] = {
        "store_id": str(store_id) if store_id else None,
        "store_name": str(store_name) if store_name else None,
        "store_address": pickup_store_address or None,
        "in_stock": in_stock,
        "quantity": int(stock) if isinstance(stock, (int, float)) and stock > 0 else None,
        "delivery_zip": str(data.get("delivery_zip") or "").strip() or None,
        # drive_minutes is intentionally null here; filled by the Distance
        # Matrix call in routes/materials.py after the playbook returns
        # (Bug C fix). Null means "not yet resolved" — UI shows "— min".
        "drive_minutes": None,
    }

    return ProductRecord(
        product_name=data.get("title", ""),
        brand=data.get("brand", ""),
        model=data.get("model_number", ""),
        sku=sku,
        upc=upc,
        product_id=product_id,
        retailer="Home Depot",
        price=data.get("price"),
        price_was=data.get("price_was"),
        price_saving=data.get("price_saving"),
        percentage_off=percentage_off,
        currency=currency,
        unit=unit,
        favorite=favorite_count,
        collection=collection_url,
        serpapi_link=serpapi_link,
        availability="in_stock" if stock and stock > 0 else "check_store",
        in_store_stock=stock,
        store_id=str(store_id or ""),
        store_name=store_name,
        delivery_info=delivery_str,
        url=data.get("link", ""),
        image_url=image_url,
        thumbnails=thumbnails_full,
        rating=data.get("rating"),
        reviews=data.get("reviews"),
        description=description,
        description_short=description_short,
        description_full=description_full,
        bay=bay,
        aisle=aisle,
        fulfillment_pickup=fulfillment_pickup,
        fulfillment_delivery=fulfillment_delivery,
        specifications=specifications,
        dimensions=dimensions,
        weight=str(data.get("weight") or ""),
        variants=variants,
        store_availability=store_availability,
        sources=[SourceAttribution(provider="serpapi_home_depot", retrieved_at=datetime.now(timezone.utc).isoformat())],
        extra={
            "thumbnail": image_url,
            "delivery": delivery_str,
            "badges": badges if isinstance(badges, list) else [],
            "availability_text": _in_stock_probe["availability_text"],
            # Pickup store address surfaced for "Available at: <store name>
            # — <address>" rendering on cards when store_summary is missing.
            "pickup_store_address": pickup_store_address,
            # SerpAPI HD price_badge ("Special-Buy", "New-Lower-Price") — UI
            # renders this as an amber chip next to the savings line.
            "price_badge": str(data.get("price_badge") or ""),
            # Bug A fix: top-level pickup wrapper the frontend reads as
            # p.pickup.in_stock, p.pickup.store_id, etc.
            "pickup": pickup_wrapper,
        },
    )
