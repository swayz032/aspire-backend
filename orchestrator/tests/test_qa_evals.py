"""Generated scaffold tests for QA Evals."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.qa_evals import QaEvalsSkillPack


@pytest.fixture
def pack() -> QaEvalsSkillPack:
    return QaEvalsSkillPack()


@pytest.fixture
def ctx() -> AgentContext:
    return AgentContext(
        suite_id="test-suite-001",
        office_id="test-office-001",
        correlation_id="test-corr-001",
        actor_id="test-user-001",
        risk_tier="green",
    )


@pytest.mark.asyncio
async def test_dispatch_known_action(pack: QaEvalsSkillPack, ctx: AgentContext):
    with patch.object(pack, "qa_eval_execute", AsyncMock()) as handler:
        handler.return_value.success = True
        await pack.dispatch_action("qa.eval.execute", {"request": "test"}, ctx)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_action_denied(pack: QaQaSkillPack, ctx: AgentContext):
    result = await pack.dispatch_action("unknown.action", {}, ctx)
    assert not result.success
    assert result.receipt["policy"]["decision"] == "deny"
