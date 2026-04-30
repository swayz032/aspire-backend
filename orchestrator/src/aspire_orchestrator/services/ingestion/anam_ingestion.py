"""Anam session-end webhook ingestion — `session_summary` + `transcript`.

Pass 14 Lane E adapter.

Anam fires a JSON webhook when a video persona session ends (Ava-video,
Finn-video):

  POST /v1/ingest/anam/session-end
  Header: X-Anam-Signature: <hex SHA-256 HMAC of raw body>
  Body (JSON):
    {
      "event": "session.ended",
      "session": {
        "persona_id": str,
        "session_id": str,
        "duration_seconds": int,
        "transcript": [{"role": "agent"|"user", "message": str}, ...],
        "metadata": {
          "tenant_id": str | null,  # Direct tenant resolution (preferred)
          "handoff_id": str | null, # Links to prior voice session (el-conv:corr:*)
          ...
        }
      }
    }

Scope resolution (two-step, fail-closed):
  1. Try `session.metadata.tenant_id` if non-null.
  2. Else try `provider_connections` (provider='anam', external_account_id=persona_id).
  3. If neither resolves → IngestionError(UNKNOWN_PERSONA, 404).

Handoff chain: if `metadata.handoff_id` is non-null, look up the prior
`handoff_note` (or voice session_summary) memory_object by
`correlation_id = handoff_id` and add it to `linked_memory_ids` on the
session_summary. This creates the voice → video audit chain (Law #2).

Two memory_objects per webhook (both idempotent):
  1. `transcript` — raw turns, idempotency_key=f"anam-transcript:{session_id}"
  2. `session_summary` — refined, idempotency_key=f"anam-summary:{session_id}",
     linked_memory_ids=[transcript_id] + optional [handoff_memory_id]

Law compliance:
  Law #2: MemoryService.write cuts receipts internally.
  Law #3: bad signature → 401; missing scope → 404.
  Law #6: scope resolved from payload only (tenant_id or persona_id lookup).
  Law #9: transcript NOT logged at full fidelity (first 80 chars at DEBUG).
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
from aspire_orchestrator.services.ingestion.signatures import verify_anam
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


async def _resolve_anam_scope(
    tenant_id_raw: str | None,
    persona_id: str,
) -> ScopedIdentity:
    """Two-step scope resolution for Anam webhooks.

    Step 1: Direct tenant_id from metadata (if present and a UUID).
    Step 2: persona_id → provider_connections (provider='anam').
    Fail-closed: raises IngestionError if neither resolves.
    """
    # Step 1 — direct tenant_id in metadata
    if tenant_id_raw:
        try:
            # Metadata tenant_id must still be enriched with suite/office from
            # provider_connections for full scope. Attempt with the UUID.
            rows = await supabase_select(
                table="provider_connections",
                filters={"provider": "anam", "tenant_id": tenant_id_raw},
                limit=1,
            )
            if rows:
                row = rows[0]
                return ScopedIdentity(
                    tenant_id=UUID(row["tenant_id"]),
                    suite_id=UUID(row["suite_id"]),
                    office_id=UUID(row["office_id"]),
                )
        except (SupabaseClientError, ValueError) as exc:
            logger.debug(
                "anam_scope_tenant_lookup_failed tenant_id=%s error=%s",
                tenant_id_raw,
                exc,
            )
            # Fall through to persona_id lookup

    # Step 2 — persona_id via provider_connections
    try:
        rows = await supabase_select(
            table="provider_connections",
            filters={"provider": "anam", "external_account_id": persona_id},
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
            f"Anam persona {persona_id} not linked to any tenant "
            f"(tenant_id={tenant_id_raw!r})",
            code="UNKNOWN_PERSONA",
            status_code=404,
        )

    row = rows[0]
    return ScopedIdentity(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
    )


# ---------------------------------------------------------------------------
# Handoff lookup
# ---------------------------------------------------------------------------


async def _resolve_handoff_memory_id(
    handoff_id: str,
    scope: ScopedIdentity,
) -> uuid.UUID | None:
    """Look up a prior memory_object by correlation_id matching handoff_id.

    Used to link voice → video handoff chain (Law #2 audit trail).
    Non-fatal: if the handoff memory is not found, returns None.
    """
    try:
        rows = await supabase_select(
            table="memory_objects",
            filters={
                "tenant_id": str(scope.tenant_id),
                "correlation_id": handoff_id,
            },
            limit=1,
        )
        if rows:
            return UUID(rows[0]["memory_id"])
    except (SupabaseClientError, ValueError, KeyError) as exc:
        logger.debug(
            "anam_handoff_lookup_failed handoff_id=%s error=%s",
            handoff_id,
            exc,
        )
    return None


# ---------------------------------------------------------------------------
# Stable IDs
# ---------------------------------------------------------------------------


def _stable_ids(session_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (trace_id, correlation_id) stable across both writes per session."""
    trace_id = uuid.uuid5(_NS, f"anam-session:trace:{session_id}")
    correlation_id = uuid.uuid5(_NS, f"anam-session:corr:{session_id}")
    return trace_id, correlation_id


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AnamIngestionAdapter(BaseIngestionAdapter):
    """Anam session.ended webhook → `transcript` + `session_summary` memory_objects.

    Overrides `ingest` to perform the two-write sequence:
      1. Write `transcript` memory_object.
      2. Write `session_summary` with linked_memory_ids=[transcript_id, handoff_id?].

    Both writes are idempotent on session_id.
    """

    provider_name = "anam"
    memory_type = "session_summary"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Anam SHA-256 HMAC of raw body, hex-encoded."""
        sig = (
            headers.get("x-anam-signature")
            or headers.get("X-Anam-Signature")
            or ""
        )
        return verify_anam(body, sig, settings.anam_webhook_secret)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from metadata.tenant_id (primary) or persona_id (fallback)."""
        session: dict[str, Any] = payload.get("session", {})
        persona_id: str = session.get("persona_id", "")
        metadata: dict[str, Any] = session.get("metadata", {})
        tenant_id_raw: str | None = metadata.get("tenant_id")
        return await _resolve_anam_scope(tenant_id_raw, persona_id)

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Not used — ingest() is overridden to perform two writes."""
        raise NotImplementedError("AnamIngestionAdapter uses override ingest()")

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
        Step 3: Optionally resolve handoff_memory_id for voice→video chain.
        Step 4: Write `transcript` memory_object.
        Step 5: Write `session_summary` with linked_memory_ids.
        """
        # 1. Signature
        if not await self.verify_signature(body=body, headers=headers):
            logger.warning("anam_signature_invalid")
            raise IngestionError(
                "Anam signature invalid",
                code="SIGNATURE_INVALID",
                status_code=401,
            )

        # 2. Extract payload fields
        session: dict[str, Any] = payload.get("session", {})
        persona_id: str = session.get("persona_id", "")
        session_id: str = session.get("session_id", "")
        metadata: dict[str, Any] = session.get("metadata", {})
        transcript_turns: list[dict[str, Any]] = session.get("transcript", [])
        duration_seconds: int = int(session.get("duration_seconds", 0))
        handoff_id: str | None = metadata.get("handoff_id")
        tenant_id_raw: str | None = metadata.get("tenant_id")

        if not session_id:
            raise IngestionError(
                "Anam payload missing session.session_id",
                code="MISSING_SESSION_ID",
                status_code=422,
            )

        # 3. Scope
        scope = await _resolve_anam_scope(tenant_id_raw, persona_id)

        trace_id, correlation_id = _stable_ids(session_id)
        event_at = datetime.now(timezone.utc)

        # Law #9: truncate transcript at DEBUG
        transcript_flat = " ".join(
            t.get("message", "") for t in transcript_turns
        )
        preview = (transcript_flat[:80] + "…") if len(transcript_flat) > 80 else transcript_flat
        logger.debug(
            "anam_ingestion_transcript_preview session_id=%s preview=%r",
            session_id,
            preview,
        )

        # 4. Optional handoff chain (non-fatal)
        handoff_memory_id: uuid.UUID | None = None
        if handoff_id:
            handoff_memory_id = await _resolve_handoff_memory_id(handoff_id, scope)
            if handoff_memory_id:
                logger.info(
                    "anam_handoff_chain session_id=%s handoff_id=%s handoff_memory_id=%s",
                    session_id,
                    handoff_id,
                    handoff_memory_id,
                )
            else:
                logger.info(
                    "anam_handoff_not_found session_id=%s handoff_id=%s",
                    session_id,
                    handoff_id,
                )

        # 5. Write transcript
        transcript_summary = (transcript_flat[:140] + "…") if len(transcript_flat) > 140 else transcript_flat

        transcript_envelope = MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="anam",
                channel="video",
                source_record_id=session_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="transcript",
            entity_type=None,
            entity_id=None,
            thread_id=None,
            title=f"Transcript — {persona_id}",
            summary=transcript_summary or "(no transcript content)",
            detail={
                "turns": transcript_turns,
                "duration_seconds": duration_seconds,
                "session_id": session_id,
                "persona_id": persona_id,
                "runtime": "anam",
            },
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=event_at,
            idempotency_key=f"anam-transcript:{session_id}",
        )

        try:
            transcript_memory = await self._memory_service.write(
                transcript_envelope, scope=scope, embed=True
            )
        except MemoryServiceError as exc:
            logger.error(
                "anam_transcript_write_failed session_id=%s code=%s",
                session_id,
                exc.code,
            )
            raise IngestionError(
                f"Anam transcript write failed: {exc.code}",
                code="MEMORY_WRITE_FAILED",
                status_code=500,
            ) from exc

        transcript_id: uuid.UUID = transcript_memory.memory_id

        # 6. Write session_summary
        linked_ids: list[str] = [str(transcript_id)]
        if handoff_memory_id:
            linked_ids.append(str(handoff_memory_id))

        summary_envelope = MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="anam",
                channel="video",
                source_record_id=session_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="session_summary",
            entity_type=None,
            entity_id=None,
            thread_id=None,
            title=f"{persona_id} session — {duration_seconds}s",
            summary=transcript_summary or "(no session content)",
            detail={
                "persona_id": persona_id,
                "session_id": session_id,
                "duration_seconds": duration_seconds,
                "handoff_id": handoff_id,
                "linked_memory_ids": linked_ids,
                "runtime": "anam",
            },
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=event_at,
            idempotency_key=f"anam-summary:{session_id}",
        )

        try:
            summary_memory = await self._memory_service.write(
                summary_envelope, scope=scope, embed=True
            )
        except MemoryServiceError as exc:
            logger.error(
                "anam_summary_write_failed session_id=%s code=%s",
                session_id,
                exc.code,
            )
            raise IngestionError(
                f"Anam session_summary write failed: {exc.code}",
                code="MEMORY_WRITE_FAILED",
                status_code=500,
            ) from exc

        logger.info(
            "anam_ingestion_success session_id=%s transcript_id=%s summary_id=%s tenant=%s",
            session_id,
            transcript_id,
            summary_memory.memory_id,
            str(scope.tenant_id),
        )
        return IngestionResult(memory=summary_memory, deduplicated=False)


__all__ = ["AnamIngestionAdapter"]
