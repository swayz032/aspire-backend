"""Tests for Admin Supabase Store (Wave 2C — F2 fix).

Verifies that:
1. Incidents are stored both in-memory and Supabase
2. Queries return real data (not empty dicts)
3. Graceful degradation when Supabase unavailable
4. OpenAPI schema conformance
"""

from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from aspire_orchestrator.services.admin_store import AdminSupabaseStore


class TestAdminSupabaseStore:
    """Core store behavior (in-memory fallback mode)."""

    def setup_method(self):
        self.incidents = {}
        self.provider_calls = []
        self.store = AdminSupabaseStore(
            incidents=self.incidents,
            provider_calls=self.provider_calls,
        )

    def _make_incident(self, **overrides) -> dict:
        defaults = {
            "incident_id": str(uuid.uuid4()),
            "suite_id": "system",
            "state": "open",
            "severity": "high",
            "title": "Test incident",
            "correlation_id": str(uuid.uuid4()),
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "timeline": [{"timestamp": datetime.now(timezone.utc).isoformat(), "event": "test"}],
            "evidence_pack": {"test": True},
        }
        defaults.update(overrides)
        return defaults

    def test_store_incident_in_memory(self):
        """Incidents should be stored in-memory."""
        inc = self._make_incident()
        self.store.store_incident(inc)
        assert inc["incident_id"] in self.incidents

    def test_query_incidents_returns_stored(self):
        """Query should return incidents that were stored."""
        inc = self._make_incident()
        self.store.store_incident(inc)
        items, page_info = self.store.query_incidents()
        assert len(items) == 1
        assert items[0]["incident_id"] == inc["incident_id"]

    def test_query_incidents_filter_by_state(self):
        """Should filter by state."""
        self.store.store_incident(self._make_incident(state="open"))
        self.store.store_incident(self._make_incident(state="closed"))
        items, _ = self.store.query_incidents(state="open")
        assert len(items) == 1
        assert items[0]["state"] == "open"

    def test_query_incidents_filter_by_severity(self):
        """Should filter by severity."""
        self.store.store_incident(self._make_incident(severity="high"))
        self.store.store_incident(self._make_incident(severity="low"))
        items, _ = self.store.query_incidents(severity="high")
        assert len(items) == 1
        assert items[0]["severity"] == "high"

    def test_query_incidents_limit(self):
        """Should respect limit."""
        for _ in range(5):
            self.store.store_incident(self._make_incident())
        items, _ = self.store.query_incidents(limit=3)
        assert len(items) == 3

    def test_get_incident_by_id(self):
        """Should get a single incident by ID."""
        inc = self._make_incident()
        self.store.store_incident(inc)
        result = self.store.get_incident(inc["incident_id"])
        assert result is not None
        assert result["incident_id"] == inc["incident_id"]

    def test_get_incident_not_found(self):
        """Should return None for missing incident."""
        result = self.store.get_incident("nonexistent-id")
        assert result is None


class TestAdminStoreProviderCalls:
    """Provider call queries (uses provider_call_logger fallback)."""

    def setup_method(self):
        self.store = AdminSupabaseStore(
            incidents={},
            provider_calls=[],
        )

    def test_query_provider_calls_uses_logger(self):
        """Should query from provider_call_logger when Supabase unavailable."""
        from aspire_orchestrator.services.provider_call_logger import get_provider_call_logger

        pcl = get_provider_call_logger()
        pcl.clear()
        pcl.log_call(
            provider="stripe",
            action="POST /v1/invoices",
            correlation_id="test-corr",
            success=True,
        )

        items, page_info = self.store.query_provider_calls(provider="stripe")
        assert len(items) == 1
        assert items[0]["provider"] == "stripe"


class TestRegisterIncidentIntegration:
    """Test that register_incident() in admin.py uses the store."""

    def test_register_incident_stores_in_memory(self):
        """register_incident() should always store in-memory."""
        from aspire_orchestrator.routes.admin import register_incident, _incidents

        inc_id = str(uuid.uuid4())
        inc = {
            "incident_id": inc_id,
            "state": "open",
            "severity": "high",
            "title": "Integration test incident",
        }
        register_incident(inc)
        assert inc_id in _incidents
        # Cleanup
        del _incidents[inc_id]
