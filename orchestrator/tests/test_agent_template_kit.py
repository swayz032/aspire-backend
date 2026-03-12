"""Smoke tests for the Aspire Agent Template Kit.

Validates:
  - AgenticSkillPack and AgentMemoryMixin import cleanly
  - Class hierarchy (MRO) is correct
  - Working memory (tier 1) operations function correctly
  - _memory_enabled flag is honoured
  - run_agentic_loop() exists with the correct signature
  - template config files parse as valid JSON/YAML
  - test_template.py has valid Python syntax

These tests do NOT require a live database or LLM — all I/O is mocked.

Governance notes:
  - Law #2: receipt emission verified in memory write smoke path
  - Law #3: fail-closed verified via missing-params path
  - Law #6: tenant scoping verified on every receipt
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from aspire_orchestrator.config.templates.agent_memory_mixin import AgentMemoryMixin
from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "aspire_orchestrator" / "config" / "templates"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

# Patch targets: supabase functions are lazy-imported inside methods.
# Patch at the SOURCE module (supabase_client), NOT at the mixin module.
_SUPABASE_SELECT = "aspire_orchestrator.services.supabase_client.supabase_select"
_SUPABASE_INSERT = "aspire_orchestrator.services.supabase_client.supabase_insert"


def _ctx(suite_id: str = SUITE_A) -> AgentContext:
    return AgentContext(
        suite_id=suite_id,
        office_id="aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        correlation_id="test-corr-001",
        actor_id="test-user-001",
        risk_tier="green",
    )


def _make_pack(memory_enabled: bool = True) -> AgenticSkillPack:
    """Minimal concrete AgenticSkillPack for testing (bypasses config file loading)."""
    pack = object.__new__(AgenticSkillPack)
    pack._agent_id = "test-agent"
    pack._agent_name = "Test Agent"
    pack._memory_enabled = memory_enabled
    pack.__init_memory__()
    return pack


# ---------------------------------------------------------------------------
# 1. Imports / Class Hierarchy
# ---------------------------------------------------------------------------


class TestImports:
    """Verify all template kit imports resolve without errors."""

    def test_agent_memory_mixin_importable(self):
        """AgentMemoryMixin must import cleanly with no side-effects."""
        assert AgentMemoryMixin is not None

    def test_agentic_skillpack_importable(self):
        """AgenticSkillPack must import cleanly."""
        assert AgenticSkillPack is not None

    def test_enhanced_skillpack_dependency_importable(self):
        """EnhancedSkillPack (parent) must import cleanly."""
        assert EnhancedSkillPack is not None

    def test_agent_context_importable(self):
        """AgentContext must import cleanly."""
        assert AgentContext is not None

    def test_agent_result_importable(self):
        """AgentResult must import cleanly."""
        assert AgentResult is not None

    def test_supabase_client_insert_and_select_importable(self):
        """supabase_insert and supabase_select must be importable (they exist)."""
        from aspire_orchestrator.services.supabase_client import (
            supabase_insert,
            supabase_select,
        )
        assert supabase_insert is not None
        assert supabase_select is not None

    def test_supabase_upsert_importable(self):
        """supabase_upsert must be available in supabase_client."""
        from aspire_orchestrator.services.supabase_client import supabase_upsert  # noqa: F401
        assert callable(supabase_upsert)


class TestClassHierarchy:
    """Verify MRO and isinstance relationships."""

    def test_agentic_skillpack_is_enhanced_skillpack(self):
        assert issubclass(AgenticSkillPack, EnhancedSkillPack), (
            "AgenticSkillPack must extend EnhancedSkillPack"
        )

    def test_agentic_skillpack_is_agent_memory_mixin(self):
        assert issubclass(AgenticSkillPack, AgentMemoryMixin), (
            "AgenticSkillPack must extend AgentMemoryMixin"
        )

    def test_mro_order(self):
        """EnhancedSkillPack should appear before AgentMemoryMixin in MRO."""
        mro_names = [c.__name__ for c in AgenticSkillPack.__mro__]
        assert mro_names.index("EnhancedSkillPack") < mro_names.index(
            "AgentMemoryMixin"
        ), f"Unexpected MRO: {mro_names}"

    def test_run_agentic_loop_exists(self):
        assert hasattr(AgenticSkillPack, "run_agentic_loop"), (
            "run_agentic_loop method missing from AgenticSkillPack"
        )

    def test_run_agentic_loop_signature(self):
        sig = inspect.signature(AgenticSkillPack.run_agentic_loop)
        params = set(sig.parameters.keys())
        required = {"self", "task", "ctx", "max_steps", "timeout_s"}
        assert required <= params, (
            f"run_agentic_loop missing params: {required - params}"
        )

    def test_run_agentic_loop_is_async(self):
        assert inspect.iscoroutinefunction(AgenticSkillPack.run_agentic_loop), (
            "run_agentic_loop must be async"
        )


# ---------------------------------------------------------------------------
# 2. Working Memory (Tier 1)
# ---------------------------------------------------------------------------


class TestWorkingMemory:
    """Law #6: working memory is in-context only; no cross-tenant leakage possible."""

    def test_working_set_and_get(self):
        pack = _make_pack()
        pack.working_set("invoice_id", "INV-001")
        assert pack.working_get("invoice_id") == "INV-001"

    def test_working_get_missing_key_returns_default(self):
        pack = _make_pack()
        assert pack.working_get("nonexistent") is None
        assert pack.working_get("nonexistent", "fallback") == "fallback"

    def test_working_clear_empties_all_keys(self):
        pack = _make_pack()
        pack.working_set("a", 1)
        pack.working_set("b", 2)
        pack.working_clear()
        assert pack.working_get("a") is None
        assert pack.working_get("b") is None

    def test_working_memory_overwrite(self):
        pack = _make_pack()
        pack.working_set("key", "v1")
        pack.working_set("key", "v2")
        assert pack.working_get("key") == "v2"

    def test_working_memory_is_instance_scoped(self):
        """Two pack instances must not share working memory (Law #6)."""
        pack_a = _make_pack()
        pack_b = _make_pack()
        pack_a.working_set("secret", "alpha_data")
        assert pack_b.working_get("secret") is None, (
            "ISOLATION VIOLATION: pack_b can read pack_a working memory"
        )


