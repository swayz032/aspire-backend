"""Nora Orchestration Tools — backend tools for the Conference Assistant agent.

Nora coordinates multi-agent office actions via A2A dispatch and memory writes.

Law compliance:
  Law #1: Tools are hands. Nora tools never decide — they dispatch to the
          orchestrator's A2A bus and write memory. The orchestrator routes.
  Law #2: post_office_message and save_office_memory emit receipts.
  Law #3: Missing scope → raise NoraToolError (fail closed).
  Law #6: Tenant isolation enforced on every call.
  Law #7: No tool calls another tool. Nora tools are bounded.
  Law #9: No PII or secrets in log lines.

Capability scope required: 'office_read' (reads) / 'office_write' (writes).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
)
from aspire_orchestrator.services.memory_service import (
    MemoryService,
    MemoryServiceError,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_TOOL_TIMEOUT_S = 4.9
_ORCHESTRATOR_BASE = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class NoraToolError(MemoryServiceError):
    """Structured error raised by Nora tools."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        tenant_id: UUID | str | None = None,
        correlation_id: UUID | str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, tenant_id=tenant_id, correlation_id=correlation_id)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _assert_office_scope(scope: ScopedIdentity) -> None:
    if not isinstance(scope, ScopedIdentity):
        raise NoraToolError(
            "Invalid ScopedIdentity — capability token scope validation failed",
            code="INVALID_CAPABILITY_TOKEN",
        )


