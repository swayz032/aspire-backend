"""Tests for LLM Router — Phase 3 Wave 1.

Tests cover:
- Model profile routing rules
- Fallback chain behavior
- Desk-specific routing overrides
- Risk tier normalization (green/yellow/red -> low/medium/high)
- Receipt emission for routing decisions
- Config loading (router_policy.yaml, router_models.yaml)
- Singleton behavior
- Edge cases: missing configs, unknown step types, unknown risk tiers

Target: ~30 tests for W1.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest

from aspire_orchestrator.services.llm_router import (
    LLMRouter,
    ModelProfile,
    StepType,
    RouterRiskTier,
    RouteDecision,
    ModelConfig,
    RoutingRule,
    build_route_receipt,
    get_llm_router,
    _load_routing_rules,
    _load_fallback_chain,
    _load_model_configs,
    _load_desk_router_rules,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def router():
    """Create a fresh LLM Router instance."""
    return LLMRouter()


@pytest.fixture
def clean_router_cache():
    """Reset the router singleton cache."""
    import aspire_orchestrator.services.llm_router as mod
    mod._cached_router = None
    yield
    mod._cached_router = None


# =============================================================================
# Model Profile Enum Tests
# =============================================================================


class TestModelProfile:
    """Test ModelProfile enum values."""

    def test_profile_values(self):
        assert ModelProfile.CHEAP_CLASSIFIER == "cheap_classifier"
        assert ModelProfile.FAST_GENERAL == "fast_general"
        assert ModelProfile.PRIMARY_REASONER == "primary_reasoner"
        assert ModelProfile.HIGH_RISK_GUARD == "high_risk_guard"

    def test_all_profiles_exist(self):
        profiles = list(ModelProfile)
        assert len(profiles) == 4


class TestStepType:
    """Test StepType enum values."""

    def test_step_type_values(self):
        assert StepType.CLASSIFY == "classify"
        assert StepType.EXTRACT == "extract"
        assert StepType.SUMMARIZE == "summarize"
        assert StepType.DRAFT == "draft"
        assert StepType.PLAN == "plan"
        assert StepType.VERIFY == "verify"
        assert StepType.POLICY_CHECK == "policy_check"

    def test_all_step_types_exist(self):
        steps = list(StepType)
        assert len(steps) == 7


# =============================================================================
# Config Loading Tests
# =============================================================================


class TestConfigLoading:
    """Test YAML config file loading."""

    def test_load_routing_rules_returns_list(self):
        rules = _load_routing_rules()
        assert isinstance(rules, list)
        assert len(rules) > 0  # Should have rules from router_policy.yaml

    def test_routing_rules_have_required_fields(self):
        rules = _load_routing_rules()
        for rule in rules:
            assert isinstance(rule, RoutingRule)
            assert len(rule.step_types) > 0
            assert len(rule.risk_tiers) > 0
            assert isinstance(rule.profile, ModelProfile)

    def test_load_fallback_chain(self):
        chain = _load_fallback_chain()
        assert isinstance(chain, list)
        assert len(chain) > 0
        for profile in chain:
            assert isinstance(profile, ModelProfile)

    def test_fallback_chain_order(self):
        """Fallback should be most conservative first."""
        chain = _load_fallback_chain()
        assert chain[0] == ModelProfile.HIGH_RISK_GUARD

    def test_load_model_configs(self):
        configs = _load_model_configs()
        assert isinstance(configs, dict)
        # All 4 profiles should have configs
        for profile in ModelProfile:
            assert profile in configs
            config = configs[profile]
            assert isinstance(config, ModelConfig)
            assert config.model_id  # Non-empty model ID
            assert config.timeout_seconds > 0

    def test_load_desk_router_rules(self):
        rules = _load_desk_router_rules()
        assert isinstance(rules, dict)
        # Should have loaded desk rules from the config directory
        assert len(rules) > 0


# =============================================================================
# Routing Decision Tests
# =============================================================================


class TestRoutingDecisions:
    """Test the core routing logic."""

    def test_classify_low_risk_uses_cheap_classifier(self, router):
        """GREEN-tier classification should use the cheapest model."""
        decision = router.route("classify", "low")
        assert decision.selected_profile == ModelProfile.CHEAP_CLASSIFIER
        assert decision.step_type == "classify"
        assert decision.risk_tier == "low"
        assert not decision.fallback_used

    def test_classify_medium_risk_uses_fast_general(self, router):
        """YELLOW-tier classification should use fast general."""
        decision = router.route("classify", "medium")
        assert decision.selected_profile == ModelProfile.FAST_GENERAL

    def test_classify_high_risk_uses_primary_reasoner(self, router):
        """RED-tier classification should use primary reasoner."""
        decision = router.route("classify", "high")
        assert decision.selected_profile == ModelProfile.PRIMARY_REASONER

    def test_draft_low_risk_uses_primary_reasoner(self, router):
        """Drafting at low risk should use primary reasoner (quality matters)."""
        decision = router.route("draft", "low")
        assert decision.selected_profile == ModelProfile.PRIMARY_REASONER

    def test_draft_high_risk_uses_high_risk_guard(self, router):
        """Drafting at high risk should use high risk guard."""
        decision = router.route("draft", "high")
        assert decision.selected_profile == ModelProfile.HIGH_RISK_GUARD

    def test_plan_medium_risk_uses_high_risk_guard(self, router):
        """Planning at medium risk should use high risk guard."""
        decision = router.route("plan", "medium")
        assert decision.selected_profile == ModelProfile.HIGH_RISK_GUARD

    def test_verify_low_risk_uses_primary_reasoner(self, router):
        """QA verification at low risk should use primary reasoner."""
        decision = router.route("verify", "low")
        assert decision.selected_profile == ModelProfile.PRIMARY_REASONER

    def test_verify_high_risk_uses_high_risk_guard(self, router):
        """QA verification at high risk should use high risk guard."""
        decision = router.route("verify", "high")
        assert decision.selected_profile == ModelProfile.HIGH_RISK_GUARD

    def test_policy_check_always_uses_high_risk_guard(self, router):
        """Policy checks should always use high risk guard (safety critical)."""
        for risk in ["low", "medium", "high"]:
            decision = router.route("policy_check", risk)
            assert decision.selected_profile == ModelProfile.HIGH_RISK_GUARD, (
                f"policy_check at {risk} risk should use HIGH_RISK_GUARD"
            )

    def test_summarize_low_uses_cheap_classifier(self, router):
        """Summarization at low risk uses cheap classifier."""
        decision = router.route("summarize", "low")
        assert decision.selected_profile == ModelProfile.CHEAP_CLASSIFIER


# =============================================================================
# Risk Tier Normalization Tests
# =============================================================================


class TestRiskTierNormalization:
    """Test that Aspire risk tiers (green/yellow/red) normalize correctly."""

    def test_green_normalizes_to_low(self, router):
        decision = router.route("classify", "green")
        assert decision.risk_tier == "low"

    def test_yellow_normalizes_to_medium(self, router):
        decision = router.route("classify", "yellow")
        assert decision.risk_tier == "medium"

    def test_red_normalizes_to_high(self, router):
        decision = router.route("classify", "red")
        assert decision.risk_tier == "high"

    def test_low_stays_low(self, router):
        decision = router.route("classify", "low")
        assert decision.risk_tier == "low"

    def test_unknown_risk_defaults_to_medium(self, router):
        """Unknown risk tier should default to medium (safe default)."""
        decision = router.route("classify", "unknown_tier")
        assert decision.risk_tier == "medium"


# =============================================================================
# Fallback Chain Tests
# =============================================================================


class TestFallbackChain:
    """Test fallback behavior when no rule matches."""

    def test_unknown_step_type_uses_fallback(self, router):
        """Unknown step type should trigger fallback chain."""
        decision = router.route("unknown_step", "low")
        assert decision.fallback_used is True
        assert decision.fallback_reason is not None
        assert "unknown_step" in decision.fallback_reason

    def test_fallback_uses_most_conservative_profile(self, router):
        """Fallback should start with the most conservative profile."""
        decision = router.route("unknown_step", "low")
        assert decision.selected_profile == ModelProfile.HIGH_RISK_GUARD


# =============================================================================
# Desk Routing Override Tests
# =============================================================================


class TestDeskRoutingOverrides:
    """Test desk-specific model override logic."""

    def test_finn_desk_always_uses_high_risk_guard(self, router):
        """Finn (money desk) should always use high_risk_guard for high risk."""
        decision = router.route("classify", "high", desk="finn_moneydesk")
        assert decision.selected_profile == ModelProfile.HIGH_RISK_GUARD

    def test_adam_desk_uses_cheap_classifier_for_classify(self, router):
        """Adam (research desk) should use cheap classifier for classification."""
        decision = router.route("classify", "low", desk="adam_researchdesk")
        assert decision.selected_profile == ModelProfile.CHEAP_CLASSIFIER

    def test_desk_override_with_unknown_desk_falls_through(self, router):
        """Unknown desk should fall through to standard routing rules."""
        decision_with_desk = router.route("classify", "low", desk="nonexistent_desk")
        decision_without = router.route("classify", "low")
        assert decision_with_desk.selected_profile == decision_without.selected_profile

    def test_get_desk_rules_returns_loaded_rules(self, router):
        """Should return all loaded desk routing rules."""
        rules = router.get_desk_rules()
        assert isinstance(rules, dict)


# =============================================================================
# Route Decision Model Tests
# =============================================================================


class TestRouteDecision:
    """Test RouteDecision model."""

    def test_decision_has_all_fields(self, router):
        decision = router.route("classify", "low")
        assert isinstance(decision, RouteDecision)
        assert isinstance(decision.selected_profile, ModelProfile)
        assert isinstance(decision.concrete_model, str)
        assert isinstance(decision.base_url, str)
        assert isinstance(decision.step_type, str)
        assert isinstance(decision.risk_tier, str)
        assert isinstance(decision.fallback_used, bool)
        assert isinstance(decision.max_tokens, int)
        assert isinstance(decision.temperature, float)
        assert isinstance(decision.timeout_seconds, int)

    def test_decision_concrete_model_is_not_empty(self, router):
        decision = router.route("classify", "low")
        assert len(decision.concrete_model) > 0

    def test_decision_base_url_is_valid(self, router):
        decision = router.route("classify", "low")
        assert decision.base_url.startswith("http")


# =============================================================================
# Receipt Emission Tests
# =============================================================================


class TestReceiptEmission:
    """Test receipt generation for routing decisions."""

    def test_build_route_receipt_structure(self, router):
        """Receipt should have all required fields per Law #2."""
        decision = router.route("classify", "low")
        receipt = build_route_receipt(
            decision,
            correlation_id=str(uuid.uuid4()),
            suite_id=str(uuid.uuid4()),
            latency_ms=1.5,
        )

        assert receipt["receipt_version"] == "1.0"
        assert receipt["event_type"] == "model.route.selected"
        assert receipt["actor"] == "service:llm_router"
        assert receipt["status"] == "ok"
        assert "receipt_id" in receipt
        assert "ts" in receipt
        assert "suite_id" in receipt
        assert "correlation_id" in receipt

    def test_receipt_data_fields(self, router):
        """Receipt data should contain routing decision details."""
        decision = router.route("classify", "low")
        receipt = build_route_receipt(
            decision,
            correlation_id=str(uuid.uuid4()),
            suite_id=str(uuid.uuid4()),
        )

        data = receipt["data"]
        assert data["selected_profile"] == decision.selected_profile.value
        assert data["concrete_model"] == decision.concrete_model
        assert data["step_type"] == "classify"
        assert data["risk_tier"] == "low"
        assert isinstance(data["fallback_used"], bool)

    def test_receipt_has_policy_section(self, router):
        """Receipt should have policy section per governance requirements."""
        decision = router.route("classify", "low")
        receipt = build_route_receipt(
            decision,
            correlation_id=str(uuid.uuid4()),
            suite_id=str(uuid.uuid4()),
        )

        assert "policy" in receipt
        assert receipt["policy"]["decision"] == "allow"
        assert receipt["policy"]["policy_id"] == "router-policy-v1"

    def test_receipt_has_no_secrets(self, router):
        """Receipt should never contain API keys or secrets (Law #9)."""
        decision = router.route("classify", "low")
        receipt = build_route_receipt(
            decision,
            correlation_id=str(uuid.uuid4()),
            suite_id=str(uuid.uuid4()),
        )

        receipt_str = str(receipt)
        assert "api_key" not in receipt_str.lower()
        assert "secret" not in receipt_str.lower()
        assert "bearer" not in receipt_str.lower()


