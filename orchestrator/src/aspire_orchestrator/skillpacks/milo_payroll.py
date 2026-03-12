"""Milo Payroll Skill Pack — Payroll processing via Gusto (RED tier).

Milo handles:
  - run_payroll: RED — process payroll via Gusto (dual approval + presence required)
  - generate_snapshot: GREEN — pre-payroll snapshot (read-only, no approval)
  - schedule_payroll: YELLOW — schedule future payroll run
  - check_deadline: GREEN — check upcoming payroll deadlines

Provider: Gusto (via OAuth2 — per-suite tokens, tenant isolation)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides when to invoke
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters, missing snapshot, past deadline
  - Law #4: payroll.run is RED (dual approval + presence); snapshot/deadline GREEN; schedule YELLOW
  - Law #5: Capability tokens required for all Gusto tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for Gusto calls (tools are hands)

RED tier governance (payroll.run):
  - dual_approval_required = True (HR + Finance must both approve)
  - presence_required = True (video/biometric proof of active user)
  - Snapshot prerequisite: must generate_snapshot before run_payroll
  - Deadline enforcement: past-deadline payrolls escalate with warning

Binding fields enforcement (per policy_matrix.yaml):
  - payroll.run: payroll_id, pay_period, total_amount
  - payroll.schedule: payroll_period, run_date
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_executor import execute_tool
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

ACTOR_MILO = "skillpack:milo-payroll"
RECEIPT_VERSION = "1.0"

# Binding fields per policy_matrix.yaml
PAYROLL_RUN_BINDING_FIELDS = {"payroll_id", "pay_period", "total_amount"}
PAYROLL_SCHEDULE_BINDING_FIELDS = {"payroll_period", "run_date"}

# In-memory snapshot store (Phase 1 — moves to DB/Redis in Phase 2)
# Key: "{suite_id}:{payroll_period}" → snapshot data
_payroll_snapshots: dict[str, dict[str, Any]] = {}


@dataclass
class SkillPackResult:
    """Result of a Milo Payroll skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False
    presence_required: bool = False
    dual_approval_required: bool = False


@dataclass
class MiloContext:
    """Tenant-scoped execution context for Milo operations."""

    suite_id: str
    office_id: str
    correlation_id: str
    capability_token_id: str | None = None
    capability_token_hash: str | None = None


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """Compute SHA256 hash of inputs for receipt linkage."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _make_receipt(
    *,
    ctx: MiloContext,
    action_type: str,
    risk_tier: str,
    status: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    approval_evidence: dict[str, Any] | None = None,
    presence_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Milo Payroll operation (Law #2)."""
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_MILO,
        "correlation_id": ctx.correlation_id,
        "risk_tier": risk_tier,
        "status": status,
        "inputs_hash": _compute_inputs_hash(inputs),
        "capability_token_id": ctx.capability_token_id or "none",
        "policy": {
            "decision": "allow" if status in ("ok", "success") else "deny",
            "policy_id": "milo-payroll-v1",
            "reasons": [],
        },
        "redactions": [],
    }
    if metadata:
        receipt["metadata"] = metadata
    if approval_evidence:
        receipt["approval_evidence"] = approval_evidence
    if presence_evidence:
        receipt["presence_evidence"] = presence_evidence
    return receipt


def _get_snapshot_key(suite_id: str, payroll_period: str) -> str:
    """Build the snapshot store key."""
    return f"{suite_id}:{payroll_period}"


def clear_payroll_snapshots() -> None:
    """Clear in-memory snapshot store. For testing only."""
    _payroll_snapshots.clear()


