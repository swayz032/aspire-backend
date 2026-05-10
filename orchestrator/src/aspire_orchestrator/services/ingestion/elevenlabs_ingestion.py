"""ElevenLabs post-call webhook ingestion — `session_summary` + `transcript`.

Pass 14 Lane D adapter.

ElevenLabs fires a single JSON webhook per completed voice session for each of
the 6 agents (Ava, Finn, Eli, Nora, Receptionist Sarah, Front Desk Sarah):

  POST /v1/ingest/elevenlabs/post-call
  Header: ElevenLabs-Signature: t=<ts>,v0=<hmac_sha256_hex>
  Body (JSON):
    {
      "type": "post_call_transcription",
      "event_timestamp": <unix_float>,
      "data": {
        "agent_id": str,
        "conversation_id": str,
        "status": "done" | "failed" | ...,
        "transcript": [{"role": "agent"|"user", "message": str, "time_in_call_secs": float}, ...],
        "metadata": {
          "duration_secs": int,
          "called_number": str,   # E.164 — used for scope resolution first
          "caller_id": str,
          ...
        },
        "analysis": {
          "transcript_summary": str,
          "intents_detected": [...],
          ...
        },
        "conversation_initiation_client_data": {
          "dynamic_variables": {...}
        }
      }
    }

Scope resolution (two-step, fail-closed):
  1. Try `tenant_phone_numbers.phone_number = data.metadata.called_number`.
  2. Else try `provider_connections` (provider='elevenlabs', external_account_id=agent_id).
  3. If neither matches → IngestionError(UNKNOWN_AGENT_OR_NUMBER, 404).

Two memory_objects per webhook (both idempotent):
  1. `transcript` — raw turns, title=f"Transcript — {agent_id}",
     idempotency_key=f"el-transcript:{conversation_id}"
  2. `session_summary` — refined, links back to transcript via linked_memory_ids,
     idempotency_key=f"el-summary:{conversation_id}"

Trace IDs are deterministic from conversation_id so receipts chain across both
writes and across webhook replays.

Law compliance:
  Law #2: MemoryService.write cuts receipts internally.
  Law #3: bad signature → 401; missing scope → 404.
  Law #6: scope resolved from payload only (called_number or agent_id lookup).
  Law #9: transcript body NOT logged at full fidelity (first 80 chars at DEBUG).
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
    IngestionResult,
)
from aspire_orchestrator.services.ingestion.signatures import verify_elevenlabs
from aspire_orchestrator.services.memory_service import MemoryService, MemoryServiceError
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

_NS = uuid.NAMESPACE_URL


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


async def _resolve_el_scope(
    called_number: str | None,
    agent_id: str,
) -> ScopedIdentity:
    """Two-step scope resolution for ElevenLabs webhooks.

    Step 1: called_number → tenant_phone_numbers
    Step 2: agent_id → provider_connections (provider='elevenlabs')
    Fail-closed: raises IngestionError if neither resolves.
    """
    # Step 1 — phone number lookup
    if called_number:
        try:
            rows = await supabase_select(
                table="tenant_phone_numbers",
                filters={"phone_number": called_number},
                limit=1,
            )
            if rows:
                row = rows[0]
                return ScopedIdentity(
                    tenant_id=UUID(row["tenant_id"]),
                    suite_id=UUID(row["suite_id"]),
                    office_id=UUID(row["office_id"]),
                )
        except SupabaseClientError as exc:
            logger.debug(
                "el_scope_phone_lookup_failed called_number=%s error=%s",
                called_number,
                exc.detail,
            )
            # Fall through to agent_id lookup

    # Step 2 — fallback lookup by agent_id against tenant_phone_numbers.
    # The legacy provider_connections table doesn't exist in this Supabase
    # project; the canonical mapping lives on tenant_phone_numbers.
    # attached_to_agent_id (set when an agent is wired to a phone number).
    if agent_id:
        try:
            rows = await supabase_select(
                table="tenant_phone_numbers",
                filters={"attached_to_agent_id": agent_id, "status": "active"},
                limit=1,
            )
            if rows:
                row = rows[0]
                return ScopedIdentity(
                    tenant_id=UUID(row["tenant_id"]),
                    suite_id=UUID(row["suite_id"]),
                    office_id=UUID(row["office_id"]),
                )
        except SupabaseClientError as exc:
            logger.warning(
                "el_scope_agent_lookup_failed agent_id=%s error=%s",
                agent_id,
                exc.detail,
            )

    raise IngestionError(
        f"ElevenLabs agent {agent_id!r} not linked to any tenant "
        f"(called_number={called_number!r})",
        code="UNKNOWN_AGENT_OR_NUMBER",
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Stable IDs
# ---------------------------------------------------------------------------


def _stable_ids(conversation_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (trace_id, correlation_id) stable across both writes per session."""
    trace_id = uuid.uuid5(_NS, f"el-conv:trace:{conversation_id}")
    correlation_id = uuid.uuid5(_NS, f"el-conv:corr:{conversation_id}")
    return trace_id, correlation_id


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ElevenLabsIngestionAdapter(BaseIngestionAdapter):
    """ElevenLabs post_call_webhook → `transcript` + `session_summary` memory_objects.

    Overrides `ingest` to perform the two-write sequence:
      1. Write `transcript` memory_object.
      2. Write `session_summary` memory_object with linked_memory_ids=[transcript_id].

    Both writes delegate to MemoryService (receipts cut internally, Law #2).
    """

    provider_name = "elevenlabs"
    memory_type = "session_summary"  # primary type; transcript is the sibling

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """ElevenLabs timestamped HMAC SHA-256 signature."""
        sig = (
            headers.get("elevenlabs-signature")
            or headers.get("ElevenLabs-Signature")
            or ""
        )
        return verify_elevenlabs(body, sig, settings.elevenlabs_webhook_secret)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from called_number (primary) or agent_id (fallback).

        EL post-call payload shape varies by phone provider:
          - Twilio inbound: `metadata.phone_call.agent_number` is the dialed
            (Aspire) number; `external_number` is the caller.
          - SIP / generic:  `metadata.phone_call.to_number` is the called.
          - Older shape:    `metadata.called_number` at the top level.
        Try all three so we handle every provider.
        """
        data = payload.get("data", {})
        agent_id: str = data.get("agent_id", "")
        metadata = data.get("metadata", {}) or {}
        phone_call = metadata.get("phone_call", {}) or {}
        called_number: str | None = (
            phone_call.get("agent_number")
            or phone_call.get("to_number")
            or metadata.get("called_number")
        )
        return await _resolve_el_scope(called_number, agent_id)

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Not used — ingest() is overridden to perform two writes.

        Must remain implemented to satisfy the ABC contract.
        """
        raise NotImplementedError("ElevenLabsIngestionAdapter uses override ingest()")

    # ------------------------------------------------------------------
    # Override ingest — two-write sequence
    # ------------------------------------------------------------------

    async def ingest(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
        payload: dict[str, Any],
    ) -> IngestionResult:
        """Two-write sequence: transcript first, then session_summary linking back.

        Step 1: Validate signature (Law #3).
        Step 2: Resolve scope (Law #6).
        Step 3: Write `transcript` memory_object.
        Step 4: Write `session_summary` with linked_memory_ids=[transcript_id].
        Returns IngestionResult for the session_summary (the primary type).
        """
        # 1. Signature (fail-closed)
        if not await self.verify_signature(body=body, headers=headers):
            logger.warning("el_signature_invalid")
            raise IngestionError(
                "ElevenLabs signature invalid",
                code="SIGNATURE_INVALID",
                status_code=401,
            )

        # 2. Scope
        data: dict[str, Any] = payload.get("data", {})
        agent_id: str = data.get("agent_id", "")
        conversation_id: str = data.get("conversation_id", "")

        if not conversation_id:
            raise IngestionError(
                "ElevenLabs payload missing data.conversation_id",
                code="MISSING_CONVERSATION_ID",
                status_code=422,
            )

        metadata: dict[str, Any] = data.get("metadata", {})
        analysis: dict[str, Any] = data.get("analysis", {})
        dynamic_variables: dict[str, Any] = (
            data.get("conversation_initiation_client_data", {})
            .get("dynamic_variables", {})
        )
        transcript_turns: list[dict[str, Any]] = data.get("transcript", [])
        # EL Twilio post-call payloads nest call data under metadata.phone_call.
        # Twilio uses `agent_number` (dialed) + `external_number` (caller).
        # SIP / generic uses `to_number` / `from_number`. Top-level
        # metadata.called_number is the legacy shape. Try them all.
        phone_call_meta_top: dict[str, Any] = metadata.get("phone_call", {}) or {}
        duration_secs: int = int(
            metadata.get("duration_secs")
            or metadata.get("call_duration_secs")
            or phone_call_meta_top.get("call_duration_secs")
            or 0
        )
        called_number: str | None = (
            phone_call_meta_top.get("agent_number")
            or phone_call_meta_top.get("to_number")
            or metadata.get("called_number")
        )
        summary_text: str = analysis.get("transcript_summary", "") or ""
        intents_detected: list[Any] = analysis.get("intents_detected", []) or []

        scope = await _resolve_el_scope(called_number, agent_id)

        trace_id, correlation_id = _stable_ids(conversation_id)
        event_at = datetime.now(timezone.utc)

        # Law #9: truncate transcript to 80 chars at DEBUG only
        # `t.get("message", "")` only catches MISSING keys, not None values —
        # EL silent turns ("agent: None") have an explicit None message which
        # crashed " ".join() with TypeError ("sequence item N: expected str
        # instance, NoneType found"). Coerce defensively.
        transcript_flat = " ".join(
            str(t.get("message") or "") for t in transcript_turns
        )
        preview = (transcript_flat[:80] + "…") if len(transcript_flat) > 80 else transcript_flat
        logger.debug(
            "el_ingestion_transcript_preview conversation_id=%s preview=%r",
            conversation_id,
            preview,
        )

        # 3. Write transcript memory_object
        transcript_summary = (transcript_flat[:140] + "…") if len(transcript_flat) > 140 else transcript_flat

        transcript_envelope = MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="elevenlabs",
                channel="voice",
                source_record_id=conversation_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="transcript",
            entity_type=None,
            entity_id=None,
            thread_id=None,
            title=f"Transcript — {agent_id}",
            summary=transcript_summary or "(no transcript content)",
            detail={
                "turns": transcript_turns,
                "duration_secs": duration_secs,
                "conversation_id": conversation_id,
                "agent_id": agent_id,
                "runtime": "elevenlabs",
            },
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=event_at,
            idempotency_key=f"el-transcript:{conversation_id}",
        )

        try:
            transcript_memory = await self._memory_service.write(
                transcript_envelope, scope=scope, embed=True
            )
        except MemoryServiceError as exc:
            logger.error(
                "el_transcript_write_failed conversation_id=%s code=%s",
                conversation_id,
                exc.code,
            )
            raise IngestionError(
                f"ElevenLabs transcript write failed: {exc.code}",
                code="MEMORY_WRITE_FAILED",
                status_code=500,
            ) from exc

        transcript_id: uuid.UUID = transcript_memory.memory_id

        # 4. Write session_summary with linked_memory_ids=[transcript_id]
        agent_name = agent_id  # Pass 16 will resolve to display name
        summary_truncated = (summary_text[:280] + "…") if len(summary_text) > 280 else summary_text

        summary_envelope = MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="elevenlabs",
                channel="voice",
                source_record_id=conversation_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="session_summary",
            entity_type=None,
            entity_id=None,
            thread_id=None,
            title=f"{agent_name} session — {duration_secs}s",
            summary=summary_truncated or transcript_summary or "(no summary)",
            detail={
                "intents_detected": intents_detected,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "dynamic_variables": dynamic_variables,
                "summary_text": summary_text,
                "linked_memory_ids": [str(transcript_id)],
            },
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=event_at,
            idempotency_key=f"el-summary:{conversation_id}",
        )

        try:
            summary_memory = await self._memory_service.write(
                summary_envelope, scope=scope, embed=True
            )
        except MemoryServiceError as exc:
            logger.error(
                "el_summary_write_failed conversation_id=%s code=%s",
                conversation_id,
                exc.code,
            )
            raise IngestionError(
                f"ElevenLabs session_summary write failed: {exc.code}",
                code="MEMORY_WRITE_FAILED",
                status_code=500,
            ) from exc

        logger.info(
            "el_ingestion_success conversation_id=%s transcript_id=%s summary_id=%s tenant=%s",
            conversation_id,
            transcript_id,
            summary_memory.memory_id,
            str(scope.tenant_id),
        )

        # ── Caller Memory enrichment (W5/W6, migration 110) ──────────────────
        # After memory_objects are persisted, enrich the structured frontdesk
        # tables: upsert contact, log call_session, optionally write voicemail
        # and notify owner. Failures here are logged but do NOT raise — the
        # memory_objects writes above are the authoritative trail per Law #2,
        # and the post-call webhook MUST return 200 to avoid EL retries.
        try:
            # Use `import package.module` form, NOT `from package import module`.
            # The latter fails in the installed wheel because contact_writer
            # itself does `from aspire_orchestrator.services import receipt_store`
            # at module load time -- when our outer import re-enters the partly-
            # loaded services package the name lookup fails (ImportError).
            # `import package.module` bypasses package-namespace lookup and goes
            # straight to the submodule loader, which is reentrant-safe.
            import aspire_orchestrator.services.contact_writer as contact_writer  # noqa: I001
            import aspire_orchestrator.services.call_logger as call_logger
            import aspire_orchestrator.services.voicemail_writer as voicemail_writer
            import aspire_orchestrator.services.voicemail_notifier as voicemail_notifier

            phone_call_meta: dict[str, Any] = metadata.get("phone_call", {}) or {}
            # Twilio: external_number is the caller. SIP/generic: from_number.
            # Legacy: caller_id at top level.
            caller_id = (
                phone_call_meta.get("external_number")
                or phone_call_meta.get("from_number")
                or metadata.get("caller_id")
                or ""
            )
            recording_url = (
                phone_call_meta.get("recording_url")
                or metadata.get("recording_url")
                or ""
            )
            data_collection_results: dict[str, Any] = (
                analysis.get("data_collection_results") or {}
            )
            start_unix = metadata.get("start_time_unix_secs")
            end_unix = metadata.get("end_time_unix_secs")
            started_at = (
                datetime.fromtimestamp(start_unix, tz=timezone.utc)
                if start_unix
                else None
            )
            ended_at = (
                datetime.fromtimestamp(end_unix, tz=timezone.utc)
                if end_unix
                else None
            )

            # Upsert contact (only when caller_id is known — anonymous calls skip)
            contact_id: str | None = None
            if caller_id and str(scope.suite_id):
                # Pull the category Tiffany classified the caller into during the call.
                # Field comes from data_collection.category (lead | client | vendor |
                # friend | other | unknown). The shape may be flat ('lead') or nested
                # ({'value': 'lead', 'reasoning': '...'}) — handle both.
                _cat_raw = data_collection_results.get("category")
                if isinstance(_cat_raw, dict):
                    contact_category = str(_cat_raw.get("value", "") or "").strip().lower()
                else:
                    contact_category = str(_cat_raw or "").strip().lower()
                contact_id = await contact_writer.upsert_contact_from_call(
                    suite_id=str(scope.suite_id),
                    tenant_id=str(scope.tenant_id),
                    office_id=str(scope.office_id),
                    phone_e164=caller_id,
                    caller_name=str(
                        (data_collection_results.get("caller_name") or {}).get("value", "")
                        if isinstance(data_collection_results.get("caller_name"), dict)
                        else (data_collection_results.get("caller_name") or "")
                    ),
                    call_summary=summary_text or transcript_summary or "",
                    conversation_id=conversation_id,
                    category=contact_category or None,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )

            # Log call_session (always — even anonymous calls)
            call_session_id = await call_logger.log_call_session(
                suite_id=str(scope.suite_id),
                tenant_id=str(scope.tenant_id),
                office_id=str(scope.office_id),
                contact_id=contact_id,
                conversation_id=conversation_id,
                from_number=caller_id,
                to_number=called_number or "",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration_secs,
                transcript_summary=summary_text or transcript_summary or "",
                recording_url=recording_url,
                data_collection_json=data_collection_results or None,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )

            # Voicemail: only if a callback_number was captured AND a reason was
            # given. This is the gate that distinguishes "agent took a message"
            # from "agent transferred / answered FAQ / call dropped early."
            def _dc_value(field: str) -> str:
                raw = data_collection_results.get(field)
                if isinstance(raw, dict):
                    return str(raw.get("value", "") or "")
                return str(raw or "")

            callback_number = _dc_value("callback_number").strip()
            call_reason = _dc_value("call_reason").strip()
            took_message = bool(callback_number and call_reason)

            if took_message:
                voicemail_id = await voicemail_writer.write_voicemail(
                    suite_id=str(scope.suite_id),
                    tenant_id=str(scope.tenant_id),
                    office_id=str(scope.office_id),
                    contact_id=contact_id,
                    call_session_id=call_session_id,
                    from_e164=caller_id,
                    to_e164=called_number or "",
                    duration_seconds=duration_secs,
                    recording_uri=recording_url,
                    transcript_text=transcript_flat,
                    data_collection_results=data_collection_results,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                # Notify owner — email always, SMS only on high urgency
                voicemail_payload = {
                    **data_collection_results,
                    "recording_uri": recording_url,
                    "transcript_text": transcript_flat,
                }
                try:
                    await voicemail_notifier.notify_owner(
                        suite_id=str(scope.suite_id),
                        tenant_id=str(scope.tenant_id),
                        office_id=str(scope.office_id),
                        voicemail_id=voicemail_id,
                        voicemail_data=voicemail_payload,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning(
                        "el_voicemail_notify_failed conversation_id=%s err=%s",
                        conversation_id,
                        notify_exc,
                    )
        except Exception as enrich_exc:  # noqa: BLE001
            # Enrichment failures must NEVER fail the webhook — the
            # memory_objects writes above already cut receipts (Law #2).
            logger.warning(
                "el_caller_memory_enrichment_failed conversation_id=%s err=%s",
                conversation_id,
                enrich_exc,
            )

        return IngestionResult(memory=summary_memory, deduplicated=False)


__all__ = ["ElevenLabsIngestionAdapter"]
