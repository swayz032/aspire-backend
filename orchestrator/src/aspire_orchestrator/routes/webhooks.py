"""Webhook ingestion routes — external provider callbacks.

Endpoints:
  POST /api/webhooks/stripe  — Stripe Connect event callback (29 events)

Auth: Stripe-Signature header (HMAC verification, not JWT).
Law compliance:
  - Law #2: Every event produces a receipt (success, denial, duplicate).
  - Law #3: Missing/invalid signature -> 401 (fail-closed).
  - Law #6: suite_id extracted from Stripe metadata for tenant scoping.
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
