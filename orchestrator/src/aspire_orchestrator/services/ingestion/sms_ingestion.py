"""SMS ingestion — Twilio inbound SMS → `memory_objects` of type `sms_thread`.

Pass 14 reference adapter. Other 6 adapters follow the same pattern.

Twilio inbound SMS webhook is FORM-ENCODED (NOT JSON) — the route layer
parses the form into a dict before calling `ingest()`. Standard fields:

    MessageSid    SMxxxxxxxxxxxxxxxxxxxx
    AccountSid    ACxxxxxxxxxxxxxxxxxx
    From          +15551234567
    To            +12125550198
    Body          "free-text message body"
    NumMedia      "0" | "1" | ...
    MediaUrl0..N  https://api.twilio.com/.../Media/MExxxx (if MMS)
    MessageStatus "received" (for inbound)

Scope resolution depends on `tenant_phone_numbers` (Pass 16 migration). Until
Pass 16 lands, this adapter raises `SCOPE_RESOLVE_FAILED` with a clear error.

memory_type = 'sms_thread' per migration 101 / plan §14.C.

For V1 each inbound SMS produces ONE memory_object (idempotent on MessageSid).
Pass 16's `sms_messages` helper table will refactor to true thread-level
grouping — plan §16.A.
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
from aspire_orchestrator.services.ingestion.signatures import verify_twilio
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)


class SMSIngestionAdapter(BaseIngestionAdapter):
    """Twilio inbound SMS → `sms_thread` memory_object."""

    provider_name = "twilio_sms"
    memory_type = "sms_thread"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Twilio HMAC SHA-1 of full URL + sorted form params."""
        sig = headers.get("X-Twilio-Signature") or headers.get("x-twilio-signature", "")
        # Full URL — provided by FastAPI route via X-Forwarded-Host or computed
        # from request.url. We require the route to inject this via headers.
        full_url = headers.get("X-Aspire-Webhook-Url", "")
        # Form params are passed via the special header X-Aspire-Form-Params
        # (route-side serialized JSON). For tests, this is empty and signature
        # check uses url-only. Production path uses the form_params header.
        import json as _json
        params_json = headers.get("X-Aspire-Form-Params", "")
        params = _json.loads(params_json) if params_json else None
        return verify_twilio(
            full_url=full_url,
            params=params,
            sig_header=sig,
            auth_token=settings.twilio_auth_token,
        )

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from To-number lookup in tenant_phone_numbers."""
        to_number = payload.get("To") or payload.get("to")
        if not to_number:
            raise IngestionError(
                "Twilio SMS payload missing 'To' field",
                code="MISSING_TO_NUMBER",
                status_code=422,
            )
        try:
            rows = await supabase_select(
                table="tenant_phone_numbers",
                filter_="phone_number=eq." + to_number,
                limit=1,
            )
        except SupabaseClientError as exc:
            # Pass 14 → Pass 16 dependency: this table doesn't exist until
            # Pass 16 lands. Until then, every inbound SMS will hit this path.
            # The route layer maps this to 503 (service unavailable) so Twilio
            # retries automatically once the table is provisioned.
            raise IngestionError(
                f"tenant_phone_numbers query failed (Pass 16 prereq): {exc.detail}",
                code="TENANT_PHONE_NUMBERS_UNAVAILABLE",
                status_code=503,
            ) from exc
        if not rows:
            raise IngestionError(
                f"To-number {to_number} not registered in tenant_phone_numbers",
                code="UNKNOWN_NUMBER",
                status_code=404,
            )
        row = rows[0]
        return ScopedIdentity(
            tenant_id=UUID(row["tenant_id"]),
            suite_id=UUID(row["suite_id"]),
            office_id=UUID(row["office_id"]),
        )

    async def thread_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
    ) -> ThreadOut | None:
        """No thread for V1 SMS — Pass 16 introduces thread-level grouping."""
        _ = (payload, scope)
        return None

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build a memory_objects row of type='sms_thread'."""
        message_sid = payload.get("MessageSid") or payload.get("message_sid")
        from_number = payload.get("From") or payload.get("from")
        to_number = payload.get("To") or payload.get("to")
        body_text = payload.get("Body") or payload.get("body") or ""

        if not message_sid:
            raise IngestionError(
                "Twilio SMS payload missing MessageSid",
                code="MISSING_MESSAGE_SID",
                status_code=422,
            )
        if not from_number or not to_number:
            raise IngestionError(
                "Twilio SMS payload missing From/To",
                code="MISSING_PHONE_NUMBERS",
                status_code=422,
            )

        # Collect MMS media URLs if present
        num_media = int(payload.get("NumMedia", "0") or 0)
        media_urls: list[str] = []
        for i in range(num_media):
            url = payload.get(f"MediaUrl{i}")
            if url:
                media_urls.append(url)

        # Build deterministic trace IDs from MessageSid for traceability across
        # ingestion → memory → receipts pipeline.
        # MessageSid is "SM" + 32 hex chars. UUID5 keeps trace_id stable.
        ns = uuid.NAMESPACE_URL
        trace_id = uuid.uuid5(ns, f"twilio-sms:trace:{message_sid}")
        correlation_id = uuid.uuid5(ns, f"twilio-sms:corr:{message_sid}")

        title = f"SMS from {from_number}"
        # Summary trimmed to first 140 chars (one screen line in detail rail)
        summary = (body_text[:140] + "…") if len(body_text) > 140 else body_text or "(MMS or empty body)"

        envelope = MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="provider_webhook",
                channel="sms",
                source_record_id=message_sid,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="sms_thread",
            entity_type="phone_contact",
            entity_id=None,  # Pass 16 will resolve to contact UUID
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail={
                "direction": "inbound",
                "from": from_number,
                "to": to_number,
                "body": body_text,
                "message_sid": message_sid,
                "media_urls": media_urls,
                "num_media": num_media,
            },
            confidence=None,
            visibility_scope="office",
            status=None,  # SMS are not "approval-gated" — null status is fine
            event_at=datetime.now(timezone.utc),
            # Idempotency: same MessageSid + same tenant always dedup
            idempotency_key=f"twilio-sms-inbound:{message_sid}",
        )
        return envelope


__all__ = ["SMSIngestionAdapter"]
