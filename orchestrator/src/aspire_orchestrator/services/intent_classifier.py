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
import re
from typing import Any

import openai
from pydantic import BaseModel, Field

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.openai_client import generate_json_async
from aspire_orchestrator.services.policy_engine import get_policy_matrix

logger = logging.getLogger(__name__)

# Confidence thresholds
CONFIDENCE_AUTO_ROUTE = 0.85
CONFIDENCE_CLARIFY = 0.5

# LLM timeout — safety net when LLM Router is unavailable.
# Reasoning models (gpt-5-mini) need chain-of-thought before outputting JSON,
# which can take 15-45s on complex prompts. 90s is the safe fallback ceiling.
_LLM_TIMEOUT_SECONDS = 90


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
    intent_type: str = Field(default="action", description="action | conversation | knowledge | advice | hybrid")
    agent_target: str | None = Field(default=None, description="Suggested agent for this query (ava, finn, eli, etc.)")


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

## Action Catalog
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
- research.search, research.places, research.image → adam_research
- email.send, email.draft → eli_inbox
  - email.read, email.triage, email.send, email.draft → eli_inbox
  - office.read, office.create, office.draft, office.send → eli_inbox
- invoice.create, invoice.send, invoice.void → quinn_invoicing
- quote.create, quote.send → quinn_invoicing
- meeting.schedule, meeting.create_room, meeting.summarize → nora_conference
- payment.send, payment.transfer → (discontinued — no provider)
- contract.generate, contract.send, contract.review, contract.sign → clara_legal
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
6. For skill_pack, use the pack ID from the mapping above. If no specific pack, use "internal".
7. Always include "intent_type" and "agent_target" in your response.
8. If the user asks for a picture/photo/image of a place, thing, or concept, prefer action_type "research.image".

## Intent Type Classification
In addition to action_type, classify the intent_type:
- "intent_type": one of "action", "conversation", "knowledge", "advice", "hybrid"
  - "action": user wants to DO something (create invoice, send email, schedule meeting)
  - "conversation": general chat, greeting, identity question ("who are you?", "hello", "thanks")
  - "knowledge": domain knowledge question ("what is a tax write-off?", "how do I file quarterly taxes?")
  - "advice": strategic advisory request ("what strategies should I use for cash flow?", "should I hire a contractor?")
  - "hybrid": involves both reasoning AND potential action ("research tax write-offs and create a summary document")
