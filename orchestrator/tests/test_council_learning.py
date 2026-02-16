"""Tests for Council Service + Learning Loop — Phase 2.5 Wave 5."""

import pytest

from aspire_orchestrator.services.council_service import (
    CouncilTrigger,
    ProposalStatus,
    adjudicate,
    clear_sessions,
    get_session,
    list_sessions,
    spawn_council,
    submit_proposal,
)
from aspire_orchestrator.services.learning_loop import (
    ChangeProposalStatus,
    LearningObjectStatus,
    LearningObjectType,
    approve_change,
    clear_stores,
    create_learning_object,
    get_learning_object,
    list_learning_objects,
    promote_object,
    propose_change,
    run_eval,
)
from aspire_orchestrator.services.receipt_store import clear_store, query_receipts


@pytest.fixture(autouse=True)
def _reset():
    """Reset all stores between tests."""
    clear_sessions()
    clear_stores()
    clear_store()
    yield
    clear_sessions()
    clear_stores()
    clear_store()


# =========================================================================
# Council Service Tests
# =========================================================================


class TestCouncilSpawn:
    """Test council session creation."""

    def test_spawn_creates_session(self):
        session, receipt = spawn_council(incident_id="inc-001")
        assert session.session_id
        assert session.incident_id == "inc-001"
        assert session.status == "open"
        assert len(session.members) == 3  # default: gpt, gemini, claude

    def test_spawn_receipt_has_required_fields(self):
        _, receipt = spawn_council(
            incident_id="inc-002",
            suite_id="suite-1",
            office_id="office-1",
        )
        assert receipt["receipt_type"] == "council.session.created"
        assert receipt["outcome"] == "success"
        assert receipt["correlation_id"] == "inc-002"
        assert receipt["suite_id"] == "suite-1"
        assert receipt["id"]
        assert receipt["created_at"]

    def test_spawn_with_custom_trigger(self):
        session, _ = spawn_council(
            incident_id="inc-003",
            trigger=CouncilTrigger.ROBOT_FAILURE,
        )
        assert session.trigger == CouncilTrigger.ROBOT_FAILURE

    def test_spawn_with_custom_members(self):
        session, _ = spawn_council(
            incident_id="inc-004",
            members=["gpt", "claude"],
        )
        assert session.members == ["gpt", "claude"]

    def test_session_retrievable(self):
        session, _ = spawn_council(incident_id="inc-005")
        retrieved = get_session(session.session_id)
        assert retrieved is not None
        assert retrieved.session_id == session.session_id


class TestCouncilProposal:
    """Test proposal submission."""

    def test_submit_proposal(self):
        session, _ = spawn_council(incident_id="inc-010")
        proposal, receipt = submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="Database connection pool exhausted",
            fix_plan="Increase pool size from 10 to 25",
            tests=["test_pool_scaling", "test_under_load"],
            confidence=0.85,
        )
        assert proposal.member == "gpt"
        assert proposal.confidence == 0.85
        assert proposal.status == ProposalStatus.SUBMITTED

    def test_proposal_receipt(self):
        session, _ = spawn_council(incident_id="inc-011")
        _, receipt = submit_proposal(
            session_id=session.session_id,
            member="gemini",
            root_cause="Cache miss storm",
            fix_plan="Add circuit breaker",
            confidence=0.7,
        )
        assert receipt["receipt_type"] == "council.member.proposal"
        assert receipt["actor_id"] == "council.gemini"

    def test_invalid_session_raises(self):
        with pytest.raises(ValueError, match="not found"):
            submit_proposal(
                session_id="nonexistent",
                member="gpt",
                root_cause="test",
                fix_plan="test",
            )


