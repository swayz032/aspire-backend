"""Respond Node — AvaResult construction and egress validation.

Responsibilities:
1. Persist any unpersisted pipeline receipts (Law #2 safety net)
2. Construct AvaResult from pipeline state
3. Validate AvaResult schema before returning (egress validation)
4. Handle error cases (return AspireError instead of AvaResult)
5. Include all receipt_ids in governance metadata

Per receipt_emission_rules.md:
  "Validate AvaResult schema before returning"

Law #2 Safety Net:
  Denied/blocked flows skip receipt_write_node. The respond node is the
  terminal node ALL graph paths pass through, so it ensures receipts are
  ALWAYS persisted — even for denied, blocked, or approval-pending flows.
  Receipts without a receipt_hash have not been through receipt_write.

This is the final node in the pipeline — its output becomes the HTTP response.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from aspire_orchestrator.models import (
    AvaResult,
    AvaResultGovernance,
    AvaResultRisk,
    Outcome,
    RiskTier,
)
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def _persist_unpersisted_receipts(state: OrchestratorState) -> list[str]:
    """Persist any pipeline receipts that were not processed by receipt_write.

    Law #2 safety net: denied/blocked flows skip receipt_write_node, so
    pipeline_receipts accumulate but are never chain-hashed or stored.
    This function detects unpersisted receipts (missing receipt_hash),
    assigns chain metadata, and stores them.

    Returns the list of receipt IDs that were persisted.
    """
    pipeline_receipts = list(state.get("pipeline_receipts", []))
    if not pipeline_receipts:
        return []

    # Check if receipts already have real chain hashes (receipt_write handled them).
    # Nodes set receipt_hash="" as a placeholder; receipt_write sets a real SHA-256 hash.
    unpersisted = [r for r in pipeline_receipts if not r.get("receipt_hash")]
    if not unpersisted:
        return []

    suite_id = state.get("suite_id", "unknown")

    try:
        from aspire_orchestrator.services.receipt_chain import assign_chain_metadata
        from aspire_orchestrator.services.receipt_store import store_receipts

        assign_chain_metadata(unpersisted, chain_id=suite_id)
        store_receipts(unpersisted)

        persisted_ids = [r["id"] for r in unpersisted if "id" in r]
        logger.info(
            "Law #2 safety net: persisted %d unpersisted receipts for suite=%s",
            len(persisted_ids), suite_id,
        )
        return persisted_ids

    except Exception as e:
        # Fail closed — log but don't crash the response
        logger.error("Law #2 safety net failed to persist receipts: %s", e)
        return []


def respond_node(state: OrchestratorState) -> dict[str, Any]:
    """Construct and validate the response.

    Returns the full response dict to be sent to the client.
    """
    # Law #2 safety net: persist any receipts that skipped receipt_write
    safety_net_ids = _persist_unpersisted_receipts(state)

    correlation_id = state.get("correlation_id", "unknown")
    request_id = state.get("request_id", "unknown")
    error_code = state.get("error_code")
    receipt_ids = list(state.get("receipt_ids", []))

    # Merge any newly persisted receipt IDs
    if safety_net_ids:
        receipt_ids.extend(safety_net_ids)

    # Error case — return structured error
    if error_code:
        response = {
            "error": error_code,
            "message": state.get("error_message", "Unknown error"),
            "correlation_id": correlation_id,
            "request_id": request_id,
            "receipt_ids": receipt_ids,
        }

        # For approval-required, include the payload hash for binding
        if error_code in ("APPROVAL_REQUIRED", "PRESENCE_REQUIRED"):
            response["approval_payload_hash"] = state.get("approval_payload_hash")
            response["required_approvals"] = state.get("required_approvals", [])
            response["presence_required"] = state.get("presence_required", False)

        return {"response": response}

    # Success case — construct AvaResult
    risk_tier = state.get("risk_tier", RiskTier.GREEN)
    risk_tier_val = risk_tier.value if isinstance(risk_tier, RiskTier) else str(risk_tier)

    # Build governance metadata
    required_approvals = state.get("required_approvals", [])
    presence_required = state.get("presence_required", False)
    capability_token_required = state.get("capability_token_id") is not None

    try:
        result = AvaResult(
            schema_version="1.0",
            request_id=request_id,
            correlation_id=correlation_id,
            route={
                "skill_pack": state.get("task_type", "").split(".")[0] if state.get("task_type") else "unknown",
                "tool": state.get("tool_used", "unknown"),
            },
            risk=AvaResultRisk(tier=RiskTier(risk_tier_val)),
            governance=AvaResultGovernance(
                approvals_required=required_approvals,
                presence_required=presence_required,
                capability_token_required=capability_token_required,
                receipt_ids=receipt_ids,
            ),
            plan={
                "task_type": state.get("task_type"),
                "outcome": state.get("outcome", Outcome.SUCCESS).value if hasattr(state.get("outcome", Outcome.SUCCESS), "value") else str(state.get("outcome", "success")),
                "execution_result": state.get("execution_result"),
            },
        )

        # Egress validation — validate AvaResult schema before returning
        response = result.model_dump()
        return {"response": response}

    except (ValidationError, Exception) as e:
        # If we can't construct a valid AvaResult, return error
        return {
            "response": {
                "error": "INTERNAL_ERROR",
                "message": f"Failed to construct AvaResult: {e}",
                "correlation_id": correlation_id,
                "request_id": request_id,
                "receipt_ids": receipt_ids,
            }
        }
