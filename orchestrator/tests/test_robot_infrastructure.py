"""Tests for Robot Infrastructure — Wave 3.

Coverage:
  - Robot runner sync_validate scenario (pass/fail)
  - Robot runner output schema validation
  - Robot ingest endpoint (auth, schema, receipts)
  - Robot config loading
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from aspire_orchestrator.server import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

client = TestClient(app)

ROBOT_S2S_SECRET = "test-robot-s2s-secret-key"


def _make_valid_robot_run(**overrides: Any) -> dict[str, Any]:
    """Build a valid RobotRun payload."""
    run = {
        "id": str(uuid.uuid4()),
        "env": "staging",
        "suite": "aspire_robots",
        "status": "passed",
        "startedAt": "2026-02-14T10:00:00Z",
        "finishedAt": "2026-02-14T10:01:00Z",
        "versionRef": "abc123def",
        "summary": "sync_validate:passed; api_smoke:passed",
        "scenarios": [
            {
                "name": "sync_validate",
                "status": "passed",
                "summary": "sentinels OK (6 files checked)",
                "evidence": [],
            }
        ],
        "meta": {"mode": "smoke", "config": "robots.config.yaml"},
    }
    run.update(overrides)
    return run


def _sign_payload(payload: dict[str, Any], secret: str = ROBOT_S2S_SECRET) -> str:
    """Compute HMAC-SHA256 signature for a payload."""
    body = json.dumps(payload).encode("utf-8")
    return hmac_mod.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def _post_ingest(
    payload: dict[str, Any],
    *,
    secret: str = ROBOT_S2S_SECRET,
    include_sig: bool = True,
    override_sig: str | None = None,
) -> Any:
    """POST to /robots/ingest with proper HMAC auth."""
    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if include_sig:
        sig = override_sig or _sign_payload(payload, secret)
        headers["X-Robot-Signature"] = sig
    return client.post("/robots/ingest", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Robot Runner — sync_validate scenario
# ---------------------------------------------------------------------------


class TestSyncValidateScenario:
    """Tests for the sync_validate scenario in the robot runner."""

    def test_sync_validate_passes_with_correct_sentinels(self, tmp_path: Path) -> None:
        """sync_validate passes when all sentinel files exist."""
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from robot_runner import scenario_sync_validate

        # Create sentinel structure
        orch = tmp_path / "backend" / "orchestrator"
        orch.mkdir(parents=True)
        (orch / "pyproject.toml").write_text("", encoding="utf-8")

        gw = tmp_path / "backend" / "gateway"
        gw.mkdir(parents=True)
        (gw / "package.json").write_text("{}", encoding="utf-8")

        cfg = {
            "paths": {
                "orchestrator_root": "backend/orchestrator",
                "gateway_root": "backend/gateway",
            },
            "sentinels": {
                "orchestrator": ["pyproject.toml"],
                "gateway": ["package.json"],
            },
        }

        result = scenario_sync_validate(cfg, tmp_path)
        assert result.status == "passed"
        assert "2 files checked" in result.summary

    def test_sync_validate_fails_with_missing_sentinel(self, tmp_path: Path) -> None:
        """sync_validate fails when a sentinel file is missing."""
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from robot_runner import scenario_sync_validate

        orch = tmp_path / "backend" / "orchestrator"
        orch.mkdir(parents=True)
        # pyproject.toml missing

        cfg = {
            "paths": {"orchestrator_root": "backend/orchestrator"},
            "sentinels": {"orchestrator": ["pyproject.toml"]},
        }

        result = scenario_sync_validate(cfg, tmp_path)
        assert result.status == "failed"
        assert "missing orchestrator sentinel" in result.summary

    def test_sync_validate_fails_with_missing_root(self, tmp_path: Path) -> None:
        """sync_validate fails when a root directory doesn't exist."""
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from robot_runner import scenario_sync_validate

        cfg = {
            "paths": {"orchestrator_root": "nonexistent/path"},
            "sentinels": {"orchestrator": ["pyproject.toml"]},
        }

        result = scenario_sync_validate(cfg, tmp_path)
        assert result.status == "failed"
        assert "root missing" in result.summary


# ---------------------------------------------------------------------------
# Robot Runner — output schema validation
# ---------------------------------------------------------------------------


