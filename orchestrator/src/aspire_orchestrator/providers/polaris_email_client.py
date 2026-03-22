"""PolarisM Email Provider Client — Email operations for Eli (Inbox) skill pack.

Provider: PolarisM via Domain Rail (S2S HMAC authenticated)
Auth: S2S HMAC-SHA256 (reuses domain_rail_client.py signing pattern)
Risk tier: YELLOW (email.send — external communication), YELLOW (email.draft — creates draft)
Idempotency: No — Domain Rail does not support idempotency headers

Tools:
  - polaris.email.read: Read/list inbox messages via PolarisM
  - polaris.email.send: Send an email via PolarisM
  - polaris.email.draft: Create an email draft via PolarisM

IMPORTANT: Email content MUST be DLP-safe in receipts (Law #9).
Body, subject, and recipient addresses are REDACTED in receipt_data.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.services.domain_rail_client import (
    DomainRailClientError,
    DomainRailResponse,
    _call_domain_rail,
)
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


def _make_email_receipt(
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
    redacted_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build receipt data for email operations with DLP redaction (Law #9).

    Email receipts NEVER contain raw email content — subject, body, and
    recipient addresses are redacted to prevent PII leakage.
    """
    return {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": "provider.polaris_email",
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
        "redacted_inputs": redacted_inputs,
    }


def _redact_email_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """DLP-redact email payload for receipt storage (Law #9).

    Email content is PII-rich — subjects, bodies, and addresses are redacted.
    Only structural metadata (from_address domain, recipient count) is preserved.
    """
    to_value = payload.get("to", "")
    if isinstance(to_value, list):
        recipient_count = len(to_value)
    elif to_value:
        recipient_count = 1
    else:
        recipient_count = 0

    from_address = payload.get("from_address", "")
    from_domain = from_address.split("@")[-1] if "@" in from_address else "REDACTED"

    return {
        "from_domain": from_domain,
        "to": "<EMAIL_REDACTED>",
        "recipient_count": recipient_count,
        "subject": "<SUBJECT_REDACTED>",
        "body_html": "<BODY_REDACTED>",
        "body_text": "<BODY_REDACTED>",
        "has_reply_to": bool(payload.get("reply_to")),
    }


def _redact_email_read_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact read/list filters for receipt safety."""
    return {
        "folder": payload.get("folder", "inbox"),
        "unread_only": bool(payload.get("unread_only", False)),
        "limit": payload.get("limit", 20),
        "since": payload.get("since"),
        "query": "<QUERY_REDACTED>" if payload.get("query") else None,
    }


def _normalize_email_read_response(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider-specific response shapes to a stable schema."""
    emails = body.get("emails")
    if emails is None and isinstance(body.get("data"), dict):
        emails = body["data"].get("emails") or body["data"].get("messages")
    if emails is None:
        emails = body.get("messages")
    if emails is None:
        emails = []
    if not isinstance(emails, list):
        emails = []

    return {
        "emails": emails,
        "email_count": len(emails),
        "next_cursor": body.get("next_cursor") or body.get("cursor") or (
            body.get("data", {}).get("next_cursor") if isinstance(body.get("data"), dict) else None
        ),
    }


