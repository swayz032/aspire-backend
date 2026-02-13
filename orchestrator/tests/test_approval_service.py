"""Tests for Approval Binding Service — Approve-then-swap defense (Law #4, W4-01/02).

Covers:
- Payload hash computation (canonical JSON, deterministic)
- Approval binding creation
- 7-check binding verification:
  1. payload_hash match (approve-then-swap defense)
  2. Not expired
  3. request_id not reused (replay defense)
  4. suite_id match
  5. office_id match
  6. policy_version match
  7. request_id match
- Adversarial scenarios (swap, replay, cross-tenant, policy downgrade)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aspire_orchestrator.services.approval_service import (
    CURRENT_POLICY_VERSION,
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    ApprovalBinding,
    ApprovalBindingError,
    ApprovalBindingResult,
    clear_used_request_ids,
    compute_payload_hash,
    create_approval_binding,
    verify_approval_binding,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SUITE_A = "00000000-0000-0000-0000-000000000001"
SUITE_B = "00000000-0000-0000-0000-000000000002"
OFFICE_A = "00000000-0000-0000-0000-000000000011"
OFFICE_B = "00000000-0000-0000-0000-000000000012"
APPROVER = "user-approver-001"


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset used request IDs before each test."""
    clear_used_request_ids()
    yield
    clear_used_request_ids()


def _make_payload(**overrides: object) -> dict:
    return {
        "task_type": "invoice.create",
        "parameters": {"amount": 500, "currency": "USD", "customer": "acme-corp"},
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        **overrides,
    }


def _make_binding(
    *,
    payload: dict | None = None,
    request_id: str | None = None,
    approver_id: str = APPROVER,
    expiry_seconds: int = DEFAULT_APPROVAL_EXPIRY_SECONDS,
    approved_at: datetime | None = None,
) -> ApprovalBinding:
    """Create a valid binding for testing."""
    p = payload or _make_payload()
    rid = request_id or str(uuid.uuid4())
    return create_approval_binding(
        suite_id=SUITE_A,
        office_id=OFFICE_A,
        request_id=rid,
        payload=p,
        approver_id=approver_id,
        expiry_seconds=expiry_seconds,
    )


# ===========================================================================
# Payload Hash Tests
# ===========================================================================


class TestPayloadHash:
    def test_deterministic(self) -> None:
        p = _make_payload()
        h1 = compute_payload_hash(p)
        h2 = compute_payload_hash(p)
        assert h1 == h2

    def test_sha256_hex_format(self) -> None:
        h = compute_payload_hash(_make_payload())
        assert len(h) == 64
        int(h, 16)  # Must be valid hex

    def test_different_payload_different_hash(self) -> None:
        h1 = compute_payload_hash(_make_payload(amount=500))
        h2 = compute_payload_hash(_make_payload(amount=5000))
        assert h1 != h2

    def test_key_order_irrelevant(self) -> None:
        """Canonical JSON sorts keys — order shouldn't matter."""
        p1 = {"z": 1, "a": 2, "m": 3}
        p2 = {"a": 2, "m": 3, "z": 1}
        assert compute_payload_hash(p1) == compute_payload_hash(p2)

    def test_empty_payload(self) -> None:
        h = compute_payload_hash({})
        assert len(h) == 64


# ===========================================================================
# Approval Binding Creation Tests
# ===========================================================================


class TestCreateBinding:
    def test_creates_valid_binding(self) -> None:
        binding = _make_binding()
        assert binding.suite_id == SUITE_A
        assert binding.office_id == OFFICE_A
        assert binding.policy_version == CURRENT_POLICY_VERSION
        assert binding.approver_id == APPROVER
        assert binding.expires_at > binding.approved_at

    def test_expiry_is_correct(self) -> None:
        binding = _make_binding(expiry_seconds=120)
        delta = (binding.expires_at - binding.approved_at).total_seconds()
        assert abs(delta - 120) < 1  # Within 1 second tolerance

    def test_payload_hash_matches_payload(self) -> None:
        p = _make_payload()
        binding = _make_binding(payload=p)
        assert binding.payload_hash == compute_payload_hash(p)


# ===========================================================================
# Verification: CHECK 1 — payload_hash match (approve-then-swap defense)
# ===========================================================================


class TestPayloadHashVerification:
    def test_matching_hash_passes(self) -> None:
        p = _make_payload()
        rid = str(uuid.uuid4())
        binding = _make_binding(payload=p, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is True

    def test_swapped_payload_rejected(self) -> None:
        """Approve-then-swap: approved payload A, execute payload B."""
        p_approved = _make_payload(amount=500)
        p_swapped = _make_payload(amount=50000)  # Attacker swaps to higher amount
        rid = str(uuid.uuid4())
        binding = _make_binding(payload=p_approved, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p_swapped),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.PAYLOAD_HASH_MISMATCH
        assert "approve-then-swap" in (result.error_message or "").lower()

    def test_added_field_changes_hash(self) -> None:
        """Adding a field to the payload after approval invalidates the hash."""
        p_original = _make_payload()
        p_tampered = {**p_original, "hidden_recipient": "attacker@evil.com"}
        rid = str(uuid.uuid4())
        binding = _make_binding(payload=p_original, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p_tampered),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.PAYLOAD_HASH_MISMATCH


# ===========================================================================
# Verification: CHECK 2 — Expiry
# ===========================================================================


class TestExpiryVerification:
    def test_fresh_approval_accepted(self) -> None:
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid, expiry_seconds=300)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is True

    def test_expired_approval_rejected(self) -> None:
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid, expiry_seconds=300)
        # Time travel past expiry
        future = datetime.now(timezone.utc) + timedelta(seconds=600)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
            now=future,
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.APPROVAL_EXPIRED


