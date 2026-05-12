"""Unit tests for services/adam/cache_normalize.py — Pass C."""
from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.cache_normalize import (
    NormalizeRejection,
    QueryRejectionCode,
    normalize_query,
)


def test_empty_string_returns_rejection():
    result = normalize_query("")
    assert isinstance(result, NormalizeRejection)
    assert result.code == QueryRejectionCode.QUERY_EMPTY


def test_whitespace_only_returns_rejection():
    result = normalize_query("   ")
    assert isinstance(result, NormalizeRejection)
    assert result.code == QueryRejectionCode.QUERY_EMPTY


def test_oversized_query_returns_rejection():
    result = normalize_query("a" * 501)
    assert isinstance(result, NormalizeRejection)
    assert result.code == QueryRejectionCode.QUERY_TOO_LONG


def test_exactly_500_chars_allowed():
    result = normalize_query("paint " * 83 + "  ")
    # 500-char normalised form
    assert isinstance(result, str)


def test_email_in_query_returns_rejection():
    result = normalize_query("contact user@example.com for price")
    assert isinstance(result, NormalizeRejection)
    assert result.code == QueryRejectionCode.CONTAINS_PII_EMAIL


def test_street_address_returns_rejection():
    result = normalize_query("2901 Main Street exterior paint")
    assert isinstance(result, NormalizeRejection)
    assert result.code == QueryRejectionCode.CONTAINS_PII_ADDRESS


def test_normal_query_normalised_lowercase():
    result = normalize_query("  Behr PAINT 5-Gallon  ")
    assert result == "behr paint 5-gallon"


def test_product_sku_not_rejected_as_address():
    # "1/2 in drywall" — must NOT trigger address detection
    result = normalize_query("1/2 in drywall sheet")
    assert isinstance(result, str)
    assert "drywall" in result
