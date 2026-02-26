"""Tests for Presence Session Service — RED-tier authority verification (Law #4, W4-04/05).

Covers:
- Presence token minting (TTL enforcement, HMAC-SHA256 signing)
- 6-check verification:
  1. Signature valid (HMAC-SHA256)
  2. Not expired
  3. Not revoked
  4. payload_hash match (nonce binding)
  5. suite_id match
  6. office_id match
- Adversarial scenarios (forged, expired, cross-tenant, replay after revocation)
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aspire_orchestrator.services.presence_service import (
    DEFAULT_PRESENCE_TTL_SECONDS,
    MAX_PRESENCE_TTL_SECONDS,
    PresenceError,
    PresenceToken,
    PresenceVerificationResult,
    clear_presence_revocations,
    mint_presence_token,
    revoke_presence_token,
    verify_presence_token,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SUITE_A = "STE-0001"
SUITE_B = "STE-0002"
OFFICE_A = "OFF-0001"
OFFICE_B = "00000000-0000-0000-0000-000000000012"
SESSION_ID = "session-" + str(uuid.uuid4())[:8]
PAYLOAD_HASH = hashlib.sha256(b"test-payload").hexdigest()
DIFFERENT_PAYLOAD_HASH = hashlib.sha256(b"different-payload").hexdigest()


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset revocations before each test."""
    clear_presence_revocations()
    yield
    clear_presence_revocations()


# ===========================================================================
# Minting Tests
# ===========================================================================