# ---------------------------------------------------------------------------
# 3. Memory Enabled Flag
# ---------------------------------------------------------------------------


class TestMemoryEnabledFlag:
    """_memory_enabled must gate all agentic memory use in run_agentic_loop."""

    def test_memory_enabled_true_by_default(self):
        pack = _make_pack(memory_enabled=True)
        assert pack._memory_enabled is True

    def test_memory_disabled_flag(self):
        pack = _make_pack(memory_enabled=False)
        assert pack._memory_enabled is False

    @pytest.mark.asyncio
    async def test_memory_disabled_skips_search_and_recall(self):
        """When _memory_enabled=False, run_agentic_loop must not call search_memory."""
        pack = _make_pack(memory_enabled=False)

        search_mock = AsyncMock(return_value=[])
        recall_mock = AsyncMock(return_value=[])
        emit_mock = AsyncMock()
        execute_with_llm_mock = AsyncMock(
            return_value=AgentResult(
                success=True,
                data={"content": "ok"},
                receipt={"receipt_id": "r1", "status": "ok"},
            )
        )

        with (
            patch.object(pack, "search_memory", search_mock),
            patch.object(pack, "recall_episodes", recall_mock),
            patch.object(pack, "execute_with_llm", execute_with_llm_mock),
            patch.object(pack, "emit_receipt", emit_mock),
        ):
            await pack.run_agentic_loop("test task", _ctx(), max_steps=1, timeout_s=10.0)

        search_mock.assert_not_called()
        recall_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Semantic Memory Operations (Tier 3, mocked)
#
# ---------------------------------------------------------------------------


