"""Generated scaffold tests for Security Review."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.security_review import SecurityReviewSkillPack


@pytest.fixture
def pack() -> SecurityReviewSkillPack:
    return SecurityReviewSkillPack()


@pytest.fixture
def ctx() -> AgentContext:
    return AgentContext(
        suite_id="test-suite-001",
        office_id="test-office-001",
        correlation_id="test-corr-001",
        actor_id="test-user-001",
        risk_tier="red",
    )


@pytest.mark.asyncio
async def test_dispatch_known_action(pack: SecurityReviewSkillPack, ctx: AgentContext):
    with patch.object(pack, "security_scan_execute", AsyncMock()) as handler:
        handler.return_value.success = True
        await pack.dispatch_action("security.scan.execute", {"request": "test"}, ctx)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_action_denied(pack: SecuritySecuritySkillPack, ctx: AgentContext):
    result = await pack.dispatch_action("unknown.action", {}, ctx)
    assert not result.success
    assert result.receipt["policy"]["decision"] == "deny"
