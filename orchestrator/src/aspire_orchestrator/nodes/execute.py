"""Execute Node — Bounded tool execution (Law #7).

Responsibilities:
1. Execute the approved action via the appropriate skill pack
2. Tools are hands — they execute bounded commands, never decide
3. Validate capability token before execution
4. Handle execution failures with receipts
5. Set outcome (success/failed/timeout)

Wave 7: Tool executor registry integration.
  - Domain Rail tools (domain.*, polaris.account.*) registered as live executors
  - All other tools → stub executor (Phase 2 implementations)
  - LangGraph nodes are sync; live Domain Rail calls use the async tool
    executor service directly (via POST /v1/tools/execute or A2A dispatch)
  - The execute node produces receipts for all outcomes
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import (
    AspireErrorCode,
    Outcome,
    ReceiptType,
)
from aspire_orchestrator.services.token_service import validate_token
from aspire_orchestrator.services.tool_executor import is_live_tool
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def _resolve_risk_tier(state: OrchestratorState) -> str:
    """Extract risk_tier as a string, handling both enum and str values."""
    risk_tier_val = state.get("risk_tier")
    return risk_tier_val.value if hasattr(risk_tier_val, "value") else str(risk_tier_val or "green")


def execute_node(state: OrchestratorState) -> dict[str, Any]:
    """Execute the approved action via tool executor registry.

    For Phase 1:
      - All tools produce receipts and set outcome
      - Live tools (Domain Rail) are flagged for async dispatch
      - Stub tools return immediate success
      - Actual async Domain Rail calls happen via the tool executor
        service when invoked through A2A dispatch or direct API
    """
    if state.get("error_code"):
        return {
            "outcome": Outcome.DENIED,
            "execution_result": None,
        }

    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    task_type = state.get("task_type", "unknown")
    allowed_tools = state.get("allowed_tools", [])
    capability_token_id = state.get("capability_token_id")
    capability_token = state.get("capability_token")
    risk_tier_str = _resolve_risk_tier(state)

    # -------------------------------------------------------------------
    # Capability token validation — full 6-check (Law #3 + Law #5)
    # This is the enforcement boundary: tokens are minted by token_mint
    # node, but MUST be validated again here before any execution.
    # -------------------------------------------------------------------
    def _deny_execution(reason_code: str, message: str) -> dict[str, Any]:
        """Build denial response with receipt."""
        receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "actor_type": "system",
            "actor_id": "executor",
            "action_type": f"execute.{task_type}",
            "risk_tier": risk_tier_str,
            "tool_used": allowed_tools[0] if allowed_tools else "unknown",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": Outcome.DENIED.value,
            "reason_code": reason_code,
            "receipt_type": ReceiptType.TOOL_EXECUTION.value,
            "receipt_hash": "",
        }
        existing = list(state.get("pipeline_receipts", []))
        existing.append(receipt)
        return {
            "outcome": Outcome.DENIED,
            "execution_result": None,
            "error_code": reason_code,
            "error_message": message,
            "tool_used": allowed_tools[0] if allowed_tools else "unknown",
            "pipeline_receipts": existing,
        }

    # Check 0: Token must exist
    if not capability_token_id or not capability_token:
        logger.warning(
            "Execution denied: missing capability token for %s (suite=%s)",
            task_type, suite_id[:8] if len(suite_id) > 8 else suite_id,
        )
        return _deny_execution(
            AspireErrorCode.CAPABILITY_TOKEN_REQUIRED.value,
            "Capability token required for execution",
        )

    # Full 6-check validation (signature, expiry, revocation, scope, suite, office)
    tool_used = allowed_tools[0] if allowed_tools else "unknown"
    verb = task_type.split(".")[-1] if "." in task_type else task_type
    scope_map = {
        "read": "read", "list": "read", "search": "read",
        "create": "write", "send": "write", "draft": "write",
        "schedule": "write", "sign": "write", "transfer": "write",
        "delete": "delete", "purchase": "write",
    }
    scope_verb = scope_map.get(verb, "execute")
    domain = task_type.split(".")[0] if "." in task_type else task_type
    required_scope = f"{domain}.{scope_verb}"

    validation = validate_token(
        capability_token,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope=required_scope,
    )

    if not validation.valid:
        logger.warning(
            "Execution denied: token validation failed (%s) for %s (suite=%s), checks_passed=%d/6",
            validation.error.value if validation.error else "unknown",
            task_type, suite_id[:8] if len(suite_id) > 8 else suite_id,
            validation.checks_passed,
        )
        return _deny_execution(
            AspireErrorCode.CAPABILITY_TOKEN_REQUIRED.value,
            f"Token validation failed: {validation.error_message}",
        )

    # Resolve tool to execute (tool_used already set during scope derivation)
    live = is_live_tool(tool_used)

    logger.info(
        "Executing tool: %s (live=%s) for task=%s, suite=%s",
        tool_used, live, task_type,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
    )

    # Build execution result
    execution_result = {
        "status": "success",
        "tool": tool_used,
        "task_type": task_type,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "stub": not live,
        "live": live,
    }

    # Tool execution receipt
    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "executor",
        "action_type": f"execute.{task_type}",
        "risk_tier": risk_tier_str,
        "tool_used": tool_used,
        "capability_token_id": capability_token_id,
        "capability_token_hash": state.get("capability_token_hash"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": Outcome.SUCCESS.value,
        "reason_code": "EXECUTED" if live else "EXECUTED_STUB",
        "receipt_type": ReceiptType.TOOL_EXECUTION.value,
        "receipt_hash": "",
    }
    existing_receipts = list(state.get("pipeline_receipts", []))
    existing_receipts.append(receipt)

    return {
        "outcome": Outcome.SUCCESS,
        "execution_result": execution_result,
        "tool_used": tool_used,
        "pipeline_receipts": existing_receipts,
    }
