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
    """Verify three channels: voice (TTS no avatar), avatar (Anam), chat (markdown)."""
    from aspire_orchestrator.nodes.agent_reason import _build_channel_context

    # Voice channel — audio-only TTS, no Anam avatar reference
    voice_ctx = _build_channel_context({"user_profile": {"channel": "voice"}})
    assert "write out numbers" in voice_ctx.lower()
    assert "no markdown" in voice_ctx.lower()
    assert "Anam avatar" not in voice_ctx
    assert "text-to-speech delivery" in voice_ctx

    # Avatar channel — Anam video rendering (Ava + Finn)
    avatar_ctx = _build_channel_context({"user_profile": {"channel": "avatar"}})
    assert "Anam avatar" in avatar_ctx
    assert "write out numbers" in avatar_ctx.lower()

    # Chat channel — structured formatting (warm conversational, light formatting OK)
    chat_ctx = _build_channel_context({"user_profile": {"channel": "chat"}})
    assert "formatting" in chat_ctx.lower()
    assert "write out numbers" not in chat_ctx.lower()


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


# =========================================================================
# v1.2.0: Channel resolution, temporal context, formatting, delegation
# =========================================================================


class TestResolveChannel:
    """Tests for _resolve_channel helper (RC0 fix)."""

    def test_resolve_channel_from_top_level_state(self) -> None:
        """Channel in top-level state takes priority."""
        from aspire_orchestrator.nodes.agent_reason import _resolve_channel
        state = {"channel": "chat"}
        assert _resolve_channel(state) == "chat"

    def test_resolve_channel_from_payload(self) -> None:
        """Channel in payload.channel when top-level missing."""
        from aspire_orchestrator.nodes.agent_reason import _resolve_channel
        state = {"request": {"payload": {"channel": "voice"}}}
        assert _resolve_channel(state) == "voice"

    def test_resolve_channel_default_chat(self) -> None:
        """No channel anywhere → defaults to 'chat' not 'voice'."""
        from aspire_orchestrator.nodes.agent_reason import _resolve_channel
        state = {}
        assert _resolve_channel(state) == "chat"

    def test_resolve_channel_normalizes_text(self) -> None:
        """'text' → 'chat' normalization."""
        from aspire_orchestrator.nodes.agent_reason import _resolve_channel
        state = {"channel": "text"}
        assert _resolve_channel(state) == "chat"

    def test_resolve_channel_from_user_profile(self) -> None:
        """Falls back to user_profile.channel as last resort."""
        from aspire_orchestrator.nodes.agent_reason import _resolve_channel
        state = {"user_profile": {"channel": "avatar"}}
        assert _resolve_channel(state) == "avatar"


class TestTemporalContext:
    """Tests for temporal context injection (RC1 fix)."""

    def test_temporal_context_in_system_prompt(self) -> None:
        """Date/time string should be present in assembled system message."""
        from aspire_orchestrator.nodes.agent_reason import _build_channel_context, _resolve_channel
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Verify that temporal_ctx format produces current year
        expected_year = str(now.year)
        expected_quarter = f"Q{(now.month - 1) // 3 + 1}"
        temporal_ctx = (
            f"## Current Date & Time\n"
            f"Today is {now.strftime('%A, %B %d, %Y')}. "
            f"Current time is {now.strftime('%I:%M %p')} UTC. "
            f"Current quarter: {expected_quarter} {now.year}."
        )
        assert expected_year in temporal_ctx
        assert expected_quarter in temporal_ctx
        assert "## Current Date & Time" in temporal_ctx


