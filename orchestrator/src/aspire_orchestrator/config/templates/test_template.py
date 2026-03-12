"""Tests for {AgentName} skill pack.

Covers:
  - Manifest/persona/policy loading
  - Action success and failure paths
  - Memory operations (store, recall, search, forget)
  - Governance compliance (receipts, risk tiers, fail-closed)
  - Agentic loop (if agent uses multi-step reasoning)

Run: pytest tests/test_{agent_file}.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# Replace these imports with your actual agent
# from aspire_orchestrator.skillpacks.{agent_file} import {AgentName}SkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def pack():
    """Create a skill pack instance with auto-loaded config."""
    # return {AgentName}SkillPack()
    raise NotImplementedError("Replace with your skill pack class")


@pytest.fixture
def ctx():
    """Standard test context — scoped to test tenant."""
    return AgentContext(
        suite_id="test-suite-001",
        office_id="test-office-001",
        correlation_id="test-corr-001",
        actor_id="test-user-001",
        risk_tier="green",
    )


# ── Config Loading ───────────────────────────────────────────────────────


class TestConfigLoading:
    """Verify manifest, persona, and policies load correctly."""

    def test_manifest_loads(self, pack):
        assert pack.manifest is not None
        assert pack.manifest["skillpack_id"] == "{agent-id}"
        assert pack.manifest["channel"] == "internal_frontend"

    def test_persona_loads(self, pack):
        assert pack.persona is not None
        assert "{AgentName}" in pack.persona

    def test_risk_profile(self, pack):
        assert pack.default_risk_tier == "green"  # or yellow/red
        assert pack.get_max_risk_tier() in ("green", "yellow", "red")

    def test_capabilities_listed(self, pack):
        caps = pack.get_capability_list()
        assert len(caps) > 0
        assert "can_{action}" in caps

    def test_tools_listed(self, pack):
        tools = pack.get_tools_list()
        assert len(tools) > 0


# ── Action Tests ─────────────────────────────────────────────────────────


class TestReadAction:
    """Test GREEN-tier read/query actions."""

    @pytest.mark.asyncio
    async def test_read_success(self, pack, ctx):
        with patch.object(pack, "call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "content": "Here are the results.",
                "model_used": "gpt-5-mini",
                "profile_used": "classify",
            }
            result = await pack.read_action({"required_field": "test_query"}, ctx)

        assert result.success
        assert result.receipt["status"] == "ok"
        assert result.receipt["event_type"] == "{domain}.read"

    @pytest.mark.asyncio
    async def test_read_missing_params_denied(self, pack, ctx):
        result = await pack.read_action({}, ctx)

        assert not result.success
        assert result.receipt["policy"]["decision"] == "deny"
        assert "MISSING_REQUIRED_FIELD" in result.receipt["policy"]["reasons"]


class TestWriteAction:
    """Test YELLOW-tier state-changing actions."""

    @pytest.mark.asyncio
    async def test_write_success(self, pack, ctx):
        with patch.object(pack, "_execute_write", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"result": "done"}
            # Also mock memory writes
            with patch.object(pack, "remember", new_callable=AsyncMock):
                result = await pack.write_action({"required_field": "test_value"}, ctx)

        assert result.success
        assert result.receipt["status"] == "ok"

    @pytest.mark.asyncio
    async def test_write_missing_params_denied(self, pack, ctx):
        result = await pack.write_action({}, ctx)

        assert not result.success
        assert result.receipt["policy"]["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_write_failure_emits_receipt(self, pack, ctx):
        with patch.object(pack, "_execute_write", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = RuntimeError("Provider error")
            result = await pack.write_action({"required_field": "test_value"}, ctx)

        assert not result.success
        assert result.receipt["status"] == "failed"
        assert "Provider error" in result.error


# ── Memory Tests ─────────────────────────────────────────────────────────


class TestMemory:
    """Test agentic memory operations (3-tier)."""

    def test_working_memory(self, pack):
        """Tier 1: In-context working memory."""
        pack.working_set("key1", "value1")
        assert pack.working_get("key1") == "value1"
        assert pack.working_get("missing") is None

        pack.working_clear()
        assert pack.working_get("key1") is None

    @pytest.mark.asyncio
    async def test_remember_and_recall(self, pack, ctx):
        """Tier 3: Semantic memory store and retrieve."""
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_upsert",
            new_callable=AsyncMock,
        ):
            with patch.object(pack, "emit_receipt", new_callable=AsyncMock):
                receipt = await pack.remember("test_key", "test_value", ctx)
                assert receipt["status"] == "ok"

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock,
            return_value=[{"fact_key": "test_key", "fact_value": "test_value"}],
        ):
            value = await pack.recall("test_key", ctx)
            assert value == "test_value"

    @pytest.mark.asyncio
    async def test_search_memory(self, pack, ctx):
        """Tier 3: Semantic memory search."""
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock,
            return_value=[
                {"fact_key": "client_abc", "fact_value": "Pays net-15", "fact_type": "business_fact"},
                {"fact_key": "client_xyz", "fact_value": "Prefers email", "fact_type": "preference"},
            ],
        ):
            results = await pack.search_memory("client", ctx)
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_store_and_recall_episode(self, pack, ctx):
        """Tier 2: Episodic memory store and retrieve."""
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_insert",
            new_callable=AsyncMock,
        ):
            with patch.object(pack, "emit_receipt", new_callable=AsyncMock):
                receipt = await pack.store_episode(
                    "Discussed Q1 invoicing with client ABC",
                    ctx,
                    session_id="session-001",
                    key_topics=["invoicing", "client_abc"],
                    key_entities={"client": "ABC Corp"},
                    turn_count=5,
                )
                assert receipt["status"] == "ok"
                assert receipt["event_type"] == "memory.episode.store"

        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock,
            return_value=[{"summary": "Q1 invoicing discussion", "agent_id": "{agent-id}"}],
        ):
            episodes = await pack.recall_episodes(ctx, limit=3)
            assert len(episodes) == 1
            assert "Q1 invoicing" in episodes[0]["summary"]

    @pytest.mark.asyncio
    async def test_forget(self, pack, ctx):
        """Tier 3: Soft-delete (confidence=0, not hard delete)."""
        with patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new_callable=AsyncMock,
            return_value=[{"fact_type": "business_fact"}],
        ):
            with patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                new_callable=AsyncMock,
            ):
                with patch.object(pack, "emit_receipt", new_callable=AsyncMock):
                    result = await pack.forget("test_key", ctx)
                    assert result is True


# ── Governance Tests ─────────────────────────────────────────────────────


class TestGovernance:
    """Verify governance compliance across all operations."""

    def test_default_risk_tier(self, pack):
        assert pack.default_risk_tier in ("green", "yellow", "red")

    @pytest.mark.asyncio
    async def test_receipt_has_required_fields(self, pack, ctx):
        """Every receipt must have: receipt_id, suite_id, event_type, status, ts."""
        with patch.object(pack, "call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "content": "result",
                "model_used": "gpt-5-mini",
                "profile_used": "classify",
            }
            result = await pack.read_action({"required_field": "test"}, ctx)

        receipt = result.receipt
        assert "receipt_id" in receipt
        assert receipt["suite_id"] == "test-suite-001"
        assert "event_type" in receipt
        assert "status" in receipt
        assert "ts" in receipt  # build_receipt uses "ts", not "timestamp"

    @pytest.mark.asyncio
    async def test_tenant_scoping(self, pack, ctx):
        """All receipts must be scoped to the requesting tenant (Law #6)."""
        with patch.object(pack, "call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"content": "ok", "model_used": "test"}
            result = await pack.read_action({"required_field": "test"}, ctx)

        assert result.receipt["suite_id"] == ctx.suite_id


# ── Agentic Loop Tests ──────────────────────────────────────────────────


class TestAgenticLoop:
    """Test multi-step reasoning loop (if agent uses it)."""

    @pytest.mark.asyncio
    async def test_agentic_loop_completes(self, pack, ctx):
        """Loop should plan, execute steps, reflect, and return."""
        with patch.object(pack, "call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "content": "Step result",
                "model_used": "gpt-5-mini",
                "profile_used": "draft",
            }
            with patch.object(pack, "emit_receipt", new_callable=AsyncMock):
                with patch.object(pack, "search_memory", new_callable=AsyncMock, return_value=[]):
                    with patch.object(pack, "recall_episodes", new_callable=AsyncMock, return_value=[]):
                        with patch.object(pack, "remember", new_callable=AsyncMock):
                            result = await pack.run_agentic_loop(
                                "Test task",
                                ctx,
                                max_steps=2,
                                timeout_s=10.0,
                            )

        assert result.success
        assert result.data["task"] == "Test task"
        assert len(result.data["steps"]) > 0

    @pytest.mark.asyncio
    async def test_agentic_loop_respects_max_steps(self, pack, ctx):
        """Loop must not exceed max_steps."""
        with patch.object(pack, "call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"content": "done", "model_used": "test"}
            with patch.object(pack, "emit_receipt", new_callable=AsyncMock):
                with patch.object(pack, "search_memory", new_callable=AsyncMock, return_value=[]):
                    with patch.object(pack, "recall_episodes", new_callable=AsyncMock, return_value=[]):
                        with patch.object(pack, "remember", new_callable=AsyncMock):
                            result = await pack.run_agentic_loop(
                                "Test", ctx, max_steps=1, timeout_s=10.0,
                            )

        # Plan (1 call) + 1 step = at most 1 step in data
        assert len(result.data["steps"]) <= 1
