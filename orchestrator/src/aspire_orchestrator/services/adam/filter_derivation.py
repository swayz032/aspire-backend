"""Filter derivation from a product list — Pass C.

Produces structured filter facets for the materials search UI:
  - Top 8 brands by frequency
  - In-stock count vs total
  - 3 dynamic price buckets (rounded to nearest $5)

Law #7: Pure computation — no external calls.
Law #9: No PII in filter keys or labels.
"""

from __future__ import annotations

from typing import Any


def _round5(value: float) -> float:
    """Round value to nearest $5 (0.5 rounds up)."""
    return round(round(value / 5) * 5, 2)


def derive_filters(products: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive UI filter facets from a normalised product list.

    Returns a dict with keys: brands, stock, price_buckets.

    - brands: top 8 by count, [{name, count}]
    - stock: {in_stock_count, total_count}
    - price_buckets: 3 buckets with label + count, e.g.
        [{label: "Under $25", min: 0, max: 25, count: 4}, ...]
    """
    if not products:
        return {"brands": [], "stock": {"in_stock_count": 0, "total_count": 0}, "price_buckets": []}

    # --- Brands ---
    brand_counts: dict[str, int] = {}
    for p in products:
        brand = (p.get("brand") or "").strip()
        if brand:
            brand_counts[brand] = brand_counts.get(brand, 0) + 1

    top_brands = sorted(brand_counts.items(), key=lambda x: x[1], reverse=True)[:8]
    brands = [{"name": name, "count": cnt} for name, cnt in top_brands]

    # --- Stock ---
    in_stock_count = 0
    for p in products:
        pickup = p.get("pickup") or {}
        delivery = p.get("delivery")
        stock_info = p.get("stock_information") or {}
        # Count as in-stock if any of these signals are truthy
        is_in_stock = bool(
            pickup.get("in_stock")
            or pickup.get("quantity")
            or (delivery is not None and delivery is not False)
            or stock_info.get("general_stock")
        )
        if is_in_stock:
            in_stock_count += 1

    stock = {"in_stock_count": in_stock_count, "total_count": len(products)}

    # --- Price buckets ---
    prices: list[float] = []
    for p in products:
        raw = p.get("price")
        if raw is not None:
            try:
                prices.append(float(raw))
            except (TypeError, ValueError):
                pass

    if not prices:
        return {"brands": brands, "stock": stock, "price_buckets": []}

    prices_sorted = sorted(prices)
    lo = prices_sorted[0]
    hi = prices_sorted[-1]

    if hi <= lo:
        # All same price — single bucket
        price_buckets = [
            {"label": f"${_round5(lo):.0f}", "min": 0.0, "max": hi * 2, "count": len(prices)}
        ]
    else:
        # Three equal-width buckets rounded to $5
        step = (hi - lo) / 3
        b1 = _round5(lo + step)
        b2 = _round5(lo + 2 * step)

        c1 = sum(1 for p in prices if p < b1)
        c2 = sum(1 for p in prices if b1 <= p < b2)
        c3 = sum(1 for p in prices if p >= b2)

        price_buckets = [
            {"label": f"Under ${b1:.0f}", "min": 0.0, "max": b1, "count": c1},
            {"label": f"${b1:.0f}–${b2:.0f}", "min": b1, "max": b2, "count": c2},
            {"label": f"Over ${b2:.0f}", "min": b2, "max": None, "count": c3},
        ]

    return {"brands": brands, "stock": stock, "price_buckets": price_buckets}