async def execute_polaris_email_read(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute polaris.email.read — read inbox messages via PolarisM/Domain Rail."""
    body: dict[str, Any] = {
        "folder": payload.get("folder", "inbox"),
        "unread_only": bool(payload.get("unread_only", False)),
        "limit": int(payload.get("limit", 20)) if str(payload.get("limit", "")).strip() else 20,
    }
    if payload.get("since"):
        body["since"] = payload["since"]
    if payload.get("query"):
        body["query"] = payload["query"]

    # Endpoint fallback chain to tolerate DR path migrations.
    candidate_paths = [
        "/v1/email/read",
        "/v1/email/list",
        "/v1/email/inbox",
    ]
    last_response: DomainRailResponse | None = None
    used_path = candidate_paths[0]

    for path in candidate_paths:
        used_path = path
        try:
            resp = await _call_domain_rail(
                method="POST",
                path=path,
                body=body,
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )
            last_response = resp
            if resp.success:
                break
            if resp.status_code not in (404, 405):
                break
        except DomainRailClientError as e:
            redacted = _redact_email_read_payload(payload)
            receipt = _make_email_receipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id="polaris.email.read",
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code=e.code,
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
                redacted_inputs=redacted,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id="polaris.email.read",
                error=e.message,
                receipt_data=receipt,
            )

    if not last_response or not last_response.success:
        redacted = _redact_email_read_payload(payload)
        reason = (last_response.error if last_response else "READ_ENDPOINT_UNAVAILABLE") or "READ_ENDPOINT_UNAVAILABLE"
        receipt = _make_email_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="polaris.email.read",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=reason,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            redacted_inputs=redacted,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.read",
            error=f"Email read failed (path={used_path}): {reason}",
            receipt_data=receipt,
        )

    normalized = _normalize_email_read_response(last_response.body)
    redacted = _redact_email_read_payload(payload)
    receipt = _make_email_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="polaris.email.read",
        risk_tier=risk_tier,
        outcome=Outcome.SUCCESS,
        reason_code="EXECUTED",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        redacted_inputs=redacted,
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="polaris.email.read",
        data={**normalized, "provider_path": used_path},
        receipt_data=receipt,
    )


async def execute_polaris_email_send(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute polaris.email.send — send an email via PolarisM/Domain Rail.

    Required payload:
      - from_address: str — sender email address
      - to: str | list[str] — recipient email address(es)
      - subject: str — email subject
      - body_html: str — HTML body content
      - body_text: str — plaintext body content (fallback)

    Optional payload:
      - reply_to: str — reply-to email address

    YELLOW tier: Sends real email to external parties.
    Receipt DLP: subject, body, and addresses are redacted (Law #9).
    """
    from_address = payload.get("from_address", "")
    to = payload.get("to", "")
    subject = payload.get("subject", "")
    body_html = payload.get("body_html", "")
    body_text = payload.get("body_text", "")

    if not all([from_address, to, subject, body_html or body_text]):
        redacted = _redact_email_payload(payload)
        receipt = _make_email_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="polaris.email.send",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            redacted_inputs=redacted,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.send",
            error="Missing required parameters: from_address, to, subject, body (html or text)",
            receipt_data=receipt,
        )

    # Build request body for Domain Rail
    body: dict[str, Any] = {
        "from_address": from_address,
        "to": to,
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
    }

    if payload.get("reply_to"):
        body["reply_to"] = payload["reply_to"]

    try:
        response = await _call_domain_rail(
            method="POST",
            path="/v1/email/send",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    except DomainRailClientError as e:
        redacted = _redact_email_payload(payload)
        receipt = _make_email_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="polaris.email.send",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            redacted_inputs=redacted,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.send",
            error=e.message,
            receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (response.error or "FAILED")

    redacted = _redact_email_payload(payload)
    receipt = _make_email_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="polaris.email.send",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        redacted_inputs=redacted,
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="polaris.email.send",
            data={
                "message_id": response.body.get("message_id", ""),
                "status": response.body.get("status", "sent"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.send",
            error=response.error or f"Email send failed: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_polaris_email_draft(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute polaris.email.draft — create an email draft via PolarisM/Domain Rail.

    Required payload:
      - from_address: str — sender email address
      - to: str | list[str] — recipient email address(es)
      - subject: str — email subject
      - body_html: str — HTML body content
      - body_text: str — plaintext body content (fallback)

    YELLOW tier: Creates a draft (does not send).
    Receipt DLP: subject, body, and addresses are redacted (Law #9).
    """
    from_address = payload.get("from_address", "")
    to = payload.get("to", "")
    subject = payload.get("subject", "")
    body_html = payload.get("body_html", "")
    body_text = payload.get("body_text", "")

    if not all([from_address, to, subject, body_html or body_text]):
        redacted = _redact_email_payload(payload)
        receipt = _make_email_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="polaris.email.draft",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            redacted_inputs=redacted,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.draft",
            error="Missing required parameters: from_address, to, subject, body (html or text)",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "from_address": from_address,
        "to": to,
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
    }

    try:
        response = await _call_domain_rail(
            method="POST",
            path="/v1/email/draft",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    except DomainRailClientError as e:
        redacted = _redact_email_payload(payload)
        receipt = _make_email_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="polaris.email.draft",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=e.code,
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            redacted_inputs=redacted,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.draft",
            error=e.message,
            receipt_data=receipt,
        )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (response.error or "FAILED")

    redacted = _redact_email_payload(payload)
    receipt = _make_email_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="polaris.email.draft",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        redacted_inputs=redacted,
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="polaris.email.draft",
            data={
                "draft_id": response.body.get("draft_id", ""),
                "status": response.body.get("status", "draft"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="polaris.email.draft",
            error=response.error or f"Email draft failed: HTTP {response.status_code}",
            receipt_data=receipt,
        )
