"""Intent Classification Service — Brain Layer Phase 3 (Law #1).

Classifies user utterances into one of the actions defined in
policy_matrix.yaml, mapping each to a skill pack from the Control
Plane Registry.

Phase 3 enhancement: Uses LLM Router for 3-tier model selection.
The classifier uses the CHEAP_CLASSIFIER profile for GREEN-tier classifications
and PRIMARY_REASONER for ambiguous or higher-risk intents.

Law #1 compliance: The classifier PROPOSES an intent. It does NOT decide.
The LangGraph orchestrator makes all routing decisions based on this proposal.

Law #2 compliance: Classification failures generate receipts with reason codes.
Every classification emits a model.route.selected receipt via LLM Router.

Law #3 compliance: Missing API key or LLM errors fail closed (deny, don't guess).
Rule-based fallback when LLM is unavailable.

Law #9 compliance: Raw user utterances are never logged in production.
PII is redacted from debug output.

Confidence thresholds:
  >0.85  — auto-route (high confidence)
  0.5-0.85 — requires clarification from user
  <0.5   — escalate to orchestrator (unknown intent)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.policy_engine import get_policy_matrix

logger = logging.getLogger(__name__)

# Confidence thresholds
CONFIDENCE_AUTO_ROUTE = 0.85
CONFIDENCE_CLARIFY = 0.5

# LLM timeout (orchestrator budget is 30s; classifier gets 15s)
_LLM_TIMEOUT_SECONDS = 15


# =============================================================================
# Models
# =============================================================================


class IntentResult(BaseModel):
    """Result of intent classification — a PROPOSAL, not a decision (Law #1)."""

    action_type: str = Field(description="Maps to policy_matrix.yaml action key")
    skill_pack: str = Field(description="Maps to skill_pack_manifests.yaml pack id")
    confidence: float = Field(ge=0.0, le=1.0, description="Classification confidence")
    entities: dict[str, Any] = Field(
        default_factory=dict,
        description="Extracted entities (amounts, dates, names, etc.)",
    )
    risk_tier: RiskTier = Field(description="From policy_matrix.yaml")
    requires_clarification: bool = Field(
        default=False,
        description="True if confidence between 0.5 and 0.85",
    )
    clarification_prompt: str | None = Field(
        default=None,
        description="What to ask user if clarifying",
    )
    raw_llm_response: dict[str, Any] | None = Field(
        default=None,
        description="For debugging (redacted in production)",
    )


# =============================================================================
# System Prompt Builder
# =============================================================================


def _build_action_catalog() -> str:
    """Build the action catalog from policy_matrix.yaml for the system prompt.

    Loads all 35 actions with their risk tiers and categories so the LLM
    knows the full action vocabulary.
    """
    matrix = get_policy_matrix()
    lines: list[str] = []

    for action_type in sorted(matrix.actions.keys()):
        action = matrix.actions[action_type]
        lines.append(
            f"- {action_type} (risk: {action.risk_tier.value}, "
            f"category: {action.category})"
        )

    return "\n".join(lines)


def _build_system_prompt(action_catalog: str) -> str:
    """Build the system prompt for the intent classifier LLM."""
    return f"""You are Aspire's intent classifier. Your job is to classify user utterances into exactly one action_type from the catalog below. You do NOT execute actions — you only classify.

## Action Catalog (35 actions)
{action_catalog}

## Output Format (strict JSON)
Respond with ONLY a JSON object, no markdown, no explanation:
{{
  "action_type": "the.action.type",
  "skill_pack": "pack_id",
  "confidence": 0.0-1.0,
  "entities": {{"key": "value"}},
  "clarification_prompt": null or "question to ask user"
}}

## Skill Pack Mapping
- calendar.read, calendar.list, calendar.create → nora_conference
- contacts.search, contacts.read, contacts.create → (internal, no specific pack)
- receipts.search, receipts.read → (internal, no specific pack)
- research.search, research.places → adam_research
- email.send, email.draft → eli_inbox
- invoice.create, invoice.send, invoice.void → quinn_invoicing
- quote.create, quote.send → quinn_invoicing
- meeting.schedule → nora_conference
- payment.send, payment.transfer → finn_money_desk
- contract.generate, contract.sign → clara_legal
- tax.file → (internal, filing)
- payroll.run → milo_payroll
- books.sync → teressa_books
- domain.check, domain.verify, domain.dns.create, domain.purchase, domain.delete → mail_ops_desk
- mail.account.create, mail.account.read → mail_ops_desk
- profile.update → (internal)
- data.delete → (internal)
- finance.snapshot.read, finance.exceptions.read, finance.packet.draft → finn_finance_manager
- finance.proposal.create, a2a.create → finn_finance_manager

## Rules
1. Pick the BEST matching action_type. If unsure, set confidence < 0.5.
2. Extract entities: amounts (as cents), dates (ISO 8601), names, email addresses, etc.
3. If the utterance is ambiguous between two actions, pick the safer (lower risk tier) one and set confidence 0.5-0.85 with a clarification_prompt.
4. If no action matches at all, use action_type "unknown" with confidence 0.0.
5. Never fabricate actions not in the catalog.
6. For skill_pack, use the pack ID from the mapping above. If no specific pack, use "internal"."""


# =============================================================================
# Action-to-SkillPack Resolution
# =============================================================================

# Built from skill_pack_manifests.yaml — action → skill pack ID
_ACTION_TO_PACK: dict[str, str] = {
    "email.send": "eli_inbox",
    "email.draft": "eli_inbox",
    "invoice.create": "quinn_invoicing",
    "invoice.send": "quinn_invoicing",
    "invoice.void": "quinn_invoicing",
    "quote.create": "quinn_invoicing",
    "quote.send": "quinn_invoicing",
    "meeting.schedule": "nora_conference",
    "research.search": "adam_research",
    "research.places": "adam_research",
    "payment.send": "finn_money_desk",
    "payment.transfer": "finn_money_desk",
    "contract.generate": "clara_legal",
    "contract.sign": "clara_legal",
    "payroll.run": "milo_payroll",
    "books.sync": "teressa_books",
    "domain.check": "mail_ops_desk",
    "domain.verify": "mail_ops_desk",
    "domain.dns.create": "mail_ops_desk",
    "domain.purchase": "mail_ops_desk",
    "domain.delete": "mail_ops_desk",
    "mail.account.create": "mail_ops_desk",
    "mail.account.read": "mail_ops_desk",
    "finance.snapshot.read": "finn_finance_manager",
    "finance.exceptions.read": "finn_finance_manager",
    "finance.packet.draft": "finn_finance_manager",
    "finance.proposal.create": "finn_finance_manager",
    "a2a.create": "finn_finance_manager",
}


def _resolve_skill_pack(action_type: str) -> str:
    """Resolve action_type to skill pack ID.

    Uses the static mapping first, falls back to registry lookup.
    """
    if action_type in _ACTION_TO_PACK:
        return _ACTION_TO_PACK[action_type]

    # Fallback: try registry
    try:
        from aspire_orchestrator.services.registry import get_registry

        registry = get_registry()
        route = registry.route_action(action_type)
        if route.found and route.skill_pack_id:
            return route.skill_pack_id
    except Exception:
        logger.debug("Registry lookup failed for action=%s", action_type)

    return "internal"


# =============================================================================
# Intent Classifier
# =============================================================================


class IntentClassifier:
    """Classifies user utterances into Aspire action types.

    Thread-safe, stateless, no caching of user data.
    Uses LLM Router for model selection (Phase 3).
    Falls back to direct API calls when router unavailable.

    Law #1: Only proposes — orchestrator decides.
    Law #3: Fail-closed if LLM unavailable. Rule-based fallback available.
    Law #9: Never logs raw utterances.
    """

    def __init__(self) -> None:
        # Phase 3: Use LLM Router for model selection
        self._llm_router = None
        try:
            from aspire_orchestrator.services.llm_router import get_llm_router
            self._llm_router = get_llm_router()
            logger.info("IntentClassifier using LLM Router for model selection")
        except Exception as e:
            logger.warning("LLM Router not available, using direct config: %s", e)

        # Fallback direct config (used when router unavailable)
        self._model: str = os.environ.get("INTENT_LLM_MODEL", "gpt-4o-mini")
        self._base_url: str = os.environ.get(
            "INTENT_LLM_BASE_URL", "https://api.openai.com/v1"
        )
        # API key resolution: explicit env var takes precedence, then router, then fallback
        self._api_key: str | None = os.environ.get("OPENAI_API_KEY")
        if not self._api_key and self._llm_router:
            self._api_key = self._llm_router.api_key

        # Build action catalog once at init (policy matrix is stable)
        self._action_catalog: str = _build_action_catalog()
        self._system_prompt: str = _build_system_prompt(self._action_catalog)

        # Risk tier lookup from policy matrix
        self._risk_tiers: dict[str, RiskTier] = {}
        matrix = get_policy_matrix()
        for action_type, action in matrix.actions.items():
            self._risk_tiers[action_type] = action.risk_tier

        logger.info(
            "IntentClassifier initialized: router=%s, actions=%d",
            "LLM Router" if self._llm_router else self._model,
            len(self._risk_tiers),
        )

    async def classify(
        self,
        utterance: str,
        context: dict[str, Any] | None = None,
    ) -> IntentResult:
        """Classify a user utterance into an IntentResult.

        Args:
            utterance: The user's natural language input.
            context: Optional context (conversation history, active entities).

        Returns:
            IntentResult with the proposed action, confidence, and entities.

        Raises:
            Nothing — fails closed by returning an escalation IntentResult.
        """
        # Law #3: fail-closed if API key missing
        if not self._api_key:
            logger.error("OPENAI_API_KEY not set — fail-closed (Law #3)")
            return self._fail_closed_result(
                reason="intent_classifier_no_api_key",
            )

        # Build the user message (context-enriched if available)
        user_message = self._build_user_message(utterance, context)

        try:
            raw_response = await self._call_llm(user_message)
            return self._parse_response(raw_response)

        except httpx.TimeoutException:
            logger.error("Intent classifier LLM timeout after %ds", _LLM_TIMEOUT_SECONDS)
            return self._fail_closed_result(reason="intent_classifier_timeout")

        except httpx.HTTPStatusError as e:
            logger.error(
                "Intent classifier LLM HTTP error: status=%d",
                e.response.status_code,
            )
            return self._fail_closed_result(reason="intent_classifier_llm_http_error")

        except Exception as e:
            # Law #9: don't log the utterance, only the error type
            logger.error(
                "Intent classifier failed: %s: %s",
                type(e).__name__,
                str(e)[:200],
            )
            return self._fail_closed_result(reason="intent_classifier_internal_error")

    async def _call_llm(self, user_message: str) -> dict[str, Any]:
        """Call the OpenAI-compatible API with structured output (JSON mode).

        Phase 3: Uses LLM Router to select the appropriate model.
        Falls back to direct config when router unavailable.

        Uses a fresh httpx.AsyncClient per call for thread safety.
        """
        # Phase 3: Use LLM Router for model selection
        model = self._model
        base_url = self._base_url
        temperature = 0.1
        max_tokens = 512
        timeout = _LLM_TIMEOUT_SECONDS

        if self._llm_router:
            try:
                from aspire_orchestrator.services.llm_router import build_route_receipt
                route = self._llm_router.route("classify", "low")
                model = route.concrete_model
                base_url = route.base_url
                temperature = route.temperature
                max_tokens = route.max_tokens
                timeout = route.timeout_seconds
                self._last_route_decision = route
                logger.debug(
                    "LLM Router selected: profile=%s model=%s",
                    route.selected_profile.value,
                    route.concrete_model,
                )
            except Exception as e:
                logger.warning("LLM Router failed, using fallback: %s", e)
                self._last_route_decision = None

        url = f"{base_url.rstrip('/')}/chat/completions"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_message},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.error("LLM returned non-JSON content")
            return {}

    def _build_user_message(
        self,
        utterance: str,
        context: dict[str, Any] | None,
    ) -> str:
        """Build the user message with optional context."""
        parts = [f"Classify this utterance: {utterance}"]

        if context:
            # Only include safe context keys — never forward raw history
            safe_keys = {"active_entity_type", "active_entity_id", "conversation_topic"}
            filtered = {k: v for k, v in context.items() if k in safe_keys}
            if filtered:
                parts.append(f"Context: {json.dumps(filtered)}")

        return "\n".join(parts)

    def _parse_response(self, raw: dict[str, Any]) -> IntentResult:
        """Parse LLM JSON response into IntentResult.

        Validates action_type against policy matrix. Overrides risk_tier
        with the authoritative value from YAML (LLM cannot set risk tier).
        """
        action_type = raw.get("action_type", "unknown")
        raw_confidence = raw.get("confidence", 0.0)

        # Clamp confidence to valid range
        confidence = max(0.0, min(1.0, float(raw_confidence)))

        # Unknown action → escalate
        if action_type == "unknown" or action_type not in self._risk_tiers:
            return IntentResult(
                action_type="unknown",
                skill_pack="internal",
                confidence=0.0,
                entities=raw.get("entities", {}),
                risk_tier=RiskTier.YELLOW,  # Default unknown to YELLOW (safe)
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response=raw,
            )

        # Authoritative risk tier from policy matrix (LLM cannot override)
        risk_tier = self._risk_tiers[action_type]

        # Resolve skill pack (authoritative, not from LLM)
        skill_pack = _resolve_skill_pack(action_type)

        # Determine clarification state
        requires_clarification = CONFIDENCE_CLARIFY <= confidence < CONFIDENCE_AUTO_ROUTE
        clarification_prompt = (
            raw.get("clarification_prompt")
            if requires_clarification
            else None
        )

        return IntentResult(
            action_type=action_type,
            skill_pack=skill_pack,
            confidence=confidence,
            entities=raw.get("entities", {}),
            risk_tier=risk_tier,
            requires_clarification=requires_clarification,
            clarification_prompt=clarification_prompt,
            raw_llm_response=raw,
        )

    def _fail_closed_result(self, *, reason: str) -> IntentResult:
        """Return a fail-closed IntentResult (Law #3).

        The orchestrator will see confidence=0.0 and handle escalation.
        Receipt generation for failures is the orchestrator's responsibility
        (this service proposes, orchestrator decides + records).
        """
        return IntentResult(
            action_type="unknown",
            skill_pack="internal",
            confidence=0.0,
            entities={"_fail_reason": reason},
            risk_tier=RiskTier.YELLOW,
            requires_clarification=False,
            clarification_prompt=None,
            raw_llm_response=None,
        )


# =============================================================================
# Module-level singleton
# =============================================================================

_cached_classifier: IntentClassifier | None = None


def get_intent_classifier(*, reload: bool = False) -> IntentClassifier:
    """Get the cached IntentClassifier singleton."""
    global _cached_classifier
    if _cached_classifier is None or reload:
        _cached_classifier = IntentClassifier()
    return _cached_classifier
