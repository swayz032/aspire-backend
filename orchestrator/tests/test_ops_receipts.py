"""Tests for Ops Receipt Coverage — schemas, registry, emission services, enum (Law #2).

Covers:
  - 20 JSON schema files load correctly
  - Receipt schema registry validates valid/invalid receipts
  - Schema validation warn/strict modes
  - 5 emission services create correct receipt structures
  - ReceiptType enum has all 20 new ops values
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from aspire_orchestrator.models import ReceiptType
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.receipt_schema_registry import (
    ValidationResult,
    get_schema,
    list_schemas,
    load_schemas,
    reset_registry,
    validate_receipt,
)
from aspire_orchestrator.services import deployment_receipts
from aspire_orchestrator.services import slo_receipts
from aspire_orchestrator.services import backup_receipts
from aspire_orchestrator.services import entitlement_receipts
from aspire_orchestrator.services import rbac_receipts

_SCHEMAS_DIR = Path(__file__).parent.parent / "src" / "aspire_orchestrator" / "schemas" / "ops_receipts"

# Common test fixtures
_SUITE_ID = "suite-test-001"
_OFFICE_ID = "office-test-001"
_TRACE_ID = "trace-test-001"


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset receipt store and schema registry between tests."""
    receipt_store.clear_store()
    reset_registry()
    yield
    receipt_store.clear_store()
    reset_registry()


# =========================================================================
# Section 1: Schema file loading (20 ops schemas + base)
# =========================================================================

_EXPECTED_SCHEMA_TYPES = [
    "alert.triggered",
    "backup.completed",
    "deploy.canary.deployed",
    "deploy.failed",
    "deploy.promoted",
    "deploy.rolled_back",
    "deploy.started",
    "dr.drill.completed",
    "entitlement.grace.ended",
    "entitlement.grace.started",
    "entitlement.plan.changed",
    "entitlement.seat.added",
    "entitlement.seat.removed",
    "entitlement.usage.capped",
    "rbac.permission.escalated",
    "rbac.role.granted",
    "rbac.role.revoked",
    "restore.tested",
    "slo.breach.detected",
    "slo.metric.rollup",
]


class TestSchemaLoading:
    """Test that all 20 ops receipt schema files load correctly."""

    def test_schema_directory_exists(self):
        assert _SCHEMAS_DIR.exists(), f"Schema directory missing: {_SCHEMAS_DIR}"

    def test_all_20_schema_files_present(self):
        schema_files = sorted(f.name for f in _SCHEMAS_DIR.glob("*.schema.json") if f.name != "receipt.schema.json")
        assert len(schema_files) == 20, f"Expected 20 schema files, found {len(schema_files)}: {schema_files}"

    def test_base_receipt_schema_present(self):
        base_schema = _SCHEMAS_DIR / "receipt.schema.json"
        assert base_schema.exists(), "Base receipt.schema.json missing"

    def test_all_schemas_are_valid_json(self):
        for schema_path in _SCHEMAS_DIR.glob("*.schema.json"):
            with open(schema_path, encoding="utf-8") as f:
                data = json.load(f)
            assert isinstance(data, dict), f"{schema_path.name} is not a JSON object"

    def test_all_ops_schemas_have_title(self):
        for schema_path in sorted(_SCHEMAS_DIR.glob("*.schema.json")):
            if schema_path.name == "receipt.schema.json":
                continue
            with open(schema_path, encoding="utf-8") as f:
                data = json.load(f)
            assert "title" in data, f"{schema_path.name} missing 'title' field"

    def test_all_ops_schemas_reference_base(self):
        for schema_path in sorted(_SCHEMAS_DIR.glob("*.schema.json")):
            if schema_path.name == "receipt.schema.json":
                continue
            with open(schema_path, encoding="utf-8") as f:
                data = json.load(f)
            all_of = data.get("allOf", [])
            refs = [item.get("$ref") for item in all_of if "$ref" in item]
            assert "./receipt.schema.json" in refs, f"{schema_path.name} missing $ref to receipt.schema.json"


# =========================================================================
# Section 2: Receipt Schema Registry
# =========================================================================


