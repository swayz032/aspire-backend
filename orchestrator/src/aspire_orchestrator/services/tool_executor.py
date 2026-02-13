"""Tool Executor Registry — Routes tool_id to executor functions (Law #7).

Per CLAUDE.md Law #7: Tools Are Hands — they execute bounded commands,
never decide. This registry maps tool IDs from the Control Plane Registry
to their actual executor implementations.

Phase 1 executor tiers:
  - LIVE: Domain Rail tools (domain.*, polaris.account.*) — S2S HMAC calls
  - STUB: All other tools — return stub success (implementations in Phase 2)

Each executor returns a ToolExecutionResult with the tool response + receipt data.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.services.domain_rail_client import (
    DomainRailClientError,
    DomainRailResponse,
    domain_check,
    domain_verify,
    domain_dns_create,
    domain_purchase,
    domain_delete,
    mail_account_create,
    mail_account_read,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolExecutionResult:
    """Result of executing a tool."""

    outcome: Outcome
    tool_id: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    receipt_data: dict[str, Any] = field(default_factory=dict)
    is_stub: bool = False


# Type alias for tool executor functions
ToolExecutorFn = Callable[..., Awaitable[ToolExecutionResult]]


def _make_receipt_data(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    tool_id: str,
    risk_tier: str,
    outcome: Outcome,
    reason_code: str,
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> dict[str, Any]:
    """Build receipt data for a tool execution."""
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "tool_executor",
        "action_type": f"execute.{tool_id}",
        "risk_tier": risk_tier,
        "tool_used": tool_id,
        "capability_token_id": capability_token_id,
        "capability_token_hash": capability_token_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome.value,
        "reason_code": reason_code,
        "receipt_type": ReceiptType.TOOL_EXECUTION.value,
        "receipt_hash": "",
    }


def _dr_response_to_result(
    response: DomainRailResponse,
    tool_id: str,
    receipt_data: dict[str, Any],
) -> ToolExecutionResult:
    """Convert a DomainRailResponse to a ToolExecutionResult."""
    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data=response.body,
            receipt_data=receipt_data,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            data=response.body,
            error=response.error or f"HTTP {response.status_code}",
            receipt_data=receipt_data,
        )


# =============================================================================
# Domain Rail Executors — LIVE (S2S HMAC authenticated)
# =============================================================================


async def execute_domain_check(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute domain.check via Domain Rail."""
    domain = payload.get("domain", "")
    if not domain:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.check",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_DOMAIN_PARAM",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.check",
            error="Missing required parameter: domain",
            receipt_data=receipt,
        )

    try:
        response = await domain_check(
            domain=domain, correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.check",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.check",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="domain.check",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "domain.check", receipt)


async def execute_domain_verify(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute domain.verify via Domain Rail."""
    domain = payload.get("domain", "")
    if not domain:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.verify",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_DOMAIN_PARAM",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.verify",
            error="Missing required parameter: domain",
            receipt_data=receipt,
        )

    try:
        response = await domain_verify(
            domain=domain, correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.verify",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.verify",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="domain.verify",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "domain.verify", receipt)


async def execute_domain_dns_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute domain.dns.create via Domain Rail."""
    domain = payload.get("domain", "")
    record_type = payload.get("record_type", "")
    value = payload.get("value", "")

    if not all([domain, record_type, value]):
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.dns.create",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_PARAMS",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.dns.create",
            error="Missing required parameters: domain, record_type, value",
            receipt_data=receipt,
        )

    try:
        response = await domain_dns_create(
            domain=domain, record_type=record_type, value=value,
            correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.dns.create",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.dns.create",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="domain.dns.create",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "domain.dns.create", receipt)


async def execute_domain_purchase(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "red",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute domain.purchase via Domain Rail."""
    domain_name = payload.get("domain_name", "")
    years = payload.get("years", 1)

    if not domain_name:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.purchase",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_DOMAIN_NAME",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.purchase",
            error="Missing required parameter: domain_name",
            receipt_data=receipt,
        )

    try:
        response = await domain_purchase(
            domain_name=domain_name, years=years,
            correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.purchase",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.purchase",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="domain.purchase",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "domain.purchase", receipt)


async def execute_domain_delete(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "red",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute domain.delete via Domain Rail."""
    domain = payload.get("domain", payload.get("domain_name", ""))

    if not domain:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.delete",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_DOMAIN_PARAM",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.delete",
            error="Missing required parameter: domain",
            receipt_data=receipt,
        )

    try:
        response = await domain_delete(
            domain=domain, correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="domain.delete",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="domain.delete",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="domain.delete",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "domain.delete", receipt)