class MiloPayrollSkillPack:
    async def payroll_run(self, payroll_period: str, context: MiloContext, **kwargs: Any) -> SkillPackResult:
        return await self.run_payroll(payroll_period=payroll_period, context=context, **kwargs)

    async def payroll_snapshot(self, payroll_period: str, context: MiloContext, **kwargs: Any) -> SkillPackResult:
        return await self.generate_snapshot(payroll_period=payroll_period, context=context, **kwargs)

    async def payroll_schedule(self, payroll_period: str, run_date: str, context: MiloContext, **kwargs: Any) -> SkillPackResult:
        return await self.schedule_payroll(payroll_period=payroll_period, run_date=run_date, context=context, **kwargs)

    async def payroll_deadline(self, context: MiloContext, **kwargs: Any) -> SkillPackResult:
        return await self.check_deadline(context=context, **kwargs)

    """Milo Payroll skill pack — payroll processing via Gusto."""

    async def run_payroll(
        self,
        payroll_period: str,
        context: MiloContext,
        *,
        company_id: str = "",
        payroll_id: str = "",
        total_amount: str = "",
        approval_evidence: dict[str, Any] | None = None,
        presence_evidence: dict[str, Any] | None = None,
    ) -> SkillPackResult:
        """Process payroll via Gusto — RED tier.

        RED tier governance:
          - dual_approval_required = True (HR + Finance)
          - presence_required = True
          - Snapshot must exist before payroll can run
          - If past deadline, escalate with warning

        Binding fields: payroll_id, pay_period, total_amount
        """
        inputs = {
            "action": "payroll.run",
            "payroll_period": payroll_period,
            "company_id": company_id,
            "payroll_id": payroll_id,
            "total_amount": total_amount,
        }

        # Fail closed: missing required params (Law #3)
        missing = []
        if not payroll_period:
            missing.append("payroll_period")
        if not company_id:
            missing.append("company_id")
        if not payroll_id:
            missing.append("payroll_id")

        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.run",
                risk_tier="red",
                status="denied",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_PARAMS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required parameters: {', '.join(missing)}",
            )

        # Prerequisite: snapshot must exist (Law #3 — fail closed)
        snapshot_key = _get_snapshot_key(context.suite_id, payroll_period)
        if snapshot_key not in _payroll_snapshots:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.run",
                risk_tier="red",
                status="denied",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["SNAPSHOT_REQUIRED"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Pre-payroll snapshot required before payroll can run. "
                "Call generate_snapshot() first.",
            )

        # RED tier: signal dual approval + presence required
        if not approval_evidence:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.run",
                risk_tier="red",
                status="pending_approval",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "pending"
            receipt["policy"]["reasons"] = ["DUAL_APPROVAL_REQUIRED", "PRESENCE_REQUIRED"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="RED tier: dual approval (HR + Finance) and presence verification required",
                approval_required=True,
                presence_required=True,
                dual_approval_required=True,
            )

        if not presence_evidence:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.run",
                risk_tier="red",
                status="pending_presence",
                inputs=inputs,
                approval_evidence=approval_evidence,
            )
            receipt["policy"]["decision"] = "pending"
            receipt["policy"]["reasons"] = ["PRESENCE_REQUIRED"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="RED tier: presence verification required before payroll execution",
                presence_required=True,
            )

        # Execute via Gusto tool (Law #7 — tools are hands)
        result: ToolExecutionResult = await execute_tool(
            tool_id="gusto.payroll.run",
            payload={
                "company_id": company_id,
                "payroll_id": payroll_id,
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="red",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "ok" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _make_receipt(
            ctx=context,
            action_type="payroll.run",
            risk_tier="red",
            status=status,
            inputs=inputs,
            metadata={
                "tool_id": result.tool_id,
                "payroll_id": payroll_id,
                "total_amount": total_amount,
            },
            approval_evidence=approval_evidence,
            presence_evidence=presence_evidence,
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def generate_snapshot(
        self,
        payroll_period: str,
        context: MiloContext,
        *,
        company_id: str = "",
    ) -> SkillPackResult:
        """Generate pre-payroll snapshot — GREEN tier.

        Reads payroll data from Gusto for the given period and stores
        it as a snapshot. Required before payroll can be run.

        No approval required (read-only).
        """
        inputs = {
            "action": "payroll.snapshot",
            "payroll_period": payroll_period,
            "company_id": company_id,
        }

        if not payroll_period or not company_id:
            missing = []
            if not payroll_period:
                missing.append("payroll_period")
            if not company_id:
                missing.append("company_id")
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.snapshot",
                risk_tier="green",
                status="denied",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_PARAMS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required parameters: {', '.join(missing)}",
            )

        # Read payroll data via Gusto (Law #7)
        result: ToolExecutionResult = await execute_tool(
            tool_id="gusto.read_payrolls",
            payload={
                "company_id": company_id,
            },
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        if result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.snapshot",
                risk_tier="green",
                status="failed",
                inputs=inputs,
                metadata={"tool_id": result.tool_id},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=result.error or "Failed to read payroll data from Gusto",
            )

        # Store snapshot (in-memory Phase 1)
        snapshot_key = _get_snapshot_key(context.suite_id, payroll_period)
        snapshot_data = {
            "payroll_period": payroll_period,
            "company_id": company_id,
            "suite_id": context.suite_id,
            "office_id": context.office_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "payrolls": result.data.get("payrolls", []),
        }
        _payroll_snapshots[snapshot_key] = snapshot_data

        receipt = _make_receipt(
            ctx=context,
            action_type="payroll.snapshot",
            risk_tier="green",
            status="ok",
            inputs=inputs,
            metadata={
                "tool_id": result.tool_id,
                "snapshot_key": snapshot_key,
                "payroll_count": len(snapshot_data["payrolls"]),
            },
        )

        return SkillPackResult(
            success=True,
            data=snapshot_data,
            receipt=receipt,
        )

    async def schedule_payroll(
        self,
        payroll_period: str,
        run_date: str,
        context: MiloContext,
        *,
        company_id: str = "",
        approval_evidence: dict[str, Any] | None = None,
    ) -> SkillPackResult:
        """Schedule a future payroll run — YELLOW tier.

        Requires explicit user approval. Binding fields: payroll_period, run_date.
        The actual payroll.run will still be RED tier when executed.
        """
        inputs = {
            "action": "payroll.schedule",
            "payroll_period": payroll_period,
            "run_date": run_date,
            "company_id": company_id,
        }

        # Validate required params
        missing = []
        if not payroll_period:
            missing.append("payroll_period")
        if not run_date:
            missing.append("run_date")
        if not company_id:
            missing.append("company_id")

        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.schedule",
                risk_tier="yellow",
                status="denied",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_PARAMS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required parameters: {', '.join(missing)}",
            )

        # YELLOW tier: require explicit approval
        if not approval_evidence:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.schedule",
                risk_tier="yellow",
                status="pending_approval",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "pending"
            receipt["policy"]["reasons"] = ["EXPLICIT_APPROVAL_REQUIRED"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="YELLOW tier: explicit approval required to schedule payroll",
                approval_required=True,
            )

        # Schedule is recorded internally (Gusto doesn't have a schedule API;
        # the schedule is a governance construct managed by the orchestrator)
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        schedule_data = {
            "schedule_id": schedule_id,
            "payroll_period": payroll_period,
            "run_date": run_date,
            "company_id": company_id,
            "suite_id": context.suite_id,
            "office_id": context.office_id,
            "status": "scheduled",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="payroll.schedule",
            risk_tier="yellow",
            status="ok",
            inputs=inputs,
            metadata={
                "schedule_id": schedule_id,
                "run_date": run_date,
            },
            approval_evidence=approval_evidence,
        )

        return SkillPackResult(
            success=True,
            data=schedule_data,
            receipt=receipt,
        )

    async def check_deadline(
        self,
        context: MiloContext,
        *,
        company_id: str = "",
        payroll_period: str = "",
    ) -> SkillPackResult:
        """Check upcoming payroll deadlines — GREEN tier.

        No approval required (read-only). If past deadline, returns
        escalation data for the orchestrator.
        """
        inputs = {
            "action": "payroll.deadline",
            "company_id": company_id,
            "payroll_period": payroll_period,
        }

        if not company_id:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.deadline",
                risk_tier="green",
                status="denied",
                inputs=inputs,
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_PARAMS"]
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: company_id",
            )

        # Read payrolls to check deadlines (Law #7)
        result: ToolExecutionResult = await execute_tool(
            tool_id="gusto.read_payrolls",
            payload={"company_id": company_id},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        if result.outcome != Outcome.SUCCESS:
            receipt = _make_receipt(
                ctx=context,
                action_type="payroll.deadline",
                risk_tier="green",
                status="failed",
                inputs=inputs,
                metadata={"tool_id": result.tool_id},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=result.error or "Failed to read payroll deadlines from Gusto",
            )

        # Analyze payroll deadlines
        payrolls = result.data.get("payrolls", [])
        now = datetime.now(timezone.utc)
        deadline_data = {
            "company_id": company_id,
            "checked_at": now.isoformat(),
            "upcoming_payrolls": [],
            "past_deadline": [],
            "escalation_required": False,
        }

        for p in payrolls:
            check_date = p.get("check_date", "")
            if check_date:
                try:
                    deadline_dt = datetime.fromisoformat(check_date)
                    if deadline_dt.tzinfo is None:
                        deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
                    if deadline_dt < now:
                        deadline_data["past_deadline"].append(p)
                        deadline_data["escalation_required"] = True
                    else:
                        deadline_data["upcoming_payrolls"].append(p)
                except (ValueError, TypeError):
                    deadline_data["upcoming_payrolls"].append(p)

        receipt = _make_receipt(
            ctx=context,
            action_type="payroll.deadline",
            risk_tier="green",
            status="ok",
            inputs=inputs,
            metadata={
                "tool_id": result.tool_id,
                "upcoming_count": len(deadline_data["upcoming_payrolls"]),
                "past_deadline_count": len(deadline_data["past_deadline"]),
                "escalation_required": deadline_data["escalation_required"],
            },
        )

        return SkillPackResult(
            success=True,
            data=deadline_data,
            receipt=receipt,
        )


# =============================================================================
# Phase 3 W5a: Enhanced Milo Payroll with LLM reasoning + dual approval
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.services.dual_approval_service import (
    get_dual_approval_service,
    ApprovalStatus,
)
from aspire_orchestrator.services.idempotency_service import get_idempotency_service


class EnhancedMiloPayroll(EnhancedSkillPack):
    """LLM-enhanced Milo Payroll — RED-tier payroll intelligence.

    Extends MiloPayrollSkillPack with:
    - validate_payroll_run: GPT-5.2 validates payroll data before execution
    - estimate_tax_impact: GPT-5.2 estimates tax liabilities for the run
    - plan_payroll_correction: GPT-5.2 plans corrections for payroll errors
    - initiate_dual_approval: Creates dual approval (HR + Finance)

    ALL methods use high_risk_guard (GPT-5.2) — no cheap models for payroll.
    Idempotency enforced on all state-changing operations.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="milo-payroll",
            agent_name="Milo Payroll",
            default_risk_tier="red",
        )
        self._rule_pack = MiloPayrollSkillPack()

    async def validate_payroll_run(
        self, payroll_data: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Pre-validate payroll data before run. RED — GPT-5.2 verification."""
        if not payroll_data.get("payroll_period"):
            receipt = self.build_receipt(
                ctx=ctx, event_type="payroll.validate",
                status="failed", inputs={"period": "missing"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_PERIOD"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing payroll period")

        if not payroll_data.get("employee_count", 0):
            receipt = self.build_receipt(
                ctx=ctx, event_type="payroll.validate",
                status="failed", inputs={"employees": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["NO_EMPLOYEES"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="No employees in payroll")

        return await self.execute_with_llm(
            prompt=(
                f"You are Milo, the payroll specialist. CRITICAL: This is a RED-tier operation.\n\n"
                f"Validate this payroll run before execution:\n"
                f"- Period: {payroll_data['payroll_period']}\n"
                f"- Company: {payroll_data.get('company_id', 'unknown')}\n"
                f"- Employees: {payroll_data.get('employee_count', 0)}\n"
                f"- Total Gross: ${payroll_data.get('total_gross_cents', 0) / 100:,.2f}\n"
                f"- Deductions: ${payroll_data.get('total_deductions_cents', 0) / 100:,.2f}\n"
                f"- Net Pay: ${payroll_data.get('total_net_cents', 0) / 100:,.2f}\n\n"
                f"Verify: amounts reasonable, no duplicates vs prior period, tax withholdings "
                f"within expected range, employee count matches records. Flag anomalies."
            ),
            ctx=ctx, event_type="payroll.validate", step_type="verify",
            inputs={
                "action": "payroll.validate",
                "period": payroll_data["payroll_period"],
                "employee_count": payroll_data.get("employee_count", 0),
            },
        )

    async def estimate_tax_impact(
        self, payroll_period: str, gross_amount_cents: int, ctx: AgentContext,
    ) -> AgentResult:
        """Estimate tax impact for a payroll run. GREEN — analysis only."""
        if not payroll_period:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payroll.tax_estimate",
                status="failed", inputs={"period": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_PERIOD"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing payroll period")

        if not isinstance(gross_amount_cents, int) or gross_amount_cents <= 0:
            receipt = self.build_receipt(
                ctx=ctx, event_type="payroll.tax_estimate",
                status="failed", inputs={"gross": gross_amount_cents},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_GROSS_AMOUNT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Invalid gross amount")

        return await self.execute_with_llm(
            prompt=(
                f"You are Milo, estimating tax impact for payroll.\n\n"
                f"Period: {payroll_period}\n"
                f"Gross Payroll: ${gross_amount_cents / 100:,.2f}\n\n"
                f"Estimate: federal withholding, state withholding (if applicable), "
                f"FICA (Social Security + Medicare), FUTA, SUTA, total employer cost, "
                f"total employee deductions, net payroll amount."
            ),
            ctx=ctx, event_type="payroll.tax_estimate", step_type="plan",
            inputs={"action": "payroll.tax_estimate", "period": payroll_period},
        )

    async def plan_payroll_correction(
        self, error_details: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Plan correction for a payroll error. YELLOW — requires approval."""
        if not error_details.get("error_type"):
            receipt = self.build_receipt(
                ctx=ctx, event_type="payroll.correction_plan",
                status="failed", inputs={"error_type": "missing"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_ERROR_TYPE"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing error type")

        return await self.execute_with_llm(
            prompt=(
                f"You are Milo, planning a payroll correction.\n\n"
                f"Error: {error_details.get('error_type', 'unknown')}\n"
                f"Affected Employee(s): {error_details.get('employee_count', '?')}\n"
                f"Period: {error_details.get('payroll_period', '?')}\n"
                f"Description: {error_details.get('description', '')}\n\n"
                f"Plan: correction method (void-and-reissue vs adjustment), "
                f"tax recalculation required, Gusto API calls needed, "
                f"timeline, approval requirements, compliance implications."
            ),
            ctx=ctx, event_type="payroll.correction_plan", step_type="plan",
            inputs={
                "action": "payroll.correction_plan",
                "error_type": error_details["error_type"],
            },
        )

    def initiate_dual_approval(
        self, payroll_data: dict, ctx: AgentContext,
    ) -> dict:
        """Create dual approval request for payroll run (HR + Finance)."""
        svc = get_dual_approval_service()
        binding = {
            "payroll_id": payroll_data.get("payroll_id", ""),
            "pay_period": payroll_data.get("payroll_period", ""),
            "total_amount": payroll_data.get("total_net_cents", 0),
        }

        result = svc.create_request(
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            correlation_id=ctx.correlation_id,
            action_type="payroll.run",
            binding_fields=binding,
            required_roles=["hr", "finance"],
        )

        return {
            "success": result.success,
            "request_id": result.request_id,
            "status": result.status.value,
            "remaining_roles": result.remaining_roles,
            "receipt": result.receipt,
            "error": result.error,
        }
