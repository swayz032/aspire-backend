"""Ava Admin Ops Desk — Internal Control-Plane Operator & Incident Commander.

This is Admin Ava's skill pack — the internal_backend persona that monitors
the entire Aspire platform and provides SRE-grade incident response.

**Admin Ava vs User Ava:**
  - User Ava (frontend): Executive assistant for SMB owners, Anam avatar, customer-facing
  - Admin Ava (backend): Platform operator, incident commander, ElevenLabs voice ops desk

**Channel:** internal_backend (ops/dev only — never customer-facing)
**Risk Tier:** GREEN (all read-only observation) / YELLOW (triage proposals, config changes)

**Capabilities:**
  1. Incident Commander Mode — structured triage of open incidents
  2. Platform Health Pulse — aggregate health across all subsystems
  3. Robot Failure Triage — analyze failed robot runs, propose fixes
  4. Provider Call Analysis — detect error patterns, rate limit spikes
  5. Receipt Chain Audit — verify hash chain integrity
  6. Council Dispatch — spawn Meeting of Minds advisors for complex incidents
  7. Learning Loop — convert incidents into runbook updates, eval cases

**ElevenLabs Voice (LLM OPS DESK):**
  Voice ID: Ava — 56bWURjYFHyYyVf490Dp (same voice, admin persona)

Law compliance:
  - Law #1: Admin Ava observes and proposes — never executes without orchestrator approval
  - Law #2: Every triage action produces a receipt
  - Law #3: Missing evidence → "insufficient data" (fail-closed on speculation)
  - Law #7: Admin Ava is a diagnostic tool, not a decision-maker
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult

logger = logging.getLogger(__name__)

# Admin Ava ElevenLabs Voice ID (same voice, admin persona)
AVA_ADMIN_VOICE_ID = "56bWURjYFHyYyVf490Dp"

# Incident severity thresholds
_STUCK_JOB_THRESHOLD = 5  # stuck jobs = high severity
_PROVIDER_ERROR_SPIKE_THRESHOLD = 10  # errors in last 100 calls
_RECEIPT_GAP_THRESHOLD = 0  # ANY gap = sev1

# Incident Commander output template (deterministic, no LLM)
_INCIDENT_COMMANDER_TEMPLATE = """## INCIDENT COMMANDER REPORT

### 1) STATUS
- **Impact:** {impact}
- **Scope:** {scope}
- **Since:** {since}
- **Severity:** {severity}

### 2) EVIDENCE
- **Incident ID:** {incident_id}
- **Correlation ID:** {correlation_id}
- **Receipt IDs:** {receipt_ids}
- **Provider Call IDs:** {provider_call_ids}

### 3) HYPOTHESES (ranked)
{hypotheses}

### 4) MITIGATION OPTIONS
{mitigation_options}

### 5) RECOMMENDATION
{recommendation}

### 6) REQUIRED APPROVALS + RECEIPTS
{approvals}

### 7) ROLLBACK TRIGGERS
{rollback_triggers}
"""

# Platform health pulse template
_HEALTH_PULSE_TEMPLATE = """## PLATFORM HEALTH PULSE

**Time:** {timestamp}
**Overall Status:** {overall_status}

### Subsystem Status
| Subsystem | Status | Details |
|-----------|--------|---------|
| Orchestrator | {orchestrator_status} | {orchestrator_detail} |
| Provider Calls | {provider_status} | {provider_detail} |
| Receipt Store | {receipt_status} | {receipt_detail} |
| Outbox Queue | {outbox_status} | {outbox_detail} |
| Incidents | {incident_status} | {incident_detail} |

