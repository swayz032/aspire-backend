"""Tests for OpenAI Agents SDK Framework — Phase 3 Wave 2.

Tests cover:
- AspireAgentBase: LLM routing, receipt building, Trust Spine adapter
- ManifestLoader: JSON manifest loading and validation
- PersonaLoader: Persona/system_prompt loading
- PackPolicyLoader: Per-pack policy loading
- EnhancedSkillPack: Integration of all components
- TrustSpineAdapter: Token validation, receipt emission, policy checks

Target: ~25 tests for W2.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.agent_sdk_base import (
    AgentContext,
    AgentResult,
    AspireAgentBase,
    TrustSpineAdapter,
    TokenValidation,
    PolicyDecision,
)
from aspire_orchestrator.services.manifest_loader import (
    load_manifest,
    load_all_manifests,
    get_manifest_schema,
)
from aspire_orchestrator.services.persona_loader import (
    load_persona,
    load_all_personas,
)
from aspire_orchestrator.services.pack_policy_loader import (
    get_autonomy_policy,
    get_observability_policy,
    get_prompt_contract,
    load_pack_policies,
    load_all_pack_policies,
    get_risk_policy,
    get_tool_policy,
)
from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def agent_context():
    """Create a standard agent context for testing."""
    return AgentContext(
        suite_id=str(uuid.uuid4()),
        office_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        actor_id="test-user",
        actor_type="user",
        risk_tier="green",
    )


@pytest.fixture
def trust_spine():
    """Create a mock Trust Spine adapter."""
    adapter = TrustSpineAdapter()
    return adapter


@pytest.fixture
def base_agent(trust_spine):
    """Create a base agent instance."""
    return AspireAgentBase(
        agent_id="test-agent",
        agent_name="Test Agent",
        default_risk_tier="green",
        trust_spine=trust_spine,
    )


@pytest.fixture
def temp_manifest_dir():
    """Create a temporary directory with test manifests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest = {
            "skillpack_id": "test-pack",
            "name": "Test Pack",
            "version": "1.0.0",
            "channel": "internal_frontend",
            "capabilities": ["can_test"],
            "risk_profile": {
                "default_risk_tier": "green",
                "max_risk_tier": "yellow",
            },
            "tools": ["test.tool.run"],
            "certification_status": "uncertified",
        }
        filepath = Path(tmpdir) / "test-pack.json"
        with open(filepath, "w") as f:
            json.dump(manifest, f)
        yield tmpdir


@pytest.fixture
def temp_persona_dir():
    """Create a temporary directory with test personas."""
    with tempfile.TemporaryDirectory() as tmpdir:
        persona_text = "You are Test Agent. You help with testing."
        filepath = Path(tmpdir) / "test_agent_system_prompt.md"
        filepath.write_text(persona_text)
        yield tmpdir