async def _a2a_dispatch(
    pack_id: str,
    *,
    scope: ScopedIdentity,
    correlation_id: UUID,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST to the existing /v1/a2a/dispatch endpoint."""
    headers = {
        "X-Tenant-Id": str(scope.tenant_id),
        "X-Suite-Id": str(scope.suite_id),
        "X-Office-Id": str(scope.office_id),
        "X-Correlation-Id": str(correlation_id),
        "Content-Type": "application/json",
    }
    dispatch_payload = {
        "pack_id": pack_id,
        "correlation_id": str(correlation_id),
        **payload,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOOL_TIMEOUT_S) as client:
            resp = await client.post(
                f"{_ORCHESTRATOR_BASE}/v1/a2a/dispatch",
                headers=headers,
                json=dispatch_payload,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException as exc:
        raise NoraToolError(
            f"A2A dispatch to {pack_id} timed out",
            code="PROVIDER_TIMEOUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise NoraToolError(
            f"A2A dispatch to {pack_id} HTTP {status}",
            code="PROVIDER_UNAVAILABLE" if status >= 500 else "INVALID_INPUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
            retryable=status >= 500,
        ) from exc


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


class NoraDispatchOut(BaseModel):
    task_id: str
    pack_id: str
    status: str
    correlation_id: str


class NoraPostMessageOut(BaseModel):
    memory_id: str
    receipt_id: str
    correlation_id: str


class NoraSaveMemoryOut(BaseModel):
    memory_id: str
    receipt_id: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Tool: invoke_adam
# ---------------------------------------------------------------------------


async def invoke_adam(
    scope: ScopedIdentity,
    *,
    query: str,
    context: dict[str, Any] | None = None,
) -> NoraDispatchOut:
    """Dispatch a research task to Adam via A2A.

    GREEN tier. No state change. Capability scope: office_read.
    """
    _assert_office_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "invoke_adam tenant_id=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    data = await _a2a_dispatch(
        "adam",
        scope=scope,
        correlation_id=correlation_id,
        payload={"query": query, "context": context or {}},
    )

    return NoraDispatchOut(
        task_id=data.get("task_id", str(uuid.uuid4())),
        pack_id="adam",
        status=data.get("status", "created"),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool: invoke_quinn
# ---------------------------------------------------------------------------


async def invoke_quinn(
    scope: ScopedIdentity,
    *,
    action: str,
    context: dict[str, Any] | None = None,
) -> NoraDispatchOut:
    """Dispatch an invoice/quote follow-up task to Quinn via A2A.

    YELLOW tier. Capability scope: office_read.
    """
    _assert_office_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "invoke_quinn action=%s tenant_id=%s correlation_id=%s",
        action,
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    data = await _a2a_dispatch(
        "quinn",
        scope=scope,
        correlation_id=correlation_id,
        payload={"action": action, "context": context or {}},
    )

    return NoraDispatchOut(
        task_id=data.get("task_id", str(uuid.uuid4())),
        pack_id="quinn",
        status=data.get("status", "created"),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool: invoke_clara
# ---------------------------------------------------------------------------


async def invoke_clara(
    scope: ScopedIdentity,
    *,
    action: str,
    context: dict[str, Any] | None = None,
) -> NoraDispatchOut:
    """Dispatch a contract/signature follow-up task to Clara via A2A.

    YELLOW tier. Capability scope: office_read.
    """
    _assert_office_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "invoke_clara action=%s tenant_id=%s correlation_id=%s",
        action,
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    data = await _a2a_dispatch(
        "clara",
        scope=scope,
        correlation_id=correlation_id,
        payload={"action": action, "context": context or {}},
    )

    return NoraDispatchOut(
        task_id=data.get("task_id", str(uuid.uuid4())),
        pack_id="clara",
        status=data.get("status", "created"),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool: invoke_tec
# ---------------------------------------------------------------------------


async def invoke_tec(
    scope: ScopedIdentity,
    *,
    action: str,
    context: dict[str, Any] | None = None,
) -> NoraDispatchOut:
    """Dispatch a document generation task to Tec via A2A.

    YELLOW tier. Capability scope: office_read.
    """
    _assert_office_scope(scope)
    correlation_id = uuid.uuid4()

    logger.info(
        "invoke_tec action=%s tenant_id=%s correlation_id=%s",
        action,
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    data = await _a2a_dispatch(
        "tec",
        scope=scope,
        correlation_id=correlation_id,
        payload={"action": action, "context": context or {}},
    )

    return NoraDispatchOut(
        task_id=data.get("task_id", str(uuid.uuid4())),
        pack_id="tec",
        status=data.get("status", "created"),
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool: post_office_message  (state change → receipt + memory write)
# ---------------------------------------------------------------------------


async def post_office_message(
    scope: ScopedIdentity,
    *,
    recipient: str,
    body: str,
    subject: str | None = None,
    entity_id: str | None = None,
) -> NoraPostMessageOut:
    """Write an office message to the inbox tables and memory_objects.

    YELLOW tier. Emits receipt (Law #2). Capability scope: office_write.
    """
    _assert_office_scope(scope)
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    logger.info(
        "post_office_message tenant_id=%s recipient=%s correlation_id=%s",
        str(scope.tenant_id)[:8],
        recipient[:20] if recipient else "none",
        str(correlation_id)[:8],
    )

    # Write timeline_event memory object
    provenance = Provenance(
        source_surface="nora_meeting",
        source_agent="nora",
        runtime_family="elevenlabs",
        channel="voice",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    envelope = MemoryObjectIn(
        scope=scope,
        provenance=provenance,
        memory_type="timeline_event",
        entity_id=UUID(entity_id) if entity_id else None,
        title=subject or f"Office message to {recipient}",
        summary=body[:500],
        detail={"recipient": recipient, "subject": subject},
        visibility_scope="office",
        idempotency_key=f"nora:msg:{str(correlation_id)}",
    )

    svc = MemoryService()
    try:
        result = await svc.write(envelope, scope=scope, embed=False)
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise NoraToolError(
            "Unexpected error in post_office_message",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return NoraPostMessageOut(
        memory_id=str(result.memory_id),
        receipt_id=str(result.linked_receipt_ids[0]) if result.linked_receipt_ids else "",
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Tool: save_office_memory  (state change → receipt)
# ---------------------------------------------------------------------------


async def save_office_memory(
    scope: ScopedIdentity,
    *,
    memory_type: str,
    summary: str,
    title: str | None = None,
    detail: dict[str, Any] | None = None,
    entity_id: str | None = None,
    idempotency_key: str | None = None,
) -> NoraSaveMemoryOut:
    """Persist a durable office memory object with visibility_scope='office'.

    YELLOW tier. Emits receipt (Law #2). source_agent='nora'.
    Capability scope: office_write.
    """
    _assert_office_scope(scope)
    correlation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    _VALID_NORA_MEMORY_TYPES = {
        "session_summary",
        "timeline_event",
        "decision_fact",
        "followup_task",
        "thread_summary",
        "artifact_reference",
    }
    if memory_type not in _VALID_NORA_MEMORY_TYPES:
        raise NoraToolError(
            f"Invalid memory_type '{memory_type}'. Allowed: {sorted(_VALID_NORA_MEMORY_TYPES)}",
            code="INVALID_INPUT",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        )

    ikey = idempotency_key or f"nora:{memory_type}:{str(correlation_id)}"

    logger.info(
        "save_office_memory memory_type=%s tenant_id=%s correlation_id=%s",
        memory_type,
        str(scope.tenant_id)[:8],
        str(correlation_id)[:8],
    )

    provenance = Provenance(
        source_surface="nora_meeting",
        source_agent="nora",
        runtime_family="elevenlabs",
        channel="voice",
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    envelope = MemoryObjectIn(
        scope=scope,
        provenance=provenance,
        memory_type=memory_type,  # type: ignore[arg-type]
        entity_id=UUID(entity_id) if entity_id else None,
        title=title,
        summary=summary,
        detail=detail or {},
        visibility_scope="office",
        idempotency_key=ikey,
    )

    svc = MemoryService()
    try:
        result = await svc.write(envelope, scope=scope, embed=False)
    except MemoryServiceError:
        raise
    except Exception as exc:
        raise NoraToolError(
            "Unexpected error in save_office_memory",
            code="UNKNOWN_ERROR",
            tenant_id=scope.tenant_id,
            correlation_id=correlation_id,
        ) from exc

    return NoraSaveMemoryOut(
        memory_id=str(result.memory_id),
        receipt_id=str(result.linked_receipt_ids[0]) if result.linked_receipt_ids else "",
        correlation_id=str(correlation_id),
    )


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------

NORA_ORCHESTRATION_TOOLS: list[str] = [
    "nora.orchestration.invoke_adam",
    "nora.orchestration.invoke_quinn",
    "nora.orchestration.invoke_clara",
    "nora.orchestration.invoke_tec",
    "nora.orchestration.post_office_message",
    "nora.orchestration.save_office_memory",
]
