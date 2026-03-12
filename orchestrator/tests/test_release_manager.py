"""Generated scaffold tests for Release Manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.release_manager import ReleaseManagerSkillPack


@pytest.fixture
def pack() -> ReleaseManagerSkillPack:
    return ReleaseManagerSkillPack()


@pytest.fixture
def ctx() -> AgentContext:
    return AgentContext(
        suite_id="test-suite-001",
        office_id="test-office-001",
        correlation_id="test-corr-001",
        actor_id="test-user-001",
        risk_tier="yellow",
    )


@pytest.mark.asyncio
async def test_dispatch_known_action(pack: ReleaseManagerSkillPack, ctx: AgentContext):
    with patch.object(pack, "release_checklist_enforce", AsyncMock()) as handler:
        handler.return_value.success = True
        await pack.dispatch_action("release.checklist.enforce", {"request": "test"}, ctx)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_action_denied(pack: ReleaseReleaseSkillPack, ctx: AgentContext):
    result = await pack.dispatch_action("unknown.action", {}, ctx)
    assert not result.success
    assert result.receipt["policy"]["decision"] == "deny"
