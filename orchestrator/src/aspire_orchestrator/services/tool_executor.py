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
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.services.tool_types import ToolExecutionResult, ToolExecutorFn
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

# Phase 2 provider imports — no circular dependency (ToolExecutionResult in tool_types)
from aspire_orchestrator.providers.tavily_client import execute_tavily_search
from aspire_orchestrator.providers.brave_client import execute_brave_search
from aspire_orchestrator.providers.stripe_client import (
    execute_stripe_invoice_create,
    execute_stripe_invoice_send,
    execute_stripe_invoice_void,
    execute_stripe_quote_create,
    execute_stripe_quote_send,
)
from aspire_orchestrator.providers.twilio_client import (
    execute_twilio_call_create,
    execute_twilio_call_status,
)
from aspire_orchestrator.providers.polaris_email_client import (
    execute_polaris_email_send,
    execute_polaris_email_draft,
)

# Wave 1: Adam Research geo/places providers
from aspire_orchestrator.providers.google_places_client import execute_google_places_search
from aspire_orchestrator.providers.tomtom_client import execute_tomtom_search
from aspire_orchestrator.providers.here_client import execute_here_search
from aspire_orchestrator.providers.foursquare_client import execute_foursquare_search
from aspire_orchestrator.providers.osm_overpass_client import execute_osm_overpass_query
from aspire_orchestrator.providers.mapbox_client import execute_mapbox_geocode

# Wave 1: Search router (meta-executors with fallback chains)
from aspire_orchestrator.services.search_router import (
    route_web_search,
    route_places_search,
    route_geocode,
)

# Wave 2: Nora (Conference) providers — LiveKit + Deepgram + ElevenLabs
from aspire_orchestrator.providers.livekit_client import (
    execute_livekit_room_create,
    execute_livekit_room_list,
)
from aspire_orchestrator.providers.deepgram_client import execute_deepgram_transcribe
from aspire_orchestrator.providers.elevenlabs_client import execute_elevenlabs_speak

# Wave 2: Tec (Documents) providers — Puppeteer + S3
from aspire_orchestrator.providers.puppeteer_client import execute_puppeteer_pdf_generate
from aspire_orchestrator.providers.s3_client import (
    execute_s3_document_upload,
    execute_s3_url_sign,
)

# Wave 4: Teressa (Books) providers — QuickBooks Online
from aspire_orchestrator.providers.quickbooks_client import (
    execute_qbo_read_company,
    execute_qbo_read_transactions,
    execute_qbo_read_accounts,
    execute_qbo_journal_entry_create,
)

# Wave 5: Plaid provider (Moov discontinued)
from aspire_orchestrator.providers.plaid_client import (
    execute_plaid_accounts_get,
    execute_plaid_transactions_get,
    execute_plaid_transfer_create,
)

# Wave 5: Milo (Payroll) providers — Gusto (RED tier)
from aspire_orchestrator.providers.gusto_client import (
    execute_gusto_read_company,
    execute_gusto_read_payrolls,
    execute_gusto_payroll_run,
)

# Wave 6: Clara (Legal) providers — PandaDoc (YELLOW/RED tier)
from aspire_orchestrator.providers.pandadoc_client import (
    execute_pandadoc_contract_generate,
    execute_pandadoc_contract_read,
    execute_pandadoc_contract_send,
    execute_pandadoc_contract_sign,
    execute_pandadoc_create_signing_session,
    execute_pandadoc_templates_list,
    execute_pandadoc_templates_details,
)

# Draft-First W5: Calendar tools (GREEN/YELLOW — via Supabase PostgREST)
from aspire_orchestrator.providers.calendar_client import (
    execute_calendar_event_create,
    execute_calendar_event_list,
    execute_calendar_event_complete,
)

logger = logging.getLogger(__name__)

# Re-export for backward compatibility (other modules import from here)
__all__ = ["ToolExecutionResult", "ToolExecutorFn"]


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


# Domain Rail tools (LIVE — S2S HMAC authenticated, Phase 0C)
_DOMAIN_RAIL_EXECUTORS: dict[str, ToolExecutorFn] = {
    "domain.check": execute_domain_check,
    "domain.verify": execute_domain_verify,
    "domain.dns.create": execute_domain_dns_create,
    "domain.purchase": execute_domain_purchase,
    "domain.delete": execute_domain_delete,
    "polaris.account.create": execute_mail_account_create,
    "polaris.account.read": execute_mail_account_read,
}

# Phase 2: Research tools (GREEN tier — Adam Research pack)
# Per ecosystem providers.yaml: brave (primary) → tavily (fallback)
_SEARCH_EXECUTORS: dict[str, ToolExecutorFn] = {
    "brave.search": execute_brave_search,
    "tavily.search": execute_tavily_search,
}

# Phase 2: Places/geo tools (GREEN tier — Adam Research pack)
# Per ecosystem: google_places -> tomtom -> here -> foursquare -> osm_overpass
_PLACES_EXECUTORS: dict[str, ToolExecutorFn] = {
    "google_places.search": execute_google_places_search,
    "tomtom.search": execute_tomtom_search,
    "here.search": execute_here_search,
    "foursquare.search": execute_foursquare_search,
    "osm_overpass.query": execute_osm_overpass_query,
    "mapbox.geocode": execute_mapbox_geocode,
}

# Phase 2: Search router meta-executors (fallback chains)
_SEARCH_ROUTER_EXECUTORS: dict[str, ToolExecutorFn] = {
    "search.web": route_web_search,
    "search.places": route_places_search,
    "search.geocode": route_geocode,
}

