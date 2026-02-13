"""Tests for Capability Token Service — 6-Check Validation (Law #5, TC-05).

Covers:
- Token minting with HMAC-SHA256
- All 6 validation checks individually
- Adversarial scenarios (replay, tamper, cross-tenant)
- TTL enforcement (Law #5: <60s)
- Revocation
- Edge cases (clock skew, missing fields, wildcard scopes)
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aspire_orchestrator.services.token_service import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    MAX_TOKEN_TTL_SECONDS,
    TokenValidationError,
    clear_revocations,
    compute_token_hash,
    mint_token,
    revoke_token,
    validate_token,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_revocations():
    """Clear revocation set before each test."""
    clear_revocations()
    yield
    clear_revocations()


@pytest.fixture
def suite_id_a() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


@pytest.fixture
def suite_id_b() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000002"))


@pytest.fixture
def office_id_a() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000011"))


@pytest.fixture
def office_id_b() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000022"))


@pytest.fixture
def valid_token(suite_id_a: str, office_id_a: str) -> dict:
    """Mint a valid token for testing."""
    return mint_token(
        suite_id=suite_id_a,
        office_id=office_id_a,
        tool="stripe.invoice.create",
        scopes=["invoice.write"],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )


# ===========================================================================
# Token Minting Tests
# ===========================================================================


class TestMintToken:
    def test_mint_produces_all_fields(self, suite_id_a: str, office_id_a: str) -> None:
        token = mint_token(
            suite_id=suite_id_a,
            office_id=office_id_a,
            tool="stripe.invoice.create",
            scopes=["invoice.write"],
            correlation_id=str(uuid.uuid4()),
        )
        required_fields = [
            "token_id", "suite_id", "office_id", "tool", "scopes",
            "issued_at", "expires_at", "signature", "revoked", "correlation_id",
        ]
        for field in required_fields:
            assert field in token, f"Missing field: {field}"

    def test_mint_signature_is_hex(self, valid_token: dict) -> None:
        sig = valid_token["signature"]
        assert len(sig) == 64  # SHA-256 hex = 64 chars
        int(sig, 16)  # Must be valid hex

    def test_mint_ttl_within_limit(self, valid_token: dict) -> None:
        issued = datetime.fromisoformat(valid_token["issued_at"])
        expires = datetime.fromisoformat(valid_token["expires_at"])
        ttl = (expires - issued).total_seconds()
        assert 0 < ttl <= MAX_TOKEN_TTL_SECONDS

    def test_mint_scopes_sorted(self, suite_id_a: str, office_id_a: str) -> None:
        token = mint_token(
            suite_id=suite_id_a,
            office_id=office_id_a,
            tool="test.tool.multi",
            scopes=["z.write", "a.read", "m.execute"],
            correlation_id=str(uuid.uuid4()),
        )
        assert token["scopes"] == ["a.read", "m.execute", "z.write"]

    def test_mint_rejects_ttl_over_60s(self, suite_id_a: str, office_id_a: str) -> None:
        with pytest.raises(ValueError, match="Law #5"):
            mint_token(
                suite_id=suite_id_a,
                office_id=office_id_a,
                tool="test.tool.slow",
                scopes=["test.read"],
                correlation_id=str(uuid.uuid4()),
                ttl_seconds=60,
            )

    def test_mint_rejects_zero_ttl(self, suite_id_a: str, office_id_a: str) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            mint_token(
                suite_id=suite_id_a,
                office_id=office_id_a,
                tool="test.tool.zero",
                scopes=["test.read"],
                correlation_id=str(uuid.uuid4()),
                ttl_seconds=0,
            )

    def test_mint_rejects_negative_ttl(self, suite_id_a: str, office_id_a: str) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            mint_token(
                suite_id=suite_id_a,
                office_id=office_id_a,
                tool="test.tool.neg",
                scopes=["test.read"],
                correlation_id=str(uuid.uuid4()),
                ttl_seconds=-5,
            )

    def test_mint_revoked_defaults_false(self, valid_token: dict) -> None:
        assert valid_token["revoked"] is False

    def test_mint_unique_token_ids(self, suite_id_a: str, office_id_a: str) -> None:
        tokens = [
            mint_token(
                suite_id=suite_id_a,
                office_id=office_id_a,
                tool="test.tool.unique",
                scopes=["test.read"],
                correlation_id=str(uuid.uuid4()),
            )
            for _ in range(10)
        ]
        token_ids = {t["token_id"] for t in tokens}
        assert len(token_ids) == 10  # All unique


# ===========================================================================
# Token Hash Tests
# ===========================================================================


class TestTokenHash:
    def test_hash_is_deterministic(self, valid_token: dict) -> None:
        h1 = compute_token_hash(valid_token)
        h2 = compute_token_hash(valid_token)
        assert h1 == h2

    def test_hash_changes_on_modification(self, valid_token: dict) -> None:
        h1 = compute_token_hash(valid_token)
        tampered = copy.deepcopy(valid_token)
        tampered["scopes"] = ["admin.all"]
        h2 = compute_token_hash(tampered)
        assert h1 != h2

    def test_hash_is_sha256_hex(self, valid_token: dict) -> None:
        h = compute_token_hash(valid_token)
        assert len(h) == 64
        int(h, 16)


# ===========================================================================
# 6-Check Validation Tests
# ===========================================================================


class TestCheck1SignatureValidation:
    """CHECK 1: Signature valid (HMAC-SHA256 verification)."""

    def test_valid_signature_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is True
        assert result.checks_passed == 6

    def test_tampered_scopes_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Adversarial: tamper with scopes after signing."""
        tampered = copy.deepcopy(valid_token)
        tampered["scopes"] = ["admin.all"]
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="admin.all",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID
        assert result.checks_passed == 0

    def test_tampered_tool_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Adversarial: change tool after signing."""
        tampered = copy.deepcopy(valid_token)
        tampered["tool"] = "admin.system.delete"
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID

    def test_tampered_suite_id_fails(
        self, valid_token: dict, suite_id_b: str, office_id_a: str,
    ) -> None:
        """Adversarial: change suite_id after signing (cross-tenant attack)."""
        tampered = copy.deepcopy(valid_token)
        tampered["suite_id"] = suite_id_b
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_b,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID

    def test_tampered_expires_at_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Adversarial: extend expiry after signing."""
        tampered = copy.deepcopy(valid_token)
        new_expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        tampered["expires_at"] = new_expiry.isoformat()
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID

    def test_forged_signature_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Adversarial: completely forged signature."""
        tampered = copy.deepcopy(valid_token)
        tampered["signature"] = "a" * 64
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID


class TestCheck2ExpiryValidation:
    """CHECK 2: Not expired (current_time < expires_at)."""

    def test_active_token_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is True

    def test_expired_token_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """TC-05: Expired token rejection."""
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
            now=future,
        )
        assert result.valid is False
        assert result.error == TokenValidationError.TOKEN_EXPIRED
        assert result.checks_passed == 1  # Signature passed, expiry failed

    def test_just_expired_within_skew_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Token expired 1s ago but within clock skew tolerance."""
        expires_at = datetime.fromisoformat(valid_token["expires_at"])
        just_after = expires_at + timedelta(seconds=1)
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
            now=just_after,
        )
        assert result.valid is True  # Within 2s clock skew tolerance

    def test_expired_beyond_skew_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Token expired 5s ago — beyond clock skew tolerance."""
        expires_at = datetime.fromisoformat(valid_token["expires_at"])
        well_after = expires_at + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS + 1)
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
            now=well_after,
        )
        assert result.valid is False
        assert result.error == TokenValidationError.TOKEN_EXPIRED


class TestCheck3RevocationValidation:
    """CHECK 3: Not revoked (revoked = false)."""

    def test_unrevoked_token_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is True

    def test_revoked_via_service_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        revoke_token(valid_token["token_id"])
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.TOKEN_REVOKED
        assert result.checks_passed == 2  # Sig + expiry passed

    def test_revoked_flag_in_payload_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Token with revoked=True in payload is rejected."""
        token = copy.deepcopy(valid_token)
        token["revoked"] = True
        result = validate_token(
            token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.TOKEN_REVOKED


class TestCheck4ScopeValidation:
    """CHECK 4: Scope matches requested action."""

    def test_matching_scope_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is True

    def test_wrong_scope_fails(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="payment.transfer",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SCOPE_MISMATCH
        assert result.checks_passed == 3  # Sig + expiry + revocation passed

    def test_multi_scope_token(self, suite_id_a: str, office_id_a: str) -> None:
        """Token with multiple scopes allows any matching scope."""
        token = mint_token(
            suite_id=suite_id_a,
            office_id=office_id_a,
            tool="polaris.email.send",
            scopes=["email.send", "email.draft", "email.read"],
            correlation_id=str(uuid.uuid4()),
        )
        # All three scopes should pass
        for scope in ["email.send", "email.draft", "email.read"]:
            result = validate_token(
                token,
                expected_suite_id=suite_id_a,
                expected_office_id=office_id_a,
                required_scope=scope,
            )
            assert result.valid is True, f"Failed for scope: {scope}"


class TestCheck5SuiteIdValidation:
    """CHECK 5: suite_id matches request context (tenant isolation)."""

    def test_matching_suite_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is True

    def test_cross_tenant_fails(
        self, valid_token: dict, suite_id_b: str, office_id_a: str,
    ) -> None:
        """EVIL TEST: Use token from suite A in suite B context."""
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_b,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SUITE_MISMATCH
        assert result.checks_passed == 4  # Sig + expiry + revocation + scope passed


class TestCheck6OfficeIdValidation:
    """CHECK 6: office_id matches request context."""

    def test_matching_office_passes(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is True

    def test_wrong_office_fails(
        self, valid_token: dict, suite_id_a: str, office_id_b: str,
    ) -> None:
        """Token scoped to office A used in office B context."""
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_b,
            required_scope="invoice.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.OFFICE_MISMATCH
        assert result.checks_passed == 5  # All but office passed


# ===========================================================================
# Edge Cases
# ===========================================================================


class TestEdgeCases:
    def test_missing_field_returns_malformed(
        self, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            {"token_id": "abc"},
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="test.read",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.MALFORMED_TOKEN

    def test_empty_token_returns_malformed(
        self, suite_id_a: str, office_id_a: str,
    ) -> None:
        result = validate_token(
            {},
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="test.read",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.MALFORMED_TOKEN

    def test_checks_passed_count_accurate(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Successful validation reports exactly 6 checks passed."""
        result = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.checks_passed == 6


# ===========================================================================
# Adversarial / Evil Tests
# ===========================================================================


class TestAdversarialScenarios:
    def test_replay_attack_after_revocation(
        self, valid_token: dict, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Use a token, revoke it, then try to use it again."""
        # First use: valid
        r1 = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert r1.valid is True

        # Revoke
        revoke_token(valid_token["token_id"])

        # Replay: should fail
        r2 = validate_token(
            valid_token,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert r2.valid is False
        assert r2.error == TokenValidationError.TOKEN_REVOKED

    def test_privilege_escalation_via_scope_tamper(
        self, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Mint token with read scope, tamper to write scope."""
        token = mint_token(
            suite_id=suite_id_a,
            office_id=office_id_a,
            tool="receipts.read.all",
            scopes=["receipts.read"],
            correlation_id=str(uuid.uuid4()),
        )
        tampered = copy.deepcopy(token)
        tampered["scopes"] = ["receipts.read", "admin.write"]
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="admin.write",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID

    def test_cross_tenant_token_reuse(
        self, suite_id_a: str, suite_id_b: str, office_id_a: str,
    ) -> None:
        """Mint token for suite A, validate against suite B — MUST fail."""
        token = mint_token(
            suite_id=suite_id_a,
            office_id=office_id_a,
            tool="stripe.invoice.create",
            scopes=["invoice.write"],
            correlation_id=str(uuid.uuid4()),
        )
        result = validate_token(
            token,
            expected_suite_id=suite_id_b,
            expected_office_id=office_id_a,
            required_scope="invoice.write",
        )
        assert result.valid is False
        # This fails at CHECK 5 (suite mismatch), not CHECK 1 (signature)
        # because the token was legitimately signed — it's just being used
        # in the wrong tenant context
        assert result.error == TokenValidationError.SUITE_MISMATCH

    def test_token_from_different_signing_key(
        self, suite_id_a: str, office_id_a: str,
    ) -> None:
        """Token signed with a different key MUST fail signature check."""
        # Mint a legitimate token
        token = mint_token(
            suite_id=suite_id_a,
            office_id=office_id_a,
            tool="test.tool.key",
            scopes=["test.read"],
            correlation_id=str(uuid.uuid4()),
        )
        # Forge a different signature (as if signed with wrong key)
        tampered = copy.deepcopy(token)
        tampered["signature"] = "b" * 64
        result = validate_token(
            tampered,
            expected_suite_id=suite_id_a,
            expected_office_id=office_id_a,
            required_scope="test.read",
        )
        assert result.valid is False
        assert result.error == TokenValidationError.SIGNATURE_INVALID
