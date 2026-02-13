"""Presence Session Service — RED-tier authority verification.

Per presence_sessions.md:
  - Server-verifiable evidence that user is actively present
  - Live session token minted server-side (short TTL) tied to suite_id + office_id + session_id
  - Proof attached to approval and persisted in receipts
  - TTL <= 5 minutes
  - Nonce bound to payload_hash
  - Server verifies token signature + freshness
  - Receipt emitted: presence_verified or presence_missing

Per CLAUDE.md Law #8:
  - Hot (video) is escalation for authority moments (RED tier)
  - Presence required for: send payment, sign contract, file taxes, payroll
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

# Maximum presence token TTL — 5 minutes per spec
MAX_PRESENCE_TTL_SECONDS = 300

# Default TTL — 3 minutes (conservative)
DEFAULT_PRESENCE_TTL_SECONDS = 180


class PresenceError(Enum):
    """Presence verification failure modes."""

    TOKEN_MISSING = "PRESENCE_TOKEN_MISSING"
    TOKEN_EXPIRED = "PRESENCE_TOKEN_EXPIRED"
    TOKEN_REVOKED = "PRESENCE_TOKEN_REVOKED"
    SIGNATURE_INVALID = "PRESENCE_SIGNATURE_INVALID"
    PAYLOAD_HASH_MISMATCH = "PRESENCE_PAYLOAD_HASH_MISMATCH"
    SUITE_MISMATCH = "PRESENCE_SUITE_MISMATCH"
    OFFICE_MISMATCH = "PRESENCE_OFFICE_MISMATCH"
    MISSING_SIGNING_KEY = "PRESENCE_MISSING_SIGNING_KEY"


@dataclass(frozen=True)
class PresenceVerificationResult:
    """Result of presence token verification."""

    valid: bool
    error: PresenceError | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class PresenceToken:
    """Server-side presence token for RED-tier operations.

    Per presence_sessions.md:
    - Minted server-side after fresh user action (biometric/re-auth/explicit "I am here")
    - Bound to suite_id, office_id
    - TTL <= 5 minutes
    - Nonce bound to payload_hash
    """

    token_id: str
    suite_id: str
    office_id: str
    session_id: str
    nonce: str
    payload_hash: str
    issued_at: str  # ISO8601
    expires_at: str  # ISO8601
    signature: str


# In-memory revocation set (Phase 1)
_revoked_presence_tokens: set[str] = set()


def _get_presence_signing_key() -> str:
    """Get the presence token signing key. Fail closed if not configured."""
    key = settings.token_signing_key
    if not key:
        key = os.environ.get("ASPIRE_TOKEN_SIGNING_KEY", "")
    if not key:
        raise ValueError(
            "Signing key not configured. Cannot mint presence tokens. Fail-closed per Law #3."
        )
    return key


def mint_presence_token(
    *,
    suite_id: str,
    office_id: str,
    session_id: str,
    payload_hash: str,
    ttl_seconds: int = DEFAULT_PRESENCE_TTL_SECONDS,
) -> PresenceToken:
    """Mint a presence token for RED-tier operations.

    Args:
        suite_id: The suite this token is scoped to
        office_id: The office this token is scoped to
        session_id: The user's active session ID
        payload_hash: SHA-256 of the execution payload (nonce binding)
        ttl_seconds: Token TTL (max 300s per spec)
    """
    if ttl_seconds > MAX_PRESENCE_TTL_SECONDS:
        raise ValueError(
            f"Presence token TTL {ttl_seconds}s exceeds maximum {MAX_PRESENCE_TTL_SECONDS}s"
        )
    if ttl_seconds <= 0:
        raise ValueError(f"Presence token TTL {ttl_seconds}s must be positive")

    signing_key = _get_presence_signing_key()

    token_id = str(uuid.uuid4())
    nonce = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)

    # Build canonical payload for signing
    payload = {
        "token_id": token_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "session_id": session_id,
        "nonce": nonce,
        "payload_hash": payload_hash,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    logger.info(
        "Presence token minted: id=%s, session=%s, suite=%s, ttl=%ds",
        token_id[:8], session_id[:8], suite_id[:8], ttl_seconds,
    )

    return PresenceToken(
        token_id=token_id,
        suite_id=suite_id,
        office_id=office_id,
        session_id=session_id,
        nonce=nonce,
        payload_hash=payload_hash,
        issued_at=issued_at.isoformat(),
        expires_at=expires_at.isoformat(),
        signature=signature,
    )


def verify_presence_token(
    token: PresenceToken | dict[str, Any],
    *,
    expected_suite_id: str,
    expected_office_id: str,
    expected_payload_hash: str,
    now: datetime | None = None,
) -> PresenceVerificationResult:
    """Verify a presence token for RED-tier operations.

    Checks:
    1. Signature valid (HMAC-SHA256)
    2. Not expired
    3. Not revoked
    4. payload_hash matches (nonce binding)
    5. suite_id matches
    6. office_id matches
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Normalize to dict
    if isinstance(token, PresenceToken):
        token_dict: dict[str, Any] = {
            "token_id": token.token_id,
            "suite_id": token.suite_id,
            "office_id": token.office_id,
            "session_id": token.session_id,
            "nonce": token.nonce,
            "payload_hash": token.payload_hash,
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
            "signature": token.signature,
        }
    else:
        token_dict = token

    # CHECK 1: Signature valid
    try:
        signing_key = _get_presence_signing_key()
    except ValueError as e:
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.MISSING_SIGNING_KEY,
            error_message=str(e),
        )

    payload_for_signing = {
        "token_id": token_dict["token_id"],
        "suite_id": token_dict["suite_id"],
        "office_id": token_dict["office_id"],
        "session_id": token_dict["session_id"],
        "nonce": token_dict["nonce"],
        "payload_hash": token_dict["payload_hash"],
        "issued_at": token_dict["issued_at"],
        "expires_at": token_dict["expires_at"],
    }
    canonical = json.dumps(payload_for_signing, sort_keys=True, separators=(",", ":"))
    expected_sig = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(token_dict["signature"], expected_sig):
        logger.warning("Presence token FAILED: invalid signature, token=%s", token_dict["token_id"][:8])
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.SIGNATURE_INVALID,
            error_message="Presence token signature verification failed",
        )

    # CHECK 2: Not expired
    expires_at = datetime.fromisoformat(token_dict["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        logger.warning("Presence token FAILED: expired, token=%s", token_dict["token_id"][:8])
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.TOKEN_EXPIRED,
            error_message=f"Presence token expired at {expires_at.isoformat()}",
        )

    # CHECK 3: Not revoked
    if token_dict["token_id"] in _revoked_presence_tokens:
        logger.warning("Presence token FAILED: revoked, token=%s", token_dict["token_id"][:8])
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.TOKEN_REVOKED,
            error_message="Presence token has been revoked",
        )

    # CHECK 4: payload_hash matches (nonce binding)
    if token_dict["payload_hash"] != expected_payload_hash:
        logger.warning(
            "Presence token FAILED: payload_hash mismatch, token=%s",
            token_dict["token_id"][:8],
        )
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.PAYLOAD_HASH_MISMATCH,
            error_message="Presence token payload_hash does not match execution payload",
        )

    # CHECK 5: suite_id matches
    if token_dict["suite_id"] != expected_suite_id:
        logger.warning("Presence token FAILED: suite_id mismatch, token=%s", token_dict["token_id"][:8])
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.SUITE_MISMATCH,
            error_message="Presence token suite_id does not match execution context",
        )

    # CHECK 6: office_id matches
    if token_dict["office_id"] != expected_office_id:
        logger.warning("Presence token FAILED: office_id mismatch, token=%s", token_dict["token_id"][:8])
        return PresenceVerificationResult(
            valid=False,
            error=PresenceError.OFFICE_MISMATCH,
            error_message="Presence token office_id does not match execution context",
        )

    logger.info(
        "Presence token VERIFIED: token=%s, suite=%s, session=%s",
        token_dict["token_id"][:8], expected_suite_id[:8],
        token_dict.get("session_id", "?")[:8],
    )
    return PresenceVerificationResult(valid=True)


def revoke_presence_token(token_id: str) -> None:
    """Revoke a presence token."""
    _revoked_presence_tokens.add(token_id)
    logger.info("Presence token revoked: %s", token_id[:8])


def clear_presence_revocations() -> None:
    """Clear in-memory revocation set. For testing only."""
    _revoked_presence_tokens.clear()