# ===========================================================================
# Verification: CHECK 3 — request_id reuse (replay defense)
# ===========================================================================


class TestReplayDefense:
    def test_first_use_accepted(self) -> None:
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is True

    def test_second_use_rejected(self) -> None:
        """Same request_id used twice — replay attack."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)

        # First use: accepted
        result1 = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result1.valid is True

        # Second use: replay detected
        result2 = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result2.valid is False
        assert result2.error == ApprovalBindingError.REQUEST_ID_REUSED

    def test_different_request_id_accepted(self) -> None:
        """Different request_ids should both be accepted."""
        p = _make_payload()
        rid1 = str(uuid.uuid4())
        rid2 = str(uuid.uuid4())

        binding1 = _make_binding(payload=p, request_id=rid1)
        binding2 = _make_binding(payload=p, request_id=rid2)

        r1 = verify_approval_binding(
            binding1,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid1,
            expected_payload_hash=compute_payload_hash(p),
        )
        r2 = verify_approval_binding(
            binding2,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid2,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert r1.valid is True
        assert r2.valid is True


# ===========================================================================
# Verification: CHECK 4, 5 — Tenant isolation
# ===========================================================================


class TestTenantIsolation:
    def test_suite_mismatch_rejected(self) -> None:
        """Cross-tenant attack: binding from suite A, verify against suite B."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_B,  # Different suite!
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.SUITE_MISMATCH

    def test_office_mismatch_rejected(self) -> None:
        """Cross-office attack: binding from office A, verify against office B."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_B,  # Different office!
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.OFFICE_MISMATCH


# ===========================================================================
# Verification: CHECK 6 — Policy version mismatch
# ===========================================================================


class TestPolicyVersionVerification:
    def test_current_version_accepted(self) -> None:
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        assert binding.policy_version == CURRENT_POLICY_VERSION
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is True

    def test_old_policy_version_rejected(self) -> None:
        """Policy downgrade attack: approval from old policy version."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        # Tamper the policy version
        old_binding = ApprovalBinding(
            suite_id=binding.suite_id,
            office_id=binding.office_id,
            request_id=binding.request_id,
            payload_hash=binding.payload_hash,
            policy_version="0.9.0",  # Old version
            approved_at=binding.approved_at,
            expires_at=binding.expires_at,
            approver_id=binding.approver_id,
        )
        result = verify_approval_binding(
            old_binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.POLICY_VERSION_MISMATCH


# ===========================================================================
# Verification: CHECK 7 — request_id mismatch
# ===========================================================================


class TestRequestIdMismatch:
    def test_matching_request_id_passes(self) -> None:
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is True

    def test_different_request_id_rejected(self) -> None:
        """Binding for request A, verify against request B."""
        rid_a = str(uuid.uuid4())
        rid_b = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid_a)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid_b,  # Different request!
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is False


# ===========================================================================
# Adversarial Scenarios
# ===========================================================================


class TestAdversarialScenarios:
    def test_full_approve_then_swap_attack(self) -> None:
        """Full scenario: user approves $500, attacker swaps to $50,000."""
        p_legit = {"task_type": "payment.send", "amount": 500, "to": "vendor"}
        p_evil = {"task_type": "payment.send", "amount": 50000, "to": "attacker"}
        rid = str(uuid.uuid4())

        # Legitimate approval for $500
        binding = create_approval_binding(
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            request_id=rid,
            payload=p_legit,
            approver_id=APPROVER,
        )

        # Attacker tries to execute with $50,000 payload
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p_evil),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.PAYLOAD_HASH_MISMATCH

    def test_cross_tenant_approval_replay(self) -> None:
        """Attacker captures approval from suite A, replays against suite B."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)

        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_B,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.SUITE_MISMATCH

    def test_timing_attack_expired_then_replay(self) -> None:
        """Approval expires, attacker re-submits later."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid, expiry_seconds=60)

        # 2 minutes later — expired
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        result = verify_approval_binding(
            binding,
            expected_suite_id=SUITE_A,
            expected_office_id=OFFICE_A,
            expected_request_id=rid,
            expected_payload_hash=compute_payload_hash(p),
            now=future,
        )
        assert result.valid is False
        assert result.error == ApprovalBindingError.APPROVAL_EXPIRED

    def test_rapid_fire_replay(self) -> None:
        """5 rapid attempts with same request_id — only first succeeds."""
        rid = str(uuid.uuid4())
        p = _make_payload()
        binding = _make_binding(payload=p, request_id=rid)
        ph = compute_payload_hash(p)

        results = []
        for _ in range(5):
            r = verify_approval_binding(
                binding,
                expected_suite_id=SUITE_A,
                expected_office_id=OFFICE_A,
                expected_request_id=rid,
                expected_payload_hash=ph,
            )
            results.append(r.valid)

        assert results[0] is True
        assert all(r is False for r in results[1:])
