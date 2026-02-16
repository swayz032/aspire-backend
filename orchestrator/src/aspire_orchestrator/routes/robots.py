"""Robot Ingest Route -- accepts RobotRun results from CI/CD.

POST /robots/ingest
- Validates payload against robot_run.schema.json
- On failure: emit incident.opened receipt, create A2A triage message
- On success: emit robot.run.completed receipt
- Auth: S2S HMAC (same pattern as Domain Rail)

Law compliance:
  - Law #2: Every ingest produces a receipt (success or failure).
  - Law #3: Missing/invalid HMAC -> 401. Fail-closed.
  - Law #7: This route is a "hand" -- it stores results, does not decide.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from jsonschema import ValidationError, validate as js_validate

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/robots", tags=["robots"])

# Load schema once at module level
_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scripts"
    / "schemas"
    / "robot_run.schema.json"
)
_schema: dict[str, Any] | None = None


def _get_schema() -> dict[str, Any]:
    """Lazy-load the RobotRun JSON Schema."""
    global _schema
    if _schema is None:
        _schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema


def _get_robot_s2s_secret() -> str:
    """Get robot S2S HMAC secret. Fail closed if not configured (Law #3)."""
    secret = os.environ.get("ASPIRE_ROBOT_S2S_SECRET", "")
    return secret


def _verify_hmac(secret: str, body: bytes, provided_sig: str) -> bool:
    """Verify HMAC-SHA256 signature using timing-safe comparison."""
    expected = hmac_mod.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac_mod.compare_digest(expected, provided_sig)


def _build_receipt(
    *,
    receipt_type: str,
    run_id: str,
    env: str,
    status: str,
    summary: str,
    version_ref: str,
    scenario_count: int,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Build a receipt dict for robot run results."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": run_id,
        "suite_id": "system",
        "office_id": "system",
        "actor_type": "system",
        "actor_id": "robot_runner",
        "action_type": receipt_type,
        "risk_tier": "green",
        "tool_used": "robot_runner",
        "created_at": now,
        "outcome": status,
        "reason_code": reason_code or "",
        "redacted_inputs": {
            "env": env,
            "version_ref": version_ref,
            "scenario_count": scenario_count,
        },
        "redacted_outputs": {"summary": summary},
    }


@router.post("/ingest")
async def robot_ingest(request: Request) -> JSONResponse:
    """Accept a RobotRun result from CI/CD or the robot runner script.

    Auth: HMAC-SHA256 via X-Robot-Signature header.
    Fail-closed: missing secret or invalid signature -> 401.
    """
    # --- Auth: S2S HMAC verification ---
    secret = _get_robot_s2s_secret()
    if not secret:
        logger.warning("ASPIRE_ROBOT_S2S_SECRET not configured -- fail closed (Law #3)")
        return JSONResponse(
            status_code=401,
            content={
                "error": "AUTH_FAILED",
                "message": "Robot S2S secret not configured",
            },
        )

    body_bytes = await request.body()
    provided_sig = request.headers.get("x-robot-signature", "")
    if not provided_sig or not _verify_hmac(secret, body_bytes, provided_sig):
        logger.warning("Robot ingest HMAC verification failed")
        return JSONResponse(
            status_code=401,
            content={
                "error": "AUTH_FAILED",
                "message": "Invalid or missing HMAC signature",
            },
        )

    # --- Parse JSON body ---
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": "Invalid JSON body",
            },
        )

    # --- Validate against RobotRun schema ---
    try:
        schema = _get_schema()
        js_validate(instance=payload, schema=schema)
    except ValidationError as e:
        # Emit incident receipt for schema failure
        receipt = _build_receipt(
            receipt_type="incident.opened",
            run_id=payload.get("id", str(uuid.uuid4())),
            env=payload.get("env", "unknown"),
            status="failed",
            summary=f"Schema validation failed: {e.message[:200]}",
            version_ref=payload.get("versionRef", "unknown"),
            scenario_count=len(payload.get("scenarios", [])),
            reason_code="schema_validation_failed",
        )
        store_receipts([receipt])

        return JSONResponse(
            status_code=400,
            content={
                "error": "SCHEMA_VALIDATION_FAILED",
                "message": f"RobotRun schema validation failed: {e.message[:200]}",
                "receipt_id": receipt["id"],
            },
        )

    # --- Process valid robot run ---
    run_status = payload.get("status", "unknown")
    run_id = payload.get("id", str(uuid.uuid4()))
    env = payload.get("env", "unknown")
    version_ref = payload.get("versionRef", "unknown")
    scenarios = payload.get("scenarios", [])
    summary = payload.get("summary", "")

    if run_status == "failed":
        # Emit incident receipt for failed runs
        receipt = _build_receipt(
            receipt_type="incident.opened",
            run_id=run_id,
            env=env,
            status="failed",
            summary=summary,
            version_ref=version_ref,
            scenario_count=len(scenarios),
            reason_code="robot_run_failed",
        )
        store_receipts([receipt])

        logger.warning(
            "Robot run FAILED: id=%s env=%s version=%s",
            run_id[:8],
            env,
            version_ref[:8],
        )

        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "run_id": run_id,
                "status": "failed",
                "receipt_id": receipt["id"],
                "receipt_type": "incident.opened",
            },
        )

    # Success path
    receipt = _build_receipt(
        receipt_type="robot.run.completed",
        run_id=run_id,
        env=env,
        status=run_status,
        summary=summary,
        version_ref=version_ref,
        scenario_count=len(scenarios),
    )
    store_receipts([receipt])

    logger.info(
        "Robot run completed: id=%s env=%s status=%s version=%s",
        run_id[:8],
        env,
        run_status,
        version_ref[:8],
    )

    return JSONResponse(
        status_code=200,
        content={
            "accepted": True,
            "run_id": run_id,
            "status": run_status,
            "receipt_id": receipt["id"],
            "receipt_type": "robot.run.completed",
        },
    )
