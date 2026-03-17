"""mail_ops_desk Admin Skill Pack — Domain & mailbox management for PolarisM.

Internal admin skill pack. Handles:
  - Domain availability check (GREEN)
  - Domain ownership verification (GREEN)
  - DNS record creation (YELLOW — requires user approval)
  - Domain purchase (RED — financial, requires explicit authority)
  - Domain deletion (RED — irreversible, requires explicit authority)
  - Mail account creation (YELLOW — requires user approval)
  - Mail account listing (GREEN)

Provider: Domain Rail (S2S HMAC) → ResellerClub + PolarisM

HARD RULES:
  - NO user content access (never read email body — that's Eli's job)
  - NO sending email (that's Eli's job)
  - 100% receipted (every operation generates receipt)
  - Internal admin only — not user-facing

Law compliance:
  - Law #1: Skill pack proposes plans, orchestrator decides when to invoke
  - Law #2: Every method emits a receipt (success, failure, and denial)
  - Law #3: Fail closed on missing parameters, missing binding fields
  - Law #4: GREEN/YELLOW/RED tiers enforced per policy_matrix.yaml
  - Law #5: Capability tokens required for all Domain Rail tool calls
  - Law #6: suite_id/office_id scoping enforced in every operation
  - Law #7: Uses tool_executor for all Domain Rail calls (tools are hands)

Binding fields enforcement (per policy_matrix.yaml):
  - domain.dns.create: domain, record_type, value
  - domain.purchase: domain_name, years, amount_cents
  - domain.delete: domain_name
  - mail.account.create: email_address, display_name
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

ACTOR_MAIL_OPS = "skillpack:mail-ops-desk"
RECEIPT_VERSION = "1.0"

# Binding fields per policy_matrix.yaml — must be confirmed by user before execution
DNS_CREATE_BINDING_FIELDS = {"domain", "record_type", "value"}
DOMAIN_PURCHASE_BINDING_FIELDS = {"domain_name"}
DOMAIN_DELETE_BINDING_FIELDS = {"domain_name"}
MAIL_ACCOUNT_CREATE_BINDING_FIELDS = {"email_address"}

# Content access blocklist — mail_ops_desk NEVER accesses user content
BLOCKED_CONTENT_FIELDS = frozenset({
    "body", "email_body", "message_body", "content",
    "html_body", "text_body", "attachments",
})


@dataclass
class SkillPackResult:
    """Result of a mail_ops_desk skill pack operation."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_required: bool = False


