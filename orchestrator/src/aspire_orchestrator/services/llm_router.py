"""LLM Router — 3-Tier Model Routing Service (Phase 3 Wave 1).

Maps (step_type, risk_tier) to model profiles, then resolves profiles
to concrete model IDs. Every routing decision emits a receipt.

Model profiles (from ecosystem router_policy.yaml):
  - CHEAP_CLASSIFIER: Intent classification, entity extraction, summarization (low risk)
  - FAST_GENERAL: General purpose drafting and reasoning (low-medium risk)
  - PRIMARY_REASONER: Complex reasoning, planning, verification (medium risk)
  - HIGH_RISK_GUARD: Financial, legal, and high-risk decisions (high risk)

Fallback chain: FAST_GENERAL -> PRIMARY_REASONER -> HIGH_RISK_GUARD

Law compliance:
  - Law #1: Router proposes model selection. Orchestrator decides.
  - Law #2: Every routing decision emits a model.route.selected receipt.
  - Law #3: Missing API key or config -> fail closed (use cheapest available model).
  - Law #9: Never log API keys or user content in routing decisions.
"""

from __future__ import annotations

import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================


class ModelProfile(str, Enum):
    """Model profiles from router_policy.yaml."""

    CHEAP_CLASSIFIER = "cheap_classifier"
    FAST_GENERAL = "fast_general"
    PRIMARY_REASONER = "primary_reasoner"
    HIGH_RISK_GUARD = "high_risk_guard"


class StepType(str, Enum):
    """Step types that determine which model profile to use."""

    CLASSIFY = "classify"
    EXTRACT = "extract"
    SUMMARIZE = "summarize"
    DRAFT = "draft"
    PLAN = "plan"
    VERIFY = "verify"
    POLICY_CHECK = "policy_check"


class RouterRiskTier(str, Enum):
    """Risk tiers for routing decisions (maps to Aspire GREEN/YELLOW/RED)."""

    LOW = "low"       # GREEN
    MEDIUM = "medium"  # YELLOW
    HIGH = "high"      # RED


# =============================================================================
# Models
# =============================================================================


class ModelConfig(BaseModel):
    """Concrete model configuration."""

    model_id: str = Field(description="OpenAI model identifier (e.g., gpt-5-mini)")
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="API base URL",
    )
    max_tokens: int = Field(default=1024, description="Max output tokens")
    temperature: float = Field(default=0.1, description="Sampling temperature")
    timeout_seconds: int = Field(default=15, description="Request timeout")


class RouteDecision(BaseModel):
    """Result of a routing decision."""

    selected_profile: ModelProfile
    concrete_model: str
    base_url: str
    step_type: str
    risk_tier: str
    fallback_used: bool = False
    fallback_reason: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.1
    timeout_seconds: int = 15


class RoutingRule(BaseModel):
    """A single routing rule from router_policy.yaml."""

    step_types: list[str]
    risk_tiers: list[str]
    profile: ModelProfile


# =============================================================================
# Config Loading
# =============================================================================


def _load_yaml_config(filename: str) -> dict[str, Any]:
    """Load a YAML config file from the config directory."""
    config_dir = Path(__file__).parent.parent / "config"
    filepath = config_dir / filename
    if not filepath.exists():
        logger.warning("Config file not found: %s", filepath)
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_routing_rules() -> list[RoutingRule]:
    """Load routing rules from router_policy.yaml."""
    config = _load_yaml_config("router_policy.yaml")
    rules: list[RoutingRule] = []

    for rule_data in config.get("rules", []):
        when = rule_data.get("when", {})
        profile_str = rule_data.get("use", "")
        try:
            profile = ModelProfile(profile_str.lower())
        except ValueError:
            logger.warning("Unknown profile '%s' in routing rules, skipping", profile_str)
            continue

        rules.append(RoutingRule(
            step_types=[s.lower() for s in when.get("step_type", [])],
            risk_tiers=[r.lower() for r in when.get("risk_tier", [])],
            profile=profile,
        ))

    return rules


def _load_fallback_chain() -> list[ModelProfile]:
    """Load the fallback chain from router_policy.yaml."""
    config = _load_yaml_config("router_policy.yaml")
    chain: list[ModelProfile] = []

    for profile_str in config.get("fallback_chain", []):
        try:
            chain.append(ModelProfile(profile_str.lower()))
        except ValueError:
            logger.warning("Unknown profile '%s' in fallback chain, skipping", profile_str)

    if not chain:
        # Default fallback chain
        chain = [
            ModelProfile.HIGH_RISK_GUARD,
            ModelProfile.PRIMARY_REASONER,
            ModelProfile.FAST_GENERAL,
        ]

    return chain


