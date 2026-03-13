"""Stripe Webhook Handler — Invoice and payment lifecycle event processing.

Handles events from Stripe Connect webhook callbacks:
  - invoice.created, invoice.paid, invoice.payment_failed, invoice.finalized
  - payment_intent.succeeded, payment_intent.payment_failed
  - Validates Stripe signature (Law #3: reject unsigned webhooks)
  - Emits receipt for each event (Law #2)
  - Idempotent: dedup by event ID via processed set
  - Tenant-scoped: extracts suite_id from Stripe metadata (Law #6)

Security:
  - Stripe-Signature header verification using stripe SDK
  - Reject all unsigned/invalid webhooks with denial receipt
  - Per-suite isolation via metadata.suite_id (Law #6)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

logger = logging.getLogger(__name__)

RECEIPT_VERSION = "1.0"
ACTOR_WEBHOOK = "webhook:stripe"


class WebhookSignatureError(Exception):
    """Raised when Stripe webhook signature verification fails."""

    def __init__(self, message: str, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class WebhookDuplicateError(Exception):
    """Raised when a duplicate event ID is detected."""

    def __init__(self, message: str, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


def verify_stripe_signature(
    payload_body: bytes,
    signature_header: str,
    webhook_secret: str,
) -> dict[str, Any]:
    """Verify Stripe webhook signature and construct event.

    Uses the official Stripe SDK for signature verification.

    Args:
        payload_body: Raw request body bytes
        signature_header: Value from Stripe-Signature header
        webhook_secret: Webhook signing secret from Stripe dashboard

    Returns:
        Parsed Stripe event dict.

    Raises:
        WebhookSignatureError: If signature is missing, invalid, or doesn't match.
    """
    if not signature_header:
        raise WebhookSignatureError("Missing Stripe-Signature header")
    if not webhook_secret or not webhook_secret.strip():
        raise WebhookSignatureError("Webhook secret not configured")

    try:
        import stripe
        event = stripe.Webhook.construct_event(
            payload_body,
            signature_header,
            webhook_secret,
        )
        return event
    except stripe.SignatureVerificationError as e:
        raise WebhookSignatureError(f"Stripe signature verification failed: {e}") from e
    except Exception as e:
        raise WebhookSignatureError(f"Failed to parse Stripe event: {e}") from e


# Stripe event type -> Aspire semantic action mapping
# Full list of 29 events subscribed in Stripe dashboard (Connected accounts)
_EVENT_TO_ACTION: dict[str, str] = {
    # Customer lifecycle
    "customer.created": "customer.created",
    "customer.updated": "customer.updated",
    "customer.deleted": "customer.deleted",
    # Invoice lifecycle
    "invoice.created": "invoice.created",
    "invoice.updated": "invoice.updated",
    "invoice.finalized": "invoice.finalized",
    "invoice.finalization_failed": "invoice.finalization_failed",
    "invoice.sent": "invoice.sent",
    "invoice.paid": "invoice.paid",
    "invoice.payment_succeeded": "invoice.payment_succeeded",
    "invoice.payment_failed": "invoice.payment_failed",
    "invoice.payment_action_required": "invoice.payment_action_required",
    "invoice.payment_attempt_required": "invoice.payment_attempt_required",
    "invoice.voided": "invoice.voided",
    "invoice.deleted": "invoice.deleted",
    "invoice.marked_uncollectible": "invoice.marked_uncollectible",
    "invoice.overdue": "invoice.overdue",
    "invoice.overpaid": "invoice.overpaid",
    "invoice.upcoming": "invoice.upcoming",
    "invoice.will_be_due": "invoice.will_be_due",
    # Invoice Payment (v2 event)
    "invoice_payment.paid": "invoice_payment.paid",
    # Invoice items
    "invoiceitem.created": "invoiceitem.created",
    "invoiceitem.deleted": "invoiceitem.deleted",
    # Quotes
    "quote.created": "quote.created",
    "quote.finalized": "quote.finalized",
    "quote.accepted": "quote.accepted",
    "quote.canceled": "quote.canceled",
    "quote.will_expire": "quote.will_expire",
}


def _build_webhook_receipt(
    *,
    event_id: str,
    event_type: str,
    stripe_object_id: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    outcome: str,
    reason_code: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build receipt for a Stripe webhook event (Law #2)."""
    return {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-stripe-wh-{uuid.uuid4().hex[:12]}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": f"webhook.stripe.{event_type}",
        "suite_id": suite_id,
        "office_id": office_id,
        "actor": ACTOR_WEBHOOK,
        "correlation_id": correlation_id,
        "status": "ok" if outcome == "success" else outcome,
        "inputs_hash": hashlib.sha256(
            json.dumps(
                {"event_id": event_id, "object_id": stripe_object_id},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "policy": {
            "decision": "allow" if outcome == "success" else "deny",
            "policy_id": "stripe-webhook-v1",
            "reasons": [] if outcome == "success" else [reason_code],
        },
        "metadata": {
            "event_id": event_id,
            "stripe_object_id": stripe_object_id,
            "stripe_event_type": event_type,
            **(metadata or {}),
        },
        "redactions": [],
    }


class StripeWebhookHandler:
    """Process Stripe webhook events with idempotency and receipt emission.

    Thread-safe via in-memory dedup set (production: use processed_webhooks table).
    """

    def __init__(self, webhook_secret: str = "") -> None:
        self._webhook_secret = webhook_secret
        self._processed_events: set[str] = set()
        # Cap dedup set to prevent unbounded memory growth
        self._max_processed: int = 10_000

    def set_webhook_secret(self, secret: str) -> None:
        if not secret or not secret.strip():
            raise ValueError("Webhook secret cannot be empty or whitespace-only")
        self._webhook_secret = secret

    def process_event(
        self,
        raw_body: bytes,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """Process a Stripe webhook event.

        Args:
            raw_body: Raw request body bytes
            signature: Stripe-Signature header value

        Returns:
            Receipt dict for the processed event.

        Raises:
            WebhookSignatureError: If signature verification fails.
            WebhookDuplicateError: If event was already processed.
        """
        # Fail-closed: reject if secret not configured (Law #3)
        if not self._webhook_secret or not self._webhook_secret.strip():
            logger.error("Stripe webhook secret not configured — rejecting event (fail-closed)")
            denial = _build_webhook_receipt(
                event_id="unknown",
                event_type="unknown",
                stripe_object_id="unknown",
                suite_id="unknown",
                office_id="unknown",
                correlation_id=str(uuid.uuid4()),
                outcome="denied",
                reason_code="WEBHOOK_SECRET_NOT_CONFIGURED",
            )
            store_receipts([denial])
            raise WebhookSignatureError(
                "Stripe webhook secret not configured (fail-closed)",
                receipt=denial,
            )

        # Fail-closed: reject if signature missing (Law #3)
        if not signature:
            denial = _build_webhook_receipt(
                event_id="unknown",
                event_type="unknown",
                stripe_object_id="unknown",
                suite_id="unknown",
                office_id="unknown",
                correlation_id=str(uuid.uuid4()),
                outcome="denied",
                reason_code="MISSING_SIGNATURE",
            )
            store_receipts([denial])
            raise WebhookSignatureError(
                "Missing Stripe-Signature header (fail-closed)",
                receipt=denial,
            )

        # Verify signature using Stripe SDK
        try:
            event = verify_stripe_signature(raw_body, signature, self._webhook_secret)
        except WebhookSignatureError:
            logger.warning("Stripe webhook signature verification failed — possible forgery")
            denial = _build_webhook_receipt(
                event_id="unknown",
                event_type="unknown",
                stripe_object_id="unknown",
                suite_id="unknown",
                office_id="unknown",
                correlation_id=str(uuid.uuid4()),
                outcome="denied",
                reason_code="SIGNATURE_VERIFICATION_FAILED",
            )
            store_receipts([denial])
            raise

        # Extract event details
        event_id = event.get("id", str(uuid.uuid4()))
        event_type = event.get("type", "unknown")
        event_data = event.get("data", {}).get("object", {})
        stripe_object_id = event_data.get("id", "")
        account_id = event.get("account", "")  # Connected account ID

        # Extract Aspire metadata from Stripe object metadata
        obj_metadata = event_data.get("metadata", {})
        suite_id = obj_metadata.get("aspire_suite_id", "unknown")
        office_id = obj_metadata.get("aspire_office_id", "unknown")
        correlation_id = obj_metadata.get("aspire_correlation_id", str(uuid.uuid4()))

        # Idempotency: dedup by event_id (Law #2 — emit receipt even for dups)
        if event_id in self._processed_events:
            logger.info("Duplicate Stripe event %s — skipping", event_id)
            dup_receipt = _build_webhook_receipt(
                event_id=event_id,
                event_type=event_type,
                stripe_object_id=stripe_object_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
                outcome="denied",
                reason_code="DUPLICATE_EVENT_ID",
            )
            store_receipts([dup_receipt])
            raise WebhookDuplicateError(
                f"Event {event_id} already processed", receipt=dup_receipt,
            )

        # Map event type to Aspire action
        aspire_action = _EVENT_TO_ACTION.get(event_type, event_type)

        # Build success receipt
        receipt = _build_webhook_receipt(
            event_id=event_id,
            event_type=event_type,
            stripe_object_id=stripe_object_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome="success",
            reason_code="WEBHOOK_PROCESSED",
            metadata={
                "aspire_action": aspire_action,
                "connected_account": account_id,
                "amount": event_data.get("amount_due") or event_data.get("amount"),
                "currency": event_data.get("currency"),
                "status": event_data.get("status"),
                "customer": event_data.get("customer"),
            },
        )

        # Mark as processed (with cap to prevent memory leak)
        if len(self._processed_events) >= self._max_processed:
            # Evict oldest half (simple strategy — production uses DB)
            to_keep = list(self._processed_events)[self._max_processed // 2:]
            self._processed_events = set(to_keep)
        self._processed_events.add(event_id)

        # Store receipt
        store_receipts([receipt])

        logger.info(
            "Stripe webhook processed: event=%s type=%s obj=%s suite=%s action=%s",
            event_id[:12],
            event_type,
            stripe_object_id[:12] if stripe_object_id else "?",
            suite_id[:12] if suite_id != "unknown" else "?",
            aspire_action,
        )

        return receipt

    def clear_processed(self) -> None:
        """Clear processed events. Testing only."""
        self._processed_events.clear()
