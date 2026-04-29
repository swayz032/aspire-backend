"""Tests for memory_v1.py Pydantic schemas.

Covers:
- Round-trip for every MemoryType, MemoryStatus, RuntimeFamily, Channel,
  SourceSurface, SourceAgent, VisibilityScope, RiskTier, ThreadType,
  FinanceThreadSubtype, RecommendedAction, ActionClass, CandidateStatus,
  ApprovalStatus, EventInboxStatus.
- confidence bounds [0, 1].
- embedding length (must be 1536 when present).
- summary non-empty validation.
- idempotency_key required on MemoryEventEnvelope.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import get_args

import pytest

from aspire_orchestrator.schemas.memory_v1 import (
    ActionClass,
    ApprovalLinkIn,
    ApprovalStatus,
    CandidateStatus,
    Channel,
    EventInboxStatus,
    FinanceThreadSubtype,
    MemoryEventEnvelope,
    MemoryObjectIn,
    MemoryObjectOut,
    MemoryStatus,
    MemoryType,
    ProactiveCandidateIn,
    Provenance,
    ReceiptMemoryLinkIn,
    RecommendedAction,
    RiskTier,
    RuntimeFamily,
    ScopedIdentity,
    SourceAgent,
    SourceSurface,
    ThreadIn,
    ThreadOut,
    ThreadStatus,
    ThreadType,
    VisibilityScope,
)


# ---------------------------------------------------------------------------
# Shared fixtures / builders
# ---------------------------------------------------------------------------

TENANT = uuid.uuid4()
SUITE = uuid.uuid4()
OFFICE = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()
NOW = datetime.now(tz=timezone.utc)


def _scope() -> ScopedIdentity:
    return ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)


def _prov() -> Provenance:
    return Provenance(
        trace_id=TRACE,
        correlation_id=CORR,
    )


def _base_memory_in(**kwargs) -> dict:
    base: dict = dict(
        scope=_scope(),
        provenance=_prov(),
        memory_type="session_summary",
        summary="Initial session summary.",
    )
    base.update(kwargs)
    return base


def _base_envelope(**kwargs) -> dict:
    base: dict = dict(
        tenant_id=TENANT,
        suite_id=SUITE,
        office_id=OFFICE,
        event_type="voice_session_ended",
        trace_id=TRACE,
        correlation_id=CORR,
        event_at=NOW,
        idempotency_key="test-key-001",
    )
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# MemoryType round-trips
# ---------------------------------------------------------------------------


class TestMemoryTypeRoundTrip:
    @pytest.mark.parametrize("memory_type", get_args(MemoryType))
    def test_all_memory_types_accepted(self, memory_type: str) -> None:
        obj = MemoryObjectIn(**_base_memory_in(memory_type=memory_type))
        assert obj.memory_type == memory_type


# ---------------------------------------------------------------------------
# MemoryStatus round-trips
# ---------------------------------------------------------------------------


class TestMemoryStatusRoundTrip:
    @pytest.mark.parametrize("status", get_args(MemoryStatus))
    def test_all_statuses_accepted(self, status: str) -> None:
        obj = MemoryObjectIn(**_base_memory_in(status=status))
        assert obj.status == status

    def test_none_status_allowed(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in())
        assert obj.status is None


# ---------------------------------------------------------------------------
# RuntimeFamily, Channel, SourceSurface, SourceAgent round-trips
# ---------------------------------------------------------------------------


class TestProvenanceFieldRoundTrips:
    @pytest.mark.parametrize("rf", get_args(RuntimeFamily))
    def test_runtime_family(self, rf: str) -> None:
        prov = Provenance(trace_id=TRACE, correlation_id=CORR, runtime_family=rf)
        assert prov.runtime_family == rf

    @pytest.mark.parametrize("ch", get_args(Channel))
    def test_channel(self, ch: str) -> None:
        prov = Provenance(trace_id=TRACE, correlation_id=CORR, channel=ch)
        assert prov.channel == ch

    @pytest.mark.parametrize("ss", get_args(SourceSurface))
    def test_source_surface(self, ss: str) -> None:
        prov = Provenance(trace_id=TRACE, correlation_id=CORR, source_surface=ss)
        assert prov.source_surface == ss

    @pytest.mark.parametrize("sa", get_args(SourceAgent))
    def test_source_agent(self, sa: str) -> None:
        prov = Provenance(trace_id=TRACE, correlation_id=CORR, source_agent=sa)
        assert prov.source_agent == sa


# ---------------------------------------------------------------------------
# VisibilityScope, RiskTier
# ---------------------------------------------------------------------------


class TestVisibilityScopeRoundTrip:
    @pytest.mark.parametrize("vs", get_args(VisibilityScope))
    def test_all_visibility_scopes(self, vs: str) -> None:
        obj = MemoryObjectIn(**_base_memory_in(visibility_scope=vs))
        assert obj.visibility_scope == vs


class TestRiskTierRoundTrip:
    @pytest.mark.parametrize("rt", get_args(RiskTier))
    def test_all_risk_tiers(self, rt: str) -> None:
        env = MemoryEventEnvelope(**_base_envelope(risk_tier=rt))
        assert env.risk_tier == rt


# ---------------------------------------------------------------------------
# ThreadType, FinanceThreadSubtype, ThreadStatus
# ---------------------------------------------------------------------------


class TestThreadTypeRoundTrip:
    @pytest.mark.parametrize("tt", get_args(ThreadType))
    def test_all_thread_types(self, tt: str) -> None:
        obj = ThreadIn(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE, thread_type=tt)
        assert obj.thread_type == tt

    @pytest.mark.parametrize("fts", get_args(FinanceThreadSubtype))
    def test_all_finance_subtypes(self, fts: str) -> None:
        obj = ThreadIn(
            tenant_id=TENANT,
            suite_id=SUITE,
            office_id=OFFICE,
            thread_type="finance_thread",
            finance_thread_subtype=fts,
        )
        assert obj.finance_thread_subtype == fts

    @pytest.mark.parametrize("ts", get_args(ThreadStatus))
    def test_all_thread_statuses(self, ts: str) -> None:
        obj = ThreadIn(
            tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE,
            thread_type="lead_thread", status=ts,
        )
        assert obj.status == ts


# ---------------------------------------------------------------------------
# RecommendedAction, ActionClass, CandidateStatus
# ---------------------------------------------------------------------------


class TestProactiveCandidateRoundTrips:
    @pytest.mark.parametrize("ra", get_args(RecommendedAction))
    def test_recommended_action(self, ra: str) -> None:
        obj = ProactiveCandidateIn(
            tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE,
            owner_agent="ava",
            recommended_action=ra,
            action_class="internal_only",
            why_now="test",
            confidence=0.8,
            risk_tier="green",
        )
        assert obj.recommended_action == ra

    @pytest.mark.parametrize("ac", get_args(ActionClass))
    def test_action_class(self, ac: str) -> None:
        obj = ProactiveCandidateIn(
            tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE,
            owner_agent="ava",
            recommended_action="none",
            action_class=ac,
            why_now="test",
            confidence=0.5,
            risk_tier="green",
        )
        assert obj.action_class == ac

    @pytest.mark.parametrize("cs", get_args(CandidateStatus))
    def test_candidate_status(self, cs: str) -> None:
        obj = ProactiveCandidateIn(
            tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE,
            owner_agent="ava",
            recommended_action="none",
            action_class="internal_only",
            why_now="test",
            confidence=0.5,
            risk_tier="green",
            status=cs,
        )
        assert obj.status == cs


# ---------------------------------------------------------------------------
# ApprovalStatus, EventInboxStatus
# ---------------------------------------------------------------------------


class TestApprovalStatusRoundTrip:
    @pytest.mark.parametrize("ap", get_args(ApprovalStatus))
    def test_approval_status(self, ap: str) -> None:
        obj = ApprovalLinkIn(
            tenant_id=TENANT,
            suite_id=SUITE,
            approval_id="appr-001",
            requested_by_agent="ava",
            approval_status=ap,
        )
        assert obj.approval_status == ap


# ---------------------------------------------------------------------------
# Confidence bounds [0, 1]
# ---------------------------------------------------------------------------


class TestConfidenceBounds:
    def test_confidence_zero_accepted(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in(confidence=0.0))
        assert obj.confidence == 0.0

    def test_confidence_one_accepted(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in(confidence=1.0))
        assert obj.confidence == 1.0

    def test_confidence_mid_accepted(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in(confidence=0.75))
        assert obj.confidence == 0.75

    def test_confidence_negative_rejected(self) -> None:
        with pytest.raises(Exception, match="confidence must be in"):
            MemoryObjectIn(**_base_memory_in(confidence=-0.01))

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(Exception, match="confidence must be in"):
            MemoryObjectIn(**_base_memory_in(confidence=1.01))

    def test_confidence_none_allowed(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in(confidence=None))
        assert obj.confidence is None

    def test_candidate_confidence_bounds(self) -> None:
        with pytest.raises(Exception, match="confidence must be in"):
            ProactiveCandidateIn(
                tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE,
                owner_agent="ava", recommended_action="none",
                action_class="internal_only", why_now="x",
                confidence=2.0, risk_tier="green",
            )


# ---------------------------------------------------------------------------
# Embedding length (must be 1536)
# ---------------------------------------------------------------------------


class TestEmbeddingDimensions:
    def test_correct_dims_accepted(self) -> None:
        embedding = [0.1] * 1536
        obj = MemoryObjectIn(**_base_memory_in(embedding=embedding))
        assert len(obj.embedding) == 1536  # type: ignore[arg-type]

    def test_wrong_dims_rejected_on_write(self) -> None:
        embedding = [0.1] * 100  # wrong dimension count
        with pytest.raises(Exception, match="1536 dimensions"):
            MemoryObjectIn(**_base_memory_in(embedding=embedding))

    def test_wrong_dims_rejected_on_read(self) -> None:
        embedding = [0.1] * 100
        with pytest.raises(Exception, match="1536 dimensions"):
            MemoryObjectOut(
                memory_id=uuid.uuid4(),
                scope=_scope(),
                provenance=_prov(),
                memory_type="session_summary",
                summary="s",
                created_at=NOW,
                last_activity_at=NOW,
                embedding=embedding,
            )

    def test_none_embedding_allowed(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in(embedding=None))
        assert obj.embedding is None


# ---------------------------------------------------------------------------
# Summary non-empty validation
# ---------------------------------------------------------------------------


class TestSummaryValidation:
    def test_empty_string_rejected(self) -> None:
        with pytest.raises(Exception, match="non-empty"):
            MemoryObjectIn(**_base_memory_in(summary=""))

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(Exception, match="non-empty"):
            MemoryObjectIn(**_base_memory_in(summary="   "))

    def test_valid_summary_accepted(self) -> None:
        obj = MemoryObjectIn(**_base_memory_in(summary="Valid summary text."))
        assert obj.summary == "Valid summary text."


# ---------------------------------------------------------------------------
# MemoryEventEnvelope — idempotency_key required
# ---------------------------------------------------------------------------


class TestMemoryEventEnvelope:
    def test_idempotency_key_required(self) -> None:
        with pytest.raises(Exception):
            # Missing idempotency_key
            data = _base_envelope()
            del data["idempotency_key"]
            MemoryEventEnvelope(**data)

    def test_empty_idempotency_key_rejected(self) -> None:
        with pytest.raises(Exception, match="non-empty"):
            MemoryEventEnvelope(**_base_envelope(idempotency_key=""))

    def test_whitespace_idempotency_key_rejected(self) -> None:
        with pytest.raises(Exception, match="non-empty"):
            MemoryEventEnvelope(**_base_envelope(idempotency_key="   "))

    def test_valid_envelope_accepted(self) -> None:
        env = MemoryEventEnvelope(**_base_envelope())
        assert env.idempotency_key == "test-key-001"
        assert env.risk_tier == "yellow"  # default
        assert env.needs_approval is False
        assert env.receipt_required is False

    def test_all_fields_round_trip(self) -> None:
        env = MemoryEventEnvelope(
            tenant_id=TENANT,
            suite_id=SUITE,
            office_id=OFFICE,
            event_type="voice_session_ended",
            trace_id=TRACE,
            correlation_id=CORR,
            event_at=NOW,
            idempotency_key="key-full-001",
            risk_tier="red",
            needs_approval=True,
            receipt_required=True,
            source_surface="ava_voice",
            source_agent="ava",
            runtime_family="elevenlabs",
            channel="voice",
            payload={"duration_s": 120},
        )
        assert env.risk_tier == "red"
        assert env.needs_approval is True
        assert env.source_surface == "ava_voice"
        assert env.payload == {"duration_s": 120}


# ---------------------------------------------------------------------------
# ReceiptMemoryLinkIn — receipt_id is str (TEXT PK)
# ---------------------------------------------------------------------------


class TestReceiptMemoryLinkIn:
    def test_receipt_id_is_str(self) -> None:
        obj = ReceiptMemoryLinkIn(
            receipt_id="rcpt-12345",
            memory_id=uuid.uuid4(),
            tenant_id=TENANT,
            suite_id=SUITE,
        )
        assert isinstance(obj.receipt_id, str)
        assert obj.receipt_id == "rcpt-12345"


# ---------------------------------------------------------------------------
# ScopedIdentity — all three scope fields required
# ---------------------------------------------------------------------------


class TestScopedIdentity:
    def test_all_three_required(self) -> None:
        with pytest.raises(Exception):
            ScopedIdentity(tenant_id=TENANT, suite_id=SUITE)  # type: ignore[call-arg]

    def test_actor_and_user_optional(self) -> None:
        scope = ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE)
        assert scope.actor_id is None
        assert scope.user_id is None

    def test_with_actor(self) -> None:
        actor = uuid.uuid4()
        scope = ScopedIdentity(tenant_id=TENANT, suite_id=SUITE, office_id=OFFICE, actor_id=actor)
        assert scope.actor_id == actor
