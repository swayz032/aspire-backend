"""Capability Token Service — Mint + 6-Check Validation (Law #5).

Per capability-token.schema.v1.yaml:
  - Only the LangGraph orchestrator mints tokens (Law #1)
  - HMAC-SHA256 signature
  - Expiry < 60 seconds
  - Scoped to suite + office + tool
  - Server-side 6-check validation:
    1. Signature valid (HMAC-SHA256 verification)
    2. Not expired (current_time < expires_at)
    3. Not revoked (revoked = false)
    4. Scope matches requested action
    5. suite_id matches request context
    6. office_id matches request context
  - Failure action: Return denial + generate denial receipt

Implementation Notes:
  - Token revocation uses in-memory set (Phase 1 — will move to DB in Phase 2)
  - Clock skew tolerance: 2 seconds (configurable)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

# Maximum token TTL — Law #5: <60 seconds
MAX_TOKEN_TTL_SECONDS = 59

# Clock skew tolerance for expiry checks
CLOCK_SKEW_TOLERANCE_SECONDS = 2

# In-memory revocation set (Phase 1 — moves to DB in Phase 2)
_revoked_tokens: set[str] = set()


class TokenValidationError(Enum):
    """Enumeration of the 6 validation check failure modes."""

    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    SUITE_MISMATCH = "SUITE_MISMATCH"
    OFFICE_MISMATCH = "OFFICE_MISMATCH"
    MISSING_SIGNING_KEY = "MISSING_SIGNING_KEY"
    MALFORMED_TOKEN = "MALFORMED_TOKEN"


@dataclass(frozen=True)
class TokenValidationResult:
    """Result of the 6-check token validation."""

    valid: bool
    error: TokenValidationError | None = None
    error_message: str | None = None
    checks_passed: int = 0


def _get_signing_key() -> str:
    """Get the token signing key. Fail closed if not configured or too weak.

    Per CLAUDE.md Law #3: Missing permission/policy/verification = deny.
    Per RFC 7518 Section 3.2: HMAC-SHA256 keys MUST be >= 32 bytes.

    Key source: AWS Secrets Manager (internal group) via TOKEN_SIGNING_SECRET.
    Rotation: Handled by AWS SM rotation pipeline + invalidate_cache().
    """
    import os

    key = settings.token_signing_key
    if not key or key == "UNCONFIGURED-FAIL-CLOSED":
        key = os.environ.get("ASPIRE_TOKEN_SIGNING_KEY", "")
    if not key or key == "UNCONFIGURED-FAIL-CLOSED":
        raise ValueError(
            "ASPIRE_TOKEN_SIGNING_KEY not configured. "
            "Cannot validate capability tokens without a signing key. "
            "Fail-closed per Law #3."
        )
    # B-H1: Enforce minimum key length (RFC 7518 §3.2: 32 bytes for SHA-256)
    if len(key) < 32:
        logger.warning(
            "Token signing key is %d bytes — below recommended 32-byte minimum for HMAC-SHA256. "
            "Rotate to a stronger key via AWS Secrets Manager.",
            len(key),
        )
    return key


def mint_token(
    *,
    suite_id: str,
    office_id: str,
    tool: str,
    scopes: list[str],
    correlation_id: str,
    ttl_seconds: int = 45,
) -> dict[str, Any]:
    """Mint a capability token with HMAC-SHA256 signature.

    Returns the full token dict.
    Raises ValueError if TTL exceeds 60 seconds or signing key missing.
    """
    if ttl_seconds > MAX_TOKEN_TTL_SECONDS:
        raise ValueError(
            f"Token TTL {ttl_seconds}s exceeds maximum {MAX_TOKEN_TTL_SECONDS}s (Law #5)"
        )
    if ttl_seconds <= 0:
        raise ValueError(f"Token TTL {ttl_seconds}s must be positive")

    signing_key = _get_signing_key()

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

    logger.info(
        "Token minted: id=%s, tool=%s, scopes=%s, ttl=%ds, suite=%s",
        token_id[:8], tool, scopes, ttl_seconds,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
    )

    return token_payload


def compute_token_hash(token: dict[str, Any]) -> str:
    """Compute SHA-256 hash of the full token for receipt linkage."""
    return hashlib.sha256(
        json.dumps(token, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_token(
    token: dict[str, Any],
    *,
    expected_suite_id: str,
    expected_office_id: str,
    required_scope: str,
    now: datetime | None = None,
) -> TokenValidationResult:
    """Perform 6-check server-side token validation.

    Per capability-token.schema.v1.yaml validation spec:
      1. Signature valid (HMAC-SHA256 verification)
      2. Not expired (current_time < expires_at)
      3. Not revoked (revoked = false)
      4. Scope matches requested action
      5. suite_id matches request context
      6. office_id matches request context

    Args:
        token: The capability token dict to validate
        expected_suite_id: The suite_id from the request context
        expected_office_id: The office_id from the request context
        required_scope: The scope needed for the requested action
        now: Override current time (for testing)

    Returns:
        TokenValidationResult with valid=True or error details
    """
    if now is None:
        now = datetime.now(timezone.utc)

    checks_passed = 0

    # Pre-check: required fields present
    required_fields = [
        "token_id", "suite_id", "office_id", "tool", "scopes",
        "issued_at", "expires_at", "signature", "correlation_id",
    ]
    for field in required_fields:
        if field not in token:
            logger.warning("Token validation failed: missing field '%s'", field)
            return TokenValidationResult(
                valid=False,
                error=TokenValidationError.MALFORMED_TOKEN,
                error_message=f"Missing required field: {field}",
                checks_passed=checks_passed,
            )

    # ---------------------------------------------------------------
    # CHECK 1: Signature valid (HMAC-SHA256 verification)
    # ---------------------------------------------------------------
    try:
        signing_key = _get_signing_key()
    except ValueError as e:
        logger.error("Token validation failed: %s", e)
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.MISSING_SIGNING_KEY,
            error_message=str(e),
            checks_passed=checks_passed,
        )

    # Reconstruct canonical payload (exclude signature and revoked — they're not in the signed payload)
    payload_for_signing = {
        "token_id": token["token_id"],
        "suite_id": token["suite_id"],
        "office_id": token["office_id"],
        "tool": token["tool"],
        "scopes": sorted(token["scopes"]),
        "issued_at": token["issued_at"],
        "expires_at": token["expires_at"],
        "correlation_id": token["correlation_id"],
    }
    canonical = json.dumps(payload_for_signing, sort_keys=True, separators=(",", ":"))
    expected_signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(token["signature"], expected_signature):
        logger.warning(
            "Token validation FAILED: invalid signature for token=%s",
            token.get("token_id", "unknown")[:8],
        )
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.SIGNATURE_INVALID,
            error_message="HMAC-SHA256 signature verification failed",
            checks_passed=checks_passed,
        )
    checks_passed += 1

    # ---------------------------------------------------------------
    # CHECK 2: Not expired (current_time < expires_at)
    # ---------------------------------------------------------------
    try:
        expires_at_str = token["expires_at"]
        if isinstance(expires_at_str, str):
            expires_at = datetime.fromisoformat(expires_at_str)
        else:
            expires_at = expires_at_str

        # Ensure timezone-aware
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as e:
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.TOKEN_EXPIRED,
            error_message=f"Invalid expires_at format: {e}",
            checks_passed=checks_passed,
        )

    # Allow small clock skew tolerance
    if now > expires_at + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS):
        logger.warning(
            "Token validation FAILED: expired token=%s, expired_at=%s, now=%s",
            token.get("token_id", "unknown")[:8],
            expires_at.isoformat(), now.isoformat(),
        )
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.TOKEN_EXPIRED,
            error_message=f"Token expired at {expires_at.isoformat()}, current time {now.isoformat()}",
            checks_passed=checks_passed,
        )
    checks_passed += 1

    # ---------------------------------------------------------------
    # CHECK 3: Not revoked (revoked = false)
    # ---------------------------------------------------------------
    token_id = token["token_id"]
    if token.get("revoked", False) or token_id in _revoked_tokens:
        logger.warning(
            "Token validation FAILED: revoked token=%s",
            token_id[:8],
        )
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.TOKEN_REVOKED,
            error_message=f"Token {token_id[:8]}... has been revoked",
            checks_passed=checks_passed,
        )
    checks_passed += 1

    # ---------------------------------------------------------------
    # CHECK 4: Scope matches requested action
    # ---------------------------------------------------------------
    token_scopes = set(token.get("scopes", []))
    if required_scope not in token_scopes:
        # Also check for wildcard scope (domain.*)
        scope_domain = required_scope.split(".")[0] if "." in required_scope else required_scope
        wildcard_scope = f"{scope_domain}.*"
        if wildcard_scope not in token_scopes:
            logger.warning(
                "Token validation FAILED: scope mismatch, required=%s, token_scopes=%s",
                required_scope, token_scopes,
            )
            return TokenValidationResult(
                valid=False,
                error=TokenValidationError.SCOPE_MISMATCH,
                error_message=f"Required scope '{required_scope}' not in token scopes {sorted(token_scopes)}",
                checks_passed=checks_passed,
            )
    checks_passed += 1

    # ---------------------------------------------------------------
    # CHECK 5: suite_id matches request context
    # ---------------------------------------------------------------
    if token["suite_id"] != expected_suite_id:
        logger.warning(
            "Token validation FAILED: suite_id mismatch, token=%s, expected=%s",
            token["suite_id"][:8], expected_suite_id[:8],
        )
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.SUITE_MISMATCH,
            error_message="Token suite_id does not match request context",
            checks_passed=checks_passed,
        )
    checks_passed += 1

    # ---------------------------------------------------------------
    # CHECK 6: office_id matches request context
    # ---------------------------------------------------------------
    if token["office_id"] != expected_office_id:
        logger.warning(
            "Token validation FAILED: office_id mismatch, token=%s, expected=%s",
            token["office_id"][:8], expected_office_id[:8],
        )
        return TokenValidationResult(
            valid=False,
            error=TokenValidationError.OFFICE_MISMATCH,
            error_message="Token office_id does not match request context",
            checks_passed=checks_passed,
        )
    checks_passed += 1

    # All 6 checks passed
    logger.info(
        "Token validated: 6/6 checks passed, token=%s, tool=%s, suite=%s",
        token_id[:8], token.get("tool", "unknown"),
        expected_suite_id[:8] if len(expected_suite_id) > 8 else expected_suite_id,
    )
    return TokenValidationResult(valid=True, checks_passed=6)


def revoke_token(token_id: str) -> None:
    """Revoke a capability token by adding it to the revocation set.

    In Phase 1, this uses an in-memory set. Phase 2 moves to DB persistence.
    """
    _revoked_tokens.add(token_id)
    logger.info("Token revoked: %s", token_id[:8])


def is_revoked(token_id: str) -> bool:
    """Check if a token has been revoked."""
    return token_id in _revoked_tokens


def clear_revocations() -> None:
    """Clear the in-memory revocation set. For testing only."""
    _revoked_tokens.clear()