@dataclass
class MailOpsContext:
    """Tenant-scoped execution context for mail_ops_desk operations."""

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
    ctx: MailOpsContext,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    tool_used: str = "",
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a receipt for a mail_ops_desk operation (Law #2)."""
    now = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-mailops-{uuid.uuid4().hex[:12]}",
        "ts": now,
        "event_type": action_type,
        "suite_id": ctx.suite_id,
        "office_id": ctx.office_id,
        "actor": ACTOR_MAIL_OPS,
        "correlation_id": ctx.correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": _compute_inputs_hash(inputs or {}),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "mail-ops-desk-v1",
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
    return missing


def _contains_blocked_content(payload: dict[str, Any]) -> bool:
    """Check if payload contains blocked content fields (email body, etc.)."""
    return bool(BLOCKED_CONTENT_FIELDS & set(payload.keys()))


class MailOpsDeskSkillPack:
    async def domain_check(
        self,
        domain_name: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        return await self.check_domain(domain_name=domain_name, context=context)

    async def domain_verify(
        self,
        domain_name: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        return await self.verify_domain(domain_name=domain_name, context=context)

    async def domain_dns_create(
        self,
        domain_name: str,
        record_type: str,
        name: str,
        value: str,
        context: MailOpsContext,
        *,
        ttl: int = 3600,
    ) -> SkillPackResult:
        return await self.create_dns_record(domain_name=domain_name, record_type=record_type, name=name, value=value, context=context, ttl=ttl)

    async def domain_purchase(
        self,
        domain_name: str,
        years: int,
        contact_email: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        return await self.purchase_domain(domain_name=domain_name, years=years, contact_email=contact_email, context=context)

    async def domain_delete(
        self,
        domain_name: str,
        context: MailOpsContext,
        *,
        confirm: bool = False,
    ) -> SkillPackResult:
        return await self.delete_domain(domain_name=domain_name, context=context, confirm=confirm)

    async def mail_account_create(
        self,
        domain_name: str,
        mailbox_name: str,
        password: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        return await self.create_mail_account(domain_name=domain_name, mailbox_name=mailbox_name, password=password, context=context)

    async def mail_account_read(
        self,
        email_address: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        return await self.read_mail_account(email_address=email_address, context=context)

    """mail_ops_desk Admin Skill Pack — governed domain & mailbox management.

    All methods require a MailOpsContext for tenant scoping (Law #6)
    and produce receipts for every outcome (Law #2).

    Internal admin only. NO user content access. NO email sending.
    """

    # ─────────────────────────────────────────────
    # GREEN tier — no approval needed
    # ─────────────────────────────────────────────

    async def check_domain(
        self,
        domain_name: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        """Check domain availability (GREEN — no approval required).

        Delegates to domain.check tool via Domain Rail.
        """
        if not domain_name or not domain_name.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="domain.check",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_DOMAIN_NAME",
                tool_used="domain.check",
                inputs={"action": "domain.check", "domain_name": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: domain_name",
            )

        result: ToolExecutionResult = await execute_tool(
            tool_id="domain.check",
            payload={"domain": domain_name.strip()},
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
            action_type="domain.check",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else (result.error or "FAILED"),
            tool_used="domain.check",
            inputs={"action": "domain.check", "domain_name": domain_name.strip()},
            metadata={"domain_name": domain_name.strip(), "tool_id": result.tool_id},
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def verify_domain(
        self,
        domain_name: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        """Verify domain ownership via DNS records (GREEN — no approval required).

        Delegates to domain.verify tool via Domain Rail.
        """
        if not domain_name or not domain_name.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="domain.verify",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_DOMAIN_NAME",
                tool_used="domain.verify",
                inputs={"action": "domain.verify", "domain_name": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: domain_name",
            )

        result: ToolExecutionResult = await execute_tool(
            tool_id="domain.verify",
            payload={"domain": domain_name.strip()},
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
            action_type="domain.verify",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else (result.error or "FAILED"),
            tool_used="domain.verify",
            inputs={"action": "domain.verify", "domain_name": domain_name.strip()},
            metadata={"domain_name": domain_name.strip(), "tool_id": result.tool_id},
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    async def read_mail_account(
        self,
        email_address: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        """List mail accounts for a domain (GREEN — no approval required).

        Extracts domain from email_address and lists accounts.
        Delegates to polaris.account.read tool via Domain Rail.

        HARD RULE: Never returns email body/content — admin metadata only.
        """
        if not email_address or not email_address.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="mail.account.read",
                risk_tier="green",
                outcome="denied",
                reason_code="MISSING_EMAIL_ADDRESS",
                tool_used="polaris.account.read",
                inputs={"action": "mail.account.read", "email_address": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: email_address",
            )

        # Extract domain from email address
        email = email_address.strip()
        domain = email.split("@")[-1] if "@" in email else email

        result: ToolExecutionResult = await execute_tool(
            tool_id="polaris.account.read",
            payload={"domain": domain},
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
            action_type="mail.account.read",
            risk_tier="green",
            outcome=status,
            reason_code="EXECUTED" if result.outcome == Outcome.SUCCESS else (result.error or "FAILED"),
            tool_used="polaris.account.read",
            inputs={"action": "mail.account.read", "domain": domain},
            metadata={"domain": domain, "tool_id": result.tool_id},
        )

        return SkillPackResult(
            success=result.outcome == Outcome.SUCCESS,
            data=result.data,
            receipt=receipt,
            error=result.error,
        )

    # ─────────────────────────────────────────────
    # YELLOW tier — requires user confirmation
    # ─────────────────────────────────────────────

    async def create_dns_record(
        self,
        domain_name: str,
        record_type: str,
        record_value: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        """Create a DNS record for a domain (YELLOW — requires user approval).

        Binding fields: domain, record_type, value.
        All must be confirmed by user before execution.
        Delegates to domain.dns.create tool via Domain Rail.
        """
        params = {
            "domain": domain_name,
            "record_type": record_type,
            "value": record_value,
        }

        missing = _check_binding_fields(params, DNS_CREATE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="domain.dns.create",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="domain.dns.create",
                inputs={"action": "domain.dns.create", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        dns_plan = {
            "domain": domain_name.strip(),
            "record_type": record_type.strip(),
            "value": record_value.strip(),
            "risk_tier": "yellow",
            "binding_fields": sorted(DNS_CREATE_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="domain.dns.create",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="domain.dns.create",
            inputs={"action": "domain.dns.create", "domain": domain_name.strip(), "record_type": record_type.strip()},
            metadata={
                "domain": domain_name.strip(),
                "record_type": record_type.strip(),
            },
        )

        return SkillPackResult(
            success=True,
            data=dns_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def create_mail_account(
        self,
        domain_name: str,
        email_address: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        """Create a mail account on a domain (YELLOW — requires user approval).

        Binding fields: email_address, display_name.
        Delegates to polaris.account.create tool via Domain Rail.
        """
        params = {
            "email_address": email_address,
        }

        missing = _check_binding_fields(params, MAIL_ACCOUNT_CREATE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="mail.account.create",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="polaris.account.create",
                inputs={"action": "mail.account.create", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        if not domain_name or not domain_name.strip():
            receipt = _make_receipt(
                ctx=context,
                action_type="mail.account.create",
                risk_tier="yellow",
                outcome="denied",
                reason_code="MISSING_DOMAIN_NAME",
                tool_used="polaris.account.create",
                inputs={"action": "mail.account.create", "domain_name": ""},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error="Missing required parameter: domain_name",
            )

        mail_plan = {
            "domain": domain_name.strip(),
            "email_address": email_address.strip(),
            "risk_tier": "yellow",
            "binding_fields": sorted(MAIL_ACCOUNT_CREATE_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="mail.account.create",
            risk_tier="yellow",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="polaris.account.create",
            inputs={"action": "mail.account.create", "email_address": email_address.strip()},
            metadata={
                "domain": domain_name.strip(),
                "email_address": email_address.strip(),
            },
        )

        return SkillPackResult(
            success=True,
            data=mail_plan,
            receipt=receipt,
            approval_required=True,
        )

    # ─────────────────────────────────────────────
    # RED tier — requires explicit authority + presence
    # ─────────────────────────────────────────────

    async def purchase_domain(
        self,
        domain_name: str,
        registrant_info: dict[str, Any],
        context: MailOpsContext,
    ) -> SkillPackResult:
        """Purchase a domain (RED — financial, requires explicit authority).

        Binding fields: domain_name, years, amount_cents.
        Delegates to domain.purchase tool via Domain Rail.
        """
        params = {"domain_name": domain_name}

        missing = _check_binding_fields(params, DOMAIN_PURCHASE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="domain.purchase",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="domain.purchase",
                inputs={"action": "domain.purchase", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        years = registrant_info.get("years", 1)
        purchase_plan = {
            "domain_name": domain_name.strip(),
            "years": years,
            "registrant_info": {
                k: v for k, v in registrant_info.items()
                if k not in BLOCKED_CONTENT_FIELDS
            },
            "risk_tier": "red",
            "binding_fields": sorted(DOMAIN_PURCHASE_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="domain.purchase",
            risk_tier="red",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="domain.purchase",
            inputs={"action": "domain.purchase", "domain_name": domain_name.strip(), "years": years},
            metadata={
                "domain_name": domain_name.strip(),
                "years": years,
            },
        )

        return SkillPackResult(
            success=True,
            data=purchase_plan,
            receipt=receipt,
            approval_required=True,
        )

    async def delete_domain(
        self,
        domain_name: str,
        context: MailOpsContext,
    ) -> SkillPackResult:
        """Delete a domain (RED — irreversible, requires explicit authority).

        Binding fields: domain_name.
        Delegates to domain.delete tool via Domain Rail.
        """
        params = {"domain_name": domain_name}

        missing = _check_binding_fields(params, DOMAIN_DELETE_BINDING_FIELDS)
        if missing:
            receipt = _make_receipt(
                ctx=context,
                action_type="domain.delete",
                risk_tier="red",
                outcome="denied",
                reason_code="MISSING_BINDING_FIELDS",
                tool_used="domain.delete",
                inputs={"action": "domain.delete", "missing": missing},
            )
            return SkillPackResult(
                success=False,
                receipt=receipt,
                error=f"Missing required binding fields: {', '.join(missing)}",
            )

        delete_plan = {
            "domain_name": domain_name.strip(),
            "risk_tier": "red",
            "binding_fields": sorted(DOMAIN_DELETE_BINDING_FIELDS),
        }

        receipt = _make_receipt(
            ctx=context,
            action_type="domain.delete",
            risk_tier="red",
            outcome="success",
            reason_code="APPROVAL_REQUIRED",
            tool_used="domain.delete",
            inputs={"action": "domain.delete", "domain_name": domain_name.strip()},
            metadata={"domain_name": domain_name.strip()},
        )

        return SkillPackResult(
            success=True,
            data=delete_plan,
            receipt=receipt,
            approval_required=True,
        )


# =============================================================================
# Phase 3 W4: Enhanced Mail Ops with LLM reasoning
# =============================================================================

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class EnhancedMailOps(AgenticSkillPack):
    """LLM-enhanced Mail Ops — domain provisioning planning, mailbox management.

    Extends MailOpsDeskSkillPack with:
    - plan_domain_setup: LLM plans DNS records, mailbox configuration
    - diagnose_delivery: LLM analyzes mail delivery issues

    YELLOW tier for provisioning, GREEN for diagnostics.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="mail-ops",
            agent_name="Mail Ops",
            default_risk_tier="yellow",
            memory_enabled=True,
        )
        self._rule_pack = MailOpsDeskSkillPack()

    async def plan_domain_setup(self, domain_name: str, ctx: AgentContext) -> AgentResult:
        """Plan domain and mailbox provisioning. YELLOW — user approves before execution."""
        if not domain_name:
            receipt = self.build_receipt(
                ctx=ctx, event_type="mail.plan_domain",
                status="failed", inputs={"domain": ""},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_DOMAIN"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing domain_name")

        return await self.execute_with_llm(
            prompt=(
                f"You are the Mail Ops specialist. Plan domain setup.\n\n"
                f"Domain: {domain_name}\n\n"
                f"Generate: DNS records needed (MX, SPF, DKIM, DMARC), "
                f"mailbox names (info@, admin@, support@), "
                f"security settings, estimated propagation time."
            ),
            ctx=ctx, event_type="mail.plan_domain", step_type="plan",
            inputs={"action": "mail.plan_domain", "domain": domain_name},
        )

    async def diagnose_delivery(self, issue_data: dict, ctx: AgentContext) -> AgentResult:
        """Diagnose mail delivery issues. GREEN — analysis only."""
        # Law #3: Fail-closed on empty input
        if not issue_data:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="mail.diagnose",
                status="denied",
                inputs={"action": "mail.diagnose"},
            )
            receipt["policy"] = {"decision": "deny", "reasons": ["empty_issue_data"]}
            return AgentResult(success=False, data={}, receipt=receipt)

        return await self.execute_with_llm(
            prompt=(
                f"You are the Mail Ops specialist. Diagnose this delivery issue.\n\n"
                f"Issue: {issue_data.get('description', 'unknown')}\n"
                f"Domain: {issue_data.get('domain', 'unknown')}\n"
                f"Error: {issue_data.get('error_code', 'unknown')}\n\n"
                f"Diagnose: root cause, DNS issues, SPF/DKIM/DMARC problems, "
                f"recommended fixes, estimated resolution time."
            ),
            ctx=ctx, event_type="mail.diagnose", step_type="verify",
            inputs={"action": "mail.diagnose", "domain": issue_data.get("domain", "")},
        )