class TestMintPresenceToken:
    def test_creates_valid_token(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        assert token.suite_id == SUITE_A
        assert token.office_id == OFFICE_A
        assert token.session_id == SESSION_ID
        assert token.payload_hash == PAYLOAD_HASH
        assert token.signature != ""
        assert len(token.token_id) > 0
        assert len(token.nonce) > 0

    def test_default_ttl(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        issued = datetime.fromisoformat(token.issued_at)
        expires = datetime.fromisoformat(token.expires_at)
        ttl = (expires - issued).total_seconds()
        assert abs(ttl - DEFAULT_PRESENCE_TTL_SECONDS) < 1

    def test_custom_ttl(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
            ttl_seconds=120,
        )
        issued = datetime.fromisoformat(token.issued_at)
        expires = datetime.fromisoformat(token.expires_at)
        ttl = (expires - issued).total_seconds()
        assert abs(ttl - 120) < 1

    def test_max_ttl_enforced(self) -> None:
        with pytest.raises(ValueError, match="exceeds maximum"):
            mint_presence_token(
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                session_id=SESSION_ID,
                payload_hash=PAYLOAD_HASH,
                ttl_seconds=MAX_PRESENCE_TTL_SECONDS + 1,
            )

    def test_zero_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            mint_presence_token(
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                session_id=SESSION_ID,
                payload_hash=PAYLOAD_HASH,
                ttl_seconds=0,
            )

    def test_negative_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            mint_presence_token(
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                session_id=SESSION_ID,
                payload_hash=PAYLOAD_HASH,
                ttl_seconds=-10,
            )

    def test_unique_token_ids(self) -> None:
        """Each mint produces a unique token_id."""
        tokens = [
            mint_presence_token(
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                session_id=SESSION_ID,
                payload_hash=PAYLOAD_HASH,
            )
            for _ in range(10)
        ]
        ids = [t.token_id for t in tokens]
        assert len(set(ids)) == 10

    def test_unique_nonces(self) -> None:
        """Each mint produces a unique nonce."""
        tokens = [
            mint_presence_token(
                suite_id=SUITE_A,
                office_id=OFFICE_A,
                session_id=SESSION_ID,
                payload_hash=PAYLOAD_HASH,
            )
            for _ in range(10)
        ]
        nonces = [t.nonce for t in tokens]
        assert len(set(nonces)) == 10


# ===========================================================================
# Verification: CHECK 1 — Signature
# ===========================================================================


class TestSignatureVerification:
    def test_valid_signature_passes(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is True

    def test_forged_signature_rejected(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        # Tamper the signature
        token_dict = {
            "token_id": token.token_id,
            "suite_id": token.suite_id,
            "office_id": token.office_id,
            "session_id": token.session_id,
            "nonce": token.nonce,
            "payload_hash": token.payload_hash,
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
            "signature": "f" * 64,  # Forged
        }
        result = verify_presence_token(
            token_dict,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is False
        assert result.error == PresenceError.SIGNATURE_INVALID

    def test_tampered_field_fails_signature(self) -> None:
        """Modify a field after signing — signature must fail."""
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        token_dict = {
            "token_id": token.token_id,
            "suite_id": SUITE_B,  # Tampered!
            "office_id": token.office_id,
            "session_id": token.session_id,
            "nonce": token.nonce,
            "payload_hash": token.payload_hash,
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
            "signature": token.signature,  # Original signature
        }
        result = verify_presence_token(
            token_dict,
            expected_suite_id=SUITE_B,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is False
        assert result.error == PresenceError.SIGNATURE_INVALID


# ===========================================================================
# Verification: CHECK 2 — Expiry
# ===========================================================================


class TestExpiryVerification:
    def test_fresh_token_accepted(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
            ttl_seconds=180,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is True

    def test_expired_token_rejected(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
            ttl_seconds=60,
        )
        # Time travel past expiry
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
            now=future,
        )
        assert result.valid is False
        assert result.error == PresenceError.TOKEN_EXPIRED

    def test_just_before_expiry_accepted(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
            ttl_seconds=60,
        )
        # 59 seconds later — still valid
        almost_expired = datetime.now(timezone.utc) + timedelta(seconds=59)
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
            now=almost_expired,
        )
        assert result.valid is True


# ===========================================================================
# Verification: CHECK 3 — Revocation
# ===========================================================================


class TestRevocationVerification:
    def test_unrevoked_token_accepted(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is True

    def test_revoked_token_rejected(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        revoke_presence_token(token.token_id)
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is False
        assert result.error == PresenceError.TOKEN_REVOKED

    def test_revoke_only_affects_target(self) -> None:
        """Revoking token A doesn't affect token B."""
        token_a = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        token_b = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        revoke_presence_token(token_a.token_id)

        result_a = verify_presence_token(
            token_a,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        result_b = verify_presence_token(
            token_b,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result_a.valid is False
        assert result_b.valid is True


# ===========================================================================
# Verification: CHECK 4 — Payload hash match (nonce binding)
# ===========================================================================


class TestPayloadHashVerification:
    def test_matching_hash_accepted(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is True

    def test_mismatched_hash_rejected(self) -> None:
        """Token bound to payload A, verify against payload B."""
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=DIFFERENT_PAYLOAD_HASH,
        )
        assert result.valid is False
        assert result.error == PresenceError.PAYLOAD_HASH_MISMATCH


# ===========================================================================
# Verification: CHECK 5, 6 — Tenant isolation
# ===========================================================================


class TestPresenceTenantIsolation:
    def test_suite_mismatch_rejected(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_B,  # Different suite
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is False
        # Signature check runs first — tampered suite_id changes the signature check
        # The error could be SIGNATURE_INVALID (since we didn't tamper the token's suite_id field)
        # or SUITE_MISMATCH (if the token dict has the original suite_id)
        assert result.error in (PresenceError.SUITE_MISMATCH, PresenceError.SIGNATURE_INVALID)

    def test_office_mismatch_rejected(self) -> None:
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_B,  # Different office
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is False
        assert result.error == PresenceError.OFFICE_MISMATCH


# ===========================================================================
# Dict Input Verification
# ===========================================================================


class TestDictInput:
    def test_dict_token_accepted(self) -> None:
        """Verify that dict representation works (for state serialization)."""
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        token_dict = {
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
        result = verify_presence_token(
            token_dict,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert result.valid is True


# ===========================================================================
# Adversarial Scenarios
# ===========================================================================


class TestAdversarialScenarios:
    def test_cross_tenant_presence_replay(self) -> None:
        """Attacker captures presence from suite A, replays in suite B context."""
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        # Token is valid for suite A
        r_legit = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert r_legit.valid is True

        # Attacker tries suite B — must fail
        r_attack = verify_presence_token(
            token,
            expected_suite_id=SUITE_B,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert r_attack.valid is False

    def test_revoke_then_replay(self) -> None:
        """Token used once, revoked, attacker replays."""
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        # First use — valid
        r1 = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert r1.valid is True

        # Revoke
        revoke_presence_token(token.token_id)

        # Replay — must fail
        r2 = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=PAYLOAD_HASH,
        )
        assert r2.valid is False
        assert r2.error == PresenceError.TOKEN_REVOKED

    def test_payload_swap_after_presence(self) -> None:
        """Presence bound to payload A, attacker swaps to payload B."""
        token = mint_presence_token(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            session_id=SESSION_ID,
            payload_hash=PAYLOAD_HASH,
        )
        result = verify_presence_token(
            token,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_payload_hash=DIFFERENT_PAYLOAD_HASH,  # Swapped!
        )
        assert result.valid is False
        assert result.error == PresenceError.PAYLOAD_HASH_MISMATCH