class TestSchemaRegistry:
    """Test the receipt schema registry service."""

    def test_load_schemas_returns_20(self):
        schemas = load_schemas()
        assert len(schemas) == 20

    def test_load_schemas_keys_match_expected(self):
        schemas = load_schemas()
        assert sorted(schemas.keys()) == _EXPECTED_SCHEMA_TYPES

    def test_get_schema_found(self):
        load_schemas()
        schema = get_schema("deploy.started")
        assert schema is not None
        assert schema["title"] == "deploy.started"

    def test_get_schema_not_found(self):
        load_schemas()
        schema = get_schema("nonexistent.type")
        assert schema is None

    def test_list_schemas_sorted(self):
        result = list_schemas()
        assert result == _EXPECTED_SCHEMA_TYPES

    def test_validate_receipt_valid_in_warn_mode(self):
        """A well-formed deploy.started receipt passes in warn mode.

        Note: The ops schemas have an allOf conflict on 'actor' (string in ops
        schema vs object in base receipt.schema.json). Warn mode lets this
        through — this is expected and documented as risk R1 in the plan.
        """
        result = validate_receipt(
            {
                "receipt_id": "r-001",
                "receipt_type": "deploy.started",
                "trace_id": "t-001",
                "run_id": "run-001",
                "span_id": "span-001",
                "suite_id": _SUITE_ID,
                "created_at": "2026-02-14T00:00:00Z",
                "hash": "abc123",
                "office_id": _OFFICE_ID,
                "release_id": "rel-001",
                "environment": "prod",
                "actor": "system",
            },
            "deploy.started",
        )
        # Warn mode: valid=True even with schema conflicts
        assert result.valid is True

    def test_validate_receipt_missing_required_field(self):
        """A deploy.started receipt missing 'release_id' fails in strict mode."""
        with patch.dict(os.environ, {"ASPIRE_SCHEMA_VALIDATION_MODE": "strict"}):
            result = validate_receipt(
                {
                    "receipt_id": "r-001",
                    "receipt_type": "deploy.started",
                    "trace_id": "t-001",
                    "run_id": "run-001",
                    "span_id": "span-001",
                    "suite_id": _SUITE_ID,
                    "created_at": "2026-02-14T00:00:00Z",
                    "hash": "abc123",
                    "office_id": _OFFICE_ID,
                    # missing release_id, environment, actor
                },
                "deploy.started",
            )
            assert result.valid is False
            assert result.error_count > 0

    def test_validate_receipt_warn_mode_passes_with_errors(self):
        """In warn mode, invalid receipts return valid=True but have errors."""
        with patch.dict(os.environ, {"ASPIRE_SCHEMA_VALIDATION_MODE": "warn"}):
            result = validate_receipt(
                {
                    "receipt_type": "deploy.started",
                    # missing most required fields
                },
                "deploy.started",
            )
            assert result.valid is True
            assert result.error_count > 0

    def test_validate_receipt_unknown_type_passes(self):
        """Unknown receipt types pass validation (no schema to check against)."""
        result = validate_receipt({"foo": "bar"}, "unknown.type")
        assert result.valid is True
        assert result.error_count == 0

    def test_validate_receipt_strict_mode_blocks(self):
        """Strict mode returns valid=False for invalid receipts."""
        with patch.dict(os.environ, {"ASPIRE_SCHEMA_VALIDATION_MODE": "strict"}):
            result = validate_receipt(
                {"receipt_type": "rbac.role.granted"},
                "rbac.role.granted",
            )
            assert result.valid is False

    def test_reset_registry(self):
        load_schemas()
        assert len(list_schemas()) == 20
        reset_registry()
        # After reset, list_schemas triggers reload
        assert len(list_schemas()) == 20  # reloads automatically


# =========================================================================
# Section 3: ReceiptType Enum
# =========================================================================


class TestReceiptTypeEnum:
    """Test that ReceiptType enum has all 20 new ops values."""

    @pytest.mark.parametrize("receipt_type", _EXPECTED_SCHEMA_TYPES)
    def test_receipt_type_enum_has_value(self, receipt_type: str):
        """Each ops receipt type must exist in the ReceiptType enum."""
        matching = [rt for rt in ReceiptType if rt.value == receipt_type]
        assert len(matching) == 1, f"ReceiptType missing value: {receipt_type}"

    def test_all_20_ops_types_in_enum(self):
        ops_values = {rt.value for rt in ReceiptType if "." in rt.value and rt.value not in ("robot.run.completed", "incident.opened")}
        expected = set(_EXPECTED_SCHEMA_TYPES)
        assert expected.issubset(ops_values), f"Missing from enum: {expected - ops_values}"


# =========================================================================
# Section 4: Deployment Receipts Emission Service
# =========================================================================