# Phase 2: Invoicing tools (YELLOW tier — Quinn Invoicing pack)
_INVOICING_EXECUTORS: dict[str, ToolExecutorFn] = {
    "stripe.invoice.create": execute_stripe_invoice_create,
    "stripe.invoice.send": execute_stripe_invoice_send,
    "stripe.invoice.void": execute_stripe_invoice_void,
    "stripe.quote.create": execute_stripe_quote_create,
    "stripe.quote.send": execute_stripe_quote_send,
}

# Phase 2: Conference tools (GREEN/YELLOW — Nora Conference pack)
_CONFERENCE_EXECUTORS: dict[str, ToolExecutorFn] = {
    "livekit.room.create": execute_livekit_room_create,
    "livekit.room.list": execute_livekit_room_list,
    "deepgram.transcribe": execute_deepgram_transcribe,
    "elevenlabs.speak": execute_elevenlabs_speak,
}

# Phase 2: Document tools (GREEN/YELLOW — Tec Documents pack)
_DOCUMENT_EXECUTORS: dict[str, ToolExecutorFn] = {
    "puppeteer.pdf.generate": execute_puppeteer_pdf_generate,
    "s3.document.upload": execute_s3_document_upload,
    "s3.url.sign": execute_s3_url_sign,
}

# Phase 2 Wave 3: Email tools (YELLOW — Eli Inbox pack)
_EMAIL_EXECUTORS: dict[str, ToolExecutorFn] = {
    "polaris.email.send": execute_polaris_email_send,
    "polaris.email.draft": execute_polaris_email_draft,
}

# Phase 2 Wave 3: Telephony tools (YELLOW — Sarah Front Desk pack)
_TELEPHONY_EXECUTORS: dict[str, ToolExecutorFn] = {
    "twilio.call.create": execute_twilio_call_create,
    "twilio.call.status": execute_twilio_call_status,
}

# Phase 2 Wave 4: Bookkeeping tools (GREEN/YELLOW — Teressa Books pack)
_BOOKS_EXECUTORS: dict[str, ToolExecutorFn] = {
    "qbo.read_company": execute_qbo_read_company,
    "qbo.read_transactions": execute_qbo_read_transactions,
    "qbo.read_accounts": execute_qbo_read_accounts,
    "qbo.journal_entry.create": execute_qbo_journal_entry_create,
}

# Phase 2 Wave 5: Payment tools (Moov discontinued, Plaid retained for reads)
_PAYMENT_EXECUTORS: dict[str, ToolExecutorFn] = {
    "plaid.accounts.get": execute_plaid_accounts_get,
    "plaid.transactions.get": execute_plaid_transactions_get,
    "plaid.transfer.create": execute_plaid_transfer_create,
}

# Phase 2 Wave 5: Payroll tools (RED — Milo Payroll pack)
_PAYROLL_EXECUTORS: dict[str, ToolExecutorFn] = {
    "gusto.read_company": execute_gusto_read_company,
    "gusto.read_payrolls": execute_gusto_read_payrolls,
    "gusto.payroll.run": execute_gusto_payroll_run,
}

# Phase 2 Wave 6: Legal tools (YELLOW/RED — Clara Legal pack)
_LEGAL_EXECUTORS: dict[str, ToolExecutorFn] = {
    "pandadoc.templates.list": execute_pandadoc_templates_list,
    "pandadoc.templates.details": execute_pandadoc_templates_details,
    "pandadoc.contract.generate": execute_pandadoc_contract_generate,
    "pandadoc.contract.read": execute_pandadoc_contract_read,
    "pandadoc.contract.send": execute_pandadoc_contract_send,
    "pandadoc.contract.sign": execute_pandadoc_contract_sign,
    "pandadoc.contract.session": execute_pandadoc_create_signing_session,
}

# Draft-First W5: Calendar tools (GREEN/YELLOW — Supabase PostgREST)
_CALENDAR_EXECUTORS: dict[str, ToolExecutorFn] = {
    "calendar.event.create": execute_calendar_event_create,
    "calendar.event.list": execute_calendar_event_list,
    "calendar.event.complete": execute_calendar_event_complete,
}

# Merged registry of all live executors
_ALL_LIVE_EXECUTORS: dict[str, ToolExecutorFn] = {
    **_DOMAIN_RAIL_EXECUTORS,
    **_SEARCH_EXECUTORS,
    **_PLACES_EXECUTORS,
    **_SEARCH_ROUTER_EXECUTORS,
    **_INVOICING_EXECUTORS,
    **_CONFERENCE_EXECUTORS,
    **_DOCUMENT_EXECUTORS,
    **_EMAIL_EXECUTORS,
    **_TELEPHONY_EXECUTORS,
    **_BOOKS_EXECUTORS,
    **_PAYMENT_EXECUTORS,
    **_PAYROLL_EXECUTORS,
    **_LEGAL_EXECUTORS,
    **_CALENDAR_EXECUTORS,
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

    Live executors: Domain Rail (Phase 0C) + Provider clients (Phase 2).
    Stub fallback: Tools not yet wired return stub success.
    """
    executor = _ALL_LIVE_EXECUTORS.get(tool_id)

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
    return list(_ALL_LIVE_EXECUTORS.keys())


def is_live_tool(tool_id: str) -> bool:
    """Check if a tool has a live executor (vs stub)."""
    return tool_id in _ALL_LIVE_EXECUTORS
