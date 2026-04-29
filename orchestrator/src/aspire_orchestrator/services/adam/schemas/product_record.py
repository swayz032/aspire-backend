"""ProductRecord — Canonical schema for product/tool/material pricing.

Sources: SerpApi Google Shopping, SerpApi Home Depot, web evidence
Dedup: STRICT — identical SKU/model required, never merge by name alone (ADR-003)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution


@dataclass
class ProductRecord:
    """Canonical product/tool/material record."""

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
    thumbnails: list[str] = field(default_factory=list)  # Full image gallery (high-res)
    rating: float | None = None
    reviews: int | None = None
    description: str = ""
    specifications: dict[str, Any] = field(default_factory=dict)
    dimensions: dict[str, Any] = field(default_factory=dict)  # {height, width, depth}
    weight: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)  # Color/size options
    store_availability: list[dict[str, Any]] = field(default_factory=list)  # Multi-store stock
    verification_status: str = "unverified"
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("sources", "extra")}
        d["sources"] = [{"provider": s.provider, "retrieved_at": s.retrieved_at} for s in self.sources]
        d.update(self.extra)
        return d