class TestChannelAwareFormatting:
    """Tests for channel-aware formatting (RC2/RC3 fix)."""

    def test_chat_channel_allows_formatting(self) -> None:
        """Chat channel should allow formatting, not TTS constraints."""
        from aspire_orchestrator.nodes.agent_reason import _build_channel_context
        state = {"channel": "chat"}
        ctx = _build_channel_context(state)
        assert "text-to-speech" not in ctx.lower()
        assert "substantive" in ctx.lower() or "formatting" in ctx.lower()

    def test_voice_channel_gets_tts_constraints(self) -> None:
        """Voice channel should get TTS constraints."""
        from aspire_orchestrator.nodes.agent_reason import _build_channel_context
        state = {"channel": "voice"}
        ctx = _build_channel_context(state)
        assert "text-to-speech" in ctx.lower()

    def test_avatar_channel_gets_tts_constraints(self) -> None:
        """Avatar channel should get TTS constraints."""
        from aspire_orchestrator.nodes.agent_reason import _build_channel_context
        state = {"channel": "avatar"}
        ctx = _build_channel_context(state)
        assert "anam avatar" in ctx.lower()


class TestAdvisoryMaxTokens:
    """Tests for advisory max_output_tokens increase (RC4 fix)."""

    def test_advisory_intent_gets_more_tokens(self) -> None:
        """knowledge/advice intents should get 1200 tokens."""
        for intent in ("knowledge", "advice"):
            _max_tokens = 1200 if intent in ("knowledge", "advice") else 500
            assert _max_tokens == 1200

    def test_default_intent_stays_at_500(self) -> None:
        """Non-advisory intents should stay at 500 tokens."""
        for intent in ("greeting", "conversation", "action"):
            _max_tokens = 1200 if intent in ("knowledge", "advice") else 500
            assert _max_tokens == 500


class TestFinnResearchDelegation:
    """Tests for Finn→Adam delegation hint (RC5 fix)."""

    def test_finn_gets_delegation_hint_when_rag_empty(self) -> None:
        """Finn should get delegation hint when RAG has no results."""
        agent_id = "finn"
        retrieval_status = "no_results"
        rag_context = ""

        if agent_id in ("finn", "finn_fm") and retrieval_status in ("no_results", "offline", "degraded"):
            _research_hint = (
                "\n## Research Delegation\n"
                "If this question is outside your financial expertise, tell the user you'll "
                "ask Adam (your research specialist) to look into it."
            )
            rag_context = (rag_context + _research_hint) if rag_context else _research_hint

        assert "Adam" in rag_context
        assert "Research Delegation" in rag_context

    def test_non_finn_no_delegation_hint(self) -> None:
        """Non-Finn agents should NOT get delegation hint."""
        agent_id = "ava"
        retrieval_status = "no_results"
        rag_context = ""

        if agent_id in ("finn", "finn_fm") and retrieval_status in ("no_results", "offline", "degraded"):
            rag_context = "delegation hint"

        assert rag_context == ""


class TestIntakeChannelExtraction:
    """Tests for channel extraction in intake node."""

    def test_intake_extracts_channel_from_payload(self) -> None:
        """intake_node should extract channel to top-level state."""
        from aspire_orchestrator.nodes.intake import intake_node
        state = {
            "request": {
                "schema_version": "1.0",
                "suite_id": "00000000-0000-0000-0000-000000000000",
                "office_id": "00000000-0000-0000-0000-000000000001",
                "request_id": "test-req",
                "correlation_id": "test-corr",
                "timestamp": "2026-01-01T00:00:00Z",
                "task_type": "unknown",
                "payload": {"text": "hello", "channel": "chat"},
            },
            "auth_suite_id": "test-suite",
            "auth_actor_id": "test-actor",
        }
        result = intake_node(state)
        assert result.get("channel") == "chat"

    def test_intake_normalizes_text_to_chat(self) -> None:
        """intake_node should normalize 'text' → 'chat'."""
        from aspire_orchestrator.nodes.intake import intake_node
        state = {
            "request": {
                "text": "hello",
                "channel": "text",
            },
            "auth_suite_id": "test-suite",
            "auth_actor_id": "test-actor",
        }
        result = intake_node(state)
        assert result.get("channel") == "chat"
