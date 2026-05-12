"""Unit tests for services/adam/filter_derivation.py — Pass C."""
from __future__ import annotations

from aspire_orchestrator.services.adam.filter_derivation import derive_filters


def test_empty_product_list_returns_empty_filters():
    result = derive_filters([])
    assert result["brands"] == []
    assert result["stock"]["total_count"] == 0
    assert result["price_buckets"] == []


def test_brand_counts_top_8():
    products = [
        {"brand": "Behr", "pickup": {}, "price": 10.0},
        {"brand": "Behr", "pickup": {}, "price": 20.0},
        {"brand": "Sherwin", "pickup": {}, "price": 30.0},
        *[{"brand": f"Brand{i}", "pickup": {}, "price": float(i)} for i in range(10)],
    ]
    result = derive_filters(products)
    brand_names = [b["name"] for b in result["brands"]]
    assert "Behr" in brand_names
    assert len(result["brands"]) <= 8
    # Behr should be first (highest count)
    assert result["brands"][0]["name"] == "Behr"


def test_three_price_buckets_generated():
    products = [
        {"brand": "X", "price": 5.0, "pickup": {}},
        {"brand": "X", "price": 50.0, "pickup": {}},
        {"brand": "X", "price": 100.0, "pickup": {}},
    ]
    result = derive_filters(products)
    assert len(result["price_buckets"]) == 3
    # Total counts across buckets should equal total products
    total = sum(b["count"] for b in result["price_buckets"])
    assert total == 3