class TestCouncilAdjudicate:
    """Test council adjudication."""

    def test_adjudicate_picks_highest_confidence(self):
        session, _ = spawn_council(incident_id="inc-020")
        submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="cause-A",
            fix_plan="plan-A",
            confidence=0.6,
        )
        submit_proposal(
            session_id=session.session_id,
            member="gemini",
            root_cause="cause-B",
            fix_plan="plan-B",
            confidence=0.9,
        )
        submit_proposal(
            session_id=session.session_id,
            member="claude",
            root_cause="cause-C",
            fix_plan="plan-C",
            confidence=0.75,
        )

        decision, receipt = adjudicate(session_id=session.session_id)
        assert decision["selected_member"] == "gemini"
        assert decision["confidence"] == 0.9
        assert decision["total_proposals"] == 3

    def test_adjudicate_receipt(self):
        session, _ = spawn_council(incident_id="inc-021")
        submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="cause",
            fix_plan="plan",
            confidence=0.8,
        )
        _, receipt = adjudicate(session_id=session.session_id)
        assert receipt["receipt_type"] == "council.decision"
        assert receipt["actor_id"] == "ava.adjudicator"

    def test_adjudicate_sets_session_decided(self):
        session, _ = spawn_council(incident_id="inc-022")
        submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="cause",
            fix_plan="plan",
            confidence=0.5,
        )
        adjudicate(session_id=session.session_id)
        updated = get_session(session.session_id)
        assert updated.status == "decided"
        assert updated.decided_at is not None

    def test_adjudicate_no_proposals_raises(self):
        session, _ = spawn_council(incident_id="inc-023")
        with pytest.raises(ValueError, match="No proposals"):
            adjudicate(session_id=session.session_id)

    def test_adjudicate_marks_winner_and_losers(self):
        session, _ = spawn_council(incident_id="inc-024")
        p1, _ = submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="A",
            fix_plan="A",
            confidence=0.9,
        )
        p2, _ = submit_proposal(
            session_id=session.session_id,
            member="gemini",
            root_cause="B",
            fix_plan="B",
            confidence=0.3,
        )
        adjudicate(session_id=session.session_id)
        assert p1.status == ProposalStatus.ACCEPTED
        assert p2.status == ProposalStatus.REJECTED


class TestCouncilList:
    """Test session listing."""

    def test_list_sessions(self):
        spawn_council(incident_id="inc-030")
        spawn_council(incident_id="inc-031")
        sessions = list_sessions()
        assert len(sessions) == 2

    def test_list_sessions_filtered(self):
        session, _ = spawn_council(incident_id="inc-032")
        submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="c",
            fix_plan="p",
            confidence=0.5,
        )
        adjudicate(session_id=session.session_id)
        spawn_council(incident_id="inc-033")

        decided = list_sessions(status="decided")
        assert len(decided) == 1
        open_sessions = list_sessions(status="open")
        assert len(open_sessions) == 1


class TestCouncilReceiptPersistence:
    """Law #2: Council receipts must be persisted in receipt store."""

    def test_spawn_persists_receipt(self):
        session, receipt = spawn_council(
            incident_id="inc-persist-1",
            suite_id="suite-persist",
        )
        receipts = query_receipts(suite_id="suite-persist")
        spawn_receipts = [r for r in receipts if r.get("receipt_type") == "council.session.created"]
        assert len(spawn_receipts) == 1
        assert spawn_receipts[0]["id"] == receipt["id"]

    def test_submit_persists_receipt(self):
        session, _ = spawn_council(incident_id="inc-persist-2", suite_id="suite-p2")
        _, proposal_receipt = submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="test",
            fix_plan="test",
            confidence=0.5,
            suite_id="suite-p2",
        )
        receipts = query_receipts(suite_id="suite-p2")
        proposal_receipts = [r for r in receipts if r.get("receipt_type") == "council.member.proposal"]
        assert len(proposal_receipts) == 1
        assert proposal_receipts[0]["id"] == proposal_receipt["id"]

    def test_adjudicate_persists_receipt(self):
        session, _ = spawn_council(incident_id="inc-persist-3", suite_id="suite-p3")
        submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="cause",
            fix_plan="plan",
            confidence=0.8,
            suite_id="suite-p3",
        )
        _, adj_receipt = adjudicate(session_id=session.session_id, suite_id="suite-p3")
        receipts = query_receipts(suite_id="suite-p3")
        adj_receipts = [r for r in receipts if r.get("receipt_type") == "council.decision"]
        assert len(adj_receipts) == 1
        assert adj_receipts[0]["id"] == adj_receipt["id"]


class TestCouncilPostAdjudicationGuard:
    """Law #1: No proposals accepted after adjudication."""

    def test_submit_after_decided_raises(self):
        session, _ = spawn_council(incident_id="inc-guard-1")
        submit_proposal(
            session_id=session.session_id,
            member="gpt",
            root_cause="cause",
            fix_plan="plan",
            confidence=0.5,
        )
        adjudicate(session_id=session.session_id)

        with pytest.raises(ValueError, match="already decided"):
            submit_proposal(
                session_id=session.session_id,
                member="claude",
                root_cause="late cause",
                fix_plan="late plan",
                confidence=0.9,
            )


# =========================================================================
# Learning Loop Tests
# =========================================================================