def _load_model_configs() -> dict[ModelProfile, ModelConfig]:
    """Load model profile -> concrete model mappings from router_models.yaml."""
    config = _load_yaml_config("router_models.yaml")
    models: dict[ModelProfile, ModelConfig] = {}

    for profile_str, model_data in config.get("profiles", {}).items():
        try:
            profile = ModelProfile(profile_str.lower())
        except ValueError:
            logger.warning("Unknown profile '%s' in model configs, skipping", profile_str)
            continue

        # Resolve model_id from env var if specified
        model_id = model_data.get("model_id", "gpt-5-mini")
        env_var = model_data.get("model_env")
        if env_var:
            model_id = os.environ.get(env_var, model_id)

        base_url = os.environ.get(
            "ASPIRE_OPENAI_BASE_URL",
            model_data.get("base_url", "https://api.openai.com/v1"),
        )

        models[profile] = ModelConfig(
            model_id=model_id,
            base_url=base_url,
            max_tokens=model_data.get("max_tokens", 1024),
            temperature=model_data.get("temperature", 0.1),
            timeout_seconds=model_data.get("timeout_seconds", 15),
        )

    # Ensure all profiles have defaults
    defaults = {
        ModelProfile.CHEAP_CLASSIFIER: ModelConfig(model_id="gpt-5-mini"),
        ModelProfile.FAST_GENERAL: ModelConfig(model_id="gpt-5"),
        ModelProfile.PRIMARY_REASONER: ModelConfig(model_id="gpt-5.2"),
        ModelProfile.HIGH_RISK_GUARD: ModelConfig(model_id="gpt-5.2"),
    }
    for profile, default_config in defaults.items():
        if profile not in models:
            models[profile] = default_config

    return models


# =============================================================================
# Desk Router Rules
# =============================================================================


def _load_desk_router_rules() -> dict[str, dict[str, Any]]:
    """Load desk-specific routing rules from config/desk_router_rules/."""
    config_dir = Path(__file__).parent.parent / "config" / "desk_router_rules"
    rules: dict[str, dict[str, Any]] = {}

    if not config_dir.exists():
        return rules

    for filepath in sorted(config_dir.glob("*.yaml")):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            rule_id = data.get("rule_id", filepath.stem)
            rules[rule_id] = data
        except Exception as e:
            logger.warning("Failed to load desk router rule %s: %s", filepath.name, e)

    return rules


# =============================================================================
# LLM Router
# =============================================================================