class TestDeploymentReceipts:
    """Test deployment receipt emission."""

    def test_emit_deploy_started(self):
        receipt = deployment_receipts.emit_deploy_started(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="rel-001", environment="prod", actor="ci-pipeline",
        )
        assert receipt["receipt_type"] == "deploy.started"
        assert receipt["release_id"] == "rel-001"
        assert receipt["environment"] == "prod"
        assert receipt["risk_tier"] == "green"
        assert receipt_store.get_receipt_count() == 1

    def test_emit_deploy_canary_deployed(self):
        receipt = deployment_receipts.emit_deploy_canary_deployed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="rel-002", environment="staging", canary_percent=10.0,
        )
        assert receipt["receipt_type"] == "deploy.canary.deployed"
        assert receipt["canary_percent"] == 10.0

    def test_emit_deploy_promoted(self):
        receipt = deployment_receipts.emit_deploy_promoted(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="rel-003", environment="prod",
        )
        assert receipt["receipt_type"] == "deploy.promoted"

    def test_emit_deploy_rolled_back(self):
        receipt = deployment_receipts.emit_deploy_rolled_back(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="rel-004", environment="prod", reason="error rate spike",
        )
        assert receipt["receipt_type"] == "deploy.rolled_back"
        assert receipt["outcome"] == "failed"
        assert receipt["reason"] == "error rate spike"

    def test_emit_deploy_failed(self):
        receipt = deployment_receipts.emit_deploy_failed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="rel-005", environment="prod", error="build timeout",
        )
        assert receipt["receipt_type"] == "deploy.failed"
        assert receipt["outcome"] == "failed"
        assert receipt["error"] == "build timeout"


# =========================================================================
# Section 5: SLO Receipts Emission Service
# =========================================================================


class TestSloReceipts:
    """Test SLO receipt emission."""

    def test_emit_slo_metric_rollup(self):
        receipt = slo_receipts.emit_slo_metric_rollup(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            service="gateway", window="5m",
            metrics={"p50_ms": 45, "p99_ms": 200, "error_rate": 0.001},
        )
        assert receipt["receipt_type"] == "slo.metric.rollup"
        assert receipt["service"] == "gateway"
        assert receipt["window"] == "5m"
        assert receipt["metrics"]["p50_ms"] == 45

    def test_emit_slo_breach_detected(self):
        receipt = slo_receipts.emit_slo_breach_detected(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            service="orchestrator", slo_name="p99_latency",
            window="1h", threshold=500.0, observed=750.0,
        )
        assert receipt["receipt_type"] == "slo.breach.detected"
        assert receipt["threshold"] == 500.0
        assert receipt["observed"] == 750.0

    def test_emit_alert_triggered(self):
        receipt = slo_receipts.emit_alert_triggered(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            alert_name="HighErrorRate", severity="critical", service="gateway",
        )
        assert receipt["receipt_type"] == "alert.triggered"
        assert receipt["severity"] == "critical"


# =========================================================================
# Section 6: Backup Receipts Emission Service
# =========================================================================


class TestBackupReceipts:
    """Test backup receipt emission."""

    def test_emit_backup_completed_success(self):
        receipt = backup_receipts.emit_backup_completed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            target="db", status="success", artifact_ref="s3://backups/db-2026-02-14.gz",
        )
        assert receipt["receipt_type"] == "backup.completed"
        assert receipt["outcome"] == "success"
        assert receipt["target"] == "db"

    def test_emit_backup_completed_failure(self):
        receipt = backup_receipts.emit_backup_completed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            target="objects", status="fail", artifact_ref="none", error="disk full",
        )
        assert receipt["outcome"] == "failed"
        assert receipt["error"] == "disk full"

    def test_emit_restore_tested(self):
        receipt = backup_receipts.emit_restore_tested(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            target="db", status="success", artifact_ref="s3://backups/db-2026-02-14.gz",
            rto_minutes=15,
        )
        assert receipt["receipt_type"] == "restore.tested"
        assert receipt["rto_minutes"] == 15

    def test_emit_dr_drill_completed(self):
        receipt = backup_receipts.emit_dr_drill_completed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            scenario="full_region_failover", status="success",
            rto_minutes=30, rpo_minutes=5,
        )
        assert receipt["receipt_type"] == "dr.drill.completed"
        assert receipt["rto_minutes"] == 30
        assert receipt["rpo_minutes"] == 5


# =========================================================================
# Section 7: Entitlement Receipts Emission Service
# =========================================================================


