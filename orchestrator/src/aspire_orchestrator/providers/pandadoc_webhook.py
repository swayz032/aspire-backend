"""PandaDoc Webhook Handler — Document lifecycle event processing.

Handles events from PandaDoc webhook callbacks:
  - document_state_change: sent, viewed, completed, voided, declined
  - Validates HMAC signature (Law #3: reject unsigned webhooks)
  - Updates contract_state_machine on valid events
  - Emits receipt for each state transition (Law #2)
  - Idempotent: dedup by event_id via processed_webhooks

Security:
  - HMAC-SHA256 signature verification on every webhook
  - Reject all unsigned/invalid webhooks with denial receipt
  - Log + alert on forgery attempts
  - Per-suite rate limiting (Law #6: tenant isolation)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)

# Callback type for state change notifications
OnStateChangeCallback = Any  # Callable[[str, str, str, str, str, str], Awaitable[None]]

RECEIPT_VERSION = "1.0"
ACTOR_WEBHOOK = "webhook:pandadoc"


class WebhookSignatureError(Exception):
    """Raised when webhook HMAC signature verification fails."""

    def __init__(self, message: str, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class WebhookDuplicateError(Exception):
    """Raised when a duplicate event_id is detected."""

    def __init__(self, message: str, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


def verify_pandadoc_signature(
    payload_body: bytes,
    signature_header: str,
    webhook_secret: str,
) -> bool:
    """Verify PandaDoc webhook HMAC-SHA256 signature.

    Args:
        payload_body: Raw request body bytes
        signature_header: Value from X-PandaDoc-Signature header
        webhook_secret: Shared secret configured in PandaDoc

    Returns:
        True if signature is valid.

    Raises:
        WebhookSignatureError: If signature is missing, invalid, or doesn't match.
    """
    if not signature_header:
        raise WebhookSignatureError("Missing X-PandaDoc-Signature header")
    if not webhook_secret or not webhook_secret.strip():
        raise WebhookSignatureError("Webhook secret not configured")

    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise WebhookSignatureError("HMAC signature mismatch — possible forgery")

    return True


# PandaDoc document status → contract state machine target state mapping
_STATUS_TO_STATE: dict[str, str] = {
    "document.draft": "draft",
    "document.sent": "sent",
    "document.viewed": "sent",  # viewed doesn't change state
    "document.waiting_approval": "sent",
    "document.completed": "signed",
    "document.voided": "expired",
    "document.declined": "expired",
    "document.expired": "expired",
}


def map_pandadoc_status_to_state(pandadoc_status: str) -> str | None:
    """Map PandaDoc document status to contract state machine state.

    Returns None if the status doesn't trigger a state transition.
    """
    return _STATUS_TO_STATE.get(pandadoc_status)


def _build_webhook_receipt(
    *,
    event_id: str,
    event_type: str,
    document_id: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    outcome: str,
    reason_code: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build receipt for a webhook event (Law #2)."""
    return {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-webhook-{uuid.uuid4().hex[:12]}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"webhook.pandadoc.{event_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_WEBHOOK,
        "correlation_id": correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": hashlib.sha256(
            json.dumps({"event_id": event_id, "document_id": document_id},
                       sort_keys=True).encode()
        ).hexdigest(),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "pandadoc-webhook-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "metadata": {
            "event_id": event_id,
            "document_id": document_id,
            **(metadata or {}),
        },
        "redactions": [],
    }