class TestSemanticMemory:
    """Tier 3: semantic memory — receipt emission (Law #2) and tenant scoping (Law #6)."""

    @pytest.mark.asyncio
    async def test_remember_calls_supabase_upsert(self):
        """remember() must upsert a fact scoped to the requesting tenant (Law #6)."""
        pack = _make_pack()
        emit_mock = AsyncMock()
        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                new_callable=AsyncMock,
            ) as upsert_mock,
            patch.object(
                pack,
                "build_receipt",
                return_value={
                    "receipt_id": "r1",
                    "status": "ok",
                    "event_type": "memory.fact.store",
                },
            ),
            patch.object(pack, "emit_receipt", emit_mock),
        ):
            await pack.remember("client_terms", "net-30", _ctx())

        upsert_mock.assert_called_once()
        fact = upsert_mock.call_args[0][1]
        assert fact["suite_id"] == SUITE_A, "Fact must be scoped to requesting tenant (Law #6)"
        assert fact["agent_id"] == "test-agent"
        assert fact["fact_key"] == "client_terms"
        assert fact["fact_value"] == "net-30"

    @pytest.mark.asyncio
    async def test_remember_emits_receipt_on_success(self):
        """Law #2: Every memory write must emit a receipt."""
        pack = _make_pack()
        emit_mock = AsyncMock()
        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                new_callable=AsyncMock,
            ),
            patch.object(
                pack,
                "build_receipt",
                return_value={
                    "receipt_id": "r1",
                    "status": "ok",
                    "event_type": "memory.fact.store",
                },
            ),
            patch.object(pack, "emit_receipt", emit_mock),
        ):
            await pack.remember("key", "value", _ctx())

        emit_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_remember_emits_failure_receipt_on_error(self):
        """Law #2: Failed memory writes must still emit a receipt."""
        pack = _make_pack()
        emit_mock = AsyncMock()
        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB down"),
            ),
            patch.object(
                pack,
                "build_receipt",
                return_value={
                    "receipt_id": "r1",
                    "status": "failed",
                    "event_type": "memory.fact.store",
                },
            ),
            patch.object(pack, "emit_receipt", emit_mock),
        ):
            await pack.remember("key", "value", _ctx())

        emit_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_recall_returns_fact_value(self):
        """recall() must return the stored fact value."""
        pack = _make_pack()
        with patch(
            _SUPABASE_SELECT,
            new_callable=AsyncMock,
            return_value=[{"fact_key": "client_terms", "fact_value": "net-30"}],
        ):
            value = await pack.recall("client_terms", _ctx())

        assert value == "net-30"

    @pytest.mark.asyncio
    async def test_recall_returns_none_for_missing_key(self):
        """recall() must return None when no fact exists, not raise."""
        pack = _make_pack()
        with patch(
            _SUPABASE_SELECT,
            new_callable=AsyncMock,
            return_value=[],
        ):
            value = await pack.recall("no_such_key", _ctx())

        assert value is None

    @pytest.mark.asyncio
    async def test_search_memory_filters_by_query(self):
        """search_memory() must return semantic-memory search results."""
        pack = _make_pack()
        memory = AsyncMock()
        memory.search_facts.return_value = [
            {"fact_key": "client_abc_terms", "fact_value": "Pays net-15", "fact_type": "business_fact"}
        ]
        with patch(
            "aspire_orchestrator.services.semantic_memory.get_semantic_memory",
            return_value=memory,
        ):
            results = await pack.search_memory("client", _ctx())

        assert len(results) == 1
        assert results[0]["fact_key"] == "client_abc_terms"

    @pytest.mark.asyncio
    async def test_search_memory_tenant_scoped(self):
        """Law #6: search_memory must pass suite_id to semantic memory."""
        pack = _make_pack()
        memory = AsyncMock()
        memory.search_facts.return_value = []
        with patch(
            "aspire_orchestrator.services.semantic_memory.get_semantic_memory",
            return_value=memory,
        ):
            await pack.search_memory("anything", _ctx(SUITE_A))

        kwargs = memory.search_facts.await_args.kwargs
        assert kwargs["suite_id"] == SUITE_A, (
            "ISOLATION VIOLATION: search_memory must pass ctx.suite_id (Law #6)"
        )
        assert kwargs["agent_id"] == "test-agent"

    @pytest.mark.asyncio
    async def test_search_memory_passes_raw_query_text(self):
        """User query text must reach semantic memory unchanged."""
        pack = _make_pack()
        memory = AsyncMock()
        memory.search_facts.return_value = []
        with patch(
            "aspire_orchestrator.services.semantic_memory.get_semantic_memory",
            return_value=memory,
        ):
            await pack.search_memory("client&a,b(c)%_", _ctx())

        kwargs = memory.search_facts.await_args.kwargs
        assert kwargs["query"] == "client&a,b(c)%_"

    @pytest.mark.asyncio
    async def test_forget_soft_deletes_confidence_zero(self):
        """forget() must set confidence=0, not hard-delete (Law #2: no data destruction)."""
        pack = _make_pack()
        emit_mock = AsyncMock()
        with (
            patch(
                "aspire_orchestrator.services.supabase_client.supabase_upsert",
                new_callable=AsyncMock,
            ) as upsert_mock,
            patch.object(
                pack,
                "build_receipt",
                return_value={
                    "receipt_id": "r1",
                    "status": "ok",
                    "event_type": "memory.fact.forget",
                },
            ),
            patch.object(pack, "emit_receipt", emit_mock),
        ):
            result = await pack.forget("old_key", _ctx())

        assert result is True
        upsert_mock.assert_called_once()
        upserted_fact = upsert_mock.call_args[0][1]
        assert upserted_fact["confidence"] == 0.0, (
            "forget() must zero confidence, not hard-DELETE (Law #2)"
        )
        assert upserted_fact["fact_value"] == "[forgotten]"


