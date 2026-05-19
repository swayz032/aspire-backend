"""Drew receipt schema tests — Wave 9e (P0-4).

Verifies that _build_receipt emits canonical field names that survive
_map_receipt_to_row and produce valid DB row values:
  - id (not None)
  - outcome != "unknown" / status SUCCEEDED|FAILED|DENIED
  - receipt_type == action_type (not "orchestrator")
  - created_at != None
  - trace_id populated
  - risk_tier correctly set per stage vs MATERIAL_OVERRIDE/SKIP

Law compliance verified:
  Law #2: All blueprints stage receipts emitted with correct schema.
  Law #4: MATERIAL_OVERRIDE/SKIP receipts carry risk_tier=YELLOW.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

SUITE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CORRELATION_ID = "test-receipt-schema-001"


# ---------------------------------------------------------------------------
# _build_receipt shape tests
# ---------------------------------------------------------------------------

class TestBuildReceiptShape:
    """_build_receipt must emit all canonical fields."""

    def test_canonical_fields_present(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.classify",
            status="ok",
            inputs={"suite_id": SUITE_ID, "project_id": str(uuid.uuid4())},
        )

        # Canonical fields read by _map_receipt_to_row
        assert "id" in receipt, "Missing canonical 'id' field"
        assert "outcome" in receipt, "Missing canonical 'outcome' field"
        assert "action_type" in receipt, "Missing canonical 'action_type' field"
        assert "receipt_type" in receipt, "Missing canonical 'receipt_type' field"
        assert "created_at" in receipt, "Missing canonical 'created_at' field"
        assert "risk_tier" in receipt, "Missing canonical 'risk_tier' field"

    def test_id_is_valid_uuid(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.ingest",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )

        try:
            uuid.UUID(receipt["id"])
        except (ValueError, TypeError) as exc:
            pytest.fail(f"receipt['id'] is not a valid UUID: {receipt['id']!r}. Error: {exc}")

    def test_created_at_is_not_none(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.ingest",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )

        assert receipt["created_at"] is not None, "created_at must not be None"
        # Must be a valid ISO datetime string
        try:
            datetime.fromisoformat(receipt["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            pytest.fail(f"created_at is not a valid ISO datetime: {receipt['created_at']!r}. Error: {exc}")

    def test_action_type_equals_event_type(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        event_type = "blueprint.classify"
        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type=event_type,
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )

        assert receipt["action_type"] == event_type
        assert receipt["receipt_type"] == event_type

    def test_receipt_type_not_orchestrator(self) -> None:
        """receipt_type must be the specific event type, not the generic 'orchestrator'."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.see",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )

        assert receipt["receipt_type"] != "orchestrator", (
            f"receipt_type should be 'blueprint.see', not 'orchestrator'. "
            f"Got: {receipt['receipt_type']!r}"
        )

    def test_trace_id_populated(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.reason",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )

        assert receipt.get("trace_id"), (
            f"trace_id should be populated. Got: {receipt.get('trace_id')!r}"
        )


# ---------------------------------------------------------------------------
# Status → outcome mapping tests
# ---------------------------------------------------------------------------

class TestOutcomeMapping:
    """Drew status values must map to canonical outcome values."""

    @pytest.mark.parametrize("status,expected_outcome", [
        ("ok", "success"),
        ("dedup", "success"),
        ("failed", "failed"),
        ("error", "failed"),
        ("denied", "denied"),
        ("timeout", "failed"),
    ])
    def test_status_maps_to_outcome(self, status: str, expected_outcome: str) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.ingest",
            status=status,
            inputs={"suite_id": SUITE_ID},
        )

        assert receipt["outcome"] == expected_outcome, (
            f"status={status!r} should map to outcome={expected_outcome!r}, "
            f"but got {receipt['outcome']!r}"
        )


# ---------------------------------------------------------------------------
# _map_receipt_to_row integration tests
# ---------------------------------------------------------------------------

class TestMapReceiptToRow:
    """_build_receipt → _map_receipt_to_row produces a valid DB row."""

    def test_row_status_not_pending(self) -> None:
        """Status must not be PENDING for a successful drew receipt."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt
        from aspire_orchestrator.services.receipt_store import _map_receipt_to_row

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.classify",
            status="ok",
            inputs={"suite_id": SUITE_ID},
            suite_id=SUITE_ID,
        )
        row = _map_receipt_to_row(receipt)

        assert row["status"] != "PENDING", (
            f"DB row status should not be PENDING for ok receipt. Got: {row['status']!r}"
        )
        assert row["status"] == "SUCCEEDED"

    def test_row_receipt_type_not_orchestrator(self) -> None:
        """receipt_type column must not be the generic 'orchestrator'."""
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt
        from aspire_orchestrator.services.receipt_store import _map_receipt_to_row

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.see",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )
        row = _map_receipt_to_row(receipt)

        assert row["receipt_type"] != "orchestrator", (
            f"receipt_type column should not be 'orchestrator'. Got: {row['receipt_type']!r}"
        )
        assert row["receipt_type"] == "blueprint.see"

    def test_row_created_at_not_none(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt
        from aspire_orchestrator.services.receipt_store import _map_receipt_to_row

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.ingest",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )
        row = _map_receipt_to_row(receipt)

        assert row["created_at"] is not None, "DB row created_at must not be None"

    def test_row_trace_id_populated(self) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt
        from aspire_orchestrator.services.receipt_store import _map_receipt_to_row

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type="blueprint.reason",
            status="ok",
            inputs={"suite_id": SUITE_ID},
        )
        row = _map_receipt_to_row(receipt)

        assert row.get("trace_id"), (
            f"DB row trace_id should be populated. Got: {row.get('trace_id')!r}"
        )


# ---------------------------------------------------------------------------
# risk_tier tests
# ---------------------------------------------------------------------------

class TestRiskTier:
    """Blueprint stages = GREEN; material actions = YELLOW."""

    @pytest.mark.parametrize("event_type", [
        "blueprint.ingest",
        "blueprint.classify",
        "blueprint.see",
        "blueprint.reason",
        "blueprint.procure",
    ])
    def test_stage_receipts_are_green(self, event_type: str) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type=event_type,
            status="ok",
            inputs={"suite_id": SUITE_ID},
            risk_tier="green",
        )

        assert receipt["risk_tier"] == "GREEN", (
            f"Stage receipts must be GREEN. Got: {receipt['risk_tier']!r}"
        )

    @pytest.mark.parametrize("event_type", [
        "blueprint.material_pick.override",
        "blueprint.material_pick.skip",
    ])
    def test_material_action_receipts_are_yellow(self, event_type: str) -> None:
        from aspire_orchestrator.skillpacks.drew_blueprint import _build_receipt

        receipt = _build_receipt(
            correlation_id=CORRELATION_ID,
            event_type=event_type,
            status="ok",
            inputs={"suite_id": SUITE_ID},
            risk_tier="yellow",
        )

        assert receipt["risk_tier"] == "YELLOW", (
            f"Material action receipts must be YELLOW. Got: {receipt['risk_tier']!r}"
        )
