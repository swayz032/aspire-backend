"""Token Mint Node — Capability token creation (Law #5).

Responsibilities:
1. Mint a short-lived (<60s) capability token for the approved action
2. Token is scoped to suite_id + office_id + specific tool + specific scopes
3. Token is signed with HMAC-SHA256
4. Enforce TTL constraint: expires_at - issued_at < 60 seconds
5. Store token_id and token_hash in state for receipt linkage
6. Fail closed if signing key is not configured (Law #3)

Per capability-token.schema.v1.yaml:
  - Only the LangGraph orchestrator mints tokens
  - HMAC-SHA256 signature
  - Expiry < 60 seconds
  - Scoped to suite + office + tool
  - 6-check validation: signature, expiry, revocation, scope, suite_id, office_id
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import AspireErrorCode, Outcome, RiskTier
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

# Maximum token TTL — Law #5: <60 seconds
MAX_TOKEN_TTL_SECONDS = 59


def _get_signing_key() -> str:
    """Get the token signing key. Fail closed if not configured.

    Per CLAUDE.md Law #3: Missing permission/policy/verification = deny.
    A missing signing key means we cannot mint valid tokens.
    """
    key = settings.token_signing_key
    if not key:
        # Allow dev override via environment for testing
        key = os.environ.get("ASPIRE_TOKEN_SIGNING_KEY", "")
    if not key:
        raise ValueError(
            "ASPIRE_TOKEN_SIGNING_KEY not configured. "
            "Cannot mint capability tokens without a signing key. "
            "Fail-closed per Law #3."
        )
    return key


def _mint_token(
    *,
    suite_id: str,
    office_id: str,
    tool: str,
    scopes: list[str],
    correlation_id: str,
    ttl_seconds: int = 45,
    signing_key: str,
) -> dict[str, Any]:
    """Mint a capability token with HMAC-SHA256 signature.

    Returns the token as a dict (not yet persisted to DB).
    Raises ValueError if TTL exceeds 60 seconds.
    """
    # Enforce Law #5: TTL < 60 seconds
    if ttl_seconds > MAX_TOKEN_TTL_SECONDS:
        raise ValueError(
            f"Token TTL {ttl_seconds}s exceeds maximum {MAX_TOKEN_TTL_SECONDS}s (Law #5)"
        )

    token_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    # Build canonical token payload for signing
    token_payload = {
        "token_id": token_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "tool": tool,
        "scopes": sorted(scopes),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "correlation_id": correlation_id,
    }

    # Sign with HMAC-SHA256
    canonical = json.dumps(token_payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    token_payload["signature"] = signature
    token_payload["revoked"] = False

    return token_payload


def token_mint_node(state: OrchestratorState) -> dict[str, Any]:
    """Mint a capability token for the approved action.

    Only reached if policy allowed + approval granted.
    Fails closed if signing key is missing.
    """
    if state.get("error_code"):
        return {}

    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    allowed_tools = state.get("allowed_tools", [])

    # Fail closed if no signing key
    try:
        signing_key = _get_signing_key()
    except ValueError as e:
        logger.error("Token mint failed: %s", e)
        # Emit failure receipt (Law #2: every state change gets a receipt)
        risk_tier = state.get("risk_tier")
        risk_tier_str = risk_tier.value if isinstance(risk_tier, RiskTier) else str(risk_tier or "unknown")
        receipt = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "actor_type": "system",
            "actor_id": "orchestrator.token_mint",
            "action_type": "token.mint",
            "risk_tier": risk_tier_str,
            "tool_used": "orchestrator.token_mint",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome": "failed",
            "reason_code": "TOKEN_SIGNING_KEY_MISSING",
            "receipt_type": "tool_execution",
            "receipt_hash": "",
        }
        existing = list(state.get("pipeline_receipts", []))
        existing.append(receipt)
        return {
            "error_code": AspireErrorCode.CAPABILITY_TOKEN_REQUIRED.value,
            "error_message": str(e),
            "outcome": Outcome.FAILED,
            "pipeline_receipts": existing,
        }

    # Use the first allowed tool for token scoping
    tool = allowed_tools[0] if allowed_tools else "unknown.tool"

    # Determine scopes from task_type
    task_type = state.get("task_type", "")
    verb = task_type.split(".")[-1] if "." in task_type else task_type
    scope_map = {
        "read": "read",
        "list": "read",
        "search": "read",
        "create": "write",
        "send": "write",
        "draft": "write",
        "schedule": "write",
        "sign": "write",
        "run": "execute",
        "file": "execute",
        "transfer": "write",
        "delete": "delete",
        "purchase": "write",
    }
    scope_verb = scope_map.get(verb, "execute")
    domain = task_type.split(".")[0] if "." in task_type else task_type
    scopes = [f"{domain}.{scope_verb}"]

    # Enforce TTL < 60s
    ttl = min(settings.token_ttl_seconds, MAX_TOKEN_TTL_SECONDS)

    token = _mint_token(
        suite_id=suite_id,
        office_id=office_id,
        tool=tool,
        scopes=scopes,
        correlation_id=correlation_id,
        ttl_seconds=ttl,
        signing_key=signing_key,
    )

    # Compute token hash for receipt linkage (SHA-256 of full token)
    token_hash = hashlib.sha256(
        json.dumps(token, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    logger.info(
        "Token minted: tool=%s, scopes=%s, ttl=%ds, suite=%s",
        tool, scopes, ttl, suite_id[:8],
    )

    return {
        "capability_token_id": token["token_id"],
        "capability_token_hash": token_hash,
        "capability_token": token,
    }
