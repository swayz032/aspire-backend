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
    retailer: str = ""
    price: float | None = None
    price_was: float | None = None
    price_saving: float | None = None
    percentage_off: float | None = None
    currency: str = "USD"
    availability: str = ""
    in_store_stock: int | None = None  # Home Depot pickup.quantity
    store_id: str = ""
    delivery_zip: str = ""
    delivery_info: str = ""
    url: str = ""
    image_url: str = ""
    rating: float | None = None
    reviews: int | None = None
    verification_status: str = "unverified"
    confidence: float = 0.0
    sources: list[SourceAttribution] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("sources", "extra")}
        d["sources"] = [{"provider": s.provider, "retrieved_at": s.retrieved_at} for s in self.sources]
        d.update(self.extra)
        return d
