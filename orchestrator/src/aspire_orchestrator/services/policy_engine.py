"""Policy Engine Service — YAML-driven deterministic evaluation (Law #4).

Per policy_engine_spec.md, 9-step evaluation:
1. Validate ingress schema (done by intake node)
2. Resolve tenant + actor
3. Compute candidate tool set
4. Apply allowlist intersection
5. Classify risk tier per action
6. If yellow/red: require approvals
7. If red: require presence proof
8. For any execution: require valid capability token
9. Emit policy_decision receipt

Fail-closed defaults:
- Unknown tool → deny
- Unknown action → deny
- Missing approvals → deny
- Missing receipt write → disable execution (draft-only)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aspire_orchestrator.models import RiskTier

logger = logging.getLogger(__name__)

# Default policy matrix path (relative to this file's package)
_DEFAULT_POLICY_PATH = Path(__file__).parent.parent / "config" / "policy_matrix.yaml"

# Risk tier mapping from YAML string to enum
_RISK_TIER_MAP = {
    "green": RiskTier.GREEN,
    "yellow": RiskTier.YELLOW,
    "red": RiskTier.RED,
}


@dataclass(frozen=True)
class PolicyAction:
    """A single action entry from the policy matrix."""

    action_type: str
    risk_tier: RiskTier
    tools: list[str]
    capability_scope: str
    category: str
    approval_type: str  # none | explicit | spend | legal
    approval_binding_fields: list[str]
    dual_approval: bool
    required_approvers: list[str]
    params_required: list[str]
    params_constraints: dict[str, Any]
    redact_fields: list[str]


@dataclass(frozen=True)
class PolicyEvalResult:
    """Result of policy evaluation for a single action."""

    allowed: bool
    risk_tier: RiskTier
    tools: list[str]
    capability_scope: str
    approval_required: bool
    presence_required: bool
    dual_approval: bool
    redact_fields: list[str]
    deny_reason: str | None = None
    action_config: PolicyAction | None = None


@dataclass
class PolicyMatrix:
    """Loaded policy matrix from YAML configuration.

    The matrix is loaded once at startup and cached. It provides
    deterministic evaluation per policy_engine_spec.md.
    """

    version: str
    defaults: dict[str, Any]
    actions: dict[str, PolicyAction]

    @property
    def deny_by_default(self) -> bool:
        return self.defaults.get("deny_by_default", True)

    @property
    def capability_max_ttl_seconds(self) -> int:
        return self.defaults.get("capability_max_ttl_seconds", 60)

    @property
    def fail_closed_on_dlp_error(self) -> bool:
        return self.defaults.get("fail_closed_on_dlp_error", True)

    def evaluate(self, action_type: str) -> PolicyEvalResult:
        """Evaluate policy for a given action type.

        Steps 3-8 of the 9-step evaluation:
        3. Compute candidate tool set
        4. Apply allowlist intersection
        5. Classify risk tier
        6. Require approvals for yellow/red
        7. Require presence for red
        8. Require capability token for all
        """
        action = self.actions.get(action_type)

        if action is None:
            # Fail-closed: unknown action → deny
            logger.warning("Policy DENY: unknown action_type=%s", action_type)
            return PolicyEvalResult(
                allowed=False,
                risk_tier=RiskTier.YELLOW,  # Default unknown to YELLOW per spec
                tools=[],
                capability_scope="",
                approval_required=False,
                presence_required=False,
                dual_approval=False,
                redact_fields=[],
                deny_reason=f"Unknown action type: {action_type}",
            )

        # Step 5: Classify risk tier
        risk_tier = action.risk_tier

        # Step 6: Approval required for yellow/red
        approval_required = risk_tier in (RiskTier.YELLOW, RiskTier.RED)

        # Step 7: Presence required for red
        presence_required = risk_tier == RiskTier.RED

        # Step 8: Capability token always required (handled by token_mint node)

        logger.info(
            "Policy ALLOW: action=%s, risk=%s, tools=%s, approval=%s, presence=%s",
            action_type, risk_tier.value, action.tools,
            approval_required, presence_required,
        )

        return PolicyEvalResult(
            allowed=True,
            risk_tier=risk_tier,
            tools=action.tools,
            capability_scope=action.capability_scope,
            approval_required=approval_required,
            presence_required=presence_required,
            dual_approval=action.dual_approval,
            redact_fields=action.redact_fields,
            action_config=action,
        )

    def get_action(self, action_type: str) -> PolicyAction | None:
        """Get a specific action configuration."""
        return self.actions.get(action_type)

    def list_actions(self, risk_tier: RiskTier | None = None) -> list[str]:
        """List all known action types, optionally filtered by risk tier."""
        if risk_tier is None:
            return list(self.actions.keys())
        return [
            name for name, action in self.actions.items()
            if action.risk_tier == risk_tier
        ]


def load_policy_matrix(path: Path | str | None = None) -> PolicyMatrix:
    """Load policy matrix from YAML file.

    Fails closed if file is missing or malformed (Law #3).
    """
    policy_path = Path(path) if path else _DEFAULT_POLICY_PATH

    if not policy_path.exists():
        raise FileNotFoundError(
            f"Policy matrix not found at {policy_path}. "
            "Fail-closed: cannot evaluate policy without matrix."
        )

    with open(policy_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Policy matrix YAML must be a mapping at top level")

    version = raw.get("version", "unknown")
    defaults = raw.get("defaults", {})
    raw_actions = raw.get("actions", {})

    if not isinstance(raw_actions, dict):
        raise ValueError("Policy matrix 'actions' must be a mapping")

    actions: dict[str, PolicyAction] = {}

    for action_name, action_def in raw_actions.items():
        if not isinstance(action_def, dict):
            logger.warning("Skipping malformed action: %s", action_name)
            continue

        risk_tier_str = action_def.get("risk_tier", "yellow")
        risk_tier = _RISK_TIER_MAP.get(risk_tier_str)
        if risk_tier is None:
            logger.warning(
                "Unknown risk_tier '%s' for action '%s', defaulting to YELLOW",
                risk_tier_str, action_name,
            )
            risk_tier = RiskTier.YELLOW

        approval_def = action_def.get("approval", {})
        params_def = action_def.get("params", {})

        actions[action_name] = PolicyAction(
            action_type=action_name,
            risk_tier=risk_tier,
            tools=action_def.get("tools", []),
            capability_scope=action_def.get("capability_scope", ""),
            category=action_def.get("category", "unknown"),
            approval_type=approval_def.get("type", "none"),
            approval_binding_fields=approval_def.get("binding_fields", []),
            dual_approval=bool(approval_def.get("dual_approval", False)),
            required_approvers=approval_def.get("required_approvers", []),
            params_required=params_def.get("required", []),
            params_constraints=params_def.get("constraints", {}),
            redact_fields=action_def.get("redact_fields", []),
        )

    logger.info(
        "Policy matrix loaded: version=%s, actions=%d (green=%d, yellow=%d, red=%d)",
        version, len(actions),
        sum(1 for a in actions.values() if a.risk_tier == RiskTier.GREEN),
        sum(1 for a in actions.values() if a.risk_tier == RiskTier.YELLOW),
        sum(1 for a in actions.values() if a.risk_tier == RiskTier.RED),
    )

    return PolicyMatrix(
        version=version,
        defaults=defaults,
        actions=actions,
    )


# Module-level singleton (loaded once, reused)
_cached_matrix: PolicyMatrix | None = None


def get_policy_matrix(*, reload: bool = False) -> PolicyMatrix:
    """Get the cached policy matrix, loading if needed."""
    global _cached_matrix
    if _cached_matrix is None or reload:
        _cached_matrix = load_policy_matrix()
    return _cached_matrix
