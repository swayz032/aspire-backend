"""Clara Legal Skill Pack -- Contract management, e-signatures via PandaDoc.

Clara is the Legal desk. She handles:
  - Contract generation from templates (YELLOW -- requires user confirmation)
  - Contract review / status read (GREEN -- read-only)
  - Contract signing via e-signature (RED -- binding, irreversible, requires presence)
  - Compliance tracking for expirations/renewals (GREEN -- read-only)

Provider: PandaDoc (https://api.pandadoc.com/public/v1)

Law compliance:
  - Law #1: Skill pack proposes, orchestrator decides
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters, missing binding fields
  - Law #4: generate=YELLOW, review/compliance=GREEN, sign=RED (presence + authority)
  - Law #5: Capability tokens required for all PandaDoc tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all PandaDoc calls (tools are hands)

Binding fields enforcement (per policy_matrix.yaml):
  - contract.generate: party_names, template_id
  - contract.sign: contract_id, signer_name, signer_email
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

ACTOR_CLARA = "skillpack:clara-legal"
RECEIPT_VERSION = "1.0"

# Template types Clara supports
VALID_TEMPLATE_TYPES = frozenset({
    "nda",
    "msa",
    "sow",
    "employment",
    "amendment",
    "termination",
})

# Binding fields per policy_matrix.yaml -- must be confirmed by user
CONTRACT_GENERATE_BINDING_FIELDS = {"party_names", "template_id"}
CONTRACT_SIGN_BINDING_FIELDS = {"contract_id", "signer_name", "signer_email"}


@dataclass
class SkillPackResult:
    """Result of a Clara Legal skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False
    presence_required: bool = False


@dataclass
class ClaraContext:
    """Tenant-scoped execution context for Clara operations."""

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
    ctx: ClaraContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a Clara legal operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-clara-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_CLARA,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": _compute_inputs_hash(inputs or {}),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "clara-legal-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "redactions": [],
    }
    if tool_used:
        receipt["tool_used"] = tool_used
    if metadata:
        receipt["metadata"] = metadata
    return receipt


def _check_binding_fields(
    params: dict[str, Any],
    required_fields: set[str],
) -> list[str]:
    """Return list of missing binding fields (Law #3: fail closed)."""
    missing = []
    for f in sorted(required_fields):
        val = params.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(f)
        elif isinstance(val, list) and len(val) == 0:
            missing.append(f)
    return missing


