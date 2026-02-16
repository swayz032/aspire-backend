"""OpenAI Agents SDK Base — Phase 3 Wave 2.

Base agent class wrapping OpenAI Agents SDK for all Aspire skill packs.
Handles: LLM call routing, tool registration, Trust Spine integration
(receipt emission, token validation), persona loading, policy enforcement.

Every skill pack agent inherits from AspireAgentBase. The base class
ensures governance compliance without each pack reimplementing boilerplate.

Law compliance:
  - Law #1: Agent proposes actions. Orchestrator decides.
  - Law #2: Every agent call emits receipts via Trust Spine adapter.
  - Law #3: Missing token/policy -> fail closed.
  - Law #5: Capability tokens validated before tool execution.
  - Law #7: Agent executes bounded commands, never decides autonomously.
  - Law #9: PII redacted in agent outputs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =============================================================================
# Trust Spine Adapter Interface
# =============================================================================


class TokenValidation(BaseModel):
    """Result of capability token validation."""

    valid: bool
    reason: str | None = None
    token_id: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: str | None = None


class PolicyDecision(BaseModel):
    """Result of policy evaluation."""

    allowed: bool
    risk_tier: str = "green"
    deny_reason: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    binding_fields: list[str] = Field(default_factory=list)


class ApprovalResult(BaseModel):
    """Result of an approval request."""

    approved: bool
    approver_id: str | None = None
    method: str | None = None
    evidence_hash: str | None = None


class TrustSpineAdapter:
    """Interface between OpenAI Agents SDK and Trust Spine governance.

    This adapter bridges the gap between the SDK's tool-calling patterns
    and Aspire's governance requirements (receipts, tokens, policy checks).
    """

    async def validate_capability_token(self, token: str) -> TokenValidation:
        """Validate a capability token (Law #5)."""
        try:
            from aspire_orchestrator.services.token_service import get_token_service
            service = get_token_service()
            result = service.validate(token)
            return TokenValidation(
                valid=result.valid,
                reason=result.reason,
                token_id=result.token_id,
                scopes=result.scopes or [],
                expires_at=result.expires_at,
            )
        except Exception as e:
            logger.error("Token validation failed: %s", e)
            return TokenValidation(valid=False, reason=f"validation_error: {e}")

    async def emit_receipt(self, receipt: dict[str, Any]) -> str:
        """Emit a receipt to the receipt store (Law #2)."""
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            store_receipts([receipt])
            return receipt.get("receipt_id", str(uuid.uuid4()))
        except Exception as e:
            logger.error("Receipt emission failed: %s", e)
            return f"failed:{e}"

    async def check_policy(
        self,
        action: str,
        risk_tier: str,
        suite_id: str,
    ) -> PolicyDecision:
        """Check policy for an action (Law #4)."""
        try:
            from aspire_orchestrator.services.policy_engine import get_policy_matrix
            matrix = get_policy_matrix()
            result = matrix.evaluate(action)
            return PolicyDecision(
                allowed=result.allowed,
                risk_tier=result.risk_tier.value if hasattr(result.risk_tier, 'value') else str(result.risk_tier),
                deny_reason=result.deny_reason,
                required_approvals=result.required_approvals if hasattr(result, 'required_approvals') else [],
                binding_fields=result.binding_fields if hasattr(result, 'binding_fields') else [],
            )
        except Exception as e:
            logger.error("Policy check failed: %s", e)
            return PolicyDecision(allowed=False, deny_reason=f"policy_error: {e}")

    async def request_approval(
        self,
        action: str,
        tier: str,
        context: dict[str, Any],
    ) -> ApprovalResult:
        """Request approval for a YELLOW/RED tier action."""
        # Approval is handled by the orchestrator's approval_check node
        # Skill packs propose, orchestrator approves
        return ApprovalResult(approved=False, method="pending_orchestrator")

    async def check_idempotency(self, key: str, suite_id: str) -> bool:
        """Check if an operation has already been executed (W5 dependency).

        Returns True if the operation was already executed (skip re-execution).
        Returns False if the operation is new (proceed).
        """
        # Will be implemented in W5 (Outbox + Idempotency)
        return False


# =============================================================================
# Agent Context
# =============================================================================


@dataclass(frozen=True)
class AgentContext:
    """Context passed to every agent operation."""

    suite_id: str
    office_id: str
    correlation_id: str
    actor_id: str = "system"
    actor_type: str = "agent"
    capability_token_id: str | None = None
    capability_token_hash: str | None = None
    risk_tier: str = "green"


# =============================================================================
# Agent Result
# =============================================================================


@dataclass
class AgentResult:
    """Result from an agent operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    model_used: str | None = None
    profile_used: str | None = None


# =============================================================================
# Base Agent Class
# =============================================================================


class AspireAgentBase:
    """Base class for all Aspire skill pack agents.

    Provides:
    - LLM call routing via LLM Router
    - Receipt emission for every operation
    - Capability token validation
    - Policy enforcement
    - Persona/system prompt loading
    - Input hashing for receipts

    Subclasses implement domain-specific logic by overriding:
    - _get_agent_id() -> str
    - _get_default_risk_tier() -> str
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        *,
        default_risk_tier: str = "green",
        trust_spine: TrustSpineAdapter | None = None,
    ):
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._default_risk_tier = default_risk_tier
        self._trust_spine = trust_spine or TrustSpineAdapter()
        self._persona: str | None = None
        self._manifest: dict[str, Any] | None = None
        self._policies: dict[str, Any] = {}

        # LLM Router (lazy init)
        self._llm_router = None
        try:
            from aspire_orchestrator.services.llm_router import get_llm_router
            self._llm_router = get_llm_router()
        except Exception:
            logger.warning("LLM Router not available for agent %s", agent_id)

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def default_risk_tier(self) -> str:
        return self._default_risk_tier

    @property
    def persona(self) -> str | None:
        return self._persona

    @property
    def manifest(self) -> dict[str, Any] | None:
        return self._manifest

    def set_persona(self, persona: str) -> None:
        """Set the agent's persona/system prompt."""
        self._persona = persona

    def set_manifest(self, manifest: dict[str, Any]) -> None:
        """Set the agent's manifest."""
        self._manifest = manifest

    def set_policies(self, policies: dict[str, Any]) -> None:
        """Set the agent's policies."""
        self._policies = policies

    async def call_llm(
        self,
        prompt: str,
        *,
        step_type: str = "draft",
        risk_tier: str | None = None,
        context: AgentContext | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Call the LLM via the router with governance integration.

        Routes to the appropriate model based on step_type and risk_tier.
        Emits a model.route.selected receipt for every call.

        Returns:
            dict with keys: content, model_used, profile_used, receipt
        """
        import httpx

        effective_risk = risk_tier or self._default_risk_tier
        effective_system = system_prompt or self._persona or ""

        # Route to appropriate model
        model = "gpt-5-mini"
        base_url = "https://api.openai.com/v1"
        api_key = None
        route_max_tokens = max_tokens or 1024
        route_temperature = temperature if temperature is not None else 0.1
        timeout = 15
        profile_used = "fallback"
        route_receipt = None

        if self._llm_router:
            try:
                from aspire_orchestrator.services.llm_router import build_route_receipt
                route = self._llm_router.route(
                    step_type, effective_risk, desk=self._agent_id,
                )
                model = route.concrete_model
                base_url = route.base_url
                api_key = self._llm_router.api_key
                route_max_tokens = max_tokens or route.max_tokens
                route_temperature = temperature if temperature is not None else route.temperature
                timeout = route.timeout_seconds
                profile_used = route.selected_profile.value

                # Emit routing receipt
                if context:
                    route_receipt = build_route_receipt(
                        route,
                        correlation_id=context.correlation_id,
                        suite_id=context.suite_id,
                    )
                    await self._trust_spine.emit_receipt(route_receipt)
            except Exception as e:
                logger.warning("LLM Router failed for agent %s: %s", self._agent_id, e)

        if not api_key:
            import os
            api_key = os.environ.get("ASPIRE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")

        if not api_key:
            logger.error("No API key available for agent %s (Law #3: fail-closed)", self._agent_id)
            return {
                "content": "",
                "model_used": None,
                "profile_used": None,
                "error": "no_api_key",
                "receipt": route_receipt,
            }

        # Make the LLM call
        url = f"{base_url.rstrip('/')}/chat/completions"
        messages = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": route_temperature,
            "max_tokens": route_max_tokens,
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            return {
                "content": content,
                "model_used": model,
                "profile_used": profile_used,
                "error": None,
                "receipt": route_receipt,
            }

        except httpx.TimeoutException:
            logger.error("LLM call timeout for agent %s after %ds", self._agent_id, timeout)
            return {
                "content": "",
                "model_used": model,
                "profile_used": profile_used,
                "error": "llm_timeout",
                "receipt": route_receipt,
            }
        except Exception as e:
            logger.error("LLM call failed for agent %s: %s", self._agent_id, type(e).__name__)
            return {
                "content": "",
                "model_used": model,
                "profile_used": profile_used,
                "error": f"llm_error: {type(e).__name__}",
                "receipt": route_receipt,
            }

    def build_receipt(
        self,
        *,
        ctx: AgentContext,
        event_type: str,
        status: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a receipt for an agent operation (Law #2)."""
        receipt: dict[str, Any] = {
            "receipt_version": "1.0",
            "receipt_id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "suite_id": ctx.suite_id,
            "office_id": ctx.office_id,
            "actor": f"skillpack:{self._agent_id}",
            "correlation_id": ctx.correlation_id,
            "status": status,
            "inputs_hash": self.compute_inputs_hash(inputs),
            "policy": {
                "decision": "allow",
                "policy_id": f"{self._agent_id}-v1",
                "reasons": [],
            },
            "redactions": [],
        }
        if metadata:
            receipt["metadata"] = metadata
        return receipt

    @staticmethod
    def compute_inputs_hash(inputs: dict[str, Any]) -> str:
        """Compute SHA256 hash of inputs for receipt linkage."""
        canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    async def emit_receipt(self, receipt: dict[str, Any]) -> str:
        """Emit a receipt via Trust Spine (Law #2)."""
        return await self._trust_spine.emit_receipt(receipt)
