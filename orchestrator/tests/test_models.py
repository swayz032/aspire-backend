"""Tests for Pydantic models — validate against canonical schema examples."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aspire_orchestrator.models import (
    ActorType,
    ApprovalEvidence,
    ApprovalMethod,
    ApprovalStatus,
    AspireError,
    AspireErrorCode,
    AvaOrchestratorRequest,
    AvaResult,
    AvaResultGovernance,
    AvaResultRisk,
    CapabilityToken,
    Outcome,
    Receipt,
    ReceiptType,
    RiskTier,
)


class TestRiskTier:
    def test_canonical_values(self) -> None:
        assert RiskTier.GREEN.value == "green"
        assert RiskTier.YELLOW.value == "yellow"
        assert RiskTier.RED.value == "red"

    def test_only_three_tiers(self) -> None:
        assert len(RiskTier) == 3


class TestOutcome:
    def test_canonical_values(self) -> None:
        assert Outcome.SUCCESS.value == "success"
        assert Outcome.DENIED.value == "denied"
        assert Outcome.FAILED.value == "failed"
        assert Outcome.TIMEOUT.value == "timeout"
        assert Outcome.PENDING.value == "pending"

    def test_five_outcomes(self) -> None:
        assert len(Outcome) == 5


class TestApprovalStatus:
    def test_rejected_not_denied(self) -> None:
        """Approval status uses 'rejected', receipt outcome uses 'denied'."""
        assert ApprovalStatus.REJECTED.value == "rejected"
        assert Outcome.DENIED.value == "denied"

    def test_canonical_values(self) -> None:
        assert len(ApprovalStatus) == 5


class TestReceiptType:
    def test_eight_receipt_types(self) -> None:
        """Per receipt_emission_rules.md, 8 receipt types + approval splits + presence types."""
        assert len(ReceiptType) == 11  # 8 base + approval_granted/denied + presence_verified/missing  # 8 types + approval splits into granted/denied


class TestReceipt:
    def test_valid_receipt(self, suite_id: str, office_id: str, correlation_id: str) -> None:
        receipt = Receipt(
            id=uuid.uuid4(),
            correlation_id=uuid.UUID(correlation_id),
            suite_id=uuid.UUID(suite_id),
            office_id=uuid.UUID(office_id),
            actor_type=ActorType.SYSTEM,
            actor_id="orchestrator",
            action_type="receipt.search",
            risk_tier=RiskTier.GREEN,
            tool_used="receipts.search",
            created_at=datetime.now(timezone.utc),
            outcome=Outcome.SUCCESS,
            receipt_hash="sha256_placeholder",
        )
        assert receipt.risk_tier == RiskTier.GREEN
        assert receipt.outcome == Outcome.SUCCESS
        assert receipt.actor_type == ActorType.SYSTEM

    def test_receipt_with_approval(self, suite_id: str, office_id: str, correlation_id: str) -> None:
        evidence = ApprovalEvidence(
            approver_id="user_123",
            approval_method=ApprovalMethod.VOICE_CONFIRM,
            approved_at=datetime.now(timezone.utc),
        )
        receipt = Receipt(
            id=uuid.uuid4(),
            correlation_id=uuid.UUID(correlation_id),
            suite_id=uuid.UUID(suite_id),
            office_id=uuid.UUID(office_id),
            actor_type=ActorType.USER,
            actor_id="user_123",
            action_type="email.send",
            risk_tier=RiskTier.YELLOW,
            tool_used="polaris.email.send",
            created_at=datetime.now(timezone.utc),
            approved_at=datetime.now(timezone.utc),
            approval_evidence=evidence,
            outcome=Outcome.SUCCESS,
            receipt_hash="sha256_placeholder",
        )
        assert receipt.approval_evidence is not None
        assert receipt.approval_evidence.approval_method == ApprovalMethod.VOICE_CONFIRM


class TestCapabilityToken:
    def test_valid_token(self, suite_id: str, office_id: str, correlation_id: str) -> None:
        now = datetime.now(timezone.utc)
        token = CapabilityToken(
            token_id=uuid.uuid4(),
            suite_id=uuid.UUID(suite_id),
            office_id=uuid.UUID(office_id),
            tool="stripe.invoice.create",
            scopes=["invoice.write"],
            issued_at=now,
            expires_at=now + timedelta(seconds=45),
            signature="hmac_sha256_test",
            correlation_id=uuid.UUID(correlation_id),
        )
        assert token.tool == "stripe.invoice.create"
        assert token.revoked is False

    def test_scopes_min_length(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(Exception):
            CapabilityToken(
                token_id=uuid.uuid4(),
                suite_id=uuid.uuid4(),
                office_id=uuid.uuid4(),
                tool="test.tool",
                scopes=[],  # Must have at least 1 scope
                issued_at=now,
                expires_at=now + timedelta(seconds=45),
                signature="sig",
                correlation_id=uuid.uuid4(),
            )

    def test_ttl_exceeds_60s_rejected(self) -> None:
        """Law #5: Token TTL >= 60s must be rejected."""
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="Law #5"):
            CapabilityToken(
                token_id=uuid.uuid4(),
                suite_id=uuid.uuid4(),
                office_id=uuid.uuid4(),
                tool="test.tool",
                scopes=["test.read"],
                issued_at=now,
                expires_at=now + timedelta(seconds=60),
                signature="sig",
                correlation_id=uuid.uuid4(),
            )

    def test_ttl_59s_accepted(self) -> None:
        """Law #5: Token TTL < 60s is valid."""
        now = datetime.now(timezone.utc)
        token = CapabilityToken(
            token_id=uuid.uuid4(),
            suite_id=uuid.uuid4(),
            office_id=uuid.uuid4(),
            tool="test.tool",
            scopes=["test.read"],
            issued_at=now,
            expires_at=now + timedelta(seconds=59),
            signature="sig",
            correlation_id=uuid.uuid4(),
        )
        ttl = (token.expires_at - token.issued_at).total_seconds()
        assert ttl == 59

    def test_expired_at_issue_rejected(self) -> None:
        """Token that's already expired at issue time must be rejected."""
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="already expired"):
            CapabilityToken(
                token_id=uuid.uuid4(),
                suite_id=uuid.uuid4(),
                office_id=uuid.uuid4(),
                tool="test.tool",
                scopes=["test.read"],
                issued_at=now,
                expires_at=now - timedelta(seconds=5),
                signature="sig",
                correlation_id=uuid.uuid4(),
            )


