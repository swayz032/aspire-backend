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
    _admin_store_mod._ensured_tenants.clear()
    yield
    _admin_store_mod._ensured_tenants.clear()
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

    def test_store_incident_ensures_reserved_system_tenant_before_insert(self):
        """System incidents should create/ensure the reserved tenant row before insert."""
        tenants_table = MagicMock()
        incidents_table = MagicMock()
        fake_client = MagicMock()

        def _table(name: str):
            return {
                "tenants": tenants_table,
                "incidents": incidents_table,
            }[name]

        fake_client.table.side_effect = _table
        tenants_table.upsert.return_value.execute.return_value = MagicMock(
            data=[{"tenant_id": "system", "name": "Aspire Internal"}]
        )
        incidents_table.insert.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "inc-system",
                    "tenant_id": "system",
                    "title": "System incident",
                    "severity": "high",
                    "source": "backend",
                    "status": "open",
                    "metadata": {},
                    "created_at": "2026-03-20T00:00:00+00:00",
                    "updated_at": "2026-03-20T00:00:00+00:00",
                }
            ]
        )

        with patch.object(_admin_store_mod, "_get_supabase", return_value=fake_client):
            row, ok = self.store.store_incident(
                tenant_id="system",
                title="System incident",
                severity="high",
            )

        assert ok is True
        tenants_table.upsert.assert_called_once_with(
            {"tenant_id": "system", "name": "Aspire Internal"},
            on_conflict="tenant_id",
        )
        incidents_table.insert.assert_called_once()
        assert row["tenant_id"] == "system"

    def test_upsert_incident_updates_existing_open_incident_without_postgrest_upsert(self):
        """Open fingerprint matches should use select+update, not broken partial-index upsert."""
        tenants_table = MagicMock()
        incidents_table = MagicMock()
        fake_client = MagicMock()

        def _table(name: str):
            return {
                "tenants": tenants_table,
                "incidents": incidents_table,
            }[name]

        fake_client.table.side_effect = _table
        tenants_table.upsert.return_value.execute.return_value = MagicMock(
            data=[{"tenant_id": "system", "name": "Aspire Internal"}]
        )

        existing_row = {
            "id": "inc-existing",
            "tenant_id": "system",
            "title": "Existing incident",
            "severity": "medium",
            "source": "desktop",
            "status": "open",
            "fingerprint": "desktop:auth:blank",
            "description": "old",
            "component": "auth",
            "provider": None,
            "correlation_id": "corr-old",
            "tags": {"existing": True},
            "metadata": {
                "timeline": [{"event": "reported"}],
                "evidence_pack": {"report_count": 1},
            },
            "created_at": "2026-03-20T00:00:00+00:00",
            "updated_at": "2026-03-20T00:00:00+00:00",
        }
        updated_row = {
            **existing_row,
            "title": "Updated incident",
            "severity": "high",
            "correlation_id": "corr-new",
            "metadata": {
                "timeline": [{"event": "reported"}, {"event": "reported_again"}],
                "evidence_pack": {"report_count": 2},
            },
            "updated_at": "2026-03-20T00:10:00+00:00",
        }

        select_query = MagicMock()
        select_query.eq.return_value = select_query
        select_query.in_.return_value = select_query
        select_query.order.return_value = select_query
        select_query.limit.return_value = select_query
        select_query.execute.return_value = MagicMock(data=[existing_row])
        incidents_table.select.return_value = select_query

        update_query = MagicMock()
        update_eq_query = MagicMock()
        update_query.eq.return_value = update_eq_query
        update_eq_query.execute.return_value = MagicMock(data=[updated_row])
        incidents_table.update.return_value = update_query

        with patch.object(_admin_store_mod, "_get_supabase", return_value=fake_client):
            incident, deduped, ok = self.store.upsert_incident(
                tenant_id="system",
                title="Updated incident",
                severity="high",
                source="desktop",
                component="auth",
                fingerprint="desktop:auth:blank",
                correlation_id="corr-new",
                metadata={
                    "timeline": [{"event": "reported_again"}],
                    "evidence_pack": {"report_count": 1},
                },
                tags={"new": True},
            )

        assert ok is True
        assert deduped is True
        incidents_table.upsert.assert_not_called()
        incidents_table.update.assert_called_once()
        update_payload = incidents_table.update.call_args.args[0]
        assert update_payload["tenant_id"] == "system"
        assert update_payload["tags"] == {"existing": True, "new": True}
        assert update_payload["metadata"]["evidence_pack"]["report_count"] == 2
        assert incident["incident_id"] == "inc-existing"
        assert incident["title"] == "Updated incident"


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
