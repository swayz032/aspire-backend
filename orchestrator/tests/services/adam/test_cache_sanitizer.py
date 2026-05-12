"""Unit tests for services/adam/cache_sanitizer.py — Pass C."""
from __future__ import annotations

from aspire_orchestrator.services.adam.cache_sanitizer import (
    sanitize_product,
    sanitize_product_list,
)


def test_strips_thumbnails_and_reviews():
    product = {
        "title": "Paint",
        "price": 12.99,
        "thumbnails": ["http://img1", "http://img2"],
        "reviews": [{"user": "Bob", "text": "great"}],
        "serpapi_product_api": "http://serpapi.com/...",
    }
    out = sanitize_product(product)
    assert "thumbnails" not in out
    assert "reviews" not in out
    assert "serpapi_product_api" not in out
    assert out["title"] == "Paint"
    assert out["price"] == 12.99


def test_truncates_specifications_to_20():
    specs = {str(i): f"val_{i}" for i in range(30)}
    product = {"title": "Tool", "specifications": specs}
    out = sanitize_product(product)
    assert len(out["specifications"]) == 20


def test_truncates_description_to_500():
    product = {"title": "X", "description": "a" * 600}
    out = sanitize_product(product)
    assert len(out["description"]) == 500


def test_truncates_breadcrumbs_to_5():
    product = {"title": "Y", "breadcrumbs": [f"cat{i}" for i in range(10)]}
    out = sanitize_product(product)
    assert len(out["breadcrumbs"]) == 5


def test_sanitize_product_list_is_non_mutating():
    original = [{"title": "Z", "reviews": [{"user": "Alice"}]}]
    result = sanitize_product_list(original)
    # Original unchanged
    assert "reviews" in original[0]
    # Output sanitized
    assert "reviews" not in result[0]