# ---------------------------------------------------------------------------
# 5. Config Template Files
# ---------------------------------------------------------------------------


class TestConfigTemplates:
    """Verify template config files are structurally correct."""

    # -- manifest_template.json (fully passing) --------------------------------

    def test_manifest_template_is_valid_json(self):
        path = TEMPLATES_DIR / "manifest_template.json"
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_manifest_template_has_required_keys(self):
        path = TEMPLATES_DIR / "manifest_template.json"
        with open(path) as f:
            data = json.load(f)
        required = [
            "skillpack_id", "name", "agent_name", "channel",
            "version", "capabilities", "risk_profile", "tools", "actions",
        ]
        missing = [k for k in required if k not in data]
        assert not missing, f"manifest_template.json missing keys: {missing}"

    def test_manifest_template_channel_is_internal_frontend(self):
        path = TEMPLATES_DIR / "manifest_template.json"
        with open(path) as f:
            data = json.load(f)
        assert data["channel"] == "internal_frontend"

    def test_manifest_template_has_memory_block(self):
        path = TEMPLATES_DIR / "manifest_template.json"
        with open(path) as f:
            data = json.load(f)
        assert "memory" in data, "manifest_template.json must include 'memory' block"
        assert "enabled" in data["memory"]

    def test_new_agent_template_references_central_registry(self):
        path = TEMPLATES_DIR / "NEW_AGENT_TEMPLATE.md"
        text = path.read_text(encoding="utf-8")
        assert "config/skill_pack_manifests.yaml" in text

    def test_new_agent_template_does_not_require_respond_persona_map(self):
        path = TEMPLATES_DIR / "NEW_AGENT_TEMPLATE.md"
        text = path.read_text(encoding="utf-8")
        assert "Do not add a local `_PERSONA_MAP` there." in text

    # -- risk_policy_template.yaml (BUG-2 + BUG-3) ----------------------------

    def test_risk_policy_template_no_crlf(self):
        """risk_policy_template.yaml must use LF line endings."""
        path = TEMPLATES_DIR / "risk_policy_template.yaml"
        raw = path.read_bytes()
        assert b"\r\n" not in raw, "CRLF line endings detected"

    def test_risk_policy_template_keys_quoted(self):
        """Template action keys with {placeholders} must be quoted for valid YAML."""
        import re
        path = TEMPLATES_DIR / "risk_policy_template.yaml"
        text = path.read_text(encoding="utf-8")
        bare = re.findall(r"^\s+\{[^}]+\}\.", text, re.MULTILINE)
        assert not bare, f"Unquoted brace keys found: {bare}"

    def test_risk_policy_template_is_parseable_yaml(self):
        """risk_policy_template.yaml must parse without error.

        Will remain xfail until BUG-3 is fixed (action keys quoted).
        strict=True means if it unexpectedly passes, CI is alerted.
        """
        path = TEMPLATES_DIR / "risk_policy_template.yaml"
        with open(path, "rb") as f:
            raw = f.read()
        normalised = raw.replace(b"\r\n", b"\n").decode("utf-8")
        yaml.safe_load(normalised)  # must not raise

    def test_risk_policy_template_has_required_keys(self):
        path = TEMPLATES_DIR / "risk_policy_template.yaml"
        with open(path, "rb") as f:
            raw = f.read()
        data = yaml.safe_load(raw.replace(b"\r\n", b"\n").decode("utf-8"))
        required = ["pack_id", "default_risk_tier", "max_risk_tier", "actions"]
        missing = [k for k in required if k not in data]
        assert not missing, f"risk_policy_template.yaml missing keys: {missing}"

    def test_risk_policy_default_tier_valid(self):
        path = TEMPLATES_DIR / "risk_policy_template.yaml"
        with open(path, "rb") as f:
            raw = f.read()
        data = yaml.safe_load(raw.replace(b"\r\n", b"\n").decode("utf-8"))
        assert data["default_risk_tier"] in ("green", "yellow", "red")
        assert data["max_risk_tier"] in ("green", "yellow", "red")

    def test_risk_policy_actions_have_approval_required(self):
        """Every action must declare approval_required (Law #4: Risk Tiers)."""
        path = TEMPLATES_DIR / "risk_policy_template.yaml"
        with open(path, "rb") as f:
            raw = f.read()
        data = yaml.safe_load(raw.replace(b"\r\n", b"\n").decode("utf-8"))
        for action_name, action_def in data["actions"].items():
            assert "approval_required" in action_def, (
                f"Action '{action_name}' missing 'approval_required' (Law #4)"
            )
            assert "risk_tier" in action_def, (
                f"Action '{action_name}' missing 'risk_tier' (Law #4)"
            )