class TestAvaOrchestratorRequest:
    def test_valid_request(self, suite_id: str, office_id: str) -> None:
        req = AvaOrchestratorRequest(
            schema_version="1.0",
            suite_id=suite_id,
            office_id=office_id,
            request_id=str(uuid.uuid4()),
            correlation_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            task_type="invoice.create",
            payload={"customer": "acme_corp", "amount": 1500},
        )
        assert req.schema_version == "1.0"
        assert req.task_type == "invoice.create"

    def test_invalid_schema_version(self) -> None:
        with pytest.raises(Exception):
            AvaOrchestratorRequest(
                schema_version="2.0",  # Only "1.0" allowed
                suite_id="test",
                office_id="test",
                request_id="test",
                correlation_id="test",
                timestamp=datetime.now(timezone.utc),
                task_type="test",
                payload={},
            )


class TestAvaResult:
    def test_valid_result(self) -> None:
        result = AvaResult(
            schema_version="1.0",
            request_id=str(uuid.uuid4()),
            correlation_id=str(uuid.uuid4()),
            route={"skill_pack": "invoicing", "agent": "quinn"},
            risk=AvaResultRisk(tier=RiskTier.YELLOW),
            governance=AvaResultGovernance(
                approvals_required=["owner_approval"],
                presence_required=False,
                capability_token_required=True,
                receipt_ids=[str(uuid.uuid4())],
            ),
            plan={"actions": [{"tool": "stripe.invoice.create"}]},
        )
        assert result.risk.tier == RiskTier.YELLOW
        assert result.governance.capability_token_required is True


class TestAspireError:
    def test_error_response(self) -> None:
        err = AspireError(
            error=AspireErrorCode.APPROVAL_REQUIRED,
            message="Yellow-tier action requires user confirmation",
            correlation_id=str(uuid.uuid4()),
        )
        assert err.error == AspireErrorCode.APPROVAL_REQUIRED
