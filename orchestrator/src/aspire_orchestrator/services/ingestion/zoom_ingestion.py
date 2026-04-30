"""Zoom recording + transcript ingestion → `meeting` memory_objects.

Pass 14 Lane F adapter. Two adapters in one file:

  1. ZoomRecordingIngestionAdapter — recording.completed
     Creates the initial `meeting` memory_object with topic, duration,
     participant_count, recording_files. Status=null (transcript may follow).
     Idempotency: f"zoom-recording:{meeting_uuid}"

  2. ZoomTranscriptIngestionAdapter — recording.transcript_completed
     Creates a NEW `meeting` memory_object enriched with transcript_text /
     transcript_download_url. Links back to the recording row via
     detail.linked_memory_ids (Law #2 — append-only, no UPDATE).
     Idempotency: f"zoom-transcript:{meeting_uuid}"

Webhook shapes:

  recording.completed:
    {
      "event": "recording.completed",
      "payload": {
        "account_id": str,
        "object": {
          "uuid": str,          # meeting UUID (used for idempotency)
          "id": int | str,      # meeting numeric ID
          "host_id": str,
          "topic": str,
          "start_time": str,    # ISO-8601
          "duration": int,      # minutes
          "participant_count": int | null,
          "recording_files": [
            {
              "file_type": "MP4" | "M4A" | "TRANSCRIPT" | ...,
              "download_url": str,
              "recording_start": str,
              "recording_end": str
            }, ...
          ]
        }
      }
    }

  recording.transcript_completed:
    {
      "event": "recording.transcript_completed",
      "payload": {
        "account_id": str,
        "object": {
          "uuid": str,
          "id": int | str,
          "topic": str | null,
          "recording_files": [
            {
              "file_type": "TRANSCRIPT",
              "download_url": str,
              "file_extension": "VTT",
              "status": "completed",
              "play_url": str | null,
              "transcript_text": str | null   # inline text if Zoom provides it
            }, ...
          ]
        }
      }
    }

Scope resolution: `provider_connections` (provider='zoom', external_account_id=account_id).

Signature verification: verify_zoom(body, X-Zm-Signature, X-Zm-Request-Timestamp, secret).

Law compliance:
  Law #2: MemoryService.write cuts receipts internally.
  Law #3: bad signature → 401; missing scope → 503/404.
  Law #6: scope resolved from payload (account_id lookup only).
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
)
from aspire_orchestrator.services.ingestion.signatures import verify_zoom
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
)

logger = logging.getLogger(__name__)

_NS = uuid.NAMESPACE_URL


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _resolve_zoom_scope(account_id: str) -> ScopedIdentity:
    """Resolve tenant from Zoom account_id via provider_connections."""
    if not account_id:
        raise IngestionError(
            "Zoom payload missing account_id",
            code="MISSING_ACCOUNT_ID",
            status_code=422,
        )
    try:
        rows = await supabase_select(
            table="provider_connections",
            filters={"provider": "zoom", "external_account_id": account_id},
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
            f"Zoom account {account_id} not linked to any tenant",
            code="UNKNOWN_ZOOM_ACCOUNT",
            status_code=404,
        )

    row = rows[0]
    return ScopedIdentity(
        tenant_id=UUID(row["tenant_id"]),
        suite_id=UUID(row["suite_id"]),
        office_id=UUID(row["office_id"]),
    )


def _stable_ids(meeting_uuid: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (trace_id, correlation_id) stable across both Zoom events."""
    trace_id = uuid.uuid5(_NS, f"zoom-meeting:trace:{meeting_uuid}")
    correlation_id = uuid.uuid5(_NS, f"zoom-meeting:corr:{meeting_uuid}")
    return trace_id, correlation_id


