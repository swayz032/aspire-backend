from __future__ import annotations

from typing import Any

from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.skillpacks.ava_admin_desk import get_ava_admin_desk
from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack


class AvaAdminSkillPack(EnhancedSkillPack):
    """Template-compliant wrapper over the existing Ava Admin desk implementation."""

    def __init__(self) -> None:
        super().__init__(
            agent_id='ava_admin',
            agent_name='Ava Admin',
            default_risk_tier='green',
        )
        self._desk = get_ava_admin_desk()

    async def admin_ops_health_pulse(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_health_pulse(ctx)

    async def admin_ops_triage(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        incident_id = str(params.get('incident_id', '')).strip()
        if not incident_id:
            return AgentResult(success=False, error='Missing required parameter: incident_id')
        return await self._desk.triage_incident(ctx, incident_id=incident_id)

    async def admin_ops_provider_analysis(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        provider = params.get('provider')
        limit = int(params.get('limit', 100))
        return await self._desk.analyze_provider_errors(ctx, provider=provider, limit=limit)

    async def admin_ops_robot_triage(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        run_id = str(params.get('run_id', '')).strip()
        if not run_id:
            return AgentResult(success=False, error='Missing required parameter: run_id')
        return await self._desk.triage_robot_failure(ctx, run_id=run_id)

    async def admin_ops_council_dispatch(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        incident_id = str(params.get('incident_id', '')).strip()
        if not incident_id:
            return AgentResult(success=False, error='Missing required parameter: incident_id')
        return await self._desk.dispatch_council(ctx, incident_id=incident_id)

    async def admin_ops_learning_entry_create(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        incident_id = str(params.get('incident_id', '')).strip()
        lesson = str(params.get('lesson', '')).strip()
        if not incident_id:
            return AgentResult(success=False, error='Missing required parameter: incident_id')
        if not lesson:
            return AgentResult(success=False, error='Missing required parameter: lesson')
        return await self._desk.create_learning_entry(ctx, incident_id=incident_id, lesson=lesson)