class TestLearningObjectCreation:
    """Test learning object lifecycle."""

    def test_create_learning_object(self):
        obj, receipt = create_learning_object(
            incident_id="inc-100",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"section": "connection_pool", "update": "increase to 25"},
        )
        assert obj.object_id
        assert obj.incident_id == "inc-100"
        assert obj.object_type == LearningObjectType.RUNBOOK_UPDATE
        assert obj.status == LearningObjectStatus.DRAFT

    def test_create_receipt(self):
        _, receipt = create_learning_object(
            incident_id="inc-101",
            object_type=LearningObjectType.EVAL_CASE,
            content={"test": "pool_overflow"},
            suite_id="suite-1",
        )
        assert receipt["receipt_type"] == "learning.object.created"
        assert receipt["outcome"] == "success"
        assert receipt["suite_id"] == "suite-1"

    def test_retrievable(self):
        obj, _ = create_learning_object(
            incident_id="inc-102",
            object_type=LearningObjectType.ROBOT_ASSERTION,
            content={"assert": "pool_size_gt_10"},
        )
        retrieved = get_learning_object(obj.object_id)
        assert retrieved is not None
        assert retrieved.object_id == obj.object_id


class TestEvalRun:
    """Test eval case execution."""

    def test_eval_run_default_passes(self):
        result, receipt = run_eval(eval_case_id="eval-001")
        assert result.passed is True
        assert receipt["receipt_type"] == "eval.run.completed"
        assert receipt["outcome"] == "success"

    def test_eval_run_with_passing_fn(self):
        result, receipt = run_eval(
            eval_case_id="eval-002",
            test_fn=lambda: True,
        )
        assert result.passed is True

    def test_eval_run_with_failing_fn(self):
        result, receipt = run_eval(
            eval_case_id="eval-003",
            test_fn=lambda: False,
        )
        assert result.passed is False
        assert receipt["outcome"] == "failed"

    def test_eval_run_with_exception(self):
        def bad_test():
            raise RuntimeError("boom")

        result, receipt = run_eval(
            eval_case_id="eval-004",
            test_fn=bad_test,
        )
        assert result.passed is False
        assert "boom" in result.details["error"]


class TestChangeProposal:
    """Test change proposal lifecycle."""

    def test_propose_change(self):
        obj, _ = create_learning_object(
            incident_id="inc-200",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"update": "pool_config"},
        )
        change, receipt = propose_change(
            learning_object_id=obj.object_id,
            change_type="runbook_update",
            proposal={"file": "runbook.md", "diff": "+pool_size=25"},
        )
        assert change.status == ChangeProposalStatus.PENDING
        assert receipt["receipt_type"] == "learning.change.proposed"

    def test_propose_updates_object_status(self):
        obj, _ = create_learning_object(
            incident_id="inc-201",
            object_type=LearningObjectType.POLICY_PROPOSAL,
            content={"policy": "rate_limit"},
        )
        propose_change(
            learning_object_id=obj.object_id,
            change_type="policy_proposal",
            proposal={"rule": "max_10_per_minute"},
        )
        assert obj.status == LearningObjectStatus.PROPOSED

    def test_propose_invalid_object_raises(self):
        with pytest.raises(ValueError, match="not found"):
            propose_change(
                learning_object_id="nonexistent",
                change_type="test",
                proposal={},
            )


class TestChangeApproval:
    """Test change approval."""

    def test_approve_change(self):
        obj, _ = create_learning_object(
            incident_id="inc-300",
            object_type=LearningObjectType.EVAL_CASE,
            content={"test": "pool"},
        )
        change, _ = propose_change(
            learning_object_id=obj.object_id,
            change_type="eval_case",
            proposal={"case": "test_pool_overflow"},
        )
        updated, receipt = approve_change(
            proposal_id=change.proposal_id,
            approver_id="admin-001",
        )
        assert updated.status == ChangeProposalStatus.APPROVED
        assert updated.approved_by == "admin-001"
        assert receipt["receipt_type"] == "learning.change.approved"

    def test_approve_invalid_proposal_raises(self):
        with pytest.raises(ValueError, match="not found"):
            approve_change(proposal_id="nonexistent", approver_id="admin")


class TestObjectPromotion:
    """Test learning object promotion."""

    def test_promote_approved_object(self):
        obj, _ = create_learning_object(
            incident_id="inc-400",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"update": "pool"},
        )
        change, _ = propose_change(
            learning_object_id=obj.object_id,
            change_type="runbook_update",
            proposal={"diff": "+pool=25"},
        )
        approve_change(
            proposal_id=change.proposal_id,
            approver_id="admin",
        )
        promoted, receipt = promote_object(
            learning_object_id=obj.object_id,
        )
        assert promoted.status == LearningObjectStatus.PROMOTED
        assert promoted.promoted_at is not None
        assert receipt["receipt_type"] == "learning.object.promoted"

    def test_promote_unapproved_fails(self):
        """Law #3: fail-closed — unapproved objects cannot be promoted."""
        obj, _ = create_learning_object(
            incident_id="inc-401",
            object_type=LearningObjectType.EVAL_CASE,
            content={"test": "x"},
        )
        with pytest.raises(ValueError, match="must be APPROVED"):
            promote_object(learning_object_id=obj.object_id)

    def test_promote_draft_fails(self):
        obj, _ = create_learning_object(
            incident_id="inc-402",
            object_type=LearningObjectType.ROBOT_ASSERTION,
            content={"assert": "x"},
        )
        with pytest.raises(ValueError, match="must be APPROVED"):
            promote_object(learning_object_id=obj.object_id)