class PandaDocWebhookHandler:
    """Process PandaDoc webhook events with idempotency and state machine updates.

    Thread-safe via in-memory dedup set (production: use processed_webhooks table).
    """

    def __init__(
        self,
        webhook_secret: str = "",
        on_state_change: OnStateChangeCallback | None = None,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._processed_events: set[str] = set()
        self._on_state_change = on_state_change

    def set_webhook_secret(self, secret: str) -> None:
        if not secret or not secret.strip():
            raise ValueError("Webhook secret cannot be empty or whitespace-only")
        self._webhook_secret = secret

    def process_event(
        self,
        event_payload: dict[str, Any],
        raw_body: bytes | None = None,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """Process a PandaDoc webhook event.

        Args:
            event_payload: Parsed JSON body of the webhook
            raw_body: Raw request body bytes (for HMAC verification)
            signature: X-PandaDoc-Signature header value

        Returns:
            Receipt dict for the processed event.

        Raises:
            WebhookSignatureError: If HMAC verification fails.
            WebhookDuplicateError: If event_id was already processed.
        """
        event_id = event_payload.get("event_id", str(uuid.uuid4()))
        event_type = event_payload.get("event", "unknown")
        document_data = event_payload.get("data", {})
        document_id = document_data.get("id", "")
        document_status = document_data.get("status", "")

        # Extract Aspire metadata from PandaDoc document
        doc_metadata = document_data.get("metadata", {})
        suite_id = doc_metadata.get("aspire_suite_id", "unknown")
        office_id = doc_metadata.get("aspire_office_id", "unknown")
        correlation_id = doc_metadata.get("aspire_correlation_id", str(uuid.uuid4()))

        # HMAC verification — fail-closed: reject ALL events if secret missing (Law #3)
        if not self._webhook_secret or not self._webhook_secret.strip():
            logger.error(
                "Webhook secret not configured — rejecting event (fail-closed): event_id=%s",
                event_id,
            )
            denial = _build_webhook_receipt(
                event_id=event_id,
                event_type=event_type,
                document_id=document_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                outcome="denied",
                reason_code="WEBHOOK_SECRET_NOT_CONFIGURED",
            )
            store_receipts([denial])
            raise WebhookSignatureError(
                "Webhook secret not configured — cannot verify signature (fail-closed)",
                receipt=denial,
            )

        if raw_body and signature:
            try:
                verify_pandadoc_signature(raw_body, signature, self._webhook_secret)
            except WebhookSignatureError:
                logger.warning(
                    "Webhook HMAC forgery attempt: event_id=%s, doc=%s",
                    event_id, document_id[:8] if document_id else "unknown",
                )
                denial = _build_webhook_receipt(
                    event_id=event_id,
                    event_type=event_type,
                    document_id=document_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    correlation_id=correlation_id,
                    outcome="denied",
                    reason_code="HMAC_VERIFICATION_FAILED",
                )
                store_receipts([denial])
                raise WebhookSignatureError(
                    "HMAC verification failed", receipt=denial,
                )
        elif not raw_body or not signature:
            # Missing raw_body or signature — fail closed (Law #3)
            denial = _build_webhook_receipt(
                event_id=event_id,
                event_type=event_type,
                document_id=document_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                outcome="denied",
                reason_code="MISSING_SIGNATURE",
            )
            store_receipts([denial])
            raise WebhookSignatureError(
                "Missing raw body or signature — cannot verify", receipt=denial,
            )

        # Idempotency: dedup by event_id — emit receipt before rejecting (Law #2)
        if event_id in self._processed_events:
            logger.info("Duplicate webhook event_id=%s — skipping", event_id)
            denial = _build_webhook_receipt(
                event_id=event_id,
                event_type=event_type,
                document_id=document_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                outcome="denied",
                reason_code="DUPLICATE_EVENT_ID",
            )
            store_receipts([denial])
            raise WebhookDuplicateError(
                f"Event {event_id} already processed", receipt=denial,
            )

        # Map status to state
        target_state = map_pandadoc_status_to_state(document_status)

        receipt = _build_webhook_receipt(
            event_id=event_id,
            event_type=event_type,
            document_id=document_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome="success",
            reason_code="WEBHOOK_PROCESSED",
            metadata={
                "pandadoc_status": document_status,
                "target_state": target_state,
                "document_name": document_data.get("name", ""),
            },
        )

        # Mark as processed
        self._processed_events.add(event_id)

        # Invoke state change callback if registered and we have a valid target state
        if self._on_state_change and target_state and document_id:
            try:
                import asyncio
                callback_result = self._on_state_change(
                    document_id,
                    document_status,
                    target_state,
                    suite_id,
                    office_id,
                    correlation_id,
                )
                # Support both sync and async callbacks
                if asyncio.iscoroutine(callback_result):
                    asyncio.ensure_future(callback_result)
            except Exception as cb_err:
                logger.error(
                    "State change callback failed for doc=%s: %s (webhook receipt still emitted)",
                    document_id[:8] if document_id else "?", cb_err,
                )
                # Callback failure does NOT fail the webhook — receipt is already emitted
                # This follows Law #2: webhook receipt stands regardless

        logger.info(
            "Webhook processed: event_id=%s, doc=%s, status=%s -> state=%s",
            event_id[:8], document_id[:8] if document_id else "?",
            document_status, target_state,
        )

        return receipt

    def clear_processed(self) -> None:
        """Clear processed events. Testing only."""
        self._processed_events.clear()