def _extract_account_and_object(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract (account_id, meeting_object) from Zoom webhook payload."""
    account_id: str = payload.get("account_id", "")
    meeting_obj: dict[str, Any] = payload.get("payload", {}).get("object", {})
    if not account_id:
        # account_id may also live inside payload envelope
        account_id = payload.get("payload", {}).get("account_id", "")
    return account_id, meeting_obj


def _verify_zoom_sig(body: bytes, headers: Mapping[str, str]) -> bool:
    """Extract Zoom signature headers and verify."""
    sig = (
        headers.get("x-zm-signature")
        or headers.get("X-Zm-Signature")
        or ""
    )
    ts = (
        headers.get("x-zm-request-timestamp")
        or headers.get("X-Zm-Request-Timestamp")
        or ""
    )
    return verify_zoom(body, sig, ts, settings.zoom_webhook_secret)


# ---------------------------------------------------------------------------
# Adapter 1 — ZoomRecordingIngestionAdapter
# ---------------------------------------------------------------------------


class ZoomRecordingIngestionAdapter(BaseIngestionAdapter):
    """Zoom recording.completed → initial `meeting` memory_object.

    Creates the meeting row with topic, duration, participant_count, and
    recording_files list. Status is null — transcript may arrive separately.
    """

    provider_name = "zoom_recording"
    memory_type = "meeting"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Zoom HMAC SHA-256 with timestamp replay protection."""
        return _verify_zoom_sig(body, headers)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from Zoom account_id."""
        account_id, _ = _extract_account_and_object(payload)
        return await _resolve_zoom_scope(account_id)

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build `meeting` memory_object from recording.completed payload."""
        account_id, meeting_obj = _extract_account_and_object(payload)
        meeting_uuid: str = meeting_obj.get("uuid", "")
        topic: str = meeting_obj.get("topic", "Untitled Meeting")
        host_id: str = meeting_obj.get("host_id", "")
        start_time: str = meeting_obj.get("start_time", "")
        duration: int = int(meeting_obj.get("duration", 0))
        participant_count: int | None = meeting_obj.get("participant_count")
        recording_files: list[dict[str, Any]] = meeting_obj.get("recording_files", [])

        if not meeting_uuid:
            raise IngestionError(
                "Zoom recording payload missing payload.object.uuid",
                code="MISSING_MEETING_UUID",
                status_code=422,
            )

        trace_id, correlation_id = _stable_ids(meeting_uuid)

        # Normalize recording_files to safe subset (no sensitive tokens)
        safe_files = [
            {
                "file_type": f.get("file_type", ""),
                "download_url": f.get("download_url", ""),
                "recording_start": f.get("recording_start", ""),
                "recording_end": f.get("recording_end", ""),
            }
            for f in recording_files
        ]

        part_str = f" with {participant_count} participant(s)" if participant_count else ""
        title = f"Meeting — {topic}"
        summary = f"{duration}min meeting{part_str}"

        # event_at from meeting start_time
        try:
            event_at = datetime.fromisoformat(start_time.replace("Z", "+00:00")) if start_time else datetime.now(timezone.utc)
        except ValueError:
            event_at = datetime.now(timezone.utc)

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="provider_webhook",
                channel="video",
                source_record_id=meeting_uuid,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="meeting",
            entity_type=None,
            entity_id=None,
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail={
                "meeting_uuid": meeting_uuid,
                "host_id": host_id,
                "topic": topic,
                "start_time": start_time,
                "duration_minutes": duration,
                "participant_count": participant_count,
                "recording_files": safe_files,
                "account_id": account_id,
                # Transcript fields — filled by ZoomTranscriptIngestionAdapter
                "transcript_text": None,
                "transcript_download_url": None,
            },
            confidence=None,
            visibility_scope="office",
            status=None,  # Terminal status arrives with transcript
            event_at=event_at,
            idempotency_key=f"zoom-recording:{meeting_uuid}",
        )


# ---------------------------------------------------------------------------
# Adapter 2 — ZoomTranscriptIngestionAdapter
# ---------------------------------------------------------------------------