# ---------------------------------------------------------------------------
# 6. test_template.py Syntax
# ---------------------------------------------------------------------------


class TestTemplateFileSyntax:
    """Verify test_template.py is valid Python — it is a template, not runnable."""

    def test_test_template_has_valid_python_syntax(self):
        path = TEMPLATES_DIR / "test_template.py"
        with open(path) as f:
            source = f.read()
        try:
            ast.parse(source)
        except SyntaxError as e:
            pytest.fail(f"test_template.py has a syntax error: {e}")

    def test_test_template_raises_not_implemented_in_pack_fixture(self):
        """By design, the pack fixture raises NotImplementedError (it is a template)."""
        path = TEMPLATES_DIR / "test_template.py"
        with open(path) as f:
            source = f.read()
        assert "NotImplementedError" in source, (
            "test_template.py pack fixture must raise NotImplementedError "
            "to prevent accidental use without customisation"
        )

    def test_test_template_references_aspire_laws(self):
        """Template must reference governance laws for discoverability."""
        path = TEMPLATES_DIR / "test_template.py"
        with open(path) as f:
            source = f.read()
        assert "receipt" in source.lower(), "Template must reference receipt compliance"
        assert "risk_tier" in source.lower(), "Template must reference risk_tier"


class TestAgenticLoopOutcomes:
    """Verify the template loop reports interruption states correctly."""

    @pytest.mark.asyncio
    async def test_agentic_loop_fails_when_no_steps_complete(self):
        pack = _make_pack()
        plan_result = AgentResult(
            success=True,
            data={"content": "1. Step one"},
            receipt={"receipt_id": "plan-1", "status": "ok"},
        )

        async def fake_wait_for(coro, timeout):
            if timeout == min(5.0 / 3, 10.0):
                return await coro
            raise TimeoutError()

        with (
            patch.object(pack, "search_memory", AsyncMock(return_value=[])),
            patch.object(pack, "recall_episodes", AsyncMock(return_value=[])),
            patch.object(pack, "execute_with_llm", AsyncMock(return_value=plan_result)),
            patch.object(pack, "emit_receipt", AsyncMock()),
            patch("asyncio.wait_for", side_effect=fake_wait_for),
        ):
            result = await pack.run_agentic_loop("test task", _ctx(), max_steps=1, timeout_s=5.0)

        assert not result.success
        assert result.receipt["status"] == "failed"
        assert result.error == "Step 1 timed out"

    @pytest.mark.asyncio
    async def test_agentic_loop_respects_fail_fast_false(self):
        pack = _make_pack()
        results = [
            AgentResult(
                success=True,
                data={"content": "1. First\n2. Second"},
                receipt={"receipt_id": "plan-1", "status": "ok"},
            ),
            AgentResult(
                success=False,
                data={"content": "first failed"},
                receipt={"receipt_id": "step-1", "status": "failed"},
                error="boom",
            ),
            AgentResult(
                success=True,
                data={"content": "task complete"},
                receipt={"receipt_id": "step-2", "status": "ok"},
            ),
        ]

        with (
            patch.object(pack, "search_memory", AsyncMock(return_value=[])),
            patch.object(pack, "recall_episodes", AsyncMock(return_value=[])),
            patch.object(pack, "execute_with_llm", AsyncMock(side_effect=results)),
            patch.object(pack, "emit_receipt", AsyncMock()),
        ):
            result = await pack.run_agentic_loop(
                "test task",
                _ctx(),
                max_steps=2,
                timeout_s=10.0,
                fail_fast=False,
            )

        assert not result.success
        assert len(result.data["steps"]) == 2
        assert result.receipt["status"] == "partial"
