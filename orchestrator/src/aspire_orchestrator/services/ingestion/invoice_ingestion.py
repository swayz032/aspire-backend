"""Stripe invoice ingestion — `invoice.created` / `invoice.paid` / `invoice.voided`
→ `memory_objects` of type `invoice`.

Pass 14 Lane A adapter. Follow sms_ingestion.py pattern exactly.

Stripe webhook payload is JSON. The route parses it with `request.json()` and
passes the parsed dict as `payload`. Raw bytes are still read inside the
dispatch helper for HMAC verification.

Scope resolution: `provider_connections` table
  (`provider='stripe'`, `external_account_id=customer_id`) → tenant_id.
If the customer is not linked, raises `UNKNOWN_CUSTOMER` 404 (fail-closed,
Law #3). Webhook providers must always be resolvable to a tenant.

Memory is append-only (Law #2). `invoice.paid` and `invoice.voided` each
create a NEW memory_object with `status='executed'` and a reference back to
the original via `idempotency_key` embedding the supersede intent. No UPDATEs.

memory_type = 'invoice' per migration 101 / plan §14.C.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
    ThreadOut,
)
from aspire_orchestrator.services.ingestion.base import (
    BaseIngestionAdapter,
    IngestionError,
)
from aspire_orchestrator.services.ingestion.signatures import verify_stripe
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

# Stripe event types handled by this adapter
_HANDLED_EVENTS = frozenset({"invoice.created", "invoice.paid", "invoice.voided"})


class InvoiceIngestionAdapter(BaseIngestionAdapter):
    """Stripe invoice.* → `invoice` memory_object."""

    provider_name = "stripe"
    memory_type = "invoice"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Stripe HMAC SHA-256 timestamp signature (t=...,v1=...)."""
        sig = (
            headers.get("stripe-signature")
            or headers.get("Stripe-Signature")
            or ""
        )
        return verify_stripe(body, sig, settings.stripe_webhook_secret)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from Stripe customer_id via provider_connections."""
        invoice_obj = payload.get("data", {}).get("object", {})
        customer_id: str | None = invoice_obj.get("customer")
        if not customer_id:
            raise IngestionError(
                "Stripe invoice payload missing data.object.customer",
                code="MISSING_CUSTOMER_ID",
                status_code=422,
            )
        try:
            rows = await supabase_select(
                table="provider_connections",
                filters={
                    "provider": "stripe",
                    "external_account_id": customer_id,
                },
                limit=1,
            )
        except SupabaseClientError as exc:
            raise IngestionError(
                f"provider_connections query failed: {exc.detail}",
                code="PROVIDER_CONNECTIONS_UNAVAILABLE",
                status_code=503,
            ) from exc
        if not rows:
            raise IngestionError(
                f"Stripe customer {customer_id} not linked to any tenant",
                code="UNKNOWN_CUSTOMER",
                status_code=404,
            )
        row = rows[0]
        return ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build a memory_objects row of type='invoice'."""
        event_type: str = payload.get("type", "")
        event_id: str = payload.get("id", "")
        invoice_obj: dict[str, Any] = payload.get("data", {}).get("object", {})

        if not event_id:
            raise IngestionError(
                "Stripe payload missing top-level 'id'",
                code="MISSING_EVENT_ID",
                status_code=422,
            )
        if event_type not in _HANDLED_EVENTS:
            raise IngestionError(
                f"Unhandled Stripe event type: {event_type}",
                code="UNHANDLED_EVENT_TYPE",
                status_code=422,
            )

        # Core invoice fields
        invoice_number: str = invoice_obj.get("number") or invoice_obj.get("id", "")
        customer_name: str = (
            invoice_obj.get("customer_name")
            or invoice_obj.get("customer_email")
            or invoice_obj.get("customer", "Unknown")
        )
        amount_cents: int = invoice_obj.get("amount_due") or invoice_obj.get("total", 0)
        due_date: str | None = _ts_to_iso(invoice_obj.get("due_date"))
        pdf_url: str = invoice_obj.get("invoice_pdf") or invoice_obj.get("hosted_invoice_url") or ""

        # Line items
        lines_data: dict[str, Any] = invoice_obj.get("lines", {})
        line_items: list[dict[str, Any]] = []
        for line in lines_data.get("data", []):
            line_items.append({
                "description": line.get("description") or line.get("type", ""),
                "amount": line.get("amount", 0),
                "quantity": line.get("quantity"),
            })

        # Deterministic trace IDs scoped to event_id
        ns = uuid.NAMESPACE_URL
        trace_id = uuid.uuid5(ns, f"stripe-invoice:trace:{event_id}")
        correlation_id = uuid.uuid5(ns, f"stripe-invoice:corr:{event_id}")

        # Per-event differentiation
        if event_type == "invoice.created":
            status_val = "drafted"
            idempotency_key = f"stripe-invoice-{event_id}"
            title = f"Invoice {invoice_number} — {customer_name}"
            amount_dollars = amount_cents / 100
            due_str = due_date or "no due date"
            desc = _first_line_description(line_items)
            summary = f"Invoice ${amount_dollars:,.2f} for {desc} due {due_str}"
            detail: dict[str, Any] = {
                "event_type": event_type,
                "invoice_number": invoice_number,
                "entity": customer_name,
                "amount": amount_cents,
                "due_date": due_date,
                "status": "draft",
                "line_items": line_items,
                "pdf_url": pdf_url,
            }

        elif event_type == "invoice.paid":
            status_val = "executed"
            idempotency_key = f"stripe-invoice-paid-{event_id}"
            title = f"Invoice {invoice_number} — {customer_name}"
            amount_dollars = amount_cents / 100
            paid_at = _ts_to_iso(invoice_obj.get("status_transitions", {}).get("paid_at"))
            payment_intent = invoice_obj.get("payment_intent") or ""
            charge_id = invoice_obj.get("charge") or ""
            summary = f"Paid ${amount_dollars:,.2f} by {customer_name} on {paid_at or 'unknown date'}"
            detail = {
                "event_type": event_type,
                "invoice_number": invoice_number,
                "entity": customer_name,
                "amount": amount_cents,
                "status": "paid",
                "paid_at": paid_at,
                "payment_method": charge_id,
                "transaction_id": payment_intent,
                "line_items": line_items,
                "pdf_url": pdf_url,
                "supersedes_idempotency_key": f"stripe-invoice-{event_id}",
            }

        else:  # invoice.voided
            status_val = "executed"
            idempotency_key = f"stripe-invoice-voided-{event_id}"
            title = f"Invoice {invoice_number} — {customer_name} (voided)"
            voided_at = _ts_to_iso(invoice_obj.get("status_transitions", {}).get("voided_at"))
            void_reason = invoice_obj.get("void_reason") or "unspecified"
            summary = f"Invoice ${amount_cents / 100:,.2f} voided: {void_reason}"
            detail = {
                "event_type": event_type,
                "invoice_number": invoice_number,
                "entity": customer_name,
                "amount": amount_cents,
                "status": "voided",
                "voided_at": voided_at,
                "void_reason": void_reason,
                "pdf_url": pdf_url,
                "supersedes_idempotency_key": f"stripe-invoice-{event_id}",
            }

        # event_at from Stripe's top-level `created` epoch
        created_epoch: int | None = payload.get("created")
        event_at = (
            datetime.fromtimestamp(created_epoch, tz=timezone.utc)
            if created_epoch
            else datetime.now(timezone.utc)
        )

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="finn_finance",
                runtime_family="provider_webhook",
                channel="finance",
                source_record_id=event_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="invoice",
            entity_type="customer",
            entity_id=None,  # Pass 16 will resolve to contact UUID
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail=detail,
            confidence=None,
            visibility_scope="finance",
            status=status_val,
            event_at=event_at,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _ts_to_iso(epoch: int | None) -> str | None:
    """Convert a Unix epoch int to ISO-8601 string, or None if missing."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _first_line_description(line_items: list[dict[str, Any]]) -> str:
    """Return first line item description, fallback to 'services'."""
    if line_items and line_items[0].get("description"):
        return str(line_items[0]["description"])
    return "services"


__all__ = ["InvoiceIngestionAdapter"]
