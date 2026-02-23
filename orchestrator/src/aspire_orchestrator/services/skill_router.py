"""Multi-Skill Router — Intent-to-Skill-Pack routing with compound intent support.

Per architecture.md Brain Layer: the skill router takes a classified intent
from the intent classifier and produces a RoutingPlan — an ordered set of
steps specifying which skill packs handle the intent, in what order, and
with what governance requirements.

Responsibilities:
1. Map classified intent → skill pack(s) via registry + policy
2. Handle compound intents (multi-action, multi-pack)
3. Determine execution strategy (sequential/parallel/mixed)
4. Escalate risk tier to MAX across all steps
5. Flag A2A delegation when cross-pack routing is needed
6. Fail-closed on unknown actions (Law #3)

Law compliance:
- Law #1: Router does NOT execute — it only creates a plan. Orchestrator executes.
- Law #3: Unknown action_type or skill_pack → deny (fail-closed)
- Law #4: Risk tier of the plan = MAX(risk tiers of all steps)
"""

from __future__ import annotations

import logging
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.policy_engine import get_policy_matrix, PolicyMatrix
from aspire_orchestrator.services.registry import (
    ControlPlaneRegistry,
    get_registry,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Risk tier ordering for MAX computation
# =============================================================================

_RISK_TIER_ORDER: dict[RiskTier, int] = {
    RiskTier.GREEN: 0,
    RiskTier.YELLOW: 1,
    RiskTier.RED: 2,
}


def _max_risk_tier(tiers: list[RiskTier]) -> RiskTier:
    """Return the highest risk tier from a list (Law #4)."""
    if not tiers:
        return RiskTier.GREEN
    return max(tiers, key=lambda t: _RISK_TIER_ORDER[t])


# =============================================================================
# Intent types (imported from intent_classifier when available)
# =============================================================================

from aspire_orchestrator.services.intent_classifier import IntentResult


# =============================================================================
# Routing Models
# =============================================================================


class ExecutionStrategy(str, Enum):
    """How steps in a routing plan should be executed."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    MIXED = "mixed"  # some parallel, some sequential


class RoutingStep(BaseModel):
    """A single step in a routing plan."""

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    skill_pack: str  # skill pack id from manifests
    action_type: str  # action from policy_matrix
    tools: list[str]  # tool_ids to execute
    risk_tier: RiskTier  # from policy
    depends_on: list[str] | None = None  # step IDs this depends on
    capability_scope: str = ""
    approval_required: bool = False
    presence_required: bool = False


class RoutingPlan(BaseModel):
    """Complete routing plan for an intent."""

    steps: list[RoutingStep]
    execution_strategy: ExecutionStrategy
    estimated_risk_tier: RiskTier  # highest risk tier across all steps
    requires_compound_approval: bool  # True if multiple YELLOW/RED steps
    delegation_required: bool  # True if A2A cross-pack delegation needed
    deny_reason: str | None = None  # set when plan is denied (fail-closed)


# =============================================================================
# Dependency detection for compound intents
# =============================================================================

# Actions whose output is commonly consumed by a subsequent action.
# Key = producer action, Value = set of consumer actions that depend on its output.
_OUTPUT_DEPENDENCIES: dict[str, set[str]] = {
    "invoice.create": {"invoice.send", "invoice.void"},
    "quote.create": {"quote.send"},
    "contract.generate": {"contract.sign", "contract.send"},
    "email.draft": {"email.send"},
    "finance.snapshot.read": {"finance.proposal.create", "finance.packet.draft"},
    "finance.exceptions.read": {"finance.proposal.create"},
    "research.search": {"email.draft", "email.send", "contacts.create"},
    "research.places": {"contacts.create"},
    "contacts.search": {"email.send", "email.draft", "invoice.create"},
    "calendar.read": {"calendar.create", "meeting.schedule"},
    "calendar.list": {"calendar.create", "meeting.schedule"},
}


def _detect_dependencies(action_types: list[str]) -> dict[str, list[str]]:
    """Detect data-flow dependencies between actions.

    Returns a mapping of action_type → list of action_types it depends on.
    For example, if action_types = ["invoice.create", "invoice.send"],
    then invoice.send depends on invoice.create (needs the invoice_id output).
    """
    # Build index: action_type → position
    positions: dict[str, int] = {}
    for i, action in enumerate(action_types):
        if action not in positions:
            positions[action] = i

    deps: dict[str, list[str]] = {action: [] for action in action_types}

    for producer, consumers in _OUTPUT_DEPENDENCIES.items():
        if producer not in positions:
            continue
        for consumer in consumers:
            if consumer in positions and positions[consumer] > positions[producer]:
                deps[consumer].append(producer)

    return deps


# =============================================================================
# Skill Router
# =============================================================================


class SkillRouter:
    """Routes classified intents to skill packs with governance awareness.

    Phase 3: Integrates desk-specific routing rules from LLM Router.

    The router ONLY creates plans — it never executes (Law #1).
    Unknown actions are denied (Law #3).
    Risk tiers escalate to MAX across all steps (Law #4).
    """

    def __init__(
        self,
        *,
        registry: ControlPlaneRegistry | None = None,
        policy: PolicyMatrix | None = None,
    ):
        self._registry = registry or get_registry()
        self._policy = policy or get_policy_matrix()

        # Phase 3: Load desk routing rules for intent-aware delegation
        self._desk_rules: dict[str, dict[str, Any]] = {}
        try:
            from aspire_orchestrator.services.llm_router import get_llm_router
            router = get_llm_router()
            self._desk_rules = router.get_desk_rules()
            logger.info("SkillRouter loaded %d desk routing rules", len(self._desk_rules))
        except Exception as e:
            logger.warning("Desk routing rules not available: %s", e)

    async def route(
        self,
        intent: IntentResult,
        context: dict[str, Any] | None = None,
    ) -> RoutingPlan:
        """Route a single classified intent to a routing plan.

        Args:
            intent: Classified intent from the intent classifier.
            context: Optional context (suite_id, office_id, current_agent, etc.)

        Returns:
            RoutingPlan with a single step (or denied plan).
        """
        return await self.route_multi([intent], context=context)

    async def route_multi(
        self,
        intents: list[IntentResult],
        context: dict[str, Any] | None = None,
    ) -> RoutingPlan:
        """Route one or more classified intents into a compound routing plan.

        For compound user requests ("create an invoice and email it"),
        the orchestrator classifies each sub-intent separately and
        passes the list here for dependency analysis and plan assembly.

        Args:
            intents: One or more classified intents.
            context: Optional context (suite_id, office_id, current_agent, etc.)

        Returns:
            RoutingPlan with steps, execution strategy, and governance metadata.
            If routing fails, returns a denied plan with deny_reason set.
        """
        ctx = context or {}
        current_agent = ctx.get("current_agent", "ava")

        # Collect action types from all intents
        action_types: list[str] = []
        for intent in intents:
            resolved = self._resolve_action_types(intent)
            action_types.extend(resolved)

        # Lifecycle-aware rerouting: "send" actions require an existing document.
        # When the user says "send an NDA to X" but no document_id is provided,
        # they mean the full lifecycle: draft → approve → send.  Reroute to the
        # generate action so Ava drafts it first.
        _SEND_NEEDS_DRAFT: dict[str, str] = {
            "contract.send": "contract.generate",
            "invoice.send": "invoice.create",
            "email.send": "email.draft",
        }
        for i, action in enumerate(action_types):
            if action in _SEND_NEEDS_DRAFT:
                has_doc_id = any(
                    intent.entities.get("document_id")
                    or intent.entities.get("invoice_id")
                    or intent.entities.get("draft_id")
                    for intent in intents
                    if intent.entities
                )
                if not has_doc_id:
                    draft_action = _SEND_NEEDS_DRAFT[action]
                    logger.info(
                        "Lifecycle reroute: %s → %s (no existing document_id)",
                        action, draft_action,
                    )
                    action_types[i] = draft_action

        if not action_types:
            logger.warning(
                "Routing DENY: no action_types resolved from %d intent(s)",
                len(intents),
            )
            return RoutingPlan(
                steps=[],
                execution_strategy=ExecutionStrategy.SEQUENTIAL,
                estimated_risk_tier=RiskTier.GREEN,
                requires_compound_approval=False,
                delegation_required=False,
                deny_reason="unknown_action",
            )

        # Build routing steps
        steps: list[RoutingStep] = []
        denied_actions: list[str] = []

        for action_type in action_types:
            step = self._build_step(action_type)
            if step is None:
                denied_actions.append(action_type)
            else:
                steps.append(step)

        # If ANY action is denied, fail-closed on the entire plan (Law #3)
        if denied_actions:
            logger.warning(
                "Routing DENY: unknown action(s) %s in compound intent",
                denied_actions,
            )
            return RoutingPlan(
                steps=[],
                execution_strategy=ExecutionStrategy.SEQUENTIAL,
                estimated_risk_tier=RiskTier.GREEN,
                requires_compound_approval=False,
                delegation_required=False,
                deny_reason=f"unknown_action: {', '.join(denied_actions)}",
            )

        # Resolve dependencies and assign depends_on
        deps = _detect_dependencies(action_types)
        step_id_by_action: dict[str, str] = {
            step.action_type: step.step_id for step in steps
        }
        for step in steps:
            action_deps = deps.get(step.action_type, [])
            if action_deps:
                step.depends_on = [
                    step_id_by_action[dep]
                    for dep in action_deps
                    if dep in step_id_by_action
                ]

        # Determine execution strategy
        execution_strategy = self._determine_strategy(steps)

        # Compute plan-level governance
        all_risk_tiers = [step.risk_tier for step in steps]
        estimated_risk_tier = _max_risk_tier(all_risk_tiers)

        elevated_steps = [
            s for s in steps
            if s.risk_tier in (RiskTier.YELLOW, RiskTier.RED)
        ]
        requires_compound_approval = len(elevated_steps) > 1

        # Delegation required if steps map to different skill packs
        unique_packs = {step.skill_pack for step in steps}
        delegation_required = len(unique_packs) > 1 or any(
            self._is_delegation(step, current_agent) for step in steps
        )

        logger.info(
            "Routing PLAN: actions=%s, strategy=%s, risk=%s, "
            "compound_approval=%s, delegation=%s, steps=%d",
            action_types,
            execution_strategy.value,
            estimated_risk_tier.value,
            requires_compound_approval,
            delegation_required,
            len(steps),
        )

        return RoutingPlan(
            steps=steps,
            execution_strategy=execution_strategy,
            estimated_risk_tier=estimated_risk_tier,
            requires_compound_approval=requires_compound_approval,
            delegation_required=delegation_required,
        )

    def _resolve_action_types(self, intent: IntentResult) -> list[str]:
        """Extract the list of action_types from a classified intent.

        Handles both single-action and compound intents.
        IntentResult has `action_type` (str) — for compound intents the
        orchestrator splits into multiple calls, so each IntentResult
        carries exactly one action_type.
        """
        if intent.action_type and intent.action_type != "unknown":
            return [intent.action_type]

        return []

    def _build_step(self, action_type: str) -> RoutingStep | None:
        """Build a RoutingStep for a single action_type.

        Returns None if the action is unknown (fail-closed, Law #3).
        """
        # Check policy first — authoritative source for risk tier and tools
        policy_result = self._policy.evaluate(action_type)
        if not policy_result.allowed:
            logger.warning(
                "Policy DENY for action=%s: %s",
                action_type,
                policy_result.deny_reason,
            )
            return None

        # Check registry for skill pack routing
        route_result = self._registry.route_action(action_type)
        if not route_result.found:
            logger.warning(
                "Registry has no skill pack for action=%s",
                action_type,
            )
            return None

        # Use policy tools (authoritative), fall back to registry tools
        tools = policy_result.tools if policy_result.tools else route_result.tools

        return RoutingStep(
            skill_pack=route_result.skill_pack_id or "unknown",
            action_type=action_type,
            tools=tools,
            risk_tier=policy_result.risk_tier,
            capability_scope=policy_result.capability_scope,
            approval_required=policy_result.approval_required,
            presence_required=policy_result.presence_required,
        )

    def _determine_strategy(self, steps: list[RoutingStep]) -> ExecutionStrategy:
        """Determine execution strategy based on step dependencies.

        - All steps independent → PARALLEL
        - All steps have dependencies → SEQUENTIAL
        - Mix of dependent and independent → MIXED
        """
        if len(steps) <= 1:
            return ExecutionStrategy.SEQUENTIAL

        has_deps = any(step.depends_on for step in steps)
        all_deps = all(step.depends_on for step in steps)

        if all_deps:
            return ExecutionStrategy.SEQUENTIAL
        if not has_deps:
            return ExecutionStrategy.PARALLEL
        return ExecutionStrategy.MIXED

    def _is_delegation(self, step: RoutingStep, current_agent: str) -> bool:
        """Check if a step requires A2A delegation (different pack owner)."""
        pack = self._registry.get_skill_pack(step.skill_pack)
        if pack is None:
            return False
        return pack.owner != current_agent


# =============================================================================
# Module-level singleton
# =============================================================================

_router: SkillRouter | None = None


def get_skill_router(*, reload: bool = False) -> SkillRouter:
    """Get the cached skill router, loading if needed."""
    global _router
    if _router is None or reload:
        _router = SkillRouter()
    return _router