- "agent_target": which agent is best suited to handle this query
  - "ava" for general/orchestration, "finn" for finance/tax, "eli" for email/communication, "quinn" for invoicing, "nora" for meetings, "sarah" for calls, "clara" for legal, "adam" for research, "tec" for documents, "teressa" for accounting, "milo" for payroll
  - Default to "ava" if unclear"""


# =============================================================================
# Action-to-SkillPack Resolution
# =============================================================================

# Built from skill_pack_manifests.yaml — action → skill pack ID
_ACTION_TO_PACK: dict[str, str] = {
    "email.read": "eli_inbox",
    "email.triage": "eli_inbox",
    "email.send": "eli_inbox",
    "email.draft": "eli_inbox",
    "office.read": "eli_inbox",
    "office.create": "eli_inbox",
    "office.draft": "eli_inbox",
    "office.send": "eli_inbox",
    "invoice.create": "quinn_invoicing",
    "invoice.send": "quinn_invoicing",
    "invoice.void": "quinn_invoicing",
    "quote.create": "quinn_invoicing",
    "quote.send": "quinn_invoicing",
    "meeting.schedule": "nora_conference",
    "meeting.create_room": "nora_conference",
    "meeting.summarize": "nora_conference",
    "calendar.create": "nora_conference",
    "calendar.read": "nora_conference",
    "calendar.list": "nora_conference",
    "research.search": "adam_research",
    "research.places": "adam_research",
    "research.image": "adam_research",
    "contract.generate": "clara_legal",
    "contract.send": "clara_legal",
    "contract.review": "clara_legal",
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
        self._model: str = os.environ.get("INTENT_LLM_MODEL", "gpt-5-mini")
        self._base_url: str = os.environ.get(
            "INTENT_LLM_BASE_URL", "https://api.openai.com/v1"
        )
        # API key resolution: ASPIRE_OPENAI_API_KEY (from .env/settings) > OPENAI_API_KEY > router
        self._api_key: str | None = (
            os.environ.get("ASPIRE_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
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
        # Deterministic fast-path for known specialist intents that frequently
        # under-classify in LLM mode (reduces CLASSIFICATION_UNCLEAR dead-ends).
        override = self._rule_based_specialist_intent(utterance, context=context)
        if override:
            return override

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

        except openai.APITimeoutError:
            logger.error("Intent classifier LLM timeout after %ds", _LLM_TIMEOUT_SECONDS)
            return self._fail_closed_result(reason="intent_classifier_timeout")

        except openai.APIStatusError as e:
            logger.error(
                "Intent classifier LLM HTTP error: status=%d",
                e.status_code,
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
        """Call the OpenAI API with structured output (JSON mode).

        Phase 3: Uses LLM Router to select the appropriate model.
        Falls back to direct config when router unavailable.
        Uses the official OpenAI SDK (AsyncOpenAI) for all API calls.
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

        # Reasoning models (gpt-5*, o1, o3) don't support temperature or
        # system role — use developer role and omit temperature.
        _is_reasoning = model.startswith(("gpt-5", "o1", "o3"))

        if _is_reasoning:
            messages = [
                {"role": "developer", "content": self._system_prompt},
                {"role": "user", "content": user_message},
            ]
        else:
            messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_message},
            ]

        # Reasoning models (gpt-5*, o1, o3) consume tokens for internal
        # chain-of-thought BEFORE producing output.  With max_completion_tokens=512,
        # the model can exhaust its budget on reasoning, leaving 0 tokens for the
        # JSON output (finish_reason="length").  Production fix: give reasoning
        # models a 4096-token budget so there's always room for the ~300-token
        # classification JSON after reasoning overhead.
        effective_max_tokens = max_tokens
        if _is_reasoning and max_tokens < 4096:
            effective_max_tokens = 4096

        parsed = await generate_json_async(
            model=model,
            messages=messages,
            api_key=self._api_key or "",
            base_url=base_url,
            timeout_seconds=float(timeout),
            max_output_tokens=effective_max_tokens,
            temperature=None if _is_reasoning else temperature,
            prefer_responses_api=True,
        )
        if not parsed:
            logger.error("LLM returned empty/invalid JSON for intent classifier (model=%s)", model)
        return parsed

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

        # Extract conversational intelligence fields
        intent_type = raw.get("intent_type", "action")
        agent_target = raw.get("agent_target", "ava")

        # Unknown action → escalate (but preserve intent_type routing)
        if action_type == "unknown" or action_type not in self._risk_tiers:
            # If intent_type signals intentional routing (not failure), set
            # confidence to 0.85 so the orchestrator routes instead of dead-ending.
            is_conversational = intent_type in ("conversation", "knowledge", "advice")
            return IntentResult(
                action_type="unknown",
                skill_pack="internal",
                confidence=0.85 if is_conversational else 0.0,
                entities=raw.get("entities", {}),
                risk_tier=RiskTier.YELLOW,  # Default unknown to YELLOW (safe)
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response=raw,
                intent_type=intent_type,
                agent_target=agent_target,
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
            intent_type=intent_type,
            agent_target=agent_target,
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
            intent_type="action",
            agent_target=None,
        )

    def _rule_based_specialist_intent(
        self,
        utterance: str,
        context: dict[str, Any] | None = None,
    ) -> IntentResult | None:
        """Handle high-signal Nora/Eli intents before LLM classification.

        These patterns are deterministic and map to existing policy actions.
        They avoid low-confidence clarify loops on clearly actionable requests.
        """
        text = (utterance or "").strip().lower()
        if not text:
            return None
        current_agent = str((context or {}).get("current_agent", "")).strip().lower()

        # Ava Admin: ALWAYS conversation path — skip LLM classify entirely.
        # Admin portal requests are pre-routed to ava_admin; the classify LLM
        # call adds ~500-1500ms of latency for zero value (we already know the
        # agent and intent type). Critical for voice latency.
        # ALL admin intents use intent_type="conversation" → agent_reason path.
        # The skill router has no admin.ops mappings, so the action path would
        # fail with "could not be routed to a valid skill path".
        if current_agent == "ava_admin":
            # action_type MUST be "unknown" — _route_after_classify checks
            # action_type != "unknown" BEFORE intent_type, so "admin.chat"
            # would fall into the ACTION path → skill router → ROUTING_DENIED.
            return IntentResult(
                action_type="unknown",
                skill_pack="ava_admin",
                confidence=0.0,
                entities={},
                risk_tier=self._risk_tiers.get("admin.chat", RiskTier.GREEN),
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response={"rule_based": "admin.chat"},
                intent_type="conversation",
                agent_target="ava_admin",
            )

        # Nora: explicit room creation request.
        if ("conference room" in text or "create room" in text or "meeting room" in text) and any(
            kw in text for kw in ("create", "open", "spin up", "start")
        ):
            return IntentResult(
                action_type="meeting.create_room",
                skill_pack="nora_conference",
                confidence=0.92,
                entities={},
                risk_tier=self._risk_tiers.get("meeting.create_room", RiskTier.GREEN),
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response={"rule_based": "meeting.create_room"},
                intent_type="action",
                agent_target="nora",
            )

        # Eli: unread/summary/reply workflow should at least route to inbox read.
        email_signals = ("email", "inbox", "unread", "messages")
        read_signals = ("read", "find", "show", "summarize", "summary", "last")
        if any(s in text for s in email_signals) and any(s in text for s in read_signals):
            limit = 5
            m = re.search(r"\b(\d{1,2})\b", text)
            if m:
                try:
                    limit = max(1, min(20, int(m.group(1))))
                except ValueError:
                    limit = 5
            entities = {
                "folder": "inbox",
                "unread_only": True if "unread" in text else None,
                "limit": limit,
            }
            return IntentResult(
                action_type="email.read",
                skill_pack="eli_inbox",
                confidence=0.9,
                entities=entities,
                risk_tier=self._risk_tiers.get("email.read", RiskTier.GREEN),
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response={"rule_based": "email.read"},
                intent_type="action",
                agent_target="eli",
            )

        # Eli: office message read flow.
        office_read_signals = ("office message", "office inbox", "office messages")
        if any(s in text for s in office_read_signals) and any(
            s in text for s in ("read", "show", "list", "unread", "inbox", "messages")
        ):
            limit = 10
            m = re.search(r"\b(\d{1,2})\b", text)
            if m:
                try:
                    limit = max(1, min(50, int(m.group(1))))
                except ValueError:
                    limit = 10
            entities = {
                "folder": "inbox",
                "unread_only": True if "unread" in text else None,
                "limit": limit,
            }
            return IntentResult(
                action_type="office.read",
                skill_pack="eli_inbox",
                confidence=0.91,
                entities=entities,
                risk_tier=self._risk_tiers.get("office.read", RiskTier.GREEN),
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response={"rule_based": "office.read"},
                intent_type="action",
                agent_target="eli",
            )

        # Eli: office message draft/create/send flow.
        if "office message" in text or "office email" in text:
            if any(s in text for s in ("draft", "prepare", "compose")):
                action = "office.draft"
            elif any(s in text for s in ("send", "deliver", "dispatch")):
                action = "office.send"
            else:
                action = "office.create"
            return IntentResult(
                action_type=action,
                skill_pack="eli_inbox",
                confidence=0.88,
                entities={},
                risk_tier=self._risk_tiers.get(action, RiskTier.YELLOW),
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response={"rule_based": action},
                intent_type="action",
                agent_target="eli",
            )

        # Eli conversational tweak loop: keep refinement turns in email.draft flow
        # so users can say "make it warmer/shorter" without repeating all fields.
        tweak_markers = (
            "tweak", "revise", "rewrite", "make it", "change it", "update it",
            "shorter", "longer", "warmer", "more friendly", "more formal", "less formal",
        )
        if current_agent == "eli" and any(marker in text for marker in tweak_markers):
            return IntentResult(
                action_type="email.draft",
                skill_pack="eli_inbox",
                confidence=0.9,
                entities={"tweak_request": True},
                risk_tier=self._risk_tiers.get("email.draft", RiskTier.YELLOW),
                requires_clarification=False,
                clarification_prompt=None,
                raw_llm_response={"rule_based": "email.draft.tweak"},
                intent_type="action",
                agent_target="eli",
            )

        return None


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



