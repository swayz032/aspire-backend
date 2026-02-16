"""Enhanced Base Skill Pack — Phase 3 Wave 2.

All skill packs inherit from EnhancedSkillPack, which integrates:
  - AspireAgentBase (LLM routing, receipts, Trust Spine)
  - Manifest loading and validation
  - Persona injection
  - Per-pack policy enforcement
  - Standardized result format

Usage:
    class MySkillPack(EnhancedSkillPack):
        def __init__(self):
            super().__init__(
                agent_id="my-pack",
                agent_name="My Pack",
                default_risk_tier="green",
            )

        async def my_action(self, params, context):
            # Use self.call_llm() for LLM calls
            # Use self.build_receipt() for receipts
            # Use self.emit_receipt() to persist
            pass
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.services.agent_sdk_base import (
    AgentContext,
    AgentResult,
    AspireAgentBase,
    TrustSpineAdapter,
)
from aspire_orchestrator.services.manifest_loader import load_manifest, load_all_manifests
from aspire_orchestrator.services.persona_loader import load_persona
from aspire_orchestrator.services.pack_policy_loader import load_pack_policies

logger = logging.getLogger(__name__)


class EnhancedSkillPack(AspireAgentBase):
    """Enhanced base class for all Phase 3 skill packs.

    Automatically loads manifest, persona, and policies on init.
    Provides standardized methods for LLM calls with governance.
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        *,
        default_risk_tier: str = "green",
        trust_spine: TrustSpineAdapter | None = None,
        auto_load_config: bool = True,
    ):
        super().__init__(
            agent_id=agent_id,
            agent_name=agent_name,
            default_risk_tier=default_risk_tier,
            trust_spine=trust_spine,
        )

        if auto_load_config:
            self._load_config()

    def _load_config(self) -> None:
        """Load manifest, persona, and policies for this pack."""
        # Load manifest
        try:
            manifests = load_all_manifests()
            # Try multiple ID formats
            for key in [self._agent_id, self._agent_id.replace("_", "-"), self._agent_id.replace("-", "_")]:
                if key in manifests:
                    self.set_manifest(manifests[key])
                    break
        except Exception as e:
            logger.debug("Manifest not available for %s: %s", self._agent_id, e)

        # Load persona
        try:
            persona = load_persona(self._agent_id)
            if persona:
                self.set_persona(persona)
        except Exception as e:
            logger.debug("Persona not available for %s: %s", self._agent_id, e)

        # Load policies
        try:
            policies = load_pack_policies(self._agent_id)
            if policies:
                self.set_policies(policies)
        except Exception as e:
            logger.debug("Policies not available for %s: %s", self._agent_id, e)

    async def execute_with_llm(
        self,
        *,
        prompt: str,
        ctx: AgentContext,
        event_type: str,
        step_type: str = "draft",
        inputs: dict[str, Any],
        system_prompt: str | None = None,
    ) -> AgentResult:
        """Execute an LLM-powered operation with full governance.

        This is the primary method for skill packs to use. It:
        1. Routes to the appropriate model via LLM Router
        2. Calls the LLM with persona + prompt
        3. Emits a receipt for the operation
        4. Returns a standardized AgentResult

        Args:
            prompt: The user-facing prompt to send to the LLM
            ctx: Agent context (suite_id, office_id, correlation_id)
            event_type: Receipt event type (e.g., "research.search")
            step_type: LLM router step type (classify, draft, plan, verify)
            inputs: Input parameters for receipt (will be hashed)
            system_prompt: Override system prompt (defaults to persona)

        Returns:
            AgentResult with success, data, receipt, and model info
        """
        # Make the LLM call
        llm_result = await self.call_llm(
            prompt,
            step_type=step_type,
            risk_tier=ctx.risk_tier,
            context=ctx,
            system_prompt=system_prompt,
        )

        # Check for errors
        if llm_result.get("error"):
            receipt = self.build_receipt(
                ctx=ctx,
                event_type=event_type,
                status="failed",
                inputs=inputs,
                metadata={
                    "error": llm_result["error"],
                    "model_used": llm_result.get("model_used"),
                    "profile_used": llm_result.get("profile_used"),
                },
            )
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False,
                receipt=receipt,
                error=llm_result["error"],
                model_used=llm_result.get("model_used"),
                profile_used=llm_result.get("profile_used"),
            )

        # Build success receipt
        receipt = self.build_receipt(
            ctx=ctx,
            event_type=event_type,
            status="ok",
            inputs=inputs,
            metadata={
                "model_used": llm_result.get("model_used"),
                "profile_used": llm_result.get("profile_used"),
            },
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"content": llm_result["content"]},
            receipt=receipt,
            model_used=llm_result.get("model_used"),
            profile_used=llm_result.get("profile_used"),
        )

    def get_capability_list(self) -> list[str]:
        """Get the list of capabilities from the manifest."""
        if self._manifest:
            return self._manifest.get("capabilities", [])
        return []

    def get_tools_list(self) -> list[str]:
        """Get the list of authorized tools from the manifest."""
        if self._manifest:
            return self._manifest.get("tools", [])
        return []

    def get_max_risk_tier(self) -> str:
        """Get the maximum risk tier from the manifest."""
        if self._manifest and "risk_profile" in self._manifest:
            return self._manifest["risk_profile"].get("max_risk_tier", self._default_risk_tier)
        return self._default_risk_tier

    def is_certified(self) -> bool:
        """Check if the skill pack is certified."""
        if self._manifest:
            return self._manifest.get("certification_status") == "certified"
        return False
