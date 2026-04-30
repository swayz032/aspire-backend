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

    # Step 2 — provider_connections lookup by agent_id
    try:
        rows = await supabase_select(
            table="provider_connections",
            filters={"provider": "elevenlabs", "external_account_id": agent_id},
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
            f"ElevenLabs agent {agent_id} not linked to any tenant "
            f"(called_number={called_number!r})",
            code="UNKNOWN_AGENT_OR_NUMBER",
            status_code=404,
        )

    row = rows[0]
    return ScopedIdentity(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
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
        """Resolve tenant from called_number (primary) or agent_id (fallback)."""
        data = payload.get("data", {})
        agent_id: str = data.get("agent_id", "")
        metadata = data.get("metadata", {})
        called_number: str | None = metadata.get("called_number")
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
        duration_secs: int = int(metadata.get("duration_secs", 0))
        called_number: str | None = metadata.get("called_number")
        summary_text: str = analysis.get("transcript_summary", "") or ""
        intents_detected: list[Any] = analysis.get("intents_detected", []) or []

        scope = await _resolve_el_scope(called_number, agent_id)

        trace_id, correlation_id = _stable_ids(conversation_id)
        event_at = datetime.now(timezone.utc)

        # Law #9: truncate transcript to 80 chars at DEBUG only
        transcript_flat = " ".join(
            t.get("message", "") for t in transcript_turns
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
        return IngestionResult(memory=summary_memory, deduplicated=False)


__all__ = ["ElevenLabsIngestionAdapter"]
