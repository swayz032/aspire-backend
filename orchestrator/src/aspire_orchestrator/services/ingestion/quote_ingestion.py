"""PandaDoc quote ingestion — `document_state_changed` events → `memory_objects`
of type `quote`.

Pass 14 Lane A adapter. Follow sms_ingestion.py pattern exactly.

PandaDoc webhook payload is JSON. The route parses it with `request.json()` and
passes the parsed dict as `payload`. Raw bytes are still read inside the
dispatch helper for HMAC verification.

Handles document states: sent, viewed, completed (accepted), declined (rejected).
Other states (e.g. draft) are silently acknowledged with 200 + no memory write
by raising IngestionError(status_code=200) — this prevents PandaDoc from
retrying non-actionable events.

Scope resolution: `provider_connections` table
  (`provider='pandadoc'`, `external_account_id=workspace_id`) → tenant_id.
If the workspace is not linked, raises `UNKNOWN_WORKSPACE` 404 (fail-closed).

Memory is append-only (Law #2). Every state change that matters creates a NEW
memory_object. No UPDATEs. Status transitions are captured via distinct
idempotency_key per event: `pandadoc-{event_id}-{action}`.

memory_type = 'quote' per migration 101 / plan §14.C.
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
from aspire_orchestrator.services.ingestion.signatures import verify_pandadoc
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

# PandaDoc document states that produce a memory_object
_HANDLED_STATES = frozenset({"sent", "viewed", "completed", "declined"})

# Map PandaDoc state → internal quote status label
_STATE_TO_STATUS: dict[str, str] = {
    "sent": "drafted",        # quote is out → drafted (pending response)
    "viewed": "drafted",      # still in-flight, just opened
    "completed": "executed",  # accepted / signed
    "declined": "rejected",   # explicitly rejected
}


class QuoteIngestionAdapter(BaseIngestionAdapter):
    """PandaDoc document_state_changed → `quote` memory_object."""

    provider_name = "pandadoc"
    memory_type = "quote"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """PandaDoc SHA-256 HMAC of raw body, hex-encoded."""
        sig = (
            headers.get("x-pandadoc-signature")
            or headers.get("X-PandaDoc-Signature")
            or ""
        )
        return verify_pandadoc(body, sig, settings.pandadoc_webhook_secret)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from PandaDoc workspace_id via provider_connections."""
        data = payload.get("data", {})
        # PandaDoc webhooks include workspace_id at the top level or in data
        workspace_id: str | None = (
            payload.get("workspace_id")
            or data.get("workspace_id")
            or data.get("workspace", {}).get("id")
            if isinstance(data.get("workspace"), dict)
            else payload.get("workspace_id") or data.get("workspace_id")
        )
        if not workspace_id:
            raise IngestionError(
                "PandaDoc payload missing workspace_id",
                code="MISSING_WORKSPACE_ID",
                status_code=422,
            )
        try:
            rows = await supabase_select(
                table="provider_connections",
                filters={
                    "provider": "pandadoc",
                    "external_account_id": workspace_id,
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
                f"PandaDoc workspace {workspace_id} not linked to any tenant",
                code="UNKNOWN_WORKSPACE",
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
        """Build a memory_objects row of type='quote'."""
        event_id: str = payload.get("event_id") or payload.get("id", "")
        action: str = payload.get("action") or payload.get("event", "document_state_changed")
        data: dict[str, Any] = payload.get("data", {})

        if not event_id:
            raise IngestionError(
                "PandaDoc payload missing 'event_id'/'id'",
                code="MISSING_EVENT_ID",
                status_code=422,
            )

        # Document state — either top-level or nested in data
        doc_state: str = (
            data.get("status")
            or data.get("document", {}).get("status") if isinstance(data.get("document"), dict) else data.get("status")
            or payload.get("status", "")
        ) or ""

        if doc_state not in _HANDLED_STATES:
            # Non-actionable states (draft, approval_not_needed, etc.) — return
            # a graceful 200 so PandaDoc stops retrying.
            raise IngestionError(
                f"PandaDoc state '{doc_state}' not actionable — skipped",
                code="UNHANDLED_DOCUMENT_STATE",
                status_code=200,
            )

        # Document metadata
        doc: dict[str, Any] = data.get("document") if isinstance(data.get("document"), dict) else data
        quote_number: str = doc.get("id") or event_id
        doc_name: str = doc.get("name") or "Quote"

        # Recipient: first recipient in recipients list
        recipients: list[dict[str, Any]] = doc.get("recipients") or []
        recipient_name: str = "Unknown"
        recipient_email: str = ""
        if recipients:
            first = recipients[0] if isinstance(recipients[0], dict) else {}
            recipient_name = (
                first.get("name")
                or f"{first.get('first_name', '')} {first.get('last_name', '')}".strip()
                or first.get("email", "Unknown")
            )
            recipient_email = first.get("email", "")

        # Amount
        grand_total: float = 0.0
        pricing = doc.get("grand_total") or doc.get("pricing")
        if isinstance(pricing, (int, float)):
            grand_total = float(pricing)
        elif isinstance(pricing, dict):
            grand_total = float(pricing.get("amount") or pricing.get("total") or 0)

        # Expiration
        expiration: str | None = doc.get("expiration_date") or doc.get("valid_till")

        # PDF URL
        pdf_url: str = doc.get("view_url") or doc.get("download_url") or ""

        # Line items
        line_items: list[dict[str, Any]] = []
        for item in doc.get("items") or doc.get("line_items") or []:
            if not isinstance(item, dict):
                continue
            line_items.append({
                "description": item.get("name") or item.get("description") or "",
                "amount": item.get("price") or item.get("total") or 0,
                "quantity": item.get("qty") or item.get("quantity") or 1,
            })

        # Deterministic trace IDs
        ns = uuid.NAMESPACE_URL
        idempotency_key = f"pandadoc-{event_id}-{action}"
        trace_id = uuid.uuid5(ns, f"pandadoc-quote:trace:{idempotency_key}")
        correlation_id = uuid.uuid5(ns, f"pandadoc-quote:corr:{event_id}")

        # State-aware title + summary + extra detail fields
        memory_status = _STATE_TO_STATUS.get(doc_state, "drafted")
        amount_str = f"${grand_total:,.2f}"

        if doc_state == "sent":
            title = f"Quote {quote_number} — {recipient_name}"
            summary = f"Quote sent for {amount_str} to {recipient_name}"
            extra: dict[str, Any] = {}
        elif doc_state == "viewed":
            title = f"Quote {quote_number} — {recipient_name}"
            summary = f"Quote viewed by {recipient_name}"
            extra = {
                "viewed_at": _now_iso(),
                "viewer_email": recipient_email,
            }
        elif doc_state == "completed":
            title = f"Quote {quote_number} — {recipient_name} (accepted)"
            summary = f"Quote accepted for {amount_str} by {recipient_name}"
            extra = {
                "decided_at": _now_iso(),
                "decision": "accepted",
                "decision_note": doc.get("grand_total_formatted") or "",
            }
        else:  # declined
            title = f"Quote {quote_number} — {recipient_name} (declined)"
            summary = f"Quote declined by {recipient_name}"
            extra = {
                "decided_at": _now_iso(),
                "decision": "rejected",
                "decision_note": "",
            }

        detail: dict[str, Any] = {
            "event_type": action,
            "quote_number": quote_number,
            "document_name": doc_name,
            "entity": recipient_name,
            "amount": grand_total,
            "expiration": expiration,
            "status": doc_state,
            "line_items": line_items,
            "pdf_url": pdf_url,
            **extra,
        }

        # event_at from payload created_at or now
        created_raw: str | None = doc.get("created") or doc.get("created_at") or payload.get("created_at")
        event_at = _parse_iso(created_raw) or datetime.now(timezone.utc)

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="estimate_studio",
                runtime_family="provider_webhook",
                channel="finance",
                source_record_id=event_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="quote",
            entity_type="customer",
            entity_id=None,  # Pass 16 will resolve to contact UUID
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail=detail,
            confidence=None,
            visibility_scope="finance",
            status=memory_status,
            event_at=event_at,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


__all__ = ["QuoteIngestionAdapter"]