class TestLearningList:
    """Test listing functionality."""

    def test_list_all_objects(self):
        create_learning_object(
            incident_id="inc-500",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"a": 1},
        )
        create_learning_object(
            incident_id="inc-501",
            object_type=LearningObjectType.EVAL_CASE,
            content={"b": 2},
        )
        objects = list_learning_objects()
        assert len(objects) == 2

    def test_list_filtered_by_status(self):
        obj, _ = create_learning_object(
            incident_id="inc-510",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"a": 1},
        )
        create_learning_object(
            incident_id="inc-511",
            object_type=LearningObjectType.EVAL_CASE,
            content={"b": 2},
        )
        propose_change(
            learning_object_id=obj.object_id,
            change_type="runbook_update",
            proposal={"diff": "x"},
        )

        proposed = list_learning_objects(status=LearningObjectStatus.PROPOSED)
        assert len(proposed) == 1
        drafts = list_learning_objects(status=LearningObjectStatus.DRAFT)
        assert len(drafts) == 1


class TestLearningReceiptPersistence:
    """Law #2: Learning loop receipts must be persisted in receipt store."""

    def test_create_learning_object_persists_receipt(self):
        _, receipt = create_learning_object(
            incident_id="inc-persist-l1",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"update": "pool"},
            suite_id="suite-l1",
        )
        receipts = query_receipts(suite_id="suite-l1")
        lo_receipts = [r for r in receipts if r.get("receipt_type") == "learning.object.created"]
        assert len(lo_receipts) == 1
        assert lo_receipts[0]["id"] == receipt["id"]

    def test_run_eval_persists_receipt(self):
        _, receipt = run_eval(eval_case_id="eval-persist-1", suite_id="suite-eval")
        receipts = query_receipts(suite_id="suite-eval")
        eval_receipts = [r for r in receipts if r.get("receipt_type") == "eval.run.completed"]
        assert len(eval_receipts) == 1
        assert eval_receipts[0]["id"] == receipt["id"]

    def test_propose_change_persists_receipt(self):
        obj, _ = create_learning_object(
            incident_id="inc-persist-l2",
            object_type=LearningObjectType.POLICY_PROPOSAL,
            content={"policy": "test"},
            suite_id="suite-l2",
        )
        _, receipt = propose_change(
            learning_object_id=obj.object_id,
            change_type="policy_proposal",
            proposal={"rule": "test"},
            suite_id="suite-l2",
        )
        receipts = query_receipts(suite_id="suite-l2")
        prop_receipts = [r for r in receipts if r.get("receipt_type") == "learning.change.proposed"]
        assert len(prop_receipts) == 1
        assert prop_receipts[0]["id"] == receipt["id"]

    def test_approve_change_persists_receipt(self):
        obj, _ = create_learning_object(
            incident_id="inc-persist-l3",
            object_type=LearningObjectType.EVAL_CASE,
            content={"test": "x"},
            suite_id="suite-l3",
        )
        change, _ = propose_change(
            learning_object_id=obj.object_id,
            change_type="eval_case",
            proposal={"case": "test"},
            suite_id="suite-l3",
        )
        _, receipt = approve_change(
            proposal_id=change.proposal_id,
            approver_id="admin",
            suite_id="suite-l3",
        )
        receipts = query_receipts(suite_id="suite-l3")
        approve_receipts = [r for r in receipts if r.get("receipt_type") == "learning.change.approved"]
        assert len(approve_receipts) == 1
        assert approve_receipts[0]["id"] == receipt["id"]

    def test_promote_object_persists_receipt(self):
        obj, _ = create_learning_object(
            incident_id="inc-persist-l4",
            object_type=LearningObjectType.RUNBOOK_UPDATE,
            content={"update": "x"},
            suite_id="suite-l4",
        )
        change, _ = propose_change(
            learning_object_id=obj.object_id,
            change_type="runbook_update",
            proposal={"diff": "x"},
            suite_id="suite-l4",
        )
        approve_change(
            proposal_id=change.proposal_id,
            approver_id="admin",
            suite_id="suite-l4",
        )
        _, receipt = promote_object(
            learning_object_id=obj.object_id,
            suite_id="suite-l4",
        )
        receipts = query_receipts(suite_id="suite-l4")
        promo_receipts = [r for r in receipts if r.get("receipt_type") == "learning.object.promoted"]
        assert len(promo_receipts) == 1
        assert promo_receipts[0]["id"] == receipt["id"]
