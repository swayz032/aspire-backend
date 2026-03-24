from __future__ import annotations

from typing import Any

from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.skillpacks.ava_admin_desk import get_ava_admin_desk
from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack


class AvaAdminSkillPack(AgenticSkillPack):
    """Template-compliant wrapper over the existing Ava Admin desk implementation."""

    def __init__(self) -> None:
        super().__init__(
            agent_id='ava_admin',
            agent_name='Ava Admin',
            default_risk_tier='green',
            memory_enabled=True,
        )
        self._desk = get_ava_admin_desk()

    async def admin_ops_health_pulse(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_health_pulse(ctx)

    async def admin_ops_triage(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        incident_id = str(params.get('incident_id', '')).strip()
        if not incident_id:
            return AgentResult(success=False, error='I need the incident ID to look that up.')
        return await self._desk.triage_incident(ctx, incident_id=incident_id)

    async def admin_ops_provider_analysis(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        provider = params.get('provider')
        limit = int(params.get('limit', 100))
        return await self._desk.analyze_provider_errors(ctx, provider=provider, limit=limit)

    async def admin_ops_robot_triage(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        run_id = str(params.get('run_id', '')).strip()
        if not run_id:
            return AgentResult(success=False, error='I need the run ID to trace that robot failure.')
        return await self._desk.triage_robot_failure(ctx, run_id=run_id)

    async def admin_ops_council_dispatch(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        incident_id = str(params.get('incident_id', '')).strip()
        if not incident_id:
            return AgentResult(success=False, error='I need the incident ID to look that up.')
        return await self._desk.dispatch_council(ctx, incident_id=incident_id, evidence_pack={})

    async def admin_ops_learning_entry_create(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        incident_id = str(params.get('incident_id', '')).strip()
        lesson = str(params.get('lesson', '')).strip()
        if not incident_id:
            return AgentResult(success=False, error='I need the incident ID to look that up.')
        if not lesson:
            return AgentResult(success=False, error='I need the lesson content to log it.')
        return await self._desk.create_learning_entry(ctx, incident_id=incident_id, entry_type="lesson", content={"lesson": lesson})

    # --- Wave 1: New capability wrappers ---

    async def admin_ops_sentry_summary(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_sentry_summary(ctx)

    async def admin_ops_sentry_issues(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        project = params.get('project')
        limit = int(params.get('limit', 10))
        return await self._desk.get_sentry_issues(ctx, project=project, limit=limit)

    async def admin_ops_workflow_status(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 20))
        return await self._desk.get_workflow_status(ctx, limit=limit)

    async def admin_ops_approval_queue(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        status = params.get('status', 'pending')
        limit = int(params.get('limit', 20))
        return await self._desk.get_approval_queue(ctx, status=status, limit=limit)

    async def admin_ops_receipt_audit(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        suite_id = params.get('suite_id', 'system')
        limit = int(params.get('limit', 50))
        return await self._desk.get_receipt_audit(ctx, suite_id=suite_id, limit=limit)

    async def admin_ops_web_search(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        query = str(params.get('query', '')).strip()
        if not query:
            return AgentResult(success=False, error='I need a search query to look that up.')
        count = int(params.get('count', 5))
        return await self._desk.search_web(ctx, query=query, count=count)

    async def admin_ops_council_history(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        status = params.get('status')
        limit = int(params.get('limit', 10))
        return await self._desk.get_council_history(ctx, status=status, limit=limit)

    async def admin_ops_metrics_snapshot(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_metrics_snapshot(ctx)

    # --- Wave 2: Data intelligence wrappers ---

    async def admin_ops_provider_call_logs(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        provider = params.get('provider')
        status = params.get('status')
        limit = int(params.get('limit', 50))
        return await self._desk.get_provider_call_logs(ctx, provider=provider, status=status, limit=limit)

    async def admin_ops_client_events(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        event_type = params.get('event_type')
        severity = params.get('severity')
        limit = int(params.get('limit', 50))
        return await self._desk.get_client_events(ctx, event_type=event_type, severity=severity, limit=limit)

    async def admin_ops_db_performance(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_db_performance(ctx)

    async def admin_ops_trace(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        correlation_id = str(params.get('correlation_id', '')).strip()
        if not correlation_id:
            return AgentResult(success=False, error='I need the correlation ID to pull that trace.')
        return await self._desk.get_trace(ctx, correlation_id=correlation_id)

    async def admin_ops_list_incidents(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        state = params.get('state')
        severity = params.get('severity')
        limit = int(params.get('limit', 20))
        return await self._desk.list_incidents(ctx, state=state, severity=severity, limit=limit)

    async def admin_ops_outbox_status(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 50))
        return await self._desk.get_outbox_status(ctx, limit=limit)

    async def admin_ops_n8n_operations(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 50))
        return await self._desk.get_n8n_operations(ctx, limit=limit)

    async def admin_ops_webhook_health(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        provider = params.get('provider')
        return await self._desk.get_webhook_health(ctx, provider=provider)

    async def admin_ops_model_policy(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_model_policy(ctx)

    async def admin_ops_business_snapshot(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 100))
        return await self._desk.get_business_snapshot(ctx, limit=limit)