### Key Metrics
- **Open incidents:** {open_incidents}
- **Recent provider errors:** {provider_errors} / {provider_total} calls
- **Outbox queue depth:** {queue_depth}
- **Stuck jobs:** {stuck_jobs}
"""


class AvaAdminDesk(AgenticSkillPack):
    """Admin Ava's skill pack — platform operator & incident commander.

    This is an internal_backend pack. It NEVER faces customers.
    All operations are read-only observation or structured proposals.
    """

    def __init__(self):
        super().__init__(
            agent_id="ava_admin_desk",
            agent_name="Ava Admin (Ops Desk)",
            default_risk_tier="green",
            memory_enabled=True,
        )

    # =========================================================================
    # 1. Platform Health Pulse (GREEN — read-only aggregation)
    # =========================================================================

    async def get_health_pulse(self, ctx: AgentContext) -> AgentResult:
        """Aggregate health across all subsystems.

        Returns a structured health report with status for:
        orchestrator, provider calls, receipt store, outbox, incidents.
        """
        now = datetime.now(timezone.utc)
        data: dict[str, Any] = {}

        # --- Orchestrator health ---
        orchestrator_status = "OK"
        orchestrator_detail = "Running"

        # --- Provider calls (last 100) ---
        try:
            from aspire_orchestrator.services.provider_call_logger import get_provider_call_logger
            pcl = get_provider_call_logger()
            recent_calls = pcl.query_calls(limit=100)
            total = len(recent_calls)
            errors = sum(1 for c in recent_calls if c.get("status") == "error")
            error_rate = (errors / total * 100) if total > 0 else 0

            if errors >= _PROVIDER_ERROR_SPIKE_THRESHOLD:
                provider_status = "DEGRADED"
            elif errors > 0:
                provider_status = "WARNING"
            else:
                provider_status = "OK"
            provider_detail = f"{errors}/{total} errors ({error_rate:.0f}%)"
        except Exception as e:
            provider_status = "UNKNOWN"
            provider_detail = str(e)[:100]
            total = 0
            errors = 0

        # --- Receipt store ---
        try:
            from aspire_orchestrator.services.receipt_store import get_receipt_count
            receipt_count = get_receipt_count()
            receipt_status = "OK"
            receipt_detail = f"{receipt_count} receipts stored"
        except Exception as e:
            receipt_status = "UNKNOWN"
            receipt_detail = str(e)[:100]
            receipt_count = 0

        # --- Outbox ---
        try:
            from aspire_orchestrator.services.outbox_client import get_outbox_client
            outbox = get_outbox_client()
            queue_status = outbox.get_queue_status()
            queue_depth = queue_status["queue_depth"]
            stuck = queue_status["stuck_jobs"]

            if stuck >= _STUCK_JOB_THRESHOLD:
                outbox_status = "CRITICAL"
            elif queue_depth > 20:
                outbox_status = "WARNING"
            else:
                outbox_status = "OK"
            outbox_detail = f"depth={queue_depth}, stuck={stuck}"
        except Exception as e:
            outbox_status = "UNKNOWN"
            outbox_detail = str(e)[:100]
            queue_depth = 0
            stuck = 0

        # --- Incidents ---
        try:
            from aspire_orchestrator.services.admin_store import get_admin_store
            store = get_admin_store()
            open_incidents, _ = store.query_incidents(state="open", limit=100)
            open_count = len(open_incidents)

            if open_count > 5:
                incident_status = "CRITICAL"
            elif open_count > 0:
                incident_status = "WARNING"
            else:
                incident_status = "OK"
            incident_detail = f"{open_count} open"
        except Exception as e:
            incident_status = "UNKNOWN"
            incident_detail = str(e)[:100]
            open_count = 0

        # --- Overall status ---
        statuses = [orchestrator_status, provider_status, receipt_status, outbox_status, incident_status]
        if "CRITICAL" in statuses:
            overall = "CRITICAL"
        elif "DEGRADED" in statuses:
            overall = "DEGRADED"
        elif "WARNING" in statuses:
            overall = "WARNING"
        elif "UNKNOWN" in statuses:
            overall = "PARTIAL"
        else:
            overall = "HEALTHY"

        report = _HEALTH_PULSE_TEMPLATE.format(
            timestamp=now.isoformat(),
            overall_status=overall,
            orchestrator_status=orchestrator_status,
            orchestrator_detail=orchestrator_detail,
            provider_status=provider_status,
            provider_detail=provider_detail,
            receipt_status=receipt_status,
            receipt_detail=receipt_detail,
            outbox_status=outbox_status,
            outbox_detail=outbox_detail,
            incident_status=incident_status,
            incident_detail=incident_detail,
            open_incidents=open_count,
            provider_errors=errors,
            provider_total=total,
            queue_depth=queue_depth,
            stuck_jobs=stuck,
        )

        data = {
            "report": report,
            "overall_status": overall,
            "subsystems": {
                "orchestrator": orchestrator_status,
                "provider_calls": provider_status,
                "receipt_store": receipt_status,
                "outbox": outbox_status,
                "incidents": incident_status,
            },
            "metrics": {
                "open_incidents": open_count,
                "provider_errors": errors,
                "provider_total": total,
                "queue_depth": queue_depth,
                "stuck_jobs": stuck,
                "receipt_count": receipt_count,
            },
            "voice_id": AVA_ADMIN_VOICE_ID,
        }

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.health_pulse",
            status="ok",
            inputs={"requested_at": now.isoformat()},
            metadata={"overall_status": overall},
        )
        await self.emit_receipt(receipt)

        return AgentResult(success=True, data=data, receipt=receipt)

    # =========================================================================
    # 2. Incident Commander Mode (GREEN — read-only triage)
    # =========================================================================

    async def triage_incident(
        self,
        ctx: AgentContext,
        *,
        incident_id: str,
    ) -> AgentResult:
        """Produce Incident Commander report for a specific incident.

        Uses deterministic template (no LLM) for the structure,
        then calls LLM for hypothesis generation only.
        """
        # Fetch incident data
        try:
            from aspire_orchestrator.services.admin_store import get_admin_store
            store = get_admin_store()
            incident = store.get_incident(incident_id)
        except Exception as e:
            return AgentResult(
                success=False,
                error=f"Failed to fetch incident {incident_id}: {e}",
            )

        if not incident:
            return AgentResult(
                success=False,
                error=f"Incident {incident_id} not found",
            )

        # Gather evidence
        correlation_id = incident.get("correlation_id", "unknown")
        evidence_pack = incident.get("evidence_pack", {})
        timeline = incident.get("timeline", [])

        # Get related receipts
        try:
            from aspire_orchestrator.services.receipt_store import query_receipts
            related_receipts = query_receipts(
                suite_id=incident.get("suite_id", "system"),
                correlation_id=correlation_id,
                limit=10,
            )
            receipt_ids = [r.get("id", "unknown") for r in related_receipts[:5]]
        except Exception:
            related_receipts = []
            receipt_ids = []

        # Get related provider calls
        try:
            from aspire_orchestrator.services.provider_call_logger import get_provider_call_logger
            pcl = get_provider_call_logger()
            related_calls = pcl.query_calls(correlation_id=correlation_id, limit=5)
            call_ids = [c.get("call_id", "unknown") for c in related_calls]
        except Exception:
            related_calls = []
            call_ids = []

        # Build hypotheses using LLM (classify step — cheap model)
        evidence_summary = {
            "title": incident.get("title", "Unknown incident"),
            "severity": incident.get("severity", "unknown"),
            "state": incident.get("state", "unknown"),
            "exception_type": evidence_pack.get("exception_type", "unknown"),
            "error_code": evidence_pack.get("error_code", "unknown"),
            "path": evidence_pack.get("path", "unknown"),
            "timeline_count": len(timeline),
            "receipt_count": len(related_receipts),
            "provider_call_count": len(related_calls),
        }

        hypotheses_text = "- H1: Unknown root cause (insufficient evidence)\n"
        recommendation = "Gather more evidence before taking action."
        try:
            llm_result = await self.call_llm(
                f"Given this incident evidence, produce 1-3 ranked hypotheses "
                f"with confidence levels and next evidence to confirm:\n{evidence_summary}",
                step_type="classify",
                risk_tier="green",
                context=ctx,
            )
            if llm_result.get("content"):
                hypotheses_text = llm_result["content"]
        except Exception as e:
            logger.warning("LLM hypothesis generation failed: %s", e)

        # Build deterministic report
        report = _INCIDENT_COMMANDER_TEMPLATE.format(
            impact=incident.get("title", "Unknown"),
            scope=f"suite_id={incident.get('suite_id', 'system')}",
            since=incident.get("first_seen", "unknown"),
            severity=incident.get("severity", "unknown"),
            incident_id=incident_id,
            correlation_id=correlation_id,
            receipt_ids=", ".join(receipt_ids) or "none found",
            provider_call_ids=", ".join(call_ids) or "none found",
            hypotheses=hypotheses_text,
            mitigation_options=(
                "- A) Restart affected service (reversible, fastest)\n"
                "- B) Rollback last deployment (if deployment-related)\n"
                "- C) Enable safe mode / degrade gracefully"
            ),
            recommendation=recommendation,
            approvals="- Admin approval required for any mitigation action\n- Receipt will be generated for triage decision",
            rollback_triggers="- Error rate > 5% for 10 minutes\n- Outbox stuck jobs > 10\n- Provider 5xx rate > 20%",
        )

        data = {
            "report": report,
            "incident": incident,
            "evidence": {
                "receipt_ids": receipt_ids,
                "provider_call_ids": call_ids,
                "correlation_id": correlation_id,
            },
            "voice_id": AVA_ADMIN_VOICE_ID,
        }

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.incident_triage",
            status="ok",
            inputs={"incident_id": incident_id},
            metadata={"severity": incident.get("severity"), "state": incident.get("state")},
        )
        await self.emit_receipt(receipt)

        return AgentResult(success=True, data=data, receipt=receipt)

    # =========================================================================
    # 3. Robot Failure Triage (GREEN — read-only analysis)
    # =========================================================================

    async def triage_robot_failure(
        self,
        ctx: AgentContext,
        *,
        run_id: str,
    ) -> AgentResult:
        """Analyze a failed robot run and produce triage proposal.

        Per robots_integration.md:
        - Treat robot failures as first-class incidents
        - Convert recurring failures into: regression scenario, eval case, runbook update
        """
        # Fetch robot run receipts
        try:
            from aspire_orchestrator.services.receipt_store import query_receipts
            run_receipts = query_receipts(
                suite_id="system",
                correlation_id=run_id,
                limit=20,
            )
        except Exception:
            run_receipts = []

        if not run_receipts:
            return AgentResult(
                success=False,
                error=f"No receipts found for robot run {run_id}",
            )

        # Analyze failure pattern
        failed_receipts = [r for r in run_receipts if r.get("outcome") in ("failed", "FAILED")]
        failure_summary = {
            "run_id": run_id,
            "total_receipts": len(run_receipts),
            "failed_receipts": len(failed_receipts),
            "failure_reasons": list(set(
                r.get("reason_code", "unknown") for r in failed_receipts
            )),
            "actions": list(set(
                r.get("action_type", "unknown") for r in run_receipts
            )),
        }

        # Build triage proposal (deterministic)
        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "type": "robot_failure_triage",
            "run_id": run_id,
            "failure_summary": failure_summary,
            "proposed_actions": [
                {
                    "action": "create_regression_scenario",
                    "description": f"Add regression test for failure pattern: {failure_summary['failure_reasons']}",
                    "risk_tier": "green",
                },
                {
                    "action": "update_runbook",
                    "description": "Update incident runbook with new failure pattern",
                    "risk_tier": "green",
                },
                {
                    "action": "create_eval_case",
                    "description": "Add eval case to prevent regression",
                    "risk_tier": "green",
                },
            ],
            "requires_approval": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.robot_triage",
            status="ok",
            inputs={"run_id": run_id},
            metadata=failure_summary,
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "proposal": proposal,
                "failure_summary": failure_summary,
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )

    # =========================================================================
    # 4. Council Dispatch (YELLOW — spawns advisors, requires approval)
    # =========================================================================

    async def dispatch_council(
        self,
        ctx: AgentContext,
        *,
        incident_id: str,
        evidence_pack: dict[str, Any],
    ) -> AgentResult:
        """Spawn Meeting of Minds council for complex incident triage.

        Council members (per MEETING_OF_MINDS_RUNBOOK.md):
          - GPT 5.2: Architecture critic, root cause analysis
          - Gemini 3: Research cross-check, alternative approaches
          - Opus 4.6: Implementation plan ($5 budget — testing only)

        Council advisors are read-only (no tool execution).
        Evidence packs are read-only snapshots.
        Ava adjudicates after receiving proposals.
        """
        council_session_id = str(uuid.uuid4())

        # Build read-only evidence snapshot for council
        evidence_snapshot = {
            "council_session_id": council_session_id,
            "incident_id": incident_id,
            "evidence_pack": evidence_pack,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "read_only": True,  # Council CANNOT execute
        }

        # Dispatch A2A triage message
        try:
            from aspire_orchestrator.services.a2a_service import get_a2a_service
            a2a = get_a2a_service()

            # Dispatch to council agent (if registered)
            result = a2a.dispatch(
                suite_id="system",
                office_id="system",
                correlation_id=ctx.correlation_id,
                task_type="ops.triage.council",
                assigned_to_agent="meeting_of_minds",
                payload=evidence_snapshot,
                priority=1,  # Highest priority for incident triage
            )

            if result.receipt_data:
                from aspire_orchestrator.services.receipt_store import store_receipts
                store_receipts([result.receipt_data])

        except Exception as e:
            logger.warning("Council dispatch failed (non-blocking): %s", e)
            result = None

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.council_dispatched",
            status="ok",
            inputs={"incident_id": incident_id, "council_session_id": council_session_id},
            metadata={
                "advisors": ["gpt-5.2", "gemini-3", "opus-4.6"],
                "evidence_keys": list(evidence_pack.keys()),
            },
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "council_session_id": council_session_id,
                "incident_id": incident_id,
                "dispatched": result.success if result else False,
                "advisors": ["gpt-5.2", "gemini-3", "opus-4.6"],
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )

    # =========================================================================
    # 5. Learning Loop Entry (GREEN — creates durable learning records)
    # =========================================================================

    async def create_learning_entry(
        self,
        ctx: AgentContext,
        *,
        incident_id: str,
        entry_type: str,  # "regression_scenario" | "eval_case" | "runbook_update"
        content: dict[str, Any],
    ) -> AgentResult:
        """Create a learning loop entry from an incident.

        Per prevention_pipeline.md:
        1) Postmortem draft (timeline from receipts)
        2) New eval case (repro + expected deny/allow)
        3) New robot scenario (synthetic reproduction)
        4) Runbook updated (operator checklist + engineer details)
        """
        valid_types = {"regression_scenario", "eval_case", "runbook_update", "postmortem_draft"}
        if entry_type not in valid_types:
            return AgentResult(
                success=False,
                error=f"Invalid entry_type: {entry_type}. Valid: {valid_types}",
            )

        entry = {
            "entry_id": str(uuid.uuid4()),
            "incident_id": incident_id,
            "entry_type": entry_type,
            "content": content,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "ava_admin_desk",
            "status": "pending_review",
        }

        # Store as receipt (durable learning record)
        receipt = self.build_receipt(
            ctx=ctx,
            event_type=f"admin.learning_loop.{entry_type}",
            status="ok",
            inputs={"incident_id": incident_id, "entry_type": entry_type},
            metadata={"entry_id": entry["entry_id"]},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"entry": entry, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 6. Provider Error Analysis (GREEN — read-only pattern detection)
    # =========================================================================

    async def analyze_provider_errors(
        self,
        ctx: AgentContext,
        *,
        provider: str | None = None,
        limit: int = 100,
    ) -> AgentResult:
        """Detect error patterns in recent provider calls."""
        try:
            from aspire_orchestrator.services.provider_call_logger import get_provider_call_logger
            pcl = get_provider_call_logger()
            calls = pcl.query_calls(provider=provider, limit=limit)
        except Exception as e:
            return AgentResult(success=False, error=f"Failed to query provider calls: {e}")

        # Analyze patterns
        total = len(calls)
        errors = [c for c in calls if c.get("status") == "error"]
        error_count = len(errors)

        # Group errors by code
        error_codes: dict[str, int] = {}
        for e in errors:
            code = e.get("error_code", "unknown")
            error_codes[code] = error_codes.get(code, 0) + 1

        # Group errors by provider
        error_providers: dict[str, int] = {}
        for e in errors:
            p = e.get("provider", "unknown")
            error_providers[p] = error_providers.get(p, 0) + 1

        analysis = {
            "total_calls": total,
            "error_count": error_count,
            "error_rate": f"{(error_count / total * 100):.1f}%" if total > 0 else "0%",
            "error_codes": dict(sorted(error_codes.items(), key=lambda x: x[1], reverse=True)),
            "error_by_provider": dict(sorted(error_providers.items(), key=lambda x: x[1], reverse=True)),
            "spike_detected": error_count >= _PROVIDER_ERROR_SPIKE_THRESHOLD,
        }

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.provider_analysis",
            status="ok",
            inputs={"provider": provider, "limit": limit},
            metadata={"error_count": error_count, "total": total},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"analysis": analysis, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )


    # =========================================================================
    # 7. Sentry Summary (GREEN — read-only)
    # =========================================================================

    async def get_sentry_summary(self, ctx: AgentContext) -> AgentResult:
        """Get aggregated Sentry issue summary."""
        try:
            from aspire_orchestrator.services.sentry_read import get_sentry_read_service
            service = get_sentry_read_service()
            summary = await service.get_summary()
        except Exception as e:
            return AgentResult(success=False, error=f"Sentry summary failed: {e}")

        issue_count = len(summary.get("issues", [])) if isinstance(summary, dict) else 0
        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.sentry_summary",
            status="ok",
            inputs={"requested_at": datetime.now(timezone.utc).isoformat()},
            metadata={"issue_count": issue_count},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"summary": summary, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 8. Sentry Issues (GREEN — read-only)
    # =========================================================================

    async def get_sentry_issues(
        self,
        ctx: AgentContext,
        *,
        project: str | None = None,
        limit: int = 10,
    ) -> AgentResult:
        """Get unresolved Sentry issues, optionally filtered by project."""
        try:
            from aspire_orchestrator.services.sentry_read import get_sentry_read_service
            service = get_sentry_read_service()
            raw = await service.get_issues(limit=limit)
            issues = raw.get("issues", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        except Exception as e:
            return AgentResult(success=False, error=f"Sentry issues failed: {e}")

        if project:
            issues = [i for i in issues if i.get("project", {}).get("slug") == project]

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.sentry_issues",
            status="ok",
            inputs={"project": project, "limit": limit},
            metadata={"issue_count": len(issues)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"issues": issues, "count": len(issues), "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 9. Workflow Status (GREEN — read-only Supabase query)
    # =========================================================================

    async def get_workflow_status(
        self,
        ctx: AgentContext,
        *,
        limit: int = 20,
    ) -> AgentResult:
        """Get recent workflow execution status with counts."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            result = await supabase_select(
                "workflow_executions",
                {},
                order_by="created_at.desc",
                limit=limit,
            )
            rows = result if isinstance(result, list) else []
        except Exception as e:
            return AgentResult(success=False, error=f"Workflow status query failed: {e}")

        # Compute counts by status
        counts: dict[str, int] = {}
        for row in rows:
            status = row.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.workflow_status",
            status="ok",
            inputs={"limit": limit},
            metadata={"total": len(rows), "counts": counts},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "workflows": rows,
                "counts": counts,
                "total": len(rows),
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )

    # =========================================================================
    # 10. Approval Queue (GREEN — read-only Supabase query)
    # =========================================================================

    async def get_approval_queue(
        self,
        ctx: AgentContext,
        *,
        status: str = "pending",
        limit: int = 20,
    ) -> AgentResult:
        """Get approval requests filtered by status."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            filters = f"status=eq.{status}" if status else ""
            result = await supabase_select(
                "approval_requests",
                filters,
                order_by="created_at.desc",
                limit=limit,
            )
            rows = result if isinstance(result, list) else []
        except Exception as e:
            return AgentResult(success=False, error=f"Approval queue query failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.approval_queue",
            status="ok",
            inputs={"status_filter": status, "limit": limit},
            metadata={"count": len(rows)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "approvals": rows,
                "count": len(rows),
                "filter": status,
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )

    # =========================================================================
    # 11. Receipt Audit (GREEN — read-only chain integrity check)
    # =========================================================================

    async def get_receipt_audit(
        self,
        ctx: AgentContext,
        *,
        suite_id: str = "system",
        limit: int = 50,
    ) -> AgentResult:
        """Audit receipt chain integrity for a suite."""
        try:
            from aspire_orchestrator.services.receipt_store import (
                get_receipt_count,
                get_chain_receipts,
            )
            total_count = get_receipt_count(suite_id=suite_id)
            chain = get_chain_receipts(suite_id=suite_id)
        except Exception as e:
            return AgentResult(success=False, error=f"Receipt audit failed: {e}")

        # Check chain integrity — look for gaps
        gaps: list[dict[str, Any]] = []
        chain_list = chain[:limit] if chain else []
        for i in range(1, len(chain_list)):
            prev = chain_list[i - 1]
            curr = chain_list[i]
            if curr.get("prev_hash") and prev.get("receipt_hash"):
                if curr["prev_hash"] != prev["receipt_hash"]:
                    gaps.append({
                        "index": i,
                        "expected": prev["receipt_hash"],
                        "actual": curr.get("prev_hash"),
                    })

        audit = {
            "suite_id": suite_id,
            "total_receipts": total_count,
            "chain_length": len(chain_list),
            "gaps_found": len(gaps),
            "gaps": gaps,
            "integrity": "INTACT" if len(gaps) == 0 else "BROKEN",
        }

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.receipt_audit",
            status="ok",
            inputs={"suite_id": suite_id, "limit": limit},
            metadata={"total": total_count, "gaps": len(gaps)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"audit": audit, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 12. Web Search via Brave (GREEN — read-only)
    # =========================================================================

    async def search_web(
        self,
        ctx: AgentContext,
        *,
        query: str,
        count: int = 5,
    ) -> AgentResult:
        """Search the web using Brave Search API."""
        if not query or not query.strip():
            return AgentResult(success=False, error="Missing required parameter: query")

        try:
            from aspire_orchestrator.providers.brave_client import execute_brave_search
            tool_result = await execute_brave_search(
                payload={"query": query.strip(), "count": count},
                correlation_id=ctx.correlation_id,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
            )
            result_data = tool_result.data if hasattr(tool_result, "data") else {}
        except Exception as e:
            return AgentResult(success=False, error=f"Brave search failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.web_search",
            status="ok",
            inputs={"query_length": len(query), "count": count},
            metadata={"results_count": len(result_data.get("results", []))},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"search_results": result_data, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 13. Council History (GREEN — read-only)
    # =========================================================================

    async def get_council_history(
        self,
        ctx: AgentContext,
        *,
        status: str | None = None,
        limit: int = 10,
    ) -> AgentResult:
        """Get Meeting of Minds council session history."""
        try:
            from aspire_orchestrator.services.council_service import list_sessions
            sessions = list_sessions(status=status)
        except Exception as e:
            return AgentResult(success=False, error=f"Council history failed: {e}")

        # Convert dataclasses to dicts and apply limit
        session_dicts = []
        for s in sessions[:limit]:
            if hasattr(s, "__dict__"):
                d = {k: v for k, v in s.__dict__.items() if not k.startswith("_")}
                # Convert datetime fields to ISO strings
                for key, val in d.items():
                    if isinstance(val, datetime):
                        d[key] = val.isoformat()
                session_dicts.append(d)
            else:
                session_dicts.append(s)

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.council_history",
            status="ok",
            inputs={"status_filter": status, "limit": limit},
            metadata={"session_count": len(session_dicts)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "sessions": session_dicts,
                "count": len(session_dicts),
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )

    # =========================================================================
    # 14. Metrics Snapshot (GREEN — read-only Prometheus counters)
    # =========================================================================

    async def get_metrics_snapshot(self, ctx: AgentContext) -> AgentResult:
        """Get current Prometheus metrics snapshot."""
        try:
            from aspire_orchestrator.services.metrics import (
                REQUEST_COUNTER,
                TOOL_EXECUTION_COUNTER,
                RECEIPT_WRITE_COUNTER,
                TOKEN_MINT_COUNTER,
                A2A_TASK_COUNTER,
                LLM_REQUEST_COUNTER,
            )

            def _counter_value(counter: Any) -> float:
                """Extract total value from a Prometheus counter."""
                try:
                    # Sum all label combinations
                    total = 0.0
                    for metric in counter.collect():
                        for sample in metric.samples:
                            if sample.name.endswith("_total"):
                                total += sample.value
                    return total
                except Exception:
                    return 0.0

            snapshot = {
                "requests_total": _counter_value(REQUEST_COUNTER),
                "tool_executions_total": _counter_value(TOOL_EXECUTION_COUNTER),
                "receipt_writes_total": _counter_value(RECEIPT_WRITE_COUNTER),
                "token_mints_total": _counter_value(TOKEN_MINT_COUNTER),
                "a2a_tasks_total": _counter_value(A2A_TASK_COUNTER),
                "llm_requests_total": _counter_value(LLM_REQUEST_COUNTER),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            return AgentResult(success=False, error=f"Metrics snapshot failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.metrics_snapshot",
            status="ok",
            inputs={"requested_at": datetime.now(timezone.utc).isoformat()},
            metadata={"counters_collected": len(snapshot) - 1},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"metrics": snapshot, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )


# =============================================================================
# Module-level singleton
# =============================================================================

_instance: AvaAdminDesk | None = None


def get_ava_admin_desk() -> AvaAdminDesk:
    """Get the singleton AvaAdminDesk instance."""
    global _instance
    if _instance is None:
        _instance = AvaAdminDesk()
    return _instance
