"""Finn Finance Manager Skill Pack — Strategic financial intelligence.

Finn Finance Manager handles high-level financial operations:
  - Finance snapshot read (GREEN — read-only aggregate view)
  - Finance exceptions read (GREEN — read-only exception alerts)
  - Finance packet draft (YELLOW — drafts proposal document)
  - Finance proposal create (YELLOW — creates change proposal for approval)
  - A2A delegation (YELLOW — dispatches analysis tasks to other agents)

Providers: internal (no external APIs — uses orchestrator state)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt via finn_receipt_service
  - Law #3: Fail closed on missing parameters
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all actions (tools are hands)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.finn_delegation import (
    FinnDelegationService,
    DelegationRequest,
)
from aspire_orchestrator.services.tax_rules_engine import load_rules

logger = logging.getLogger(__name__)

ACTOR_FINN_FM = "skillpack:finn-finance-manager"
RECEIPT_VERSION = "1.0"


@dataclass
class SkillPackResult:
    """Result of a Finn Finance Manager skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False


@dataclass
class FinnFMContext:
    """Tenant-scoped execution context for Finn Finance Manager operations."""

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
    ctx: FinnFMContext,
    action_type: str,
    outcome: str,
    inputs: dict[str, Any],
    reason_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Finn Finance Manager operation (Law #2)."""
    return {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_FINN_FM,
        "correlation_id": ctx.correlation_id,
        "inputs_hash": _compute_inputs_hash(inputs),
        "outcome": outcome,
        "reason_code": reason_code,
        "capability_token_id": ctx.capability_token_id,
        "metadata": metadata or {},
    }


# =============================================================================
# Skill Pack Methods
# =============================================================================


def read_finance_snapshot(
    ctx: FinnFMContext,
    *,
    period: str = "current_month",
    include_tax: bool = False,
) -> SkillPackResult:
    """Read aggregated financial snapshot (GREEN — no approval needed).

    Returns revenue, expenses, net income, cash position, and optionally
    tax liability estimates for the requested period.
    """
    inputs = {"period": period, "include_tax": include_tax}

    # Law #3: fail closed on missing context
    if not ctx.suite_id or not ctx.office_id:
        receipt = _make_receipt(
            ctx=ctx, action_type="finance.snapshot.read",
            outcome="denied", inputs=inputs, reason_code="missing_tenant_context",
        )
        return SkillPackResult(success=False, receipt=receipt, error="Missing suite_id or office_id")

    # Build snapshot data (stub — real implementation queries Supabase)
    snapshot = {
        "period": period,
        "revenue_cents": 0,
        "expenses_cents": 0,
        "net_income_cents": 0,
        "cash_position_cents": 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stub": True,
        "data_source": "stub",
        "message": "No financial data yet. Connect your providers in the Connections page to see real numbers.",
    }

    if include_tax:
        try:
            rules = load_rules("US", 2026)
            snapshot["tax_rules_loaded"] = len(rules)
        except Exception:
            snapshot["tax_rules_loaded"] = 0

    receipt = _make_receipt(
        ctx=ctx, action_type="finance.snapshot.read",
        outcome="success", inputs=inputs,
    )
    return SkillPackResult(success=True, data=snapshot, receipt=receipt)


def read_finance_exceptions(
    ctx: FinnFMContext,
    *,
    severity: str = "all",
) -> SkillPackResult:
    """Read active financial exceptions/alerts (GREEN — no approval needed).

    Returns overdue invoices, low cash warnings, failed payments, etc.
    """
    inputs = {"severity": severity}

    if not ctx.suite_id or not ctx.office_id:
        receipt = _make_receipt(
            ctx=ctx, action_type="finance.exceptions.read",
            outcome="denied", inputs=inputs, reason_code="missing_tenant_context",
        )
        return SkillPackResult(success=False, receipt=receipt, error="Missing suite_id or office_id")

    # Stub — real implementation queries Supabase for active exceptions
    exceptions: list[dict[str, Any]] = []
    stub_message = "No exceptions detected. Connect your providers in the Connections page to enable real-time monitoring."

    receipt = _make_receipt(
        ctx=ctx, action_type="finance.exceptions.read",
        outcome="success", inputs=inputs,
        metadata={"exception_count": len(exceptions)},
    )
    return SkillPackResult(
        success=True,
        data={"exceptions": exceptions, "stub": True, "data_source": "stub", "message": stub_message},
        receipt=receipt,
    )


def draft_finance_packet(
    ctx: FinnFMContext,
    *,
    packet_type: str,
    title: str,
    description: str,
    evidence_ids: list[str] | None = None,
) -> SkillPackResult:
    """Draft a finance proposal packet (YELLOW — requires approval to finalize).

    Creates a draft proposal document that must be approved before
    it becomes a formal change proposal.
    """
    inputs = {
        "packet_type": packet_type,
        "title": title,
        "description": description,
        "evidence_ids": evidence_ids or [],
    }

    if not ctx.suite_id or not ctx.office_id:
        receipt = _make_receipt(
            ctx=ctx, action_type="finance.packet.draft",
            outcome="denied", inputs=inputs, reason_code="missing_tenant_context",
        )
        return SkillPackResult(success=False, receipt=receipt, error="Missing suite_id or office_id")

    if not title or not packet_type:
        receipt = _make_receipt(
            ctx=ctx, action_type="finance.packet.draft",
            outcome="denied", inputs=inputs, reason_code="missing_required_fields",
        )
        return SkillPackResult(success=False, receipt=receipt, error="title and packet_type are required")

    packet = {
        "packet_id": str(uuid.uuid4()),
        "packet_type": packet_type,
        "title": title,
        "description": description,
        "evidence_ids": evidence_ids or [],
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    receipt = _make_receipt(
        ctx=ctx, action_type="finance.packet.draft",
        outcome="success", inputs=inputs,
        metadata={"packet_id": packet["packet_id"]},
    )
    return SkillPackResult(
        success=True, data=packet, receipt=receipt,
        approval_required=True,  # YELLOW — needs user confirmation to finalize
    )


def create_finance_proposal(
    ctx: FinnFMContext,
    *,
    packet_id: str,
    proposal_type: str,
    amount_cents: int | None = None,
) -> SkillPackResult:
    """Create a formal change proposal (YELLOW — requires user approval).

    Converts an approved draft packet into a formal proposal that
    enters the approval workflow.
    """
    inputs = {
        "packet_id": packet_id,
        "proposal_type": proposal_type,
        "amount_cents": amount_cents,
    }

    if not ctx.suite_id or not ctx.office_id:
        receipt = _make_receipt(
            ctx=ctx, action_type="finance.proposal.create",
            outcome="denied", inputs=inputs, reason_code="missing_tenant_context",
        )
        return SkillPackResult(success=False, receipt=receipt, error="Missing suite_id or office_id")

    if not packet_id or not proposal_type:
        receipt = _make_receipt(
            ctx=ctx, action_type="finance.proposal.create",
            outcome="denied", inputs=inputs, reason_code="missing_required_fields",
        )
        return SkillPackResult(success=False, receipt=receipt, error="packet_id and proposal_type are required")

    proposal = {
        "proposal_id": str(uuid.uuid4()),
        "packet_id": packet_id,
        "proposal_type": proposal_type,
        "amount_cents": amount_cents,
        "status": "pending_approval",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    receipt = _make_receipt(
        ctx=ctx, action_type="finance.proposal.create",
        outcome="success", inputs=inputs,
        metadata={"proposal_id": proposal["proposal_id"]},
    )
    return SkillPackResult(
        success=True, data=proposal, receipt=receipt,
        approval_required=True,
    )


def dispatch_a2a_delegation(
    ctx: FinnFMContext,
    *,
    to_agent: str,
    request_type: str,
    payload: dict[str, Any],
    delegation_depth: int = 0,
) -> SkillPackResult:
    """Dispatch A2A delegation request (YELLOW — requires orchestrator routing).

    Finn delegates specialized analysis to allowlisted agents
    (adam, teressa, milo, eli) via the A2A service.
    """
    inputs = {
        "to_agent": to_agent,
        "request_type": request_type,
        "delegation_depth": delegation_depth,
    }

    if not ctx.suite_id or not ctx.office_id:
        receipt = _make_receipt(
            ctx=ctx, action_type="a2a.create",
            outcome="denied", inputs=inputs, reason_code="missing_tenant_context",
        )
        return SkillPackResult(success=False, receipt=receipt, error="Missing suite_id or office_id")

    # Validate delegation through finn_delegation service
    delegation_req = DelegationRequest(
        suite_id=ctx.suite_id,
        office_id=ctx.office_id,
        correlation_id=ctx.correlation_id,
        to_agent=to_agent,
        request_type=request_type,
        payload=payload,
        risk_tier="yellow",
        delegation_depth=delegation_depth,
    )
    svc = FinnDelegationService()
    validation_result = svc.validate_delegation(delegation_req)

    if not validation_result.allowed:
        receipt = _make_receipt(
            ctx=ctx, action_type="a2a.create",
            outcome="denied", inputs=inputs,
            reason_code=validation_result.deny_reason or "delegation_validation_failed",
        )
        return SkillPackResult(
            success=False, receipt=receipt,
            error=validation_result.deny_reason or "Delegation validation failed",
        )

    delegation_data = {
        "delegation_id": str(uuid.uuid4()),
        "from_agent": "finn",
        "to_agent": to_agent,
        "request_type": request_type,
        "delegation_depth": delegation_depth,
        "status": "dispatched",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    receipt = _make_receipt(
        ctx=ctx, action_type="a2a.create",
        outcome="success", inputs=inputs,
        metadata={"delegation_id": delegation_data["delegation_id"], "to_agent": to_agent},
    )
    return SkillPackResult(success=True, data=delegation_data, receipt=receipt)


# =============================================================================
# Phase 3 W4: Enhanced Finn Finance Manager with LLM reasoning
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedFinnFinanceManager(EnhancedSkillPack):
    """LLM-enhanced Finn Finance Manager — strategic financial intelligence.

    Extends rule-based FinnFM with:
    - analyze_financial_health: LLM synthesizes snapshot + exceptions into insights
    - plan_budget_adjustment: LLM proposes budget changes based on trends
    - generate_finance_report: LLM creates executive-ready financial summary
    - recommend_delegation: LLM determines which agent should handle a finance task

    YELLOW tier for proposals/delegations, GREEN for analysis.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="finn-finance-manager",
            agent_name="Finn Finance Manager",
            default_risk_tier="yellow",
        )
        self._rule_pack_funcs = {
            "snapshot": read_finance_snapshot,
            "exceptions": read_finance_exceptions,
            "draft": draft_finance_packet,
            "proposal": create_finance_proposal,
            "delegation": dispatch_a2a_delegation,
        }

    async def analyze_financial_health(
        self, snapshot_data: dict, exceptions: list, ctx: AgentContext,
    ) -> AgentResult:
        """Synthesize financial snapshot + exceptions into actionable insights. GREEN."""
        if not snapshot_data:
            receipt = self.build_receipt(
                ctx=ctx, event_type="finance.health.analyze",
                status="failed", inputs={"snapshot": "empty"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EMPTY_SNAPSHOT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty snapshot data")

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, the finance manager.\n\n"
                f"Financial Snapshot:\n"
                f"- Revenue: ${snapshot_data.get('revenue_cents', 0) / 100:,.2f}\n"
                f"- Expenses: ${snapshot_data.get('expenses_cents', 0) / 100:,.2f}\n"
                f"- Net Income: ${snapshot_data.get('net_income_cents', 0) / 100:,.2f}\n"
                f"- Cash Position: ${snapshot_data.get('cash_position_cents', 0) / 100:,.2f}\n\n"
                f"Active Exceptions ({len(exceptions)}):\n"
                f"{json.dumps(exceptions[:10], default=str)}\n\n"
                f"Provide: health score (1-10), top 3 risks, top 3 opportunities, "
                f"recommended actions ranked by urgency."
            ),
            ctx=ctx, event_type="finance.health.analyze", step_type="verify",
            inputs={"action": "finance.health.analyze", "exception_count": len(exceptions)},
        )

    async def plan_budget_adjustment(
        self, current_budget: dict, reason: str, ctx: AgentContext,
    ) -> AgentResult:
        """Plan budget adjustments based on financial trends. YELLOW — proposal only."""
        if not reason:
            receipt = self.build_receipt(
                ctx=ctx, event_type="finance.budget.plan",
                status="failed", inputs={"reason": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_REASON"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing adjustment reason")

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, planning a budget adjustment.\n\n"
                f"Current Budget: {json.dumps(current_budget, default=str)}\n"
                f"Reason for Adjustment: {reason}\n\n"
                f"Propose: line items to adjust, amounts, justification for each, "
                f"impact on cash flow, risk assessment. This is YELLOW tier — "
                f"user must approve before any changes take effect."
            ),
            ctx=ctx, event_type="finance.budget.plan", step_type="plan",
            inputs={"action": "finance.budget.plan", "reason": reason[:100]},
        )

    async def generate_finance_report(
        self, period: str, report_type: str, ctx: AgentContext,
    ) -> AgentResult:
        """Generate executive-ready financial summary. GREEN — read-only analysis."""
        valid_types = ("monthly", "quarterly", "annual", "custom")
        if report_type not in valid_types:
            receipt = self.build_receipt(
                ctx=ctx, event_type="finance.report.generate",
                status="failed", inputs={"period": period, "type": report_type},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_REPORT_TYPE"]}
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False, receipt=receipt,
                error=f"Invalid report type. Must be one of: {valid_types}",
            )

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, generating a {report_type} financial report.\n\n"
                f"Period: {period}\n"
                f"Report Type: {report_type}\n\n"
                f"Generate: executive summary, key metrics table, trend analysis, "
                f"comparison to previous period, outlook and recommendations."
            ),
            ctx=ctx, event_type="finance.report.generate", step_type="draft",
            inputs={"action": "finance.report.generate", "period": period, "type": report_type},
        )

    async def recommend_delegation(
        self, task_description: str, ctx: AgentContext,
    ) -> AgentResult:
        """Determine which agent should handle a finance-related task. GREEN — analysis only."""
        if not task_description:
            receipt = self.build_receipt(
                ctx=ctx, event_type="finance.delegation.recommend",
                status="failed", inputs={"task": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EMPTY_TASK"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty task description")

        return await self.execute_with_llm(
            prompt=(
                f"You are Finn, the finance manager deciding task delegation.\n\n"
                f"Task: {task_description[:500]}\n\n"
                f"Available agents for delegation:\n"
                f"- adam: Research (vendor search, market analysis)\n"
                f"- teressa: Books (QBO sync, transaction categorization)\n"
                f"- milo: Payroll (Gusto, tax calculations)\n"
                f"- eli: Inbox (email communications, follow-ups)\n"
                f"- quinn: Invoicing (Stripe, billing)\n\n"
                f"Recommend: target agent, reasoning, task parameters, "
                f"risk assessment, and whether A2A delegation is appropriate."
            ),
            ctx=ctx, event_type="finance.delegation.recommend", step_type="classify",
            inputs={"action": "finance.delegation.recommend", "task_length": len(task_description)},
        )

    async def search_financial_knowledge(
        self, query: str, ctx: AgentContext,
    ) -> AgentResult:
        """Search finance knowledge base for relevant information. GREEN — read-only."""
        if not query:
            receipt = self.build_receipt(
                ctx=ctx, event_type="finance.knowledge.search",
                status="failed", inputs={"query": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EMPTY_QUERY"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty query")

        from aspire_orchestrator.services.financial_retrieval_service import get_financial_retrieval_service

        svc = get_financial_retrieval_service()
        result = await svc.retrieve(query, suite_id=ctx.suite_id)

        if result.chunks:
            context = svc.assemble_rag_context(result)
            return await self.execute_with_llm(
                prompt=(
                    f"You are Finn, the finance manager. The user asked: {query}\n\n"
                    f"Relevant knowledge from your knowledge base:\n{context}\n\n"
                    "Synthesize the knowledge into a clear, actionable answer. "
                    "Reference specific rules, thresholds, or deadlines where applicable. "
                    "If the user should consult a professional for their specific situation, say so."
                ),
                ctx=ctx, event_type="finance.knowledge.search", step_type="verify",
                inputs={"action": "finance.knowledge.search", "query_length": len(query)},
            )
        else:
            return await self.execute_with_llm(
                prompt=(
                    f"You are Finn, the finance manager. The user asked: {query}\n\n"
                    "No specific knowledge base entries were found for this query. "
                    "Provide general guidance based on your expertise. "
                    "Recommend consulting a licensed CPA or tax professional "
                    "for complex or situation-specific matters."
                ),
                ctx=ctx, event_type="finance.knowledge.search", step_type="verify",
                inputs={"action": "finance.knowledge.search", "no_rag_results": True},
            )

    async def research_and_answer(
        self, query: str, ctx: AgentContext,
    ) -> AgentResult:
        """Search RAG first, then delegate to Adam for live research if no results. YELLOW."""
        if not query:
            receipt = self.build_receipt(
                ctx=ctx, event_type="finance.research",
                status="failed", inputs={"query": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EMPTY_QUERY"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty query")

        # 1. Try local RAG first
        from aspire_orchestrator.services.financial_retrieval_service import get_financial_retrieval_service

        svc = get_financial_retrieval_service()
        rag_result = await svc.retrieve(query, suite_id=ctx.suite_id)

        if rag_result.chunks:
            # RAG has the answer — use it directly
            context = svc.assemble_rag_context(rag_result)
            return await self.execute_with_llm(
                prompt=(
                    f"You are Finn, the finance manager. The user asked: {query}\n\n"
                    f"Relevant knowledge from your knowledge base:\n{context}\n\n"
                    "Synthesize the knowledge into a clear, actionable answer. "
                    "Reference specific rules, thresholds, or deadlines where applicable."
                ),
                ctx=ctx, event_type="finance.research", step_type="verify",
                inputs={"action": "finance.research", "source": "rag", "chunk_count": len(rag_result.chunks)},
            )

        # 2. No RAG results — delegate to Adam for web research
        logger.info("No RAG results for '%s' — delegating to Adam for research", query[:80])

        fm_ctx = FinnFMContext(
            suite_id=ctx.suite_id,
            office_id=ctx.office_id or "default",
            correlation_id=ctx.correlation_id or str(uuid.uuid4()),
        )

        delegation_result = dispatch_a2a_delegation(
            fm_ctx,
            to_agent="adam",
            request_type="ResearchRequest",
            payload={
                "query": query,
                "context": "financial_research",
                "requested_by": "finn",
                "urgency": "normal",
            },
        )

        if delegation_result.success:
            # Adam research dispatched — give user immediate general guidance + note that research is in progress
            return await self.execute_with_llm(
                prompt=(
                    f"You are Finn, the finance manager. The user asked: {query}\n\n"
                    "You don't have specific knowledge base entries for this question, "
                    "so you've asked Adam (your research specialist) to look into it. "
                    "Provide general guidance based on your expertise while noting that "
                    "Adam is researching more specific and current information. "
                    "Recommend consulting a licensed CPA or tax professional for complex matters."
                ),
                ctx=ctx, event_type="finance.research", step_type="verify",
                inputs={
                    "action": "finance.research",
                    "source": "adam_delegation",
                    "delegation_id": delegation_result.data.get("delegation_id"),
                },
            )
        else:
            # Delegation failed — fall back to general LLM answer
            return await self.execute_with_llm(
                prompt=(
                    f"You are Finn, the finance manager. The user asked: {query}\n\n"
                    "No specific knowledge base entries were found. "
                    "Provide general guidance based on your expertise. "
                    "Recommend consulting a licensed CPA or tax professional "
                    "for complex or situation-specific matters."
                ),
                ctx=ctx, event_type="finance.research", step_type="verify",
                inputs={"action": "finance.research", "source": "fallback", "delegation_error": delegation_result.error},
            )