@pytest.fixture
def temp_policy_dir():
    """Create a temporary directory with test policies."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pack_dir = Path(tmpdir) / "test_agent"
        pack_dir.mkdir()

        (pack_dir / "risk_policy.yaml").write_text(
            "default_tier: green\nmax_tier: yellow\n"
        )
        (pack_dir / "tool_policy.yaml").write_text(
            "allowed_tools:\n  - test.tool.run\n"
        )
        (pack_dir / "autonomy_policy.yaml").write_text(
            "autonomy:\n  max_agentic_iterations: 3\n  timeout_ms: 7000\n"
        )
        (pack_dir / "observability_policy.yaml").write_text(
            "alerts:\n  - quality_regression\nmetrics:\n  - response_quality_score\n"
        )
        (pack_dir / "prompt_contract.md").write_text(
            "Never claim execution without receipt.\nUse concise operational language.\n"
        )
        yield tmpdir


# =============================================================================
# AspireAgentBase Tests
# =============================================================================


class TestAspireAgentBase:
    """Test the base agent class."""

    def test_agent_properties(self, base_agent):
        assert base_agent.agent_id == "test-agent"
        assert base_agent.agent_name == "Test Agent"
        assert base_agent.default_risk_tier == "green"

    def test_set_persona(self, base_agent):
        base_agent.set_persona("You are a test agent.")
        assert base_agent.persona == "You are a test agent."

    def test_set_manifest(self, base_agent):
        manifest = {"skillpack_id": "test", "name": "Test", "version": "1.0.0"}
        base_agent.set_manifest(manifest)
        assert base_agent.manifest == manifest

    def test_build_effective_system_prompt_includes_prompt_contract(self, base_agent):
        base_agent.set_persona("You are a test agent.")
        base_agent.set_policies({"prompt_contract": "Never claim execution without receipt."})
        prompt = base_agent.build_effective_system_prompt()
        assert "You are a test agent." in prompt
        assert "Runtime Prompt Contract" in prompt
        assert "Never claim execution without receipt." in prompt

    def test_compute_inputs_hash_deterministic(self, base_agent):
        inputs = {"action": "test", "value": 42}
        hash1 = base_agent.compute_inputs_hash(inputs)
        hash2 = base_agent.compute_inputs_hash(inputs)
        assert hash1 == hash2

    def test_compute_inputs_hash_starts_with_sha256(self, base_agent):
        inputs = {"action": "test"}
        result = base_agent.compute_inputs_hash(inputs)
        assert result.startswith("sha256:")

    def test_compute_inputs_hash_different_for_different_inputs(self, base_agent):
        hash1 = base_agent.compute_inputs_hash({"action": "test1"})
        hash2 = base_agent.compute_inputs_hash({"action": "test2"})
        assert hash1 != hash2

    def test_build_receipt_structure(self, base_agent, agent_context):
        receipt = base_agent.build_receipt(
            ctx=agent_context,
            event_type="test.action",
            status="ok",
            inputs={"action": "test"},
        )

        assert receipt["receipt_version"] == "1.0"
        assert receipt["event_type"] == "test.action"
        assert receipt["status"] == "ok"
        assert receipt["actor"] == "skillpack:test-agent"
        assert receipt["suite_id"] == agent_context.suite_id
        assert receipt["office_id"] == agent_context.office_id
        assert receipt["correlation_id"] == agent_context.correlation_id
        assert "receipt_id" in receipt
        assert "ts" in receipt
        assert "inputs_hash" in receipt
        assert "policy" in receipt

    def test_build_receipt_with_metadata(self, base_agent, agent_context):
        receipt = base_agent.build_receipt(
            ctx=agent_context,
            event_type="test.action",
            status="ok",
            inputs={"action": "test"},
            metadata={"model_used": "gpt-5-mini"},
        )
        assert receipt["metadata"]["model_used"] == "gpt-5-mini"


# =============================================================================
# TrustSpineAdapter Tests
# =============================================================================


class TestTrustSpineAdapter:
    """Test the Trust Spine adapter."""

    @pytest.mark.asyncio
    async def test_emit_receipt(self, trust_spine):
        receipt = {
            "receipt_id": str(uuid.uuid4()),
            "event_type": "test",
            "status": "ok",
        }
        result = await trust_spine.emit_receipt(receipt)
        assert result == receipt["receipt_id"]

    @pytest.mark.asyncio
    async def test_check_idempotency_default_false(self, trust_spine):
        """Idempotency check returns False by default (not yet executed)."""
        result = await trust_spine.check_idempotency("key-123", "suite-123")
        assert result is False


# =============================================================================
# ManifestLoader Tests
# =============================================================================


class TestManifestLoader:
    """Test manifest loading and validation."""

    def test_load_manifest_valid(self, temp_manifest_dir):
        filepath = Path(temp_manifest_dir) / "test-pack.json"
        manifest = load_manifest(filepath)
        assert manifest["skillpack_id"] == "test-pack"
        assert manifest["name"] == "Test Pack"
        assert manifest["version"] == "1.0.0"

    def test_load_manifest_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_manifest("/nonexistent/path/manifest.json")

    def test_load_manifest_invalid_schema(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"invalid": "manifest"}, f)
            f.flush()
            with pytest.raises(Exception):  # jsonschema.ValidationError
                load_manifest(f.name)
            os.unlink(f.name)

    def test_load_all_manifests(self, temp_manifest_dir):
        manifests = load_all_manifests(temp_manifest_dir)
        assert "test-pack" in manifests
        assert manifests["test-pack"]["name"] == "Test Pack"

    def test_load_all_manifests_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifests = load_all_manifests(tmpdir)
            assert manifests == {}

    def test_get_manifest_schema_returns_dict(self):
        schema = get_manifest_schema()
        assert isinstance(schema, dict)
        assert "properties" in schema


# =============================================================================
# PersonaLoader Tests
# =============================================================================


class TestPersonaLoader:
    """Test persona/system_prompt loading."""

    def test_load_persona_found(self, temp_persona_dir):
        persona = load_persona("test_agent", directory=temp_persona_dir)
        assert persona == "You are Test Agent. You help with testing."

    def test_load_persona_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persona = load_persona("nonexistent", directory=tmpdir)
            assert persona is None

    def test_load_all_personas(self, temp_persona_dir):
        personas = load_all_personas(directory=temp_persona_dir)
        assert "test_agent" in personas


# =============================================================================
# PackPolicyLoader Tests
# =============================================================================


class TestPackPolicyLoader:
    """Test per-pack policy loading."""

    def test_load_pack_policies(self, temp_policy_dir):
        policies = load_pack_policies("test_agent", directory=temp_policy_dir)
        assert "risk_policy" in policies
        assert "tool_policy" in policies
        assert policies["risk_policy"]["default_tier"] == "green"

    def test_load_pack_policies_missing_pack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policies = load_pack_policies("nonexistent", directory=tmpdir)
            assert policies == {}

    def test_get_risk_policy_from_loaded(self, temp_policy_dir):
        policies = load_pack_policies("test_agent", directory=temp_policy_dir)
        risk = get_risk_policy("test_agent", policies)
        assert risk["default_tier"] == "green"

    def test_get_tool_policy_from_loaded(self, temp_policy_dir):
        policies = load_pack_policies("test_agent", directory=temp_policy_dir)
        tool_pol = get_tool_policy("test_agent", policies)
        assert "test.tool.run" in tool_pol["allowed_tools"]

    def test_get_risk_policy_default(self):
        """Missing risk policy returns safe default."""
        risk = get_risk_policy("nonexistent")
        assert risk.get("default_tier") == "green"

    def test_get_tool_policy_default(self):
        """Missing tool policy returns empty allowlist (fail-closed)."""
        tool_pol = get_tool_policy("nonexistent")
        assert tool_pol.get("allowed_tools") == []

    def test_get_autonomy_policy_from_loaded(self, temp_policy_dir):
        policies = load_pack_policies("test_agent", directory=temp_policy_dir)
        autonomy = get_autonomy_policy("test_agent", policies)
        assert autonomy["autonomy"]["max_agentic_iterations"] == 3

    def test_get_observability_policy_from_loaded(self, temp_policy_dir):
        policies = load_pack_policies("test_agent", directory=temp_policy_dir)
        observability = get_observability_policy("test_agent", policies)
        assert "quality_regression" in observability["alerts"]

    def test_get_prompt_contract_from_directory(self, temp_policy_dir):
        contract = get_prompt_contract("test_agent", load_pack_policies("test_agent", directory=temp_policy_dir))
        assert "Never claim execution without receipt." in contract


# =============================================================================
# EnhancedSkillPack Tests
# =============================================================================


class TestEnhancedSkillPack:
    """Test the enhanced base skill pack class."""

    def test_create_enhanced_pack(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        assert pack.agent_id == "test-pack"
        assert pack.agent_name == "Test Pack"

    def test_get_capability_list_empty(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        assert pack.get_capability_list() == []

    def test_get_capability_list_from_manifest(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        pack.set_manifest({
            "skillpack_id": "test-pack",
            "name": "Test Pack",
            "version": "1.0.0",
            "capabilities": ["can_search", "can_draft"],
        })
        assert pack.get_capability_list() == ["can_search", "can_draft"]

    def test_is_certified_uncertified(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        assert pack.is_certified() is False

    def test_is_certified_with_manifest(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        pack.set_manifest({
            "skillpack_id": "test-pack",
            "name": "Test Pack",
            "version": "1.0.0",
            "certification_status": "certified",
        })
        assert pack.is_certified() is True

    def test_get_max_risk_tier_default(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            default_risk_tier="green",
            auto_load_config=False,
        )
        assert pack.get_max_risk_tier() == "green"

    def test_get_max_risk_tier_from_manifest(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        pack.set_manifest({
            "skillpack_id": "test-pack",
            "name": "Test Pack",
            "version": "1.0.0",
            "risk_profile": {
                "default_risk_tier": "green",
                "max_risk_tier": "red",
            },
        })
        assert pack.get_max_risk_tier() == "red"

    def test_get_autonomy_policy_default(self):
        pack = EnhancedSkillPack(
            agent_id="test-pack",
            agent_name="Test Pack",
            auto_load_config=False,
        )
        assert pack.get_autonomy_policy()["autonomy"]["max_agentic_iterations"] >= 1


# =============================================================================
# AgentContext Tests
# =============================================================================


class TestAgentContext:
    """Test agent context dataclass."""

    def test_context_creation(self, agent_context):
        assert agent_context.suite_id
        assert agent_context.office_id
        assert agent_context.correlation_id
        assert agent_context.actor_id == "test-user"
        assert agent_context.actor_type == "user"
        assert agent_context.risk_tier == "green"

    def test_context_immutable(self, agent_context):
        """AgentContext should be frozen (immutable)."""
        with pytest.raises(AttributeError):
            agent_context.suite_id = "new-id"