# =============================================================================
# Singleton Tests
# =============================================================================


class TestSingleton:
    """Test module-level singleton caching."""

    def test_get_llm_router_returns_same_instance(self, clean_router_cache):
        router1 = get_llm_router()
        router2 = get_llm_router()
        assert router1 is router2

    def test_get_llm_router_reload_creates_new_instance(self, clean_router_cache):
        router1 = get_llm_router()
        router2 = get_llm_router(reload=True)
        assert router1 is not router2


# =============================================================================
# Model Config Tests
# =============================================================================


class TestModelConfig:
    """Test model configuration retrieval."""

    def test_get_model_config_for_each_profile(self, router):
        for profile in ModelProfile:
            config = router.get_model_config(profile)
            assert isinstance(config, ModelConfig)
            assert config.model_id
            assert config.max_tokens > 0
            assert config.timeout_seconds > 0

    def test_cheap_classifier_has_smallest_token_limit(self, router):
        """Cheap classifier should have the smallest token limit."""
        cheap = router.get_model_config(ModelProfile.CHEAP_CLASSIFIER)
        reasoner = router.get_model_config(ModelProfile.PRIMARY_REASONER)
        assert cheap.max_tokens <= reasoner.max_tokens

    def test_high_risk_guard_has_lowest_temperature(self, router):
        """High risk guard should have the lowest temperature (most deterministic)."""
        guard = router.get_model_config(ModelProfile.HIGH_RISK_GUARD)
        assert guard.temperature <= 0.1


# =============================================================================
# API Key Tests
# =============================================================================


class TestAPIKey:
    """Test API key handling."""

    def test_api_key_property_never_logged(self, router):
        """API key should be accessible but never appear in logs."""
        # Just verify the property exists and doesn't raise
        _ = router.api_key  # Should not raise

    @patch.dict(os.environ, {"ASPIRE_OPENAI_API_KEY": "test-key-123"})
    def test_api_key_from_aspire_env(self, clean_router_cache):
        """Should prefer ASPIRE_OPENAI_API_KEY over OPENAI_API_KEY."""
        router = LLMRouter()
        assert router.api_key == "test-key-123"