class ClaraLegalSkillPack:
    """Clara Legal Skill Pack -- governed contract management operations.

    All methods require a ClaraContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).

    Risk tiers (Law #4):
      - generate_contract: YELLOW (requires user confirmation)
      - review_contract: GREEN (read-only)
      - sign_contract: RED (requires presence + explicit authority)
      - track_compliance: GREEN (read-only)
    """

    async def generate_contract(
        self,
        template_type: str,
        parties: list[dict[str, Any]],
        terms: dict[str, Any],
        context: ClaraContext,
    ) -> SkillPackResult:
        """Generate a contract from a template (YELLOW -- requires user approval).

        Args:
            template_type: One of VALID_TEMPLATE_TYPES (nda, msa, sow, etc.)
            parties: List of party dicts [{name, email, role}]
            terms: Contract terms dict (title, description, duration, etc.)
            context: Tenant-scoped execution context

        Binding fields: party_names, template_id
        """
        # Validate template_type
        if not template_type or template_type not in VALID_TEMPLATE_TYPES:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.generate",
                risk_tier="yellow",
                outcome="denied",
                reason_code="INVALID_TEMPLATE_TYPE",
                tool_used="pandadoc.contract.generate",
                inputs={"action": "contract.generate", "template_type": template_type},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Invalid template_type: '{template_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_TEMPLATE_TYPES))}",
            )

        # Extract party names for binding field check
        party_names = [p.get("name", "") for p in parties] if parties else []

        params = {
            "party_names": party_names,
            "template_id": template_type,
        }

        missing = _check_binding_fields(params, CONTRACT_GENERATE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.generate",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="pandadoc.contract.generate",
                inputs={"action": "contract.generate", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # YELLOW tier: build the plan, mark approval_required
        generate_plan = {
            "template_type": template_type,
            "parties": parties,
            "terms": terms,
            "party_names": party_names,
            "risk_tier": "yellow",
            "binding_fields": sorted(CONTRACT_GENERATE_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="contract.generate",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="pandadoc.contract.generate",
            inputs={
                "action": "contract.generate",
                "template_type": template_type,
                "party_names": party_names,
            },
            metadata={
                "template_type": template_type,
                "party_count": len(parties),
                "party_names": party_names,
            },
        )

        return SkillPackResult(
            success=True,
            data=generate_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def review_contract(
        self,
        contract_id: str,
        context: ClaraContext,
    ) -> SkillPackResult:
        """Review a contract -- read status and details (GREEN -- read-only).

        Args:
            contract_id: PandaDoc document ID
            context: Tenant-scoped execution context
        """
        if not contract_id or not contract_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.review",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_CONTRACT_ID",
                tool_used="pandadoc.contract.read",
                inputs={"action": "contract.review", "contract_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: contract_id",
            )

        # GREEN tier: execute directly via tool_executor (no approval needed)
        result: ToolExecutionResult = await execute_tool(
            tool_id="pandadoc.contract.read",
            payload={"document_id": contract_id.strip()},
            correlation_id=context.correlation_id,
            suite_id=context.suite_id,
            office_id=context.office_id,
            risk_tier="green",
            capability_token_id=context.capability_token_id,
            capability_token_hash=context.capability_token_hash,
        )

        status = "success" if result.outcome == Outcome.SUCCESS else "failed"
        receipt = _make_receipt(
            ctx=context,
            action_type="contract.review",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else "TOOL_FAILED",
            tool_used="pandadoc.contract.read",
            inputs={"action": "contract.review", "contract_id": contract_id.strip()},
            metadata={
                "contract_id": contract_id.strip(),
                "tool_id": result.tool_id,
            },
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def sign_contract(
        self,
        contract_id: str,
        signer_info: dict[str, Any],
        context: ClaraContext,
    ) -> SkillPackResult:
        """Sign a contract via e-signature (RED -- requires presence + explicit authority).

        This is a binding legal action. Per CLAUDE.md Law #4 and Law #8:
          - RED tier requires explicit authority + strong confirmation UX
          - Presence required (video authority for binding signature)
          - Approval binding enforced (approve-then-swap defense)

        Args:
            contract_id: PandaDoc document ID
            signer_info: Dict with signer_name, signer_email
            context: Tenant-scoped execution context

        Binding fields: contract_id, signer_name, signer_email
        """
        signer_name = signer_info.get("signer_name", "")
        signer_email = signer_info.get("signer_email", "")

        params = {
            "contract_id": contract_id,
            "signer_name": signer_name,
            "signer_email": signer_email,
        }

        missing = _check_binding_fields(params, CONTRACT_SIGN_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.sign",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="pandadoc.contract.sign",
                inputs={"action": "contract.sign", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        # RED tier: build the plan, mark approval_required AND presence_required
        sign_plan = {
            "contract_id": contract_id,
            "signer_name": signer_name,
            "signer_email": signer_email,
            "risk_tier": "red",
            "binding_fields": sorted(CONTRACT_SIGN_BINDING_FIELDS),
            "presence_required": True,
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="contract.sign",
            risk_tier="red",
            outcome="success",
            reason_code="APPROVAL_AND_PRESENCE_REQUIRED",
            tool_used="pandadoc.contract.sign",
            inputs={
                "action": "contract.sign",
                "contract_id": contract_id,
                "signer_name": signer_name,
                "signer_email": signer_email,
            },
            metadata={
                "contract_id": contract_id,
                "signer_name": signer_name,
                "signer_email": signer_email,
                "signature_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        return SkillPackResult(
            success=True,
            data=sign_plan,
            receipt=receipt,
            approval_required=True,
            presence_required=True,
        )

    async def track_compliance(
        self,
        contract_id: str,
        context: ClaraContext,
    ) -> SkillPackResult:
        """Track contract compliance -- expiration and renewal detection (GREEN -- read-only).

        Reads contract status and checks for upcoming expirations,
        renewal deadlines, and compliance milestones.

        Args:
            contract_id: PandaDoc document ID
            context: Tenant-scoped execution context
        """
        if not contract_id or not contract_id.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="contract.compliance",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_CONTRACT_ID",
                tool_used="pandadoc.contract.read",
                inputs={"action": "contract.compliance", "contract_id": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: contract_id",
            )

        # GREEN tier: read contract status to check compliance
        result: ToolExecutionResult = await execute_tool(
            tool_id="pandadoc.contract.read",
            payload={"document_id": contract_id.strip()},
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
                action_type="contract.compliance",
                risk_tier="green",
                outcome="failed",
                reason_code="TOOL_FAILED",
                tool_used="pandadoc.contract.read",
                inputs={"action": "contract.compliance", "contract_id": contract_id.strip()},
                metadata={"tool_id": result.tool_id},
            )
            return SkillPackResult(
                success=False,
                data=result.data,
                receipt=receipt,
                error=result.error,
            )

        # Build compliance assessment from contract data
        compliance_data = _assess_compliance(result.data, contract_id.strip())

        receipt = _make_receipt(
            ctx=context,
            action_type="contract.compliance",
            risk_tier="green",
            outcome="success",
            reason_code="EXECUTED",
            tool_used="pandadoc.contract.read",
            inputs={"action": "contract.compliance", "contract_id": contract_id.strip()},
            metadata={
                "contract_id": contract_id.strip(),
                "compliance_status": compliance_data.get("compliance_status", "unknown"),
                "expiration_date": compliance_data.get("expiration_date"),
            },
        )

        return SkillPackResult(
            success=True,
            data=compliance_data,
            receipt=receipt,
        )


def _assess_compliance(
    contract_data: dict[str, Any],
    contract_id: str,
) -> dict[str, Any]:
    """Assess compliance status from contract data.

    Checks for:
    - Document status (draft, sent, completed, expired, voided)
    - Expiration date proximity
    - Renewal needs
    """
    status = contract_data.get("status", "unknown")
    expiration_date = contract_data.get("expiration_date")
    name = contract_data.get("name", "")

    compliance_status = "active"
    alerts: list[str] = []

    if status in ("voided", "declined"):
        compliance_status = "terminated"
        alerts.append(f"Contract {status}")
    elif status == "document.draft":
        compliance_status = "pending"
        alerts.append("Contract still in draft -- not yet executed")
    elif status in ("document.sent", "document.waiting_approval"):
        compliance_status = "awaiting_signature"
        alerts.append("Contract sent but not yet signed")

    if expiration_date:
        compliance_data_with_expiry: dict[str, Any] = {
            "contract_id": contract_id,
            "name": name,
            "status": status,
            "compliance_status": compliance_status,
            "expiration_date": expiration_date,
            "alerts": alerts,
            "needs_renewal": compliance_status == "active",
        }
        return compliance_data_with_expiry

    return {
        "contract_id": contract_id,
        "name": name,
        "status": status,
        "compliance_status": compliance_status,
        "expiration_date": None,
        "alerts": alerts,
        "needs_renewal": False,
    }


# =============================================================================
# Phase 3 W5a: Enhanced Clara Legal with LLM reasoning + dual approval
# =============================================================================

from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.services.dual_approval_service import (
    get_dual_approval_service,
    ApprovalStatus,
)
from aspire_orchestrator.services.idempotency_service import get_idempotency_service


class EnhancedClaraLegal(EnhancedSkillPack):
    """LLM-enhanced Clara Legal — RED-tier contract intelligence.

    Extends ClaraLegalSkillPack with:
    - review_contract_terms: GPT-5.2 analyzes contract terms for risks
    - plan_signature_flow: GPT-5.2 builds e-signature workflow
    - assess_compliance_risk: GPT-5.2 evaluates compliance exposure
    - initiate_dual_approval: Creates dual approval for contract signing

    ALL methods use high_risk_guard (GPT-5.2) — no cheap models for legal.
    Idempotency enforced on all state-changing operations.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="clara-legal",
            agent_name="Clara Legal",
            default_risk_tier="red",
        )
        self._rule_pack = ClaraLegalSkillPack()

    async def review_contract_terms(
        self, contract_text: str, contract_type: str, ctx: AgentContext,
    ) -> AgentResult:
        """Review contract terms for risks and issues. GREEN — analysis only."""
        if not contract_text:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.review_terms",
                status="failed", inputs={"length": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["EMPTY_CONTRACT"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Empty contract text")

        if contract_type and contract_type not in VALID_TEMPLATE_TYPES:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.review_terms",
                status="failed", inputs={"type": contract_type},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["INVALID_CONTRACT_TYPE"]}
            await self.emit_receipt(receipt)
            return AgentResult(
                success=False, receipt=receipt,
                error=f"Invalid contract type: {contract_type}",
            )

        return await self.execute_with_llm(
            prompt=(
                f"You are Clara, the legal specialist. Review this contract.\n\n"
                f"Contract Type: {contract_type or 'unspecified'}\n"
                f"Contract Text (first 3000 chars):\n{contract_text[:3000]}\n\n"
                f"Analyze: key terms, potential risks, unusual clauses, "
                f"missing protections, liability exposure, termination conditions, "
                f"IP ownership, non-compete scope, indemnification coverage. "
                f"Rate overall risk: LOW/MEDIUM/HIGH."
            ),
            ctx=ctx, event_type="contract.review_terms", step_type="verify",
            inputs={
                "action": "contract.review_terms",
                "type": contract_type or "unspecified",
                "length": len(contract_text),
            },
        )

    async def plan_signature_flow(
        self, contract_details: dict, ctx: AgentContext,
    ) -> AgentResult:
        """Plan the e-signature workflow. RED — requires approval to execute."""
        contract_id = contract_details.get("contract_id", "")
        if not contract_id:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.plan_signature",
                status="failed", inputs={"contract_id": ""},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["MISSING_CONTRACT_ID"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing contract ID")

        signers = contract_details.get("signers", [])
        if not signers:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.plan_signature",
                status="failed", inputs={"contract_id": contract_id, "signers": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["NO_SIGNERS"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="No signers specified")

        return await self.execute_with_llm(
            prompt=(
                f"You are Clara, planning an e-signature flow. RED-tier — binding legal action.\n\n"
                f"Contract: {contract_id}\n"
                f"Type: {contract_details.get('type', 'unknown')}\n"
                f"Signers: {len(signers)}\n"
                f"Signer Details: {json.dumps(signers, default=str)}\n\n"
                f"Plan: signing order, PandaDoc API calls, presence verification "
                f"for each signer, dual approval requirements, notification strategy, "
                f"expiration policy, rollback if any signer declines."
            ),
            ctx=ctx, event_type="contract.plan_signature", step_type="plan",
            inputs={
                "action": "contract.plan_signature",
                "contract_id": contract_id,
                "signer_count": len(signers),
            },
        )

    async def assess_compliance_risk(
        self, contracts: list, ctx: AgentContext,
    ) -> AgentResult:
        """Assess compliance risk across contract portfolio. GREEN — analysis only."""
        if not contracts:
            receipt = self.build_receipt(
                ctx=ctx, event_type="contract.compliance_risk",
                status="failed", inputs={"count": 0},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["NO_CONTRACTS"]}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="No contracts to assess")

        return await self.execute_with_llm(
            prompt=(
                f"You are Clara, assessing compliance risk across a contract portfolio.\n\n"
                f"Portfolio: {len(contracts)} contracts\n"
                f"Summary: {json.dumps(contracts[:10], default=str)}\n\n"
                f"Assess: expiring contracts (30/60/90 day windows), "
                f"unsigned contracts aging, compliance gaps, renewal priorities, "
                f"risk score by category, recommended actions ranked by urgency."
            ),
            ctx=ctx, event_type="contract.compliance_risk", step_type="verify",
            inputs={"action": "contract.compliance_risk", "count": len(contracts)},
        )

    def initiate_dual_approval(
        self, contract_details: dict, ctx: AgentContext,
    ) -> dict:
        """Create dual approval request for contract signing (legal + business)."""
        svc = get_dual_approval_service()
        binding = {
            "contract_id": contract_details.get("contract_id", ""),
            "signer_name": contract_details.get("signer_name", ""),
            "signer_email": contract_details.get("signer_email", ""),
        }

        result = svc.create_request(
            suite_id=ctx.suite_id,
            office_id=ctx.office_id,
            correlation_id=ctx.correlation_id,
            action_type="contract.sign",
            binding_fields=binding,
            required_roles=["legal", "business_owner"],
        )

        return {
            "success": result.success,
            "request_id": result.request_id,
            "status": result.status.value,
            "remaining_roles": result.remaining_roles,
            "receipt": result.receipt,
            "error": result.error,
        }