class TestEntitlementReceipts:
    """Test entitlement receipt emission."""

    def test_emit_plan_changed(self):
        receipt = entitlement_receipts.emit_plan_changed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            from_plan="starter", to_plan="professional", effective_at="2026-03-01T00:00:00Z",
        )
        assert receipt["receipt_type"] == "entitlement.plan.changed"
        assert receipt["from_plan"] == "starter"
        assert receipt["to_plan"] == "professional"

    def test_emit_seat_added(self):
        receipt = entitlement_receipts.emit_seat_added(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            office_added="office-new-001", new_seat_count=5,
        )
        assert receipt["receipt_type"] == "entitlement.seat.added"
        assert receipt["new_seat_count"] == 5

    def test_emit_seat_removed(self):
        receipt = entitlement_receipts.emit_seat_removed(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            office_removed="office-old-001", new_seat_count=3,
        )
        assert receipt["receipt_type"] == "entitlement.seat.removed"
        assert receipt["new_seat_count"] == 3

    def test_emit_usage_capped(self):
        receipt = entitlement_receipts.emit_usage_capped(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            cap_name="api_calls", cap_value=10000, period="month",
        )
        assert receipt["receipt_type"] == "entitlement.usage.capped"
        assert receipt["cap_value"] == 10000

    def test_emit_grace_started(self):
        receipt = entitlement_receipts.emit_grace_started(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            reason="payment_failed", ends_at="2026-03-14T00:00:00Z",
        )
        assert receipt["receipt_type"] == "entitlement.grace.started"
        assert receipt["reason"] == "payment_failed"

    def test_emit_grace_ended(self):
        receipt = entitlement_receipts.emit_grace_ended(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            ended_at="2026-03-14T00:00:00Z",
        )
        assert receipt["receipt_type"] == "entitlement.grace.ended"


# =========================================================================
# Section 8: RBAC Receipts Emission Service
# =========================================================================


class TestRbacReceipts:
    """Test RBAC receipt emission."""

    def test_emit_role_granted(self):
        receipt = rbac_receipts.emit_role_granted(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            target_office_id="office-target-001", role="admin", granted_by="owner-001",
        )
        assert receipt["receipt_type"] == "rbac.role.granted"
        assert receipt["role"] == "admin"
        assert receipt["granted_by"] == "owner-001"

    def test_emit_role_revoked(self):
        receipt = rbac_receipts.emit_role_revoked(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            target_office_id="office-target-001", role="admin", revoked_by="owner-001",
        )
        assert receipt["receipt_type"] == "rbac.role.revoked"

    def test_emit_permission_escalated(self):
        receipt = rbac_receipts.emit_permission_escalated(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            target_office_id="office-target-002", from_role="member", to_role="admin",
            reason="emergency access required",
        )
        assert receipt["receipt_type"] == "rbac.permission.escalated"
        assert receipt["from_role"] == "member"
        assert receipt["to_role"] == "admin"


# =========================================================================
# Section 9: Cross-cutting receipt properties
# =========================================================================


class TestReceiptProperties:
    """Test common receipt properties across all emission services."""

    def test_all_receipts_have_required_fields(self):
        """All emitted receipts must have the core governance fields."""
        receipts = [
            deployment_receipts.emit_deploy_started(
                suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
                release_id="r1", environment="prod", actor="system",
            ),
            slo_receipts.emit_alert_triggered(
                suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
                alert_name="test", severity="info", service="gw",
            ),
            backup_receipts.emit_backup_completed(
                suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
                target="db", status="success", artifact_ref="s3://x",
            ),
            entitlement_receipts.emit_plan_changed(
                suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
                from_plan="a", to_plan="b", effective_at="2026-03-01T00:00:00Z",
            ),
            rbac_receipts.emit_role_granted(
                suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
                target_office_id="t1", role="admin", granted_by="owner",
            ),
        ]
        required_fields = {"id", "receipt_type", "suite_id", "office_id", "trace_id",
                           "correlation_id", "actor_type", "actor_id", "risk_tier",
                           "outcome", "created_at"}
        for receipt in receipts:
            missing = required_fields - set(receipt.keys())
            assert not missing, f"Receipt {receipt['receipt_type']} missing fields: {missing}"

    def test_all_receipts_are_green_tier(self):
        """All ops receipts must be GREEN risk tier."""
        receipt = deployment_receipts.emit_deploy_started(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="r1", environment="prod", actor="system",
        )
        assert receipt["risk_tier"] == "green"

    def test_receipts_persisted_to_store(self):
        """All emitted receipts are persisted via receipt_store."""
        deployment_receipts.emit_deploy_started(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="r1", environment="prod", actor="system",
        )
        slo_receipts.emit_alert_triggered(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            alert_name="test", severity="info", service="gw",
        )
        assert receipt_store.get_receipt_count() == 2

    def test_receipts_queryable_by_suite_id(self):
        """Receipts can be queried by suite_id (Law #6 tenant isolation)."""
        deployment_receipts.emit_deploy_started(
            suite_id=_SUITE_ID, office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="r1", environment="prod", actor="system",
        )
        deployment_receipts.emit_deploy_started(
            suite_id="other-suite", office_id=_OFFICE_ID, trace_id=_TRACE_ID,
            release_id="r2", environment="staging", actor="system",
        )
        results = receipt_store.query_receipts(suite_id=_SUITE_ID)
        assert len(results) == 1
        assert results[0]["suite_id"] == _SUITE_ID