class TestRobotRunnerOutput:
    """Tests for robot runner JSON output conforming to RobotRun schema."""

    def test_valid_robot_run_passes_schema(self) -> None:
        """A well-formed RobotRun payload validates against the JSON schema."""
        from jsonschema import validate as js_validate

        schema_path = (
            Path(__file__).parent.parent
            / "scripts"
            / "schemas"
            / "robot_run.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        run = _make_valid_robot_run()
        js_validate(instance=run, schema=schema)  # no exception = pass

    def test_invalid_robot_run_fails_schema(self) -> None:
        """A RobotRun missing required fields fails schema validation."""
        from jsonschema import ValidationError, validate as js_validate

        schema_path = (
            Path(__file__).parent.parent
            / "scripts"
            / "schemas"
            / "robot_run.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        run = {"id": "abc123", "status": "passed"}  # Missing required fields
        with pytest.raises(ValidationError):
            js_validate(instance=run, schema=schema)


# ---------------------------------------------------------------------------
# Robot Ingest Endpoint — HMAC auth
# ---------------------------------------------------------------------------


class TestRobotIngestAuth:
    """Tests for /robots/ingest HMAC authentication."""

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_valid_hmac_accepted(self) -> None:
        """Valid HMAC signature is accepted."""
        payload = _make_valid_robot_run()
        resp = _post_ingest(payload)
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_invalid_hmac_rejected(self) -> None:
        """Invalid HMAC signature returns 401."""
        payload = _make_valid_robot_run()
        resp = _post_ingest(payload, override_sig="deadbeef")
        assert resp.status_code == 401
        assert resp.json()["error"] == "AUTH_FAILED"

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_missing_hmac_rejected(self) -> None:
        """Missing HMAC signature returns 401."""
        payload = _make_valid_robot_run()
        resp = _post_ingest(payload, include_sig=False)
        assert resp.status_code == 401
        assert resp.json()["error"] == "AUTH_FAILED"

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ""}, clear=False)
    def test_missing_secret_config_fails_closed(self) -> None:
        """Missing ASPIRE_ROBOT_S2S_SECRET fails closed (Law #3)."""
        # Remove the env var entirely
        env = os.environ.copy()
        env.pop("ASPIRE_ROBOT_S2S_SECRET", None)
        with patch.dict(os.environ, env, clear=True):
            payload = _make_valid_robot_run()
            resp = _post_ingest(payload)
            assert resp.status_code == 401
            assert "not configured" in resp.json()["message"]


# ---------------------------------------------------------------------------
# Robot Ingest Endpoint — schema validation + receipts
# ---------------------------------------------------------------------------


class TestRobotIngestPayload:
    """Tests for /robots/ingest payload validation and receipt emission."""

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_valid_passed_run_emits_receipt(self) -> None:
        """Successful robot run emits robot.run.completed receipt."""
        payload = _make_valid_robot_run(status="passed")
        resp = _post_ingest(payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["receipt_type"] == "robot.run.completed"
        assert data["receipt_id"] is not None

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_failed_run_emits_incident_receipt(self) -> None:
        """Failed robot run emits incident.opened receipt."""
        payload = _make_valid_robot_run(status="failed")
        resp = _post_ingest(payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["receipt_type"] == "incident.opened"
        assert data["status"] == "failed"

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_invalid_payload_rejected_with_receipt(self) -> None:
        """Invalid RobotRun schema emits incident receipt and returns 400."""
        payload = {"id": "short", "status": "passed"}  # Missing required fields
        resp = _post_ingest(payload)
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "SCHEMA_VALIDATION_FAILED"
        assert data["receipt_id"] is not None

    @patch.dict(os.environ, {"ASPIRE_ROBOT_S2S_SECRET": ROBOT_S2S_SECRET})
    def test_invalid_json_returns_400(self) -> None:
        """Non-JSON body returns 400."""
        body = b"not json at all"
        sig = hmac_mod.new(
            ROBOT_S2S_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        resp = client.post(
            "/robots/ingest",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Robot-Signature": sig,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "SCHEMA_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Robot Config Loading
# ---------------------------------------------------------------------------


class TestRobotConfig:
    """Tests for robot config YAML loading."""

    def test_config_loads_correctly(self) -> None:
        """robots.config.yaml loads and has expected structure."""
        cfg_path = (
            Path(__file__).parent.parent
            / "src"
            / "aspire_orchestrator"
            / "config"
            / "robots.config.yaml"
        )
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["mode"] == "smoke"
        assert "orchestrator_root" in data["paths"]
        assert "gateway_root" in data["paths"]
        assert "sync_validate" in data["scenarios"]["enabled"]
        assert "api_smoke" in data["scenarios"]["enabled"]
        assert "staging" in data["env_defaults"]
        assert "production" in data["env_defaults"]

    def test_config_sentinels_list_files(self) -> None:
        """Config sentinels contain expected critical files."""
        cfg_path = (
            Path(__file__).parent.parent
            / "src"
            / "aspire_orchestrator"
            / "config"
            / "robots.config.yaml"
        )
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert "pyproject.toml" in data["sentinels"]["orchestrator"]
        assert "package.json" in data["sentinels"]["gateway"]


# ---------------------------------------------------------------------------
# ReceiptType enum values
# ---------------------------------------------------------------------------


class TestReceiptTypeEnum:
    """Verify robot receipt types are registered in models."""

    def test_robot_run_completed_in_enum(self) -> None:
        from aspire_orchestrator.models import ReceiptType

        assert ReceiptType.ROBOT_RUN_COMPLETED.value == "robot.run.completed"

    def test_incident_opened_in_enum(self) -> None:
        from aspire_orchestrator.models import ReceiptType

        assert ReceiptType.INCIDENT_OPENED.value == "incident.opened"
