"""ProductRecord — Canonical schema for product/tool/material pricing.

Sources: SerpApi Google Shopping, SerpApi Home Depot, web evidence
Dedup: STRICT — identical SKU/model required, never merge by name alone (ADR-003)

Field provenance:
  - Always populated: filled by the basic SerpApi `home_depot` (or
    `google_shopping`) search. These fields drive the carousel cards Ava
    returns from a single voice query.
  - Lazy-only: populated when the user opens the product detail modal and
    the desktop calls `POST /v1/tools/enrich_product`, which hits SerpApi's
    `home_depot_product` engine for one extra unit per product.

Voice path returns ProductRecord[] from basic search only — never blocks on
lazy fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution


@dataclass
class ProductRecord:
    """Canonical product/tool/material record."""

    # ─ Always populated (from basic SerpApi search) ──────────────────────────
    product_name: str = ""
    brand: str = ""
    model: str = ""
    sku: str = ""
    upc: str = ""
    product_id: str = ""
    retailer: str = ""
    price: float | None = None
    price_was: float | None = None
    price_saving: float | None = None
    percentage_off: float | None = None
    currency: str = "USD"
    availability: str = ""
    in_store_stock: int | None = None  # Home Depot pickup.quantity
    store_id: str = ""
    store_name: str = ""  # Per-product local store name (Home Depot pickup.store_name)
    delivery_zip: str = ""
    delivery_info: str = ""
    url: str = ""
    image_url: str = ""
    rating: float | None = None
    reviews: int | None = None

    # ─ Populated only after /v1/tools/enrich_product call ────────────────────
    # Basic search may seed some of these (e.g., a short description), but the
    # full gallery, bay/aisle, and rich specs only arrive from the
    # home_depot_product engine. None / empty == "not yet enriched".
    thumbnails: list[str] = field(default_factory=list)  # Full image gallery (high-res)
    description: str = ""
    specifications: dict[str, Any] = field(default_factory=dict)
    dimensions: dict[str, Any] = field(default_factory=dict)
    weight: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)
    store_availability: list[dict[str, Any]] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    description_short: str = ""
    description_full: str = ""
    bay: str = ""
    aisle: str = ""
    fulfillment_pickup: dict[str, Any] = field(default_factory=dict)
    fulfillment_delivery: dict[str, Any] = field(default_factory=dict)
    purchasing_limit_notes: str = ""

    # ─ Verification + provenance (always populated) ──────────────────────────
    verification_status: str = "unverified"
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("sources", "extra")}
        d["sources"] = [{"provider": s.provider, "retrieved_at": s.retrieved_at} for s in self.sources]
        d.update(self.extra)
        return d
