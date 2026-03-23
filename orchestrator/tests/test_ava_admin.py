from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.ava_admin import AvaAdminSkillPack

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / 'scripts' / 'scaffold_agent.py'
spec = importlib.util.spec_from_file_location('scaffold_agent', SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_ava_admin_validates() -> None:
    target = module.build_validation_target(
        ROOT,
        type('Args', (), {'registry_id': 'ava_admin', 'owner_key': None, 'manifest_id': 'ava-admin'})(),
    )
    problems = module.validate_agent(ROOT, target)
    assert problems == []


def test_ava_admin_certifies() -> None:
    target = module.build_validation_target(
        ROOT,
        type('Args', (), {'registry_id': 'ava_admin', 'owner_key': None, 'manifest_id': 'ava-admin'})(),
    )
    problems = module.certify_agent(ROOT, target)
    assert problems == []


@pytest.mark.asyncio
async def test_ava_admin_health_pulse_wrapper() -> None:
    pack = AvaAdminSkillPack()
    ctx = AgentContext(suite_id='system', office_id='system', correlation_id='corr-ava-admin')
    result = await pack.admin_ops_health_pulse({}, ctx)
    assert result.success is True
    assert result.data['voice_id'] == '56bWURjYFHyYyVf490Dp'


# =========================================================================
# Wave 1: New capability tests
# =========================================================================

def _make_ctx() -> AgentContext:
    return AgentContext(suite_id='test-suite', office_id='test-office', correlation_id='corr-test')


@pytest.mark.asyncio
async def test_get_sentry_summary() -> None:
    """Mock Sentry service, verify AgentResult + receipt."""
    mock_service = MagicMock()
    mock_service.get_summary = AsyncMock(return_value={"issues": [{"id": "1", "title": "Test"}]})

    with patch(
        'aspire_orchestrator.services.sentry_read.get_sentry_read_service',
        return_value=mock_service,
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.get_sentry_summary(ctx)
            assert result.success is True
            assert result.data['summary']['issues'][0]['title'] == 'Test'
            assert result.receipt is not None
            assert result.receipt['event_type'] == 'admin.sentry_summary'
        finally:
            desk_mod._instance = old


@pytest.mark.asyncio
async def test_get_sentry_issues_with_project_filter() -> None:
    """Mock Sentry service, verify project filtering."""
    mock_service = MagicMock()
    mock_service.get_issues = AsyncMock(return_value=[
        {"id": "1", "project": {"slug": "backend"}},
        {"id": "2", "project": {"slug": "frontend"}},
    ])

    with patch(
        'aspire_orchestrator.services.sentry_read.get_sentry_read_service',
        return_value=mock_service,
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.get_sentry_issues(ctx, project="backend", limit=10)
            assert result.success is True
            assert result.data['count'] == 1
            assert result.data['issues'][0]['project']['slug'] == 'backend'
        finally:
            desk_mod._instance = old


@pytest.mark.asyncio
async def test_get_workflow_status() -> None:
    """Mock supabase_select, verify status counts."""
    mock_rows = [
        {"id": "1", "status": "completed", "created_at": "2026-01-01"},
        {"id": "2", "status": "failed", "created_at": "2026-01-01"},
        {"id": "3", "status": "completed", "created_at": "2026-01-01"},
    ]

    with patch(
        'aspire_orchestrator.services.supabase_client.supabase_select',
        new_callable=AsyncMock,
        return_value=mock_rows,
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.get_workflow_status(ctx, limit=20)
            assert result.success is True
            assert result.data['counts']['completed'] == 2
            assert result.data['counts']['failed'] == 1
            assert result.data['total'] == 3
        finally:
            desk_mod._instance = old


@pytest.mark.asyncio
async def test_get_approval_queue() -> None:
    """Mock supabase_select, verify filtering."""
    mock_rows = [
        {"id": "1", "status": "pending", "action": "invoice.create"},
    ]

    with patch(
        'aspire_orchestrator.services.supabase_client.supabase_select',
        new_callable=AsyncMock,
        return_value=mock_rows,
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.get_approval_queue(ctx, status="pending", limit=20)
            assert result.success is True
            assert result.data['count'] == 1
            assert result.data['filter'] == 'pending'
        finally:
            desk_mod._instance = old


@pytest.mark.asyncio
async def test_get_receipt_audit() -> None:
    """Mock receipt_store, verify chain integrity check."""
    with patch(
        'aspire_orchestrator.services.receipt_store.get_receipt_count',
        return_value=100,
    ), patch(
        'aspire_orchestrator.services.receipt_store.get_chain_receipts',
        return_value=[
            {"receipt_hash": "abc123", "prev_hash": None},
            {"receipt_hash": "def456", "prev_hash": "abc123"},
        ],
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.get_receipt_audit(ctx, suite_id="system", limit=50)
            assert result.success is True
            assert result.data['audit']['integrity'] == 'INTACT'
            assert result.data['audit']['total_receipts'] == 100
        finally:
            desk_mod._instance = old


@pytest.mark.asyncio
async def test_search_web() -> None:
    """Mock brave_client, verify query passthrough."""
    from aspire_orchestrator.services.tool_types import ToolExecutionResult
    from aspire_orchestrator.models import Outcome
    mock_tool_result = ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="brave.search",
        data={"results": [{"title": "Test", "url": "https://example.com"}]},
    )

    with patch(
        'aspire_orchestrator.providers.brave_client.execute_brave_search',
        new_callable=AsyncMock,
        return_value=mock_tool_result,
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.search_web(ctx, query="test query", count=5)
            assert result.success is True
            assert len(result.data['search_results']['results']) == 1
        finally:
            desk_mod._instance = old


@pytest.mark.asyncio
async def test_search_web_missing_query() -> None:
    """Verify error when query empty."""
    from aspire_orchestrator.skillpacks.ava_admin_desk import get_ava_admin_desk
    desk = get_ava_admin_desk()
    ctx = _make_ctx()
    result = await desk.search_web(ctx, query="", count=5)
    assert result.success is False
    assert "Missing required parameter" in result.error


@pytest.mark.asyncio
async def test_get_council_history() -> None:
    """Mock council_service, verify session listing."""
    from datetime import datetime, timezone
    from dataclasses import dataclass

    @dataclass
    class FakeSession:
        session_id: str = 's1'
        status: str = 'decided'
        created_at: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with patch(
        'aspire_orchestrator.services.council_service.list_sessions',
        return_value=[FakeSession()],
    ):
        import aspire_orchestrator.skillpacks.ava_admin_desk as desk_mod
        old = desk_mod._instance
        desk_mod._instance = None
        try:
            desk = desk_mod.get_ava_admin_desk()
            ctx = _make_ctx()
            result = await desk.get_council_history(ctx, status="decided", limit=10)
            assert result.success is True
            assert result.data['count'] == 1
            # Datetime should be converted to ISO string
            assert '2026-01-01' in result.data['sessions'][0]['created_at']
        finally:
            desk_mod._instance = old


# =========================================================================
# Wave 2: Persona/voice/greeting tests
# =========================================================================

def test_ava_admin_greeting_formal_name() -> None:
    """Verify Mr./Mrs. LastName in greeting output."""
    from aspire_orchestrator.nodes.greeting_fast_path import greeting_response, _formal_name

    # Test with salutation
    profile_mr = {"owner_name": "John Smith", "salutation": "Mr."}
    name = _formal_name(profile_mr)
    assert "Mr. Smith" in name

    # Test with title override
    profile_mrs = {"owner_name": "Jane Doe", "title": "Mrs."}
    name = _formal_name(profile_mrs)
    assert "Mrs. Doe" in name

    # Test fallback to Mr.
    profile_default = {"owner_name": "Alex Johnson"}
    name = _formal_name(profile_default)
    assert "Mr. Johnson" in name

    # Test ava_admin greeting includes formal name
    response = greeting_response("ava_admin", profile_mr)
    assert "Mr. Smith" in response


def test_ava_admin_identity_intro() -> None:
    """Verify _identity_intro returns admin-specific intro."""
    from aspire_orchestrator.nodes.agent_reason import _identity_intro
    intro = _identity_intro("ava_admin")
    assert "ops commander" in intro.lower()
    assert "platform health" in intro.lower()
    assert "council" in intro.lower()


def test_ava_admin_voice_anam_style() -> None:
    """Verify voice channel context includes TTS instructions."""
    from aspire_orchestrator.nodes.agent_reason import _build_channel_context
    state = {"user_profile": {"channel": "voice"}}
    ctx = _build_channel_context(state)
    assert "Anam avatar" in ctx
    assert "write out numbers" in ctx.lower()
    assert "no markdown" in ctx.lower()


# =========================================================================
# Wave 2: Router/config tests
# =========================================================================

def test_ava_admin_desk_router_loaded() -> None:
    """YAML loads without parse errors."""
    import yaml
    router_path = Path(__file__).resolve().parent.parent / (
        'src/aspire_orchestrator/config/desk_router_rules/ava_admin_desk_router.yaml'
    )
    with open(router_path) as f:
        data = yaml.safe_load(f)
    assert data['rule_id'] == 'ava_admin_desk_router'
    assert data['agent'] == 'ava_admin'
    assert 'platform_health' in data['match']['intents']


def test_route_decision_no_duplicate_temperature() -> None:
    """RouteDecision should have exactly one temperature field."""
    from aspire_orchestrator.services.llm_router import RouteDecision
    fields = RouteDecision.model_fields
    # Pydantic v2: model_fields is a dict — 'temperature' appears once
    temp_count = sum(1 for k in fields if k == 'temperature')
    assert temp_count == 1
