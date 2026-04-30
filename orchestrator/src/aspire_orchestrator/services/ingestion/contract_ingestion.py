"""PandaDoc contract ingestion — `document_state_changed` events → `memory_objects`
of type `contract`.

Pass 14 expansion adapter. Mirrors QuoteIngestionAdapter but handles the full
contract lifecycle: draft → sent → viewed → completed (signed) → rejected /
expired / voided.

The pandadoc_webhook route dispatches here when `data.tags` contains 'contract'
(or template_uuid matches a known contract template). Quote dispatch is the
default fallback — this adapter handles only contract-tagged documents.

Scope resolution: `provider_connections`
  (`provider='pandadoc'`, `external_account_id=workspace_id`) → tenant_id.

Memory is append-only (Law #2). Each state transition has a distinct
idempotency_key: `pandadoc-contract-{document_id}-{action}` so every state
produces a unique row. No UPDATEs ever.

memory_type = 'contract' per migration 103 / plan §14 expansion.
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

# PandaDoc document statuses that produce a contract memory_object
_HANDLED_STATES = frozenset({
    "document.draft",
    "document.sent",
    "document.viewed",
    "document.completed",
    "document.rejected",
    "document.expired",
    "document.voided",
    # Also handle without prefix (PandaDoc inconsistency between webhook vs API)
    "draft",
    "sent",
    "viewed",
    "completed",
    "rejected",
    "expired",
    "voided",
})

# Non-terminal states that produce a memory row but are not yet actionable
_SKIP_STATES = frozenset({"document.draft", "draft"})

# Map normalized state → internal MemoryStatus
_STATE_TO_STATUS: dict[str, str] = {
    "sent": "drafted",
    "viewed": "approved",    # recipient has read it — treat as acknowledged
    "completed": "executed",  # signed
    "rejected": "rejected",
    "expired": "failed",
    "voided": "failed",
}


def _normalize_state(raw: str) -> str:
    """Strip 'document.' prefix so callers always get a bare token."""
    return raw.removeprefix("document.").strip()


class ContractIngestionAdapter(BaseIngestionAdapter):
    """PandaDoc document_state_changed (contract-tagged) → `contract` memory_object."""

    provider_name = "pandadoc_contract"
    memory_type = "contract"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """PandaDoc SHA-256 HMAC of raw body, hex-encoded (same secret as quotes)."""
        sig = (
            headers.get("x-pandadoc-signature")
            or headers.get("X-PandaDoc-Signature")
            or ""
        )
        return verify_pandadoc(body, sig, settings.pandadoc_webhook_secret)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from PandaDoc workspace_id via provider_connections."""
        data = payload.get("data", {})
        workspace_id: str | None = (
            payload.get("workspace_id")
            or data.get("workspace_id")
            or (data.get("workspace", {}).get("id") if isinstance(data.get("workspace"), dict) else None)
        )
        if not workspace_id:
            raise IngestionError(
                "PandaDoc contract payload missing workspace_id",
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
        """Build a memory_objects row of type='contract'."""
        event_id: str = payload.get("event_id") or payload.get("id", "")
        action: str = payload.get("action") or payload.get("event", "document_state_changed")
        data: dict[str, Any] = payload.get("data", {})

        if not event_id:
            raise IngestionError(
                "PandaDoc contract payload missing 'event_id'/'id'",
                code="MISSING_EVENT_ID",
                status_code=422,
            )

        # Document state — either top-level or nested in data
        raw_state: str = (
            data.get("status")
            or (data.get("document", {}).get("status") if isinstance(data.get("document"), dict) else None)
            or payload.get("status")
            or ""
        )

        if raw_state not in _HANDLED_STATES:
            raise IngestionError(
                f"PandaDoc contract state '{raw_state}' not actionable — skipped",
                code="UNHANDLED_DOCUMENT_STATE",
                status_code=200,
            )

        if raw_state in _SKIP_STATES:
            raise IngestionError(
                f"PandaDoc contract draft state — no memory written",
                code="UNHANDLED_DOCUMENT_STATE",
                status_code=200,
            )

        state = _normalize_state(raw_state)
        memory_status = _STATE_TO_STATUS.get(state, "drafted")

        # Document metadata
        doc: dict[str, Any] = (
            data.get("document")
            if isinstance(data.get("document"), dict)
            else data
        )
        document_id: str = doc.get("id") or event_id
        doc_name: str = doc.get("name") or "Contract"
        tags: list[str] = doc.get("tags") or []
        template_id: str | None = doc.get("template_uuid") or doc.get("template", {}).get("id") if isinstance(doc.get("template"), dict) else doc.get("template_uuid")

        # Recipients
        recipients: list[dict[str, Any]] = doc.get("recipients") or []
        recipient_name = "Unknown"
        recipient_email = ""
        if recipients and isinstance(recipients[0], dict):
            first = recipients[0]
            recipient_name = (
                first.get("name")
                or f"{first.get('first_name', '')} {first.get('last_name', '')}".strip()
                or first.get("email", "Unknown")
            )
            recipient_email = first.get("email", "")

        # Signers with timestamps
        signers: list[dict[str, Any]] = []
        for signer in doc.get("fields") or doc.get("signers") or []:
            if not isinstance(signer, dict):
                continue
            signers.append({
                "name": signer.get("name") or signer.get("assignee", {}).get("email", ""),
                "email": signer.get("email") or signer.get("assignee", {}).get("email", ""),
                "signed_at": signer.get("date_signed") or signer.get("signed_at"),
            })

        # PDF URL
        pdf_url: str = doc.get("download_url") or doc.get("view_url") or ""

        # State-specific timestamps
        signed_at: str | None = None
        viewed_at: str | None = None
        expired_at: str | None = None

        if state == "completed":
            signed_at = doc.get("date_completed") or doc.get("completed_at") or _now_iso()
        elif state == "viewed":
            viewed_at = doc.get("date_modified") or _now_iso()
        elif state == "expired":
            expired_at = doc.get("expiration_date") or _now_iso()

        # Deterministic trace IDs
        ns = uuid.NAMESPACE_URL
        idempotency_key = f"pandadoc-contract-{document_id}-{action}"
        trace_id = uuid.uuid5(ns, f"pandadoc-contract:trace:{idempotency_key}")
        correlation_id = uuid.uuid5(ns, f"pandadoc-contract:corr:{document_id}")

        # Title + summary per state
        if state == "sent":
            title = f"Contract sent — {recipient_name}"
            summary = f"Contract '{doc_name}' sent to {recipient_name} for signature."
        elif state == "viewed":
            title = f"Contract viewed — {recipient_name}"
            summary = f"Contract '{doc_name}' viewed by {recipient_name}."
        elif state == "completed":
            signed_display = signed_at or "unknown date"
            title = f"Contract signed — {recipient_name}"
            summary = f"Contract '{doc_name}' signed by {recipient_name} on {signed_display}."
        elif state == "rejected":
            title = f"Contract rejected — {recipient_name}"
            summary = f"Contract '{doc_name}' rejected by {recipient_name}."
        elif state == "expired":
            title = f"Contract expired — {recipient_name}"
            summary = f"Contract '{doc_name}' expired without signature."
        else:  # voided
            title = f"Contract voided — {recipient_name}"
            summary = f"Contract '{doc_name}' voided."

        detail: dict[str, Any] = {
            "document_id": document_id,
            "recipient_name": recipient_name,
            "recipient_email": recipient_email,
            "signed_at": signed_at,
            "viewed_at": viewed_at,
            "expired_at": expired_at,
            "pdf_url": pdf_url,
            "signers": signers,
            "tags": tags,
            "template_id": template_id,
            "status": state,
            "document_name": doc_name,
        }

        created_raw: str | None = (
            doc.get("date_created")
            or doc.get("created_at")
            or payload.get("created_at")
        )
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
            memory_type="contract",
            entity_type="customer",
            entity_id=None,
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


__all__ = ["ContractIngestionAdapter"]