async def execute_mail_account_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute polaris.account.create via Domain Rail."""
    domain = payload.get("domain", "")
    email_address = payload.get("email_address", "")
    display_name = payload.get("display_name", "")

    if not all([domain, email_address]):
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="polaris.account.create",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_PARAMS",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="polaris.account.create",
            error="Missing required parameters: domain, email_address",
            receipt_data=receipt,
        )

    try:
        response = await mail_account_create(
            domain=domain, email_address=email_address,
            display_name=display_name or email_address.split("@")[0],
            correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="polaris.account.create",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="polaris.account.create",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="polaris.account.create",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "polaris.account.create", receipt)


async def execute_mail_account_read(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute polaris.account.read via Domain Rail."""
    domain = payload.get("domain", "")

    if not domain:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="polaris.account.read",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code="MISSING_DOMAIN_PARAM",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="polaris.account.read",
            error="Missing required parameter: domain",
            receipt_data=receipt,
        )

    try:
        response = await mail_account_read(
            domain=domain, correlation_id=correlation_id,
            suite_id=suite_id, office_id=office_id,
        )
    except DomainRailClientError as e:
        receipt = _make_receipt_data(
            correlation_id=correlation_id, suite_id=suite_id,
            office_id=office_id, tool_id="polaris.account.read",
            risk_tier=risk_tier, outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED, tool_id="polaris.account.read",
            error=e.message, receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id="polaris.account.read",
        risk_tier=risk_tier, outcome=outcome,
        reason_code="EXECUTED" if response.success else (response.error or "FAILED"),
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )
    return _dr_response_to_result(response, "polaris.account.read", receipt)


# =============================================================================
# Stub Executor — Phase 2 tools that aren't wired yet
# =============================================================================


async def execute_stub(
    *,
    tool_id: str,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Stub executor for tools not yet implemented.

    Returns success with stub=True marker. Phase 2 replaces these
    with real provider integrations.
    """
    receipt = _make_receipt_data(
        correlation_id=correlation_id, suite_id=suite_id,
        office_id=office_id, tool_id=tool_id,
        risk_tier=risk_tier, outcome=Outcome.SUCCESS,
        reason_code="EXECUTED_STUB",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id=tool_id,
        data={
            "status": "success",
            "tool": tool_id,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
        },
        receipt_data=receipt,
        is_stub=True,
    )


# =============================================================================
# Tool Executor Registry — maps tool_id → executor function
# =============================================================================


# Domain Rail tools (LIVE — S2S HMAC authenticated)
_DOMAIN_RAIL_EXECUTORS: dict[str, ToolExecutorFn] = {
    "domain.check": execute_domain_check,
    "domain.verify": execute_domain_verify,
    "domain.dns.create": execute_domain_dns_create,
    "domain.purchase": execute_domain_purchase,
    "domain.delete": execute_domain_delete,
    "polaris.account.create": execute_mail_account_create,
    "polaris.account.read": execute_mail_account_read,
}


async def execute_tool(
    *,
    tool_id: str,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute a tool by ID. Routes to live executors or stub.

    Domain Rail tools → S2S HMAC authenticated HTTP calls.
    All other tools → stub executor (Phase 2 implementation).
    """
    executor = _DOMAIN_RAIL_EXECUTORS.get(tool_id)

    if executor:
        logger.info("Tool executor LIVE: %s", tool_id)
        return await executor(
            payload=payload,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            risk_tier=risk_tier,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
    else:
        logger.info("Tool executor STUB: %s", tool_id)
        return await execute_stub(
            tool_id=tool_id,
            payload=payload,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            risk_tier=risk_tier,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )


def get_live_tools() -> list[str]:
    """Return list of tool IDs with live (non-stub) executors."""
    return list(_DOMAIN_RAIL_EXECUTORS.keys())


def is_live_tool(tool_id: str) -> bool:
    """Check if a tool has a live executor (vs stub)."""
    return tool_id in _DOMAIN_RAIL_EXECUTORS
