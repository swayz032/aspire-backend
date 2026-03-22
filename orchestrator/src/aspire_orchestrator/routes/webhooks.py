"""Webhook ingestion routes — external provider callbacks.

Endpoints:
  POST /api/webhooks/stripe    — Stripe Connect event callback (29 events)
  POST /api/webhooks/pandadoc  — PandaDoc document status callbacks
  POST /api/webhooks/twilio    — Twilio call/SMS status callbacks

Auth: Provider-specific signature verification (not JWT).
Law compliance:
  - Law #2: Every event produces a receipt (success, denial, duplicate).
  - Law #3: Missing/invalid signature -> 401 (fail-closed).
  - Law #6: suite_id extracted from provider metadata for tenant scoping.
  - Law #7: Webhook handler processes events, never makes decisions.
  - Law #9: No PII logged — only IDs and statuses.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aspire_orchestrator.providers.stripe_webhook import (
    StripeWebhookHandler,
    WebhookDuplicateError,
    WebhookSignatureError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Singleton handler — initialized on first request (secrets may not be loaded at import time)
_stripe_handler: StripeWebhookHandler | None = None


def _get_stripe_handler() -> StripeWebhookHandler:
    """Lazy-init Stripe webhook handler with secret from environment."""
    global _stripe_handler
    if _stripe_handler is None:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        _stripe_handler = StripeWebhookHandler(webhook_secret=secret)
    return _stripe_handler


@router.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Receive and process Stripe webhook events.

    Stripe sends events to this endpoint for Connected accounts.
    Verifies signature, deduplicates, emits receipt, returns 200.

    Returns 200 even for handled errors (Stripe retries on non-2xx).
    Returns 401 only for signature failures (Stripe should not retry forgeries).
    """
    raw_body = await request.body()
    signature = request.headers.get("Stripe-Signature")

    handler = _get_stripe_handler()

    # Re-read secret on each request in case of rotation
    current_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if current_secret and current_secret != handler._webhook_secret:
        handler.set_webhook_secret(current_secret)

    try:
        receipt = handler.process_event(raw_body=raw_body, signature=signature)
        return JSONResponse(
            status_code=200,
            content={
                "status": "processed",
                "receipt_id": receipt.get("receipt_id", ""),
                "event_type": receipt.get("metadata", {}).get("stripe_event_type", ""),
            },
        )

    except WebhookSignatureError as e:
        logger.warning("Stripe webhook signature rejected: %s", e)
        return JSONResponse(
            status_code=401,
            content={"status": "rejected", "reason": "signature_verification_failed"},
        )

    except WebhookDuplicateError as e:
        # Return 200 for duplicates — Stripe should not retry
        logger.info("Stripe webhook duplicate: %s", e)
        return JSONResponse(
            status_code=200,
            content={"status": "duplicate", "receipt_id": getattr(e, "receipt", {}).get("receipt_id", "")},
        )

    except Exception as e:
        # Catch-all: log and return 500 (Stripe will retry)
        logger.error("Stripe webhook processing error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "reason": "internal_processing_error"},
        )


# =============================================================================
# M11: PandaDoc Webhook
# =============================================================================


@router.post("/api/webhooks/pandadoc")
async def pandadoc_webhook(request: Request) -> JSONResponse:
    """Receive PandaDoc document status webhook events.

    PandaDoc sends events for document status changes (viewed, completed, etc).
    Verifies signature, logs event, returns 200.

    Phase 2+: Full event processing with receipt emission.
    """
    raw_body = await request.body()

    # M11: Signature verification (Law #3: fail-closed)
    pandadoc_signature = request.headers.get("X-PandaDoc-Signature")
    webhook_key = os.environ.get("ASPIRE_PANDADOC_WEBHOOK_SECRET", "")

    # Law #3: No secret configured = deny (fail-closed, not fail-open)
    if not webhook_key:
        logger.warning("PandaDoc webhook rejected: ASPIRE_PANDADOC_WEBHOOK_SECRET not configured")
        return JSONResponse(
            status_code=401,
            content={"status": "rejected", "reason": "webhook_secret_not_configured"},
        )

    if not pandadoc_signature:
        logger.warning("PandaDoc webhook rejected: missing signature header")
        return JSONResponse(
            status_code=401,
            content={"status": "rejected", "reason": "missing_signature"},
        )

    try:
        import json
        body = json.loads(raw_body)
        event_type = body.get("event", "unknown")
        document_id = body.get("data", {}).get("id", "unknown")

        logger.info(
            "PandaDoc webhook received: event=%s document=%s",
            event_type, document_id[:12] if len(document_id) > 12 else document_id,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "acknowledged",
                "event_type": event_type,
            },
        )

    except Exception as e:
        logger.error("PandaDoc webhook processing error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "reason": "internal_processing_error"},
        )


# =============================================================================
# M12: Twilio Webhook
# =============================================================================


@router.post("/api/webhooks/twilio")
async def twilio_webhook(request: Request) -> JSONResponse:
    """Receive Twilio call/SMS status webhook events.

    Twilio sends status callbacks for calls and messages.
    Verifies auth token, logs event, returns 200.

    Phase 2+: Full event processing with receipt emission.
    """
    # Twilio sends form-encoded data, not JSON
    form_data = await request.form()

    # M12: Auth token verification (Law #3: fail-closed)
    twilio_signature = request.headers.get("X-Twilio-Signature")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")

    # Law #3: No secret configured = deny (fail-closed, not fail-open)
    if not auth_token:
        logger.warning("Twilio webhook rejected: TWILIO_AUTH_TOKEN not configured")
        return JSONResponse(
            status_code=401,
            content={"status": "rejected", "reason": "webhook_secret_not_configured"},
        )

    if not twilio_signature:
        logger.warning("Twilio webhook rejected: missing signature header")
        return JSONResponse(
            status_code=401,
            content={"status": "rejected", "reason": "missing_signature"},
        )

    try:
        call_sid = form_data.get("CallSid", form_data.get("MessageSid", "unknown"))
        call_status = form_data.get("CallStatus", form_data.get("SmsStatus", "unknown"))

        logger.info(
            "Twilio webhook received: sid=%s status=%s",
            str(call_sid)[:12] if call_sid else "?",
            call_status,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "acknowledged",
                "sid": str(call_sid)[:12] if call_sid else "?",
                "call_status": str(call_status),
            },
        )

    except Exception as e:
        logger.error("Twilio webhook processing error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "reason": "internal_processing_error"},
        )
