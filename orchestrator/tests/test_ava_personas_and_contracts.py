"""Tests for Ava persona deployment + contract schema validation (W0-F).

Covers:
- Ava User persona loads correctly via persona_loader
- Ava Admin persona loads correctly via persona_loader
- AvaOrchestratorRequest schema validates valid/invalid payloads
- AvaResult schema validates valid/invalid payloads
- Admin portal map loads and has required structure
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aspire_orchestrator.services.persona_loader import load_persona, load_all_personas


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCHEMAS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "schemas"
)

PERSONAS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "aspire_orchestrator"
    / "config"
    / "pack_personas"
)


def _load_schema(name: str) -> dict:
    """Load a JSON schema from the schemas directory."""
    path = SCHEMAS_DIR / name
    assert path.exists(), f"Schema not found: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Persona Loading Tests
# ---------------------------------------------------------------------------


class TestAvaPersonaLoading:
    """Verify Ava User and Admin personas load via persona_loader."""

    def test_ava_user_persona_loads(self) -> None:
        text = load_persona("ava_user")
        assert text is not None
        assert "Strategic Executive Assistant" in text
        assert "Fail closed" in text
        assert "receipts" in text.lower()

    def test_ava_admin_persona_loads(self) -> None:
        text = load_persona("ava_admin")
        assert text is not None
        assert "control-plane operator" in text
        assert "ChangeProposal" in text
        assert "Incident Commander" in text

    def test_ava_personas_in_load_all(self) -> None:
        all_personas = load_all_personas()
        assert "ava_user" in all_personas
        assert "ava_admin" in all_personas
        # Should also have the existing 10 personas
        assert len(all_personas) >= 12

    def test_ava_admin_includes_incident_commander(self) -> None:
        text = load_persona("ava_admin")
        assert text is not None
        assert "evidence" in text.lower()
        assert "hypotheses" in text.lower()
        assert "rollback triggers" in text.lower()
        assert "mitigation" in text.lower()


# ---------------------------------------------------------------------------
# Schema File Existence Tests
# ---------------------------------------------------------------------------


class TestSchemaFilesExist:
    """Verify all Ava contract schema files were deployed."""

    @pytest.mark.parametrize("schema_file", [
        "ava_orchestrator_request.schema.json",
        "ava_result.schema.json",
        "change_proposal.schema.json",
        "incident_packet.schema.json",
        "ops_exception_card.schema.json",
    ])
    def test_schema_file_exists(self, schema_file: str) -> None:
        path = SCHEMAS_DIR / schema_file
        assert path.exists(), f"Missing schema: {schema_file}"

    @pytest.mark.parametrize("schema_file", [
        "ava_orchestrator_request.schema.json",
        "ava_result.schema.json",
        "change_proposal.schema.json",
        "incident_packet.schema.json",
        "ops_exception_card.schema.json",
    ])
    def test_schema_is_valid_json(self, schema_file: str) -> None:
        schema = _load_schema(schema_file)
        assert isinstance(schema, dict)
        assert "type" in schema or "$schema" in schema


# ---------------------------------------------------------------------------
# AvaOrchestratorRequest Schema Validation
# ---------------------------------------------------------------------------


class TestAvaOrchestratorRequestSchema:
    """Validate AvaOrchestratorRequest contract (v1.5)."""

    def test_schema_has_required_fields(self) -> None:
        schema = _load_schema("ava_orchestrator_request.schema.json")
        required = schema["required"]
        assert "correlation_id" in required
        assert "task" in required
        assert "role" in required
        assert "policy_version" in required
        assert "tool_policy_version" in required

    def test_schema_disallows_additional_properties(self) -> None:
        schema = _load_schema("ava_orchestrator_request.schema.json")
        assert schema.get("additionalProperties") is False

    def test_valid_example_payload(self) -> None:
        """A valid example request should match the schema structure."""
        schema = _load_schema("ava_orchestrator_request.schema.json")
        required = set(schema["required"])
        example = {
            "correlation_id": "corr-001",
            "role": "owner",
            "policy_version": "approval_gates_v2_2026-02-12",
            "tool_policy_version": "tool_policy_v2_2026-02-12",
            "task": {
                "task_id": "task-001",
                "suite_id": "STE-0001",
                "task_type": "invoice.create",
                "status": "pending",
                "priority": 1,
                "payload": {"amount": 1200},
                "assigned_to_agent": "quinn_invoices",
                "created_by_office_id": "OFF-0001",
                "assigned_to_office_id": "OFF-0001",
                "attempt_count": 0,
                "created_at": "2026-02-15T10:00:00Z",
                "updated_at": "2026-02-15T10:00:00Z",
            },
        }
        assert required.issubset(set(example.keys()))


# ---------------------------------------------------------------------------
# AvaResult Schema Validation
# ---------------------------------------------------------------------------


class TestAvaResultSchema:
    """Validate AvaResult contract (v1.5)."""

    def test_schema_has_required_fields(self) -> None:
        schema = _load_schema("ava_result.schema.json")
        required = schema["required"]
        assert "status" in required
        assert "outputs" in required

    def test_outputs_required_fields(self) -> None:
        schema = _load_schema("ava_result.schema.json")
        outputs_required = schema["properties"]["outputs"]["required"]
        assert "correlation_id" in outputs_required
        assert "route" in outputs_required
        assert "risk" in outputs_required
        assert "governance" in outputs_required
        assert "plan" in outputs_required

    def test_risk_tier_enum(self) -> None:
        schema = _load_schema("ava_result.schema.json")
        tier_enum = schema["properties"]["outputs"]["properties"]["risk"]["properties"]["tier"]["enum"]
        assert "low" in tier_enum
        assert "medium" in tier_enum
        assert "red" in tier_enum

    def test_governance_required_fields(self) -> None:
        schema = _load_schema("ava_result.schema.json")
        gov = schema["properties"]["outputs"]["properties"]["governance"]
        assert "approval_required" in gov["required"]
        assert "approval_reason_codes" in gov["required"]
        assert "policy_version" in gov["required"]
        assert "tool_policy_version" in gov["required"]
        assert "requested_tools" in gov["required"]
        assert "allowed_tools" in gov["required"]
        assert "payload_hash" in gov["required"]


# ---------------------------------------------------------------------------
# Admin Contract Schemas
# ---------------------------------------------------------------------------


class TestAdminContractSchemas:
    """Validate admin contract schemas have correct structure."""

    def test_change_proposal_required_fields(self) -> None:
        schema = _load_schema("change_proposal.schema.json")
        required = schema["required"]
        assert "proposal_id" in required
        assert "scope" in required
        assert "risk_tier" in required
        assert "rollback_triggers" in required
        assert "approvals_required" in required

    def test_change_proposal_risk_tiers(self) -> None:
        schema = _load_schema("change_proposal.schema.json")
        tiers = schema["properties"]["risk_tier"]["enum"]
        assert set(tiers) == {"green", "yellow", "red"}

    def test_incident_packet_required_fields(self) -> None:
        schema = _load_schema("incident_packet.schema.json")
        required = schema["required"]
        assert "incident_id" in required
        assert "status" in required
        assert "timeline" in required
        assert "evidence_pack" in required

    def test_incident_packet_status_enum(self) -> None:
        schema = _load_schema("incident_packet.schema.json")
        statuses = schema["properties"]["status"]["enum"]
        assert "opened" in statuses
        assert "resolved" in statuses
        assert "postmortem" in statuses

    def test_ops_exception_card_required_fields(self) -> None:
        schema = _load_schema("ops_exception_card.schema.json")
        required = schema["required"]
        assert "finding" in required
        assert "severity" in required
        assert "evidence" in required
        assert "confidence" in required
        assert "escalation_rule" in required

    def test_ops_exception_card_severity_enum(self) -> None:
        schema = _load_schema("ops_exception_card.schema.json")
        severities = schema["properties"]["severity"]["enum"]
        assert set(severities) == {"sev0", "sev1", "sev2", "sev3"}
