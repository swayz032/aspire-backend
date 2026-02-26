"""Tests for receipt_store.py — Dual-Write Persistence (Wave 9).

Tests:
1. In-memory store works without Supabase configured (existing behavior)
2. _supabase_enabled() returns False when no URL configured
3. _map_receipt_to_row() produces correct Supabase schema
4. store_receipts() calls Supabase when configured (mocked)
5. Supabase failure does NOT block in-memory storage
6. Outcome → status mapping correctness
7. clear_store resets Supabase state
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from aspire_orchestrator.services.receipt_store import (
    clear_store,
    get_chain_receipts,
    get_receipt_count,
    query_receipts,
    store_receipts,
    _map_receipt_to_row,
    _supabase_enabled,
)


@pytest.fixture(autouse=True)
def _clean_store():
    """Reset receipt store before each test."""
    clear_store()
    yield
    clear_store()


# =============================================================================
# In-Memory Store (existing behavior preserved)
# =============================================================================


class TestInMemoryStore:
    """Verify in-memory storage works identically to pre-Wave 9."""

    def test_store_and_query_basic(self):
        receipts = [
            {"id": "r1", "suite_id": "STE-0001", "action_type": "calendar.read", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "r2", "suite_id": "STE-0001", "action_type": "invoice.create", "created_at": "2026-01-02T00:00:00Z"},
            {"id": "r3", "suite_id": "STE-0002", "action_type": "calendar.read", "created_at": "2026-01-03T00:00:00Z"},
        ]
        store_receipts(receipts)

        # Query by suite_id (Law #6: tenant isolation)
        result = query_receipts(suite_id="STE-0001")
        assert len(result) == 2
        assert all(r["suite_id"] == "STE-0001" for r in result)

        # Cross-tenant query returns nothing
        result = query_receipts(suite_id="s999")
        assert len(result) == 0

    def test_query_with_filters(self):
        receipts = [
            {"id": "r1", "suite_id": "STE-0001", "action_type": "calendar.read", "risk_tier": "green",
             "correlation_id": "c1", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "r2", "suite_id": "STE-0001", "action_type": "invoice.create", "risk_tier": "yellow",
             "correlation_id": "c2", "created_at": "2026-01-02T00:00:00Z"},
        ]
        store_receipts(receipts)

        # Filter by action_type
        result = query_receipts(suite_id="STE-0001", action_type="invoice.create")
        assert len(result) == 1
        assert result[0]["id"] == "r2"

        # Filter by risk_tier
        result = query_receipts(suite_id="STE-0001", risk_tier="green")
        assert len(result) == 1
        assert result[0]["id"] == "r1"

        # Filter by correlation_id
        result = query_receipts(suite_id="STE-0001", correlation_id="c1")
        assert len(result) == 1
        assert result[0]["id"] == "r1"

    def test_query_pagination(self):
        receipts = [
            {"id": f"r{i}", "suite_id": "STE-0001", "created_at": f"2026-01-{i+1:02d}T00:00:00Z"}
            for i in range(10)
        ]
        store_receipts(receipts)

        result = query_receipts(suite_id="STE-0001", limit=3, offset=0)
        assert len(result) == 3

        result = query_receipts(suite_id="STE-0001", limit=3, offset=7)
        assert len(result) == 3

    def test_query_sorted_newest_first(self):
        receipts = [
            {"id": "r-old", "suite_id": "STE-0001", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "r-new", "suite_id": "STE-0001", "created_at": "2026-01-05T00:00:00Z"},
            {"id": "r-mid", "suite_id": "STE-0001", "created_at": "2026-01-03T00:00:00Z"},
        ]
        store_receipts(receipts)

        result = query_receipts(suite_id="STE-0001")
        assert result[0]["id"] == "r-new"
        assert result[1]["id"] == "r-mid"
        assert result[2]["id"] == "r-old"

    def test_get_chain_receipts(self):
        receipts = [
            {"id": "r1", "suite_id": "STE-0001", "chain_id": "s1", "sequence": 2},
            {"id": "r2", "suite_id": "STE-0001", "chain_id": "s1", "sequence": 1},
            {"id": "r3", "suite_id": "STE-0001", "chain_id": "other", "sequence": 1},
        ]
        store_receipts(receipts)

        chain = get_chain_receipts(suite_id="STE-0001", chain_id="s1")
        assert len(chain) == 2
        assert chain[0]["sequence"] == 1  # sorted ascending
        assert chain[1]["sequence"] == 2

    def test_get_receipt_count(self):
        store_receipts([{"id": "r1", "suite_id": "STE-0001"}, {"id": "r2", "suite_id": "STE-0002"}])
        assert get_receipt_count() == 2
        assert get_receipt_count("STE-0001") == 1
        assert get_receipt_count("s999") == 0

    def test_clear_store(self):
        store_receipts([{"id": "r1", "suite_id": "STE-0001"}])
        assert get_receipt_count() == 1
        clear_store()
        assert get_receipt_count() == 0


# =============================================================================
# Supabase Configuration Detection
# =============================================================================


class TestSupabaseEnabled:
    """Verify Supabase detection logic."""

    def test_disabled_when_no_url(self):
        with patch("aspire_orchestrator.config.settings.settings") as mock_settings:
            mock_settings.supabase_url = ""
            mock_settings.supabase_service_role_key = "some-key"
            assert not _supabase_enabled()

    def test_disabled_when_no_key(self):
        with patch("aspire_orchestrator.config.settings.settings") as mock_settings:
            mock_settings.supabase_url = "https://example.supabase.co"
            mock_settings.supabase_service_role_key = ""
            assert not _supabase_enabled()

    def test_enabled_when_both_set(self):
        with patch("aspire_orchestrator.config.settings.settings") as mock_settings:
            mock_settings.supabase_url = "https://example.supabase.co"
            mock_settings.supabase_service_role_key = "service-role-key"
            assert _supabase_enabled()


# =============================================================================
# Field Mapping (Orchestrator → Supabase)
# =============================================================================


class TestReceiptFieldMapping:
    """Verify _map_receipt_to_row produces correct Supabase schema."""

    def test_full_receipt_mapping(self):
        receipt = {
            "id": "rcpt-001",
            "suite_id": "suite-abc-123",
            "office_id": "office-xyz-789",
            "receipt_type": "execution",
            "outcome": "success",
            "correlation_id": "corr-456",
            "actor_type": "user",
            "actor_id": "actor-001",
            "action_type": "invoice.create",
            "tool_used": "stripe.invoice.create",
            "risk_tier": "yellow",
            "capability_token_id": "tok-789",
            "redacted_inputs": '{"amount": 100}',
            "redacted_outputs": '{"invoice_id": "inv-001"}',
            "reason_code": None,
            "receipt_hash": "abcdef1234567890",
            "created_at": "2026-02-13T10:00:00Z",
        }

        row = _map_receipt_to_row(receipt)

        assert row["receipt_id"] == "rcpt-001"
        assert row["suite_id"] == "suite-abc-123"
        assert row["tenant_id"] == "suite-abc-123"  # Phase 1: suite == tenant
        assert row["office_id"] == "office-xyz-789"
        assert row["receipt_type"] == "execution"
        assert row["status"] == "SUCCEEDED"
        assert row["correlation_id"] == "corr-456"
        assert row["actor_type"] == "USER"
        assert row["actor_id"] == "actor-001"
        assert row["created_at"] == "2026-02-13T10:00:00Z"

        # Action jsonb
        assert row["action"]["action_type"] == "invoice.create"
        assert row["action"]["tool_used"] == "stripe.invoice.create"
        assert row["action"]["risk_tier"] == "yellow"
        assert row["action"]["capability_token_id"] == "tok-789"

        # Result jsonb
        assert row["result"]["redacted_inputs"] == '{"amount": 100}'
        assert row["result"]["redacted_outputs"] == '{"invoice_id": "inv-001"}'

        # Hash as bytea hex
        assert row["receipt_hash"] == "\\xabcdef1234567890"

    def test_outcome_to_status_mapping(self):
        """All outcome values map to correct Supabase status enum."""
        mappings = {
            "success": "SUCCEEDED",
            "succeeded": "SUCCEEDED",
            "failed": "FAILED",
            "denied": "DENIED",
            "pending": "PENDING",
        }
        for outcome, expected_status in mappings.items():
            row = _map_receipt_to_row({"id": "r1", "outcome": outcome})
            assert row["status"] == expected_status, f"{outcome} should map to {expected_status}"

    def test_unknown_outcome_defaults_to_pending(self):
        row = _map_receipt_to_row({"id": "r1", "outcome": "weird"})
        assert row["status"] == "PENDING"

    def test_missing_outcome_defaults_to_pending(self):
        row = _map_receipt_to_row({"id": "r1"})
        assert row["status"] == "PENDING"

    def test_minimal_receipt_mapping(self):
        """A receipt with only an ID should map without errors."""
        row = _map_receipt_to_row({"id": "r-minimal"})
        assert row["receipt_id"] == "r-minimal"
        assert row["suite_id"] == ""
        assert row["tenant_id"] == ""
        assert row["correlation_id"] == ""
        assert row["actor_type"] == "SYSTEM"
        assert row["action"] is None
        assert row["result"] is None

    def test_office_id_excluded_when_none(self):
        row = _map_receipt_to_row({"id": "r1", "office_id": None})
        assert "office_id" not in row

    def test_office_id_included_when_present(self):
        row = _map_receipt_to_row({"id": "r1", "office_id": "off-123"})
        assert row["office_id"] == "off-123"

    def test_actor_type_uppercased(self):
        row = _map_receipt_to_row({"id": "r1", "actor_type": "user"})
        assert row["actor_type"] == "USER"

        row = _map_receipt_to_row({"id": "r1", "actor_type": "system"})
        assert row["actor_type"] == "SYSTEM"


# =============================================================================
# Dual-Write Behavior (Mocked Supabase)
# =============================================================================


class TestDualWrite:
    """Verify dual-write: in-memory always + Supabase when configured."""

    @patch("aspire_orchestrator.services.receipt_store._supabase_enabled", return_value=False)
    def test_no_supabase_call_when_disabled(self, mock_enabled):
        """When Supabase is not configured, only in-memory storage happens."""
        with patch("aspire_orchestrator.services.receipt_store._persist_to_supabase") as mock_persist:
            store_receipts([{"id": "r1", "suite_id": "STE-0001"}])
            mock_persist.assert_not_called()
            assert get_receipt_count() == 1

    @patch("aspire_orchestrator.services.receipt_store._supabase_enabled", return_value=True)
    @patch("aspire_orchestrator.services.receipt_store._persist_to_supabase")
    def test_supabase_called_when_enabled(self, mock_persist, mock_enabled):
        """When Supabase is configured, both stores receive receipts."""
        receipts = [{"id": "r1", "suite_id": "STE-0001"}]
        store_receipts(receipts)

        assert get_receipt_count() == 1  # In-memory works
        mock_persist.assert_called_once_with(receipts)  # Supabase called

    @patch("aspire_orchestrator.services.receipt_store._supabase_enabled", return_value=True)
    @patch("aspire_orchestrator.services.receipt_store._persist_to_supabase", side_effect=Exception("Supabase down"))
    def test_supabase_failure_does_not_block_inmemory(self, mock_persist, mock_enabled):
        """Supabase failure must NOT prevent in-memory storage (resilience)."""
        store_receipts([{"id": "r1", "suite_id": "STE-0001"}])
        assert get_receipt_count() == 1  # In-memory still works

    @patch("aspire_orchestrator.services.receipt_store._get_supabase_client")
    def test_persist_to_supabase_calls_insert(self, mock_get_client):
        """Verify the Supabase client receives correct insert call (append-only)."""
        from aspire_orchestrator.services.receipt_store import _persist_to_supabase

        mock_client = MagicMock()
        mock_table = MagicMock()
        mock_insert = MagicMock()
        mock_execute = MagicMock()

        mock_get_client.return_value = mock_client
        mock_client.table.return_value = mock_table
        mock_table.insert.return_value = mock_insert
        mock_insert.execute.return_value = mock_execute

        receipts = [{"id": "r1", "suite_id": "STE-0001", "outcome": "success", "correlation_id": "c1"}]
        _persist_to_supabase(receipts)

        mock_client.table.assert_called_once_with("receipts")
        call_args = mock_table.insert.call_args
        rows = call_args[0][0]
        assert len(rows) == 1
        assert rows[0]["receipt_id"] == "r1"
        assert rows[0]["status"] == "SUCCEEDED"

    @patch("aspire_orchestrator.services.receipt_store._get_supabase_client", return_value=None)
    def test_persist_gracefully_handles_no_client(self, mock_get_client):
        """If client init failed, _persist_to_supabase returns without error."""
        from aspire_orchestrator.services.receipt_store import _persist_to_supabase
        # Should not raise
        _persist_to_supabase([{"id": "r1", "suite_id": "STE-0001"}])


# =============================================================================
# Immutability Guarantees (Law #2)
# =============================================================================


class TestImmutability:
    """Verify Law #2: receipts are append-only, no update/delete."""

    def test_store_is_append_only(self):
        store_receipts([{"id": "r1", "suite_id": "STE-0001"}])
        store_receipts([{"id": "r2", "suite_id": "STE-0001"}])
        assert get_receipt_count() == 2

    def test_no_update_method_exists(self):
        """The module should not expose any update/delete functions."""
        import aspire_orchestrator.services.receipt_store as rs
        assert not hasattr(rs, "update_receipt")
        assert not hasattr(rs, "delete_receipt")
        assert not hasattr(rs, "remove_receipt")