class ZoomTranscriptIngestionAdapter(BaseIngestionAdapter):
    """Zoom recording.transcript_completed → enriched `meeting` memory_object.

    Creates a NEW append-only memory_object (Law #2 — no UPDATE) enriched with
    transcript_text / transcript_download_url and links back to the recording
    row via detail.linked_memory_ids (looked up by idempotency_key).
    """

    provider_name = "zoom_transcript"
    memory_type = "meeting"

    async def verify_signature(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        """Zoom HMAC SHA-256 with timestamp replay protection."""
        return _verify_zoom_sig(body, headers)

    async def resolve_scope(self, payload: dict[str, Any]) -> ScopedIdentity:
        """Resolve tenant from Zoom account_id."""
        account_id, _ = _extract_account_and_object(payload)
        return await _resolve_zoom_scope(account_id)

    async def _lookup_recording_memory_id(
        self,
        meeting_uuid: str,
        scope: ScopedIdentity,
    ) -> uuid.UUID | None:
        """Look up the recording memory_object by idempotency_key.

        Non-fatal: if the recording row doesn't exist yet (e.g. transcript
        arrived before recording event), returns None. linked_memory_ids will
        be empty in that case.
        """
        idem_key = f"zoom-recording:{meeting_uuid}"
        try:
            rows = await supabase_select(
                table="memory_objects",
                filters={
                    "tenant_id": str(scope.tenant_id),
                    "idempotency_key": idem_key,
                },
                limit=1,
            )
            if rows:
                return UUID(rows[0]["memory_id"])
        except (SupabaseClientError, ValueError, KeyError) as exc:
            logger.debug(
                "zoom_recording_lookup_failed meeting_uuid=%s error=%s",
                meeting_uuid,
                exc,
            )
        return None

    async def build_envelope(
        self,
        payload: dict[str, Any],
        *,
        scope: ScopedIdentity,
        thread: ThreadOut | None,
    ) -> MemoryObjectIn:
        """Build enriched `meeting` memory_object from transcript_completed payload."""
        account_id, meeting_obj = _extract_account_and_object(payload)
        meeting_uuid: str = meeting_obj.get("uuid", "")
        topic: str = meeting_obj.get("topic", "") or "Untitled Meeting"
        recording_files: list[dict[str, Any]] = meeting_obj.get("recording_files", [])

        if not meeting_uuid:
            raise IngestionError(
                "Zoom transcript payload missing payload.object.uuid",
                code="MISSING_MEETING_UUID",
                status_code=422,
            )

        trace_id, correlation_id = _stable_ids(meeting_uuid)

        # Extract transcript from recording_files (TRANSCRIPT type)
        transcript_text: str | None = None
        transcript_download_url: str = ""
        for f in recording_files:
            if f.get("file_type") == "TRANSCRIPT":
                transcript_text = f.get("transcript_text") or None
                transcript_download_url = f.get("download_url", "")
                break

        # Law #9: truncate transcript at DEBUG
        if transcript_text:
            preview = (transcript_text[:80] + "…") if len(transcript_text) > 80 else transcript_text
            logger.debug(
                "zoom_transcript_preview meeting_uuid=%s preview=%r",
                meeting_uuid,
                preview,
            )

        # Summary from inline transcript or download URL pointer
        if transcript_text:
            summary = (transcript_text[:140] + "…") if len(transcript_text) > 140 else transcript_text
        else:
            summary = f"Transcript ready: {transcript_download_url}" if transcript_download_url else "Transcript completed"

        # Lookup recording row for linked_memory_ids (non-fatal)
        recording_memory_id = await self._lookup_recording_memory_id(meeting_uuid, scope)
        linked_ids: list[str] = [str(recording_memory_id)] if recording_memory_id else []

        title = f"Meeting transcript — {topic}"

        return MemoryObjectIn(
            scope=scope,
            provenance=Provenance(
                source_surface="system",
                runtime_family="provider_webhook",
                channel="video",
                source_record_id=meeting_uuid,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
            memory_type="meeting",
            entity_type=None,
            entity_id=None,
            thread_id=thread.thread_id if thread else None,
            title=title,
            summary=summary,
            detail={
                "meeting_uuid": meeting_uuid,
                "topic": topic,
                "account_id": account_id,
                "transcript_text": transcript_text,
                "transcript_download_url": transcript_download_url,
                "linked_memory_ids": linked_ids,
                # Back-reference to recording row (Law #2 audit chain)
                "supersedes_idempotency_key": f"zoom-recording:{meeting_uuid}",
            },
            confidence=None,
            visibility_scope="office",
            status="executed",
            event_at=datetime.now(timezone.utc),
            idempotency_key=f"zoom-transcript:{meeting_uuid}",
        )


__all__ = [
    "ZoomRecordingIngestionAdapter",
    "ZoomTranscriptIngestionAdapter",
]