class LLMRouter:
    """3-tier LLM model router.

    Routes (step_type, risk_tier) to the appropriate model profile,
    then resolves the profile to a concrete model configuration.

    Thread-safe, stateless after initialization.
    """

    def __init__(self) -> None:
        self._rules = _load_routing_rules()
        self._fallback_chain = _load_fallback_chain()
        self._model_configs = _load_model_configs()
        self._desk_rules = _load_desk_router_rules()
        self._api_key: str | None = os.environ.get("ASPIRE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")

        logger.info(
            "LLMRouter initialized: rules=%d, models=%d, desk_rules=%d, api_key=%s",
            len(self._rules),
            len(self._model_configs),
            len(self._desk_rules),
            "set" if self._api_key else "NOT SET",
        )

    @property
    def api_key(self) -> str | None:
        """Get the OpenAI API key (never log this)."""
        return self._api_key

    def route(
        self,
        step_type: str,
        risk_tier: str,
        *,
        desk: str | None = None,
    ) -> RouteDecision:
        """Route a step to the appropriate model profile.

        Args:
            step_type: The type of processing step (classify, draft, plan, etc.)
            risk_tier: The risk tier (low/medium/high or green/yellow/red)
            desk: Optional desk identifier for desk-specific routing overrides

        Returns:
            RouteDecision with the selected profile and concrete model config.
        """
        start_time = time.monotonic()

        # Normalize risk tier (Aspire uses green/yellow/red, router uses low/medium/high)
        normalized_risk = self._normalize_risk_tier(risk_tier)
        normalized_step = step_type.lower()

        # Try desk-specific override first
        if desk:
            desk_profile = self._check_desk_override(desk, normalized_step, normalized_risk)
            if desk_profile is not None:
                config = self._model_configs[desk_profile]
                latency_ms = (time.monotonic() - start_time) * 1000
                logger.debug(
                    "Desk override: desk=%s step=%s risk=%s -> %s (%.1fms)",
                    desk, normalized_step, normalized_risk, desk_profile.value, latency_ms,
                )
                return RouteDecision(
                    selected_profile=desk_profile,
                    concrete_model=config.model_id,
                    base_url=config.base_url,
                    step_type=normalized_step,
                    risk_tier=normalized_risk,
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    timeout_seconds=config.timeout_seconds,
                )

        # Match against routing rules
        profile = self._match_rules(normalized_step, normalized_risk)

        if profile is None:
            # Fallback chain
            profile = self._fallback_chain[0] if self._fallback_chain else ModelProfile.FAST_GENERAL
            config = self._model_configs[profile]
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.debug(
                "Fallback: step=%s risk=%s -> %s (%.1fms)",
                normalized_step, normalized_risk, profile.value, latency_ms,
            )
            return RouteDecision(
                selected_profile=profile,
                concrete_model=config.model_id,
                base_url=config.base_url,
                step_type=normalized_step,
                risk_tier=normalized_risk,
                fallback_used=True,
                fallback_reason=f"No rule matched step_type={normalized_step}, risk_tier={normalized_risk}",
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                timeout_seconds=config.timeout_seconds,
            )

        config = self._model_configs[profile]
        latency_ms = (time.monotonic() - start_time) * 1000
        logger.debug(
            "Routed: step=%s risk=%s -> %s (%s, %.1fms)",
            normalized_step, normalized_risk, profile.value, config.model_id, latency_ms,
        )

        return RouteDecision(
            selected_profile=profile,
            concrete_model=config.model_id,
            base_url=config.base_url,
            step_type=normalized_step,
            risk_tier=normalized_risk,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout_seconds=config.timeout_seconds,
        )

    def get_model_config(self, profile: ModelProfile) -> ModelConfig:
        """Get the concrete model config for a profile."""
        return self._model_configs[profile]

    def get_desk_rules(self) -> dict[str, dict[str, Any]]:
        """Get all loaded desk routing rules."""
        return dict(self._desk_rules)

    def _normalize_risk_tier(self, risk_tier: str) -> str:
        """Normalize Aspire risk tier names to router risk tier names."""
        mapping = {
            "green": "low",
            "yellow": "medium",
            "red": "high",
            "low": "low",
            "medium": "medium",
            "high": "high",
        }
        return mapping.get(risk_tier.lower(), "medium")

    def _match_rules(self, step_type: str, risk_tier: str) -> ModelProfile | None:
        """Match step_type + risk_tier against routing rules."""
        for rule in self._rules:
            if step_type in rule.step_types and risk_tier in rule.risk_tiers:
                return rule.profile
        return None

    def _check_desk_override(
        self,
        desk: str,
        step_type: str,
        risk_tier: str,
    ) -> ModelProfile | None:
        """Check if a desk has a specific model override for this step/risk combo."""
        # Look for desk-specific routing rule
        desk_key = f"{desk}_router"
        if desk_key not in self._desk_rules:
            # Try without _router suffix
            desk_key = desk
        if desk_key not in self._desk_rules:
            return None

        rule_data = self._desk_rules[desk_key]
        overrides = rule_data.get("model_overrides", {})

        # Check for step_type-specific override
        override_key = f"{step_type}.{risk_tier}"
        if override_key in overrides:
            try:
                return ModelProfile(overrides[override_key].lower())
            except ValueError:
                pass

        # Check for risk_tier-only override
        if risk_tier in overrides:
            try:
                return ModelProfile(overrides[risk_tier].lower())
            except ValueError:
                pass

        return None


# =============================================================================
# Receipt Builder
# =============================================================================


def build_route_receipt(
    decision: RouteDecision,
    *,
    correlation_id: str,
    suite_id: str,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    """Build a model.route.selected receipt for a routing decision (Law #2).

    Every routing decision produces a receipt for auditability.
    """
    import uuid
    from datetime import datetime, timezone

    return {
        "receipt_version": "1.0",
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": "model.route.selected",
        "suite_id": suite_id,
        "correlation_id": correlation_id,
        "actor": "service:llm_router",
        "status": "ok",
        "data": {
            "selected_profile": decision.selected_profile.value,
            "concrete_model": decision.concrete_model,
            "step_type": decision.step_type,
            "risk_tier": decision.risk_tier,
            "fallback_used": decision.fallback_used,
            "fallback_reason": decision.fallback_reason,
            "latency_ms": latency_ms,
        },
        "policy": {
            "decision": "allow",
            "policy_id": "router-policy-v1",
            "reasons": [],
        },
        "redactions": [],
    }


# =============================================================================
# Module-level singleton
# =============================================================================

_cached_router: LLMRouter | None = None


def get_llm_router(*, reload: bool = False) -> LLMRouter:
    """Get the cached LLMRouter singleton."""
    global _cached_router
    if _cached_router is None or reload:
        _cached_router = LLMRouter()
    return _cached_router
