"""Control Plane Registry — Skill Pack + Tool + Provider catalog.

Per architecture.md: the Control Plane Registry provides capability
discovery so the orchestrator (Law #1) can route intents to the
correct skill pack.

Responsibilities:
1. Load skill pack manifests from YAML config
2. Resolve action_type → skill pack for dispatch
3. Resolve tool_id → provider for execution context
4. Provide capability discovery API for UI/clients
5. Support per-suite enable/disable of skill packs

Pattern: YAML-driven singleton, same as policy_engine.py.
Phase 1: In-memory from YAML. Phase 2+: Supabase-backed with per-suite config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aspire_orchestrator.models import RiskTier

logger = logging.getLogger(__name__)

_DEFAULT_MANIFEST_PATH = Path(__file__).parent.parent / "config" / "skill_pack_manifests.yaml"

_RISK_TIER_MAP = {
    "green": RiskTier.GREEN,
    "yellow": RiskTier.YELLOW,
    "red": RiskTier.RED,
}


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class SkillPackManifest:
    """A registered skill pack in the Control Plane."""

    id: str
    name: str
    owner: str
    category: str
    risk_tier: RiskTier
    status: str  # registered | active | suspended
    description: str
    actions: list[str]
    providers: list[str]
    capability_scopes: list[str]
    tools: list[str]
    per_suite_enabled: bool


@dataclass(frozen=True)
class ToolDefinition:
    """A registered tool in the Control Plane."""

    id: str
    provider: str
    category: str
    risk_tier: RiskTier


@dataclass(frozen=True)
class ProviderDefinition:
    """An external provider registration."""

    id: str
    name: str
    category: str
    auth_type: str
    base_url: str
    rate_limit_rpm: int
    timeout_ms: int
    retry_strategy: str
    idempotency_support: bool


@dataclass(frozen=True)
class SkillPackRouteResult:
    """Result of routing an action_type to a skill pack."""

    found: bool
    skill_pack_id: str | None = None
    skill_pack_name: str | None = None
    owner: str | None = None
    risk_tier: RiskTier | None = None
    tools: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)


# =============================================================================
# Registry
# =============================================================================


@dataclass
class ControlPlaneRegistry:
    """Loaded registry from YAML configuration.

    Provides deterministic routing from action_type to skill pack,
    tool discovery, and provider metadata.
    """

    version: str
    defaults: dict[str, Any]
    skill_packs: dict[str, SkillPackManifest]
    tools: dict[str, ToolDefinition]
    providers: dict[str, ProviderDefinition]
    _action_index: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Build the action → skill_pack reverse index."""
        for pack_id, pack in self.skill_packs.items():
            for action in pack.actions:
                if action in self._action_index:
                    logger.warning(
                        "Action '%s' registered by both '%s' and '%s' — using first",
                        action, self._action_index[action], pack_id,
                    )
                else:
                    self._action_index[action] = pack_id

    def route_action(self, action_type: str) -> SkillPackRouteResult:
        """Route an action_type to the appropriate skill pack.

        Used by the orchestrator to determine which skill pack handles
        a given intent. Returns SkillPackRouteResult with found=False
        if no skill pack is registered for this action.
        """
        pack_id = self._action_index.get(action_type)
        if pack_id is None:
            return SkillPackRouteResult(found=False)

        pack = self.skill_packs[pack_id]
        return SkillPackRouteResult(
            found=True,
            skill_pack_id=pack.id,
            skill_pack_name=pack.name,
            owner=pack.owner,
            risk_tier=pack.risk_tier,
            tools=list(pack.tools),
            providers=list(pack.providers),
        )

    def get_skill_pack(self, pack_id: str) -> SkillPackManifest | None:
        """Get a specific skill pack manifest by ID."""
        return self.skill_packs.get(pack_id)

    def list_skill_packs(
        self,
        *,
        category: str | None = None,
        risk_tier: RiskTier | None = None,
        status: str | None = None,
    ) -> list[SkillPackManifest]:
        """List skill packs with optional filters."""
        results = list(self.skill_packs.values())

        if category:
            results = [p for p in results if p.category == category]
        if risk_tier:
            results = [p for p in results if p.risk_tier == risk_tier]
        if status:
            results = [p for p in results if p.status == status]

        return results

    def get_tool(self, tool_id: str) -> ToolDefinition | None:
        """Get a specific tool definition."""
        return self.tools.get(tool_id)

    def get_provider(self, provider_id: str) -> ProviderDefinition | None:
        """Get a specific provider definition."""
        return self.providers.get(provider_id)

    def list_capabilities(self) -> list[dict[str, Any]]:
        """List all capabilities for discovery API.

        Returns a client-friendly view of registered skill packs
        with their actions, tools, and risk tiers.
        """
        capabilities = []
        for pack in self.skill_packs.values():
            capabilities.append({
                "skill_pack_id": pack.id,
                "name": pack.name,
                "owner": pack.owner,
                "category": pack.category,
                "risk_tier": pack.risk_tier.value,
                "status": pack.status,
                "description": pack.description,
                "actions": pack.actions,
                "capability_scopes": pack.capability_scopes,
                "tools": pack.tools,
                "providers": pack.providers,
            })
        return capabilities

    def get_stats(self) -> dict[str, Any]:
        """Get registry statistics."""
        packs = list(self.skill_packs.values())
        return {
            "version": self.version,
            "total_skill_packs": len(packs),
            "total_tools": len(self.tools),
            "total_providers": len(self.providers),
            "total_actions_mapped": len(self._action_index),
            "by_category": _count_by(packs, lambda p: p.category),
            "by_risk_tier": _count_by(packs, lambda p: p.risk_tier.value),
            "by_status": _count_by(packs, lambda p: p.status),
        }


