from __future__ import annotations

from typing import Any

from aspire_orchestrator.models import RiskTier
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.services.intent_classifier import get_intent_classifier
from aspire_orchestrator.services.policy_engine import get_policy_matrix
from aspire_orchestrator.services.skill_router import get_skill_router
from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack


class AvaUserSkillPack(AgenticSkillPack):
    """Template-compliant wrapper for the Ava User orchestration surface."""

    def __init__(self) -> None:
        super().__init__(
            agent_id='ava_user',
            agent_name='Ava User',
            default_risk_tier='green',
            memory_enabled=True,
        )

    async def get_greeting(
        self, ctx: AgentContext, *, user_name: str | None = None, time_of_day: str | None = None,
    ) -> str:
        """Ava's greeting — warm orchestrator personality (7b)."""
        if time_of_day is None:
            from datetime import datetime, timezone
            hour = datetime.now(timezone.utc).hour
            time_of_day = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")

        name_part = f" {user_name}" if user_name else ""
        is_returning = False
        if self._memory_enabled:
            try:
                episodes = await self.recall_episodes(ctx, limit=1)
                is_returning = bool(episodes)
            except Exception:
                pass

        if is_returning:
            return f"Good {time_of_day}{name_part}. What can I help you with?"
        else:
            return f"Good {time_of_day}{name_part}, I'm Ava — your business operating system. How can I help you today?"


    async def intent_classify(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        utterance = str(params.get('utterance', '')).strip()
        if not utterance:
            return AgentResult(success=False, error='Missing required parameter: utterance')

        classifier = get_intent_classifier()
        intent = await classifier.classify(utterance, params.get('context'))
        payload = intent.model_dump()
        receipt = self.build_receipt(
            ctx=ctx,
            event_type='intent.classify',
            status='succeeded',
            inputs={'utterance': utterance, 'context': params.get('context', {})},
            metadata={'agent_surface': 'ava_user', 'action': 'intent.classify'},
        )
        await self.emit_receipt(receipt)
        return AgentResult(success=True, data={'intent_result': payload}, receipt=receipt)

    async def route_plan(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        action_type = str(params.get('action_type', '')).strip()
        skill_pack = str(params.get('skill_pack', '')).strip()
        if not action_type:
            return AgentResult(success=False, error='Missing required parameter: action_type')

        from aspire_orchestrator.services.intent_classifier import IntentResult

        intent = IntentResult(
            action_type=action_type,
            skill_pack=skill_pack or None,
            confidence=float(params.get('confidence', 0.95)),
            entities=params.get('entities') or {},
            risk_tier=RiskTier(str(params.get('risk_tier', 'green'))),
            requires_clarification=bool(params.get('requires_clarification', False)),
        )
        router = get_skill_router()
        plan = await router.route(
            intent,
            context={
                'suite_id': ctx.suite_id,
                'office_id': ctx.office_id,
                'current_agent': 'ava',
                'allow_internal_routing': bool(params.get('allow_internal_routing', False)),
            },
        )
        receipt = self.build_receipt(
            ctx=ctx,
            event_type='route.plan',
            status='succeeded' if not plan.deny_reason else 'denied',
            inputs={'action_type': action_type, 'skill_pack': skill_pack},
            metadata={'deny_reason': plan.deny_reason, 'agent_surface': 'ava_user'},
        )
        await self.emit_receipt(receipt)
        return AgentResult(success=True, data={'routing_plan': plan.model_dump()}, receipt=receipt)

    async def governance_preview(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        action_type = str(params.get('action_type', '')).strip()
        if not action_type:
            return AgentResult(success=False, error='Missing required parameter: action_type')

        matrix = get_policy_matrix()
        evaluation = matrix.evaluate(action_type)
        risk_value = evaluation.risk_tier.value if hasattr(evaluation.risk_tier, 'value') else str(evaluation.risk_tier)
        preview = {
            'action_type': action_type,
            'allowed': bool(evaluation.allowed),
            'risk_tier': risk_value,
            'approval_required': bool(evaluation.approval_required),
            'presence_required': bool(getattr(evaluation, 'presence_required', False)),
            'capability_scope': getattr(evaluation, 'capability_scope', None),
            'tool_ids': list(getattr(evaluation, 'tools', []) or []),
            'deny_reason': getattr(evaluation, 'deny_reason', None),
        }
        receipt = self.build_receipt(
            ctx=ctx,
            event_type='governance.preview',
            status='succeeded' if preview['allowed'] else 'denied',
            inputs={'action_type': action_type},
            metadata={'agent_surface': 'ava_user', 'risk_tier': preview['risk_tier']},
        )
        await self.emit_receipt(receipt)
        return AgentResult(success=True, data={'governance': preview}, receipt=receipt)
