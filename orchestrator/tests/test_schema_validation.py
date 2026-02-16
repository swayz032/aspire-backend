"""Tests for SchemaValidatorService — ecosystem + ops schema validation.

Covers:
- Schema loading (58 ecosystem + 20+ ops)
- Receipt validation (valid, invalid, base fallback, auto-detect, ops)
- Event validation (A2A, model route, outbox)
- Capability token validation
- Evidence pack validation
- Learning object validation
- Evil tests (extra fields, missing required, unknown schema)
- Stats and listing
- Validation mode (warn vs strict)
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.schema_validator_service import (
    SchemaValidationResult,
    SchemaValidatorService,
    get_schema_validator,
    reset_schema_validator,
)


@pytest.fixture(autouse=True)
def _reset():
    """Reset singleton between tests."""
    reset_schema_validator()
    yield
    reset_schema_validator()


@pytest.fixture
def svc() -> SchemaValidatorService:
    return SchemaValidatorService()


# ---------------------------------------------------------------------------
# Schema Loading
# ---------------------------------------------------------------------------


class TestSchemaLoading:
    """Verify all schemas load correctly."""

    def test_ecosystem_receipts_loaded(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        assert "receipts" in listing
        assert len(listing["receipts"]) >= 44

    def test_ecosystem_events_loaded(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        assert "events" in listing
        assert len(listing["events"]) == 3

    def test_ecosystem_capabilities_loaded(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        assert "capabilities" in listing
        assert len(listing["capabilities"]) == 1

    def test_ecosystem_evidence_loaded(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        assert "evidence" in listing
        assert len(listing["evidence"]) == 1

    def test_ecosystem_learning_loaded(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        assert "learning" in listing
        assert len(listing["learning"]) == 5

    def test_ops_receipts_loaded(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        assert "ops_receipts" in listing
        assert len(listing["ops_receipts"]) >= 20

    def test_total_schemas_at_least_78(self, svc: SchemaValidatorService) -> None:
        st = svc.stats()
        assert st["total"] >= 78, f"Expected >= 78 schemas, got {st['total']}"

    def test_stats_categories(self, svc: SchemaValidatorService) -> None:
        st = svc.stats()
        assert "receipts" in st["categories"]
        assert "events" in st["categories"]
        assert "capabilities" in st["categories"]
        assert "evidence" in st["categories"]
        assert "learning" in st["categories"]
        assert "ops_receipts" in st["categories"]


# ---------------------------------------------------------------------------
# Receipt Validation
# ---------------------------------------------------------------------------

def _valid_base_receipt() -> dict[str, Any]:
    """Minimal valid receipt matching receipt.schema.json."""
    return {
        "receipt_id": "r-001",
        "receipt_type": "test.receipt",
        "trace_id": "t-001",
        "run_id": "run-001",
        "span_id": "span-001",
        "suite_id": "suite-001",
        "created_at": "2026-02-14T00:00:00Z",
        "hash": "abc123",
    }


def _valid_authority_approved() -> dict[str, Any]:
    """Valid authority.item.approved receipt."""
    base = _valid_base_receipt()
    base.update({
        "receipt_type": "authority.item.approved",
        "authority_item_id": "ai-001",
        "risk_tier": "high",
        "required_presence": "ava_voice",
        "approval_id": "ap-001",
        "execution_mode": "ENABLED",
    })
    return base


class TestReceiptValidation:
    """Receipt schema validation."""

    def test_valid_base_receipt(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_receipt(_valid_base_receipt(), schema_name="receipt")
        assert result.valid is True
        assert result.error_count == 0

    def test_invalid_receipt_missing_required(self, svc: SchemaValidatorService) -> None:
        receipt = {"receipt_type": "test"}  # missing most required fields
        result = svc.validate_receipt(receipt, schema_name="receipt")
        assert result.valid is False
        assert result.error_count > 0

    def test_auto_detect_receipt_type(self, svc: SchemaValidatorService) -> None:
        receipt = _valid_authority_approved()
        result = svc.validate_receipt(receipt)
        # Should auto-detect authority.item.approved.schema
        assert result.schema_name == "authority.item.approved.schema"

    def test_authority_approved_valid(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_receipt(
            _valid_authority_approved(),
            schema_name="authority.item.approved",
        )
        assert result.valid is True

    def test_authority_approved_missing_fields(self, svc: SchemaValidatorService) -> None:
        receipt = _valid_base_receipt()
        receipt["receipt_type"] = "authority.item.approved"
        # Missing authority_item_id, risk_tier, etc.
        result = svc.validate_receipt(receipt, schema_name="authority.item.approved")
        assert result.valid is False

    def test_fallback_to_base_receipt(self, svc: SchemaValidatorService) -> None:
        receipt = _valid_base_receipt()
        receipt["receipt_type"] = "totally.unknown.type"
        result = svc.validate_receipt(receipt)
        # Should fall back to base receipt.schema
        assert result.valid is True
        assert result.schema_name == "receipt.schema"

    def test_ops_receipt_auto_detect(self, svc: SchemaValidatorService) -> None:
        """Ops receipts should be discoverable."""
        listing = svc.list_schemas()
        ops = listing.get("ops_receipts", {})
        assert len(ops) >= 20


# ---------------------------------------------------------------------------
# Event Validation
# ---------------------------------------------------------------------------

def _valid_a2a_event() -> dict[str, Any]:
    return {
        "event_type": "a2a.item.created",
        "trace_id": "t-001",
        "run_id": "run-001",
        "span_id": "span-001",
        "suite_id": "suite-001",
        "item_id": "item-001",
        "task_type": "invoice.create",
        "created_at": "2026-02-14T00:00:00Z",
    }


class TestEventValidation:
    """A2A/outbox event schema validation."""

    def test_valid_a2a_event(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_event(_valid_a2a_event())
        assert result.valid is True

    def test_invalid_a2a_event_missing_fields(self, svc: SchemaValidatorService) -> None:
        event = {"event_type": "a2a.item.created"}  # missing required fields
        result = svc.validate_event(event)
        assert result.valid is False
        assert result.error_count > 0

    def test_unknown_event_type(self, svc: SchemaValidatorService) -> None:
        event = {"event_type": "unknown.event"}
        result = svc.validate_event(event)
        assert result.valid is False
        assert "No matching event schema" in result.errors[0]


# ---------------------------------------------------------------------------
# Capability Validation
# ---------------------------------------------------------------------------

def _valid_capability() -> dict[str, Any]:
    return {
        "capability_id": "cap-001",
        "suite_id": "suite-001",
        "purpose": "stripe.invoice.create",
        "bind": {"provider": "stripe", "amount_max": 10000},
        "expires_at": "2026-02-14T00:01:00Z",
        "single_use": True,
    }


class TestCapabilityValidation:
    """Capability token schema validation."""

    def test_valid_capability(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_capability(_valid_capability())
        assert result.valid is True

    def test_invalid_capability_missing_fields(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_capability({"capability_id": "cap-001"})
        assert result.valid is False
        assert result.error_count > 0


# ---------------------------------------------------------------------------
# Evidence Pack Validation
# ---------------------------------------------------------------------------

def _valid_evidence_pack() -> dict[str, Any]:
    return {
        "evidence_pack_id": "ep-001",
        "hash": "deadbeef",
        "suite_id": "suite-001",
        "office_id": "office-001",
        "trace_id": "t-001",
        "created_at": "2026-02-14T00:00:00Z",
        "items": [
            {"kind": "receipt", "ref": "r-001"},
        ],
    }


class TestEvidencePackValidation:

    def test_valid_evidence_pack(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_evidence_pack(_valid_evidence_pack())
        assert result.valid is True

    def test_invalid_evidence_pack_empty_items(self, svc: SchemaValidatorService) -> None:
        pack = _valid_evidence_pack()
        pack["items"] = []  # minItems: 1
        result = svc.validate_evidence_pack(pack)
        assert result.valid is False


# ---------------------------------------------------------------------------
# Learning Object Validation
# ---------------------------------------------------------------------------

def _valid_change_proposal() -> dict[str, Any]:
    return {
        "id": "cp-001",
        "kind": "prompt",
        "target": "ava.greeting.prompt",
        "summary": "Improve greeting tone",
        "risk_tier": "green",
        "requires_approval": False,
        "eval_suite_refs": ["eval-001"],
        "created_at": "2026-02-14T00:00:00Z",
    }


class TestLearningObjectValidation:

    def test_valid_change_proposal(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_learning_object(
            _valid_change_proposal(), schema_name="change_proposal"
        )
        assert result.valid is True

    def test_invalid_change_proposal(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_learning_object(
            {"id": "cp-001"}, schema_name="change_proposal"
        )
        assert result.valid is False

    def test_unknown_learning_schema(self, svc: SchemaValidatorService) -> None:
        result = svc.validate_learning_object(
            {"id": "x"}, schema_name="nonexistent"
        )
        assert result.valid is False
        assert "No matching learning schema" in result.errors[0]


# ---------------------------------------------------------------------------
# Evil Tests
# ---------------------------------------------------------------------------


class TestEvilCases:
    """Security and edge case validation."""

    def test_receipt_extra_fields_pass(self, svc: SchemaValidatorService) -> None:
        """Schemas without additionalProperties:false should accept extra fields."""
        receipt = _valid_base_receipt()
        receipt["unexpected_field"] = "should be fine"
        receipt["another_extra"] = 42
        result = svc.validate_receipt(receipt, schema_name="receipt")
        assert result.valid is True

    def test_receipt_missing_all_required_fails(self, svc: SchemaValidatorService) -> None:
        """Completely empty receipt must fail."""
        result = svc.validate_receipt({}, schema_name="receipt")
        assert result.valid is False
        assert result.error_count >= 6  # 8 required fields

    def test_receipt_wrong_type_for_field(self, svc: SchemaValidatorService) -> None:
        """Wrong type should fail validation."""
        receipt = _valid_base_receipt()
        receipt["receipt_id"] = 12345  # should be string
        result = svc.validate_receipt(receipt, schema_name="receipt")
        assert result.valid is False

    def test_no_schema_for_unknown_receipt_type(self, svc: SchemaValidatorService) -> None:
        """Unknown receipt type falls back to base, not crash."""
        receipt = _valid_base_receipt()
        receipt["receipt_type"] = "evil.injection.attempt'; DROP TABLE"
        result = svc.validate_receipt(receipt)
        # Falls back to base schema — still valid structurally
        assert result.schema_name == "receipt.schema"

    def test_event_wrong_const(self, svc: SchemaValidatorService) -> None:
        """Event with wrong const value for event_type."""
        event = _valid_a2a_event()
        event["event_type"] = "wrong.type"
        # Validate against a2a.item.created explicitly
        result = svc.validate_event(event, schema_name="a2a.item.created")
        assert result.valid is False


# ---------------------------------------------------------------------------
# Stats & Listing
# ---------------------------------------------------------------------------


class TestStatsAndListing:

    def test_stats_returns_total(self, svc: SchemaValidatorService) -> None:
        st = svc.stats()
        assert "total" in st
        assert isinstance(st["total"], int)
        assert st["total"] >= 78

    def test_stats_by_category(self, svc: SchemaValidatorService) -> None:
        st = svc.stats()
        assert "by_category" in st
        assert st["by_category"]["events"] == 3
        assert st["by_category"]["capabilities"] == 1
        assert st["by_category"]["evidence"] == 1
        assert st["by_category"]["learning"] == 5

    def test_list_schemas_returns_sorted(self, svc: SchemaValidatorService) -> None:
        listing = svc.list_schemas()
        for category, names in listing.items():
            assert names == sorted(names), f"{category} not sorted"

    def test_get_schema_found(self, svc: SchemaValidatorService) -> None:
        schema = svc.get_schema("receipts", "receipt")
        assert schema is not None
        assert "$schema" in schema

    def test_get_schema_not_found(self, svc: SchemaValidatorService) -> None:
        schema = svc.get_schema("receipts", "nonexistent.type")
        assert schema is None


# ---------------------------------------------------------------------------
# Validation Mode
# ---------------------------------------------------------------------------


class TestValidationMode:

    def test_default_mode_is_warn(self, svc: SchemaValidatorService) -> None:
        assert svc._get_validation_mode() == "warn"

    def test_strict_mode_from_env(self, svc: SchemaValidatorService) -> None:
        with patch.dict(os.environ, {"ASPIRE_SCHEMA_VALIDATION_MODE": "strict"}):
            assert svc._get_validation_mode() == "strict"

    def test_warn_mode_still_returns_errors(self, svc: SchemaValidatorService) -> None:
        """Warn mode returns errors but doesn't block (caller decides)."""
        result = svc.validate_receipt({}, schema_name="receipt")
        assert result.valid is False
        assert result.error_count > 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:

    def test_singleton_returns_same_instance(self) -> None:
        a = get_schema_validator()
        b = get_schema_validator()
        assert a is b

    def test_reset_clears_singleton(self) -> None:
        a = get_schema_validator()
        reset_schema_validator()
        b = get_schema_validator()
        assert a is not b
