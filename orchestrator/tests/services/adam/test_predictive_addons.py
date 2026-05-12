"""Unit tests for services/adam/predictive_addons.py — Pass C."""
from __future__ import annotations

from aspire_orchestrator.services.adam.predictive_addons import (
    detect_category,
    get_predictive_addons,
)


def test_paint_category_detected():
    assert detect_category("behr marquee paint 5 gallon") == "paint"


def test_drywall_category_detected():
    assert detect_category("half inch drywall sheet 4x8") == "drywall"


def test_electrical_category_detected():
    assert detect_category("12 2 romex wire 250 ft") == "electrical"


def test_unknown_category_returns_none():
    assert detect_category("random query xyz") is None


def test_get_predictive_addons_returns_max_4():
    addons = get_predictive_addons("exterior paint primer")
    assert 1 <= len(addons) <= 4
    for a in addons:
        assert "title" in a
        assert "category" in a


def test_unknown_query_returns_empty_list():
    addons = get_predictive_addons("xyzzy unknown product")
    assert addons == []
