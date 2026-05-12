"""Unit tests for services/adam/drive_minutes_cache.py — Pass C."""
from __future__ import annotations

import time

import pytest

from aspire_orchestrator.services.adam import drive_minutes_cache as dmc


def setup_function():
    dmc._reset_for_tests()


def teardown_function():
    dmc._reset_for_tests()


def test_miss_returns_none():
    assert dmc.get_drive_minutes("78701", "ChIJabc123") is None


def test_set_then_get_returns_value():
    dmc.set_drive_minutes("78701", "ChIJabc123", drive_minutes=14, in_traffic=True)
    result = dmc.get_drive_minutes("78701", "ChIJabc123")
    assert result == (14, True)


def test_different_keys_dont_collide():
    dmc.set_drive_minutes("78701", "ChIJaaa", 10, False)
    dmc.set_drive_minutes("78701", "ChIJbbb", 25, True)
    assert dmc.get_drive_minutes("78701", "ChIJaaa") == (10, False)
    assert dmc.get_drive_minutes("78701", "ChIJbbb") == (25, True)


def test_expired_entry_returns_none(monkeypatch):
    """Simulate TTL expiry by patching time.monotonic."""
    dmc.set_drive_minutes("78701", "ChIJexp", 8, False)
    # Advance time beyond TTL
    original_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original_monotonic() + dmc._TTL_SECONDS + 1)
    assert dmc.get_drive_minutes("78701", "ChIJexp") is None
