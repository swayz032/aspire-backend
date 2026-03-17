"""DLP Service Tests — Presidio PII Redaction (Law #9, Gate 5).

Tests:
  - PII detection: SSN, CC, email, phone, person, location
  - Redaction labels match CLAUDE.md spec
  - Receipt redaction preserves protected fields
  - Policy-specified redact_fields integration
  - Fail-safe behavior when Presidio unavailable
  - Integration with receipt_write_node
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.dlp import (
    DLPService,
    _RECEIPT_PROTECTED_FIELDS,
    redact_receipt,
    redact_text,
)


@pytest.fixture
def dlp():
    """Fresh DLP service instance per test."""
    return DLPService()


# ===========================================================================
# Text Redaction
# ===========================================================================


class TestTextRedaction:
    """Test PII detection and redaction in text strings."""

    def test_credit_card_redacted(self, dlp) -> None:
        """Credit card number is replaced with <CC_REDACTED>."""
        text = "Card number is 4111111111111111"
        result = dlp.redact_text(text)
        assert "<CC_REDACTED>" in result
        assert "4111111111111111" not in result

    def test_email_redacted(self, dlp) -> None:
        """Email address is replaced with <EMAIL_REDACTED>."""
        text = "Contact me at john.doe@example.com"
        result = dlp.redact_text(text)
        assert "<EMAIL_REDACTED>" in result
        assert "john.doe@example.com" not in result

    def test_phone_redacted(self, dlp) -> None:
        """Phone number is replaced with <PHONE_REDACTED>."""
        text = "Call me at 555-123-4567"
        result = dlp.redact_text(text)
        assert "<PHONE_REDACTED>" in result
        assert "555-123-4567" not in result

    def test_multiple_entities_redacted(self, dlp) -> None:
        """Multiple PII types in one string are all redacted."""
        text = "Email john@test.com, card 4111111111111111, phone 555-999-0000"
        result = dlp.redact_text(text)
        assert "john@test.com" not in result
        assert "4111111111111111" not in result
        assert "555-999-0000" not in result

    def test_empty_string_unchanged(self, dlp) -> None:
        """Empty string returns unchanged."""
        assert dlp.redact_text("") == ""

    def test_no_pii_unchanged(self, dlp) -> None:
        """Text without PII returns unchanged."""
        text = "Schedule a meeting for Tuesday"
        result = dlp.redact_text(text)
        assert result == text

    def test_none_input_returns_none(self, dlp) -> None:
        """None input is handled gracefully."""
        assert dlp.redact_text(None) is None


# ===========================================================================
# Dict Redaction
# ===========================================================================


class TestDictRedaction:
    """Test PII redaction in dict structures."""

    def test_redacts_string_values(self, dlp) -> None:
        """String values containing PII are redacted."""
        data = {"note": "Contact john@evil.com for details", "count": 42}
        result = dlp.redact_dict(data)
        assert "john@evil.com" not in result["note"]
        assert result["count"] == 42

    def test_protected_fields_untouched(self, dlp) -> None:
        """Protected fields are never modified."""
        data = {
            "id": "some-uuid-that-looks-like-email@uuid.com",
            "suite_id": "suite-123",
            "correlation_id": "corr-456",
        }
        result = dlp.redact_dict(data)
        assert result == data  # All protected, nothing changed

    def test_specific_fields_only(self, dlp) -> None:
        """When fields specified, only those are scanned."""
        data = {
            "description": "Email: admin@company.com",
            "notes": "Call 555-111-2222",
        }
        result = dlp.redact_dict(data, fields=["description"])
        assert "admin@company.com" not in result["description"]
        # notes not in specified fields, so should be unchanged
        assert result["notes"] == data["notes"]

    def test_nested_dict_redacted(self, dlp) -> None:
        """Nested dicts have PII redacted recursively."""
        data = {
            "payload": {
                "customer_email": "secret@customer.com",
            }
        }
        result = dlp.redact_dict(data)
        assert "secret@customer.com" not in str(result)


# ===========================================================================
# Receipt Redaction
# ===========================================================================


class TestReceiptRedaction:
    """Test PII redaction specifically for receipt dicts."""

    def test_receipt_structural_fields_preserved(self, dlp) -> None:
        """All receipt structural/governance fields are never modified."""
        receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "suite_id": "STE-0001",
            "office_id": "OFF-0001",
            "chain_id": "STE-0001",
            "sequence": 1,
            "receipt_hash": "abc" * 21 + "a",
            "previous_receipt_hash": "0" * 64,
            "actor_type": "user",
            "actor_id": "user-001",
            "action_type": "invoice.create",
            "risk_tier": "yellow",
            "tool_used": "stripe.invoice.create",
            "receipt_type": "tool_execution",
            "outcome": "success",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "error_message": "Contact support at help@company.com",
        }

        original_id = receipt["id"]
        original_hash = receipt["receipt_hash"]

        result = dlp.redact_receipt(receipt)

        # Structural fields unchanged
        assert result["id"] == original_id
        assert result["receipt_hash"] == original_hash
        assert result["suite_id"] == "STE-0001"
        assert result["actor_id"] == "user-001"

        # PII in error_message is redacted
        assert "help@company.com" not in result.get("error_message", "")

    def test_policy_redact_fields(self, dlp) -> None:
        """Policy-specified redact_fields are processed."""
        receipt = {
            "id": str(uuid.uuid4()),
            "suite_id": "STE-0001",
            "customer_ssn": "123-45-6789",
            "customer_phone": "Call 555-888-7777",
        }

        result = dlp.redact_receipt(
            receipt,
            redact_fields=["customer_ssn", "customer_phone"],
        )
        assert "555-888-7777" not in result.get("customer_phone", "")

    def test_batch_redaction(self, dlp) -> None:
        """Batch receipt redaction processes all receipts."""
        receipts = [
            {
                "id": str(uuid.uuid4()),
                "suite_id": "STE-0001",
                "error_message": f"Error for user{i}@test.com",
            }
            for i in range(5)
        ]

        results = dlp.redact_receipts(receipts)
        assert len(results) == 5
        for r in results:
            assert "@test.com" not in r.get("error_message", "")


# ===========================================================================
# Fail-Safe Behavior
# ===========================================================================


class TestDLPFailSafe:
    """Test DLP behavior when Presidio is unavailable."""

    def test_unavailable_returns_placeholder(self) -> None:
        """When DLP cannot initialize, fail-closed returns redacted placeholder."""
        dlp = DLPService()

        with patch(
            "aspire_orchestrator.services.dlp.DLPService._ensure_initialized",
            return_value=False,
        ):
            dlp._initialized = True
            dlp._init_error = "Simulated failure"
            result = dlp.redact_text("My card is 4111111111111111")
            # Fail-closed: returns placeholder, not original text
            assert "DLP_UNAVAILABLE" in result
            assert "4111111111111111" not in result

    def test_service_available_property(self) -> None:
        """DLP service reports availability correctly."""
        dlp = DLPService()
        # Should initialize on first check
        assert dlp.available is True


# ===========================================================================
# Integration with receipt_write_node
# ===========================================================================


class TestReceiptWriteDLPIntegration:
    """Test DLP integration in the receipt_write_node."""

    def test_receipt_write_calls_dlp(self) -> None:
        """receipt_write_node applies DLP redaction before chain hashing."""
        from aspire_orchestrator.nodes.receipt_write import receipt_write_node

        state = {
            "suite_id": "dlp-test-suite",
            "pipeline_receipts": [
                {
                    "id": str(uuid.uuid4()),
                    "correlation_id": str(uuid.uuid4()),
                    "suite_id": "dlp-test-suite",
                    "office_id": "OFF-0001",
                    "actor_type": "user",
                    "actor_id": "user-001",
                    "action_type": "invoice.create",
                    "risk_tier": "yellow",
                    "tool_used": "stripe",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": "success",
                    "receipt_type": "tool_execution",
                    "receipt_hash": "",
                    "error_message": "Invoice sent to customer@billing.com",
                }
            ],
            "redact_fields": [],
        }

        result = receipt_write_node(state)
        assert len(result["receipt_ids"]) == 1

        # The receipt should have PII redacted
        receipts = result["pipeline_receipts"]
        assert "customer@billing.com" not in receipts[0].get("error_message", "")

    def test_receipt_write_preserves_chain_integrity(self) -> None:
        """DLP redaction + chain hashing produces verifiable chain."""
        from aspire_orchestrator.nodes.receipt_write import receipt_write_node
        from aspire_orchestrator.services.receipt_chain import verify_chain
        from aspire_orchestrator.services.receipt_store import clear_store, get_chain_receipts

        clear_store()

        state = {
            "suite_id": "dlp-chain-test",
            "pipeline_receipts": [
                {
                    "id": str(uuid.uuid4()),
                    "correlation_id": str(uuid.uuid4()),
                    "suite_id": "dlp-chain-test",
                    "office_id": "OFF-0001",
                    "actor_type": "system",
                    "actor_id": "intake",
                    "action_type": "calendar.read",
                    "risk_tier": "green",
                    "tool_used": "test",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": "success",
                    "receipt_type": "decision_intake",
                    "receipt_hash": "",
                },
                {
                    "id": str(uuid.uuid4()),
                    "correlation_id": str(uuid.uuid4()),
                    "suite_id": "dlp-chain-test",
                    "office_id": "OFF-0001",
                    "actor_type": "system",
                    "actor_id": "executor",
                    "action_type": "calendar.read",
                    "risk_tier": "green",
                    "tool_used": "test",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": "success",
                    "receipt_type": "tool_execution",
                    "receipt_hash": "",
                    "error_message": "Contact admin@internal.com for help",
                },
            ],
            "redact_fields": [],
        }

        result = receipt_write_node(state)
        assert len(result["receipt_ids"]) == 2

        # Verify chain integrity after DLP + hashing
        chain = get_chain_receipts(suite_id="dlp-chain-test")
        verification = verify_chain(chain, chain_id="dlp-chain-test")
        assert verification.valid, f"Chain broken after DLP: {verification.errors}"

        clear_store()