def _count_by(items: list[Any], key_fn: Any) -> dict[str, int]:
    """Count items by a key function."""
    counts: dict[str, int] = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


# =============================================================================
# Loader
# =============================================================================


def load_registry(path: Path | str | None = None) -> ControlPlaneRegistry:
    """Load the Control Plane Registry from YAML.

    Fails closed if file is missing or malformed (Law #3).
    """
    manifest_path = Path(path) if path else _DEFAULT_MANIFEST_PATH

    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Skill pack manifest not found at {manifest_path}. "
            "Fail-closed: cannot route actions without registry."
        )

    with open(manifest_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Skill pack manifest YAML must be a mapping at top level")

    version = raw.get("version", "unknown")
    defaults = raw.get("defaults", {})

    # Parse skill packs
    raw_packs = raw.get("skill_packs", {})
    skill_packs: dict[str, SkillPackManifest] = {}

    for pack_id, pack_def in raw_packs.items():
        if not isinstance(pack_def, dict):
            logger.warning("Skipping malformed skill pack: %s", pack_id)
            continue

        risk_tier_str = pack_def.get("risk_tier", "yellow")
        risk_tier = _RISK_TIER_MAP.get(risk_tier_str, RiskTier.YELLOW)

        skill_packs[pack_id] = SkillPackManifest(
            id=pack_def.get("id", pack_id),
            name=pack_def.get("name", pack_id),
            owner=pack_def.get("owner", "unknown"),
            category=pack_def.get("category", "unknown"),
            risk_tier=risk_tier,
            status=pack_def.get("status", "registered"),
            description=pack_def.get("description", ""),
            actions=pack_def.get("actions", []),
            providers=pack_def.get("providers", []),
            capability_scopes=pack_def.get("capability_scopes", []),
            tools=pack_def.get("tools", []),
            per_suite_enabled=pack_def.get(
                "per_suite_enabled",
                defaults.get("per_suite_enabled", True),
            ),
        )

    # Parse tools
    raw_tools = raw.get("tools", {})
    tools: dict[str, ToolDefinition] = {}

    for tool_id, tool_def in raw_tools.items():
        if not isinstance(tool_def, dict):
            continue
        risk_str = tool_def.get("risk_tier", "yellow")
        tools[tool_id] = ToolDefinition(
            id=tool_id,
            provider=tool_def.get("provider", "unknown"),
            category=tool_def.get("category", "unknown"),
            risk_tier=_RISK_TIER_MAP.get(risk_str, RiskTier.YELLOW),
        )

    # Parse providers
    raw_providers = raw.get("providers", {})
    providers: dict[str, ProviderDefinition] = {}

    for prov_id, prov_def in raw_providers.items():
        if not isinstance(prov_def, dict):
            continue
        providers[prov_id] = ProviderDefinition(
            id=prov_def.get("id", prov_id),
            name=prov_def.get("name", prov_id),
            category=prov_def.get("category", "unknown"),
            auth_type=prov_def.get("auth_type", "unknown"),
            base_url=prov_def.get("base_url", ""),
            rate_limit_rpm=prov_def.get("rate_limit_rpm", 100),
            timeout_ms=prov_def.get("timeout_ms", 10000),
            retry_strategy=prov_def.get("retry_strategy", "none"),
            idempotency_support=prov_def.get("idempotency_support", False),
        )

    logger.info(
        "Registry loaded: version=%s, packs=%d, tools=%d, providers=%d",
        version, len(skill_packs), len(tools), len(providers),
    )

    return ControlPlaneRegistry(
        version=version,
        defaults=defaults,
        skill_packs=skill_packs,
        tools=tools,
        providers=providers,
    )


# =============================================================================
# Module-level singleton
# =============================================================================

_cached_registry: ControlPlaneRegistry | None = None


def get_registry(*, reload: bool = False) -> ControlPlaneRegistry:
    """Get the cached registry, loading if needed."""
    global _cached_registry
    if _cached_registry is None or reload:
        _cached_registry = load_registry()
    return _cached_registry
