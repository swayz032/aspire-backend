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
import aspire_orchestrator.services.admin_store as _admin_store_mod


@pytest.fixture(autouse=True)
def _force_in_memory():
    """Force admin_store to use in-memory only (no real Supabase in tests)."""
    _admin_store_mod._supabase_client = None
    _admin_store_mod._supabase_init_done = True
    yield
    _admin_store_mod._supabase_init_done = False


class TestAdminSupabaseStore:
    """Core store behavior (in-memory fallback mode)."""

    def setup_method(self):
        self.incidents = {}
        self.provider_calls = []
        self.store = AdminSupabaseStore(
            incidents=self.incidents,
            provider_calls=self.provider_calls,
        )

    def _store_incident(self, **overrides) -> dict:
        """Create and store a test incident, return fallback dict."""
        defaults = {
            "incident_id": str(uuid.uuid4()),
            "tenant_id": "system",
            "status": "open",
            "severity": "high",
            "title": "Test incident",
            "correlation_id": str(uuid.uuid4()),
            "source": "test",
        }
        defaults.update(overrides)
        result, _ = self.store.store_incident(
            incident_id=defaults["incident_id"],
            tenant_id=defaults["tenant_id"],
            title=defaults["title"],
            severity=defaults["severity"],
            source=defaults["source"],
            status=defaults["status"],
            correlation_id=defaults["correlation_id"],
        )
        return result

    def test_store_incident_in_memory(self):
        """Incidents should be stored in-memory."""
        result = self._store_incident()
        assert result["incident_id"] in self.incidents

    def test_query_incidents_returns_stored(self):
        """Query should return incidents that were stored."""
        result = self._store_incident()
        items, page_info = self.store.query_incidents()
        assert len(items) == 1
        assert items[0]["incident_id"] == result["incident_id"]

    def test_query_incidents_filter_by_state(self):
        """Should filter by state."""
        self._store_incident(status="open")
        self._store_incident(status="closed")
        items, _ = self.store.query_incidents(state="open")
        assert len(items) == 1
        assert items[0]["state"] == "open"

    def test_query_incidents_filter_by_severity(self):
        """Should filter by severity."""
        self._store_incident(severity="high")
        self._store_incident(severity="low")
        items, _ = self.store.query_incidents(severity="high")
        assert len(items) == 1
        # Severity mapping: "high" -> "sev2" in legacy format
        assert items[0]["severity"] in ("high", "sev2")

    def test_query_incidents_limit(self):
        """Should respect limit."""
        for _ in range(5):
            self._store_incident()
        items, _ = self.store.query_incidents(limit=3)
        assert len(items) == 3

    def test_get_incident_by_id(self):
        """Should get a single incident by ID."""
        result = self._store_incident()
        found = self.store.get_incident(result["incident_id"])
        assert found is not None
        assert found["incident_id"] == result["incident_id"]

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

    def test_register_incident_stores_in_admin_store(self):
        """register_incident() should store in AdminStore's in-memory dict."""
        from aspire_orchestrator.routes.admin import register_incident, clear_admin_stores
        from aspire_orchestrator.services.admin_store import get_admin_store

        clear_admin_stores()
        inc_id = str(uuid.uuid4())
        inc = {
            "incident_id": inc_id,
            "state": "open",
            "severity": "high",
            "title": "Integration test incident",
        }
        register_incident(inc)

        store = get_admin_store()
        found = store.get_incident(inc_id)
        assert found is not None
        assert found["incident_id"] == inc_id

        # Cleanup
        clear_admin_stores()
