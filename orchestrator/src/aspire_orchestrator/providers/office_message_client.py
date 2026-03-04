"""Internal Office Message Provider.

Implements office-to-office messaging for Eli:
  - internal.office.read
  - internal.office.create
  - internal.office.draft
  - internal.office.send

Storage backend: Supabase table `inbox_items` (type='OFFICE').
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.services.tool_types import ToolExecutionResult


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_receipt(
    *,
    tool_id: str,
    suite_id: str,
    office_id: str,
    correlation_id: str,
    outcome: Outcome,
    reason_code: str,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "tool_used": tool_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "correlation_id": correlation_id,
        "outcome": outcome.value,
        "reason_code": reason_code,
        "timestamp": _ts(),
    }


def _build_office_item(
    *,
    recipient_suite_id: str,
    recipient_office_id: str,
    sender_suite_id: str,
    sender_office_id: str,
    title: str,
    body: str,
    priority: str,
    status: str,
    is_draft: bool,
) -> dict[str, Any]:
    preview = (body or "").strip().replace("\n", " ")
    if len(preview) > 240:
        preview = preview[:237] + "..."
    return {
        "id": str(uuid.uuid4()),
        "suite_id": recipient_suite_id,
        # Defensive default; DB trigger syncs canonical tenant_id from suite_id.
        "tenant_id": recipient_suite_id,
        "office_id": recipient_office_id,
        "type": "OFFICE",
        "title": title,
        "preview": preview,
        "priority": priority,
        "status": status,
        "unread": not is_draft,
        "metadata": {
            "message_type": "office",
            "body": body,
            "is_draft": is_draft,
            "sender_suite_id": sender_suite_id,
            "sender_office_id": sender_office_id,
            "recipient_suite_id": recipient_suite_id,
            "recipient_office_id": recipient_office_id,
        },
    }


def _normalize_priority(value: Any) -> str:
    raw = str(value or "NORMAL").strip().upper()
    return raw if raw in {"LOW", "NORMAL", "HIGH", "URGENT"} else "NORMAL"


async def execute_office_read(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    **kwargs: Any,
) -> ToolExecutionResult:
    tool_id = "internal.office.read"
    try:
        folder = str(payload.get("folder", "inbox")).strip().lower()
        unread_only = bool(payload.get("unread_only", False))
        try:
            limit = int(payload.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(50, limit))

        _ = folder  # reserved for future sent/archive server filtering
        filters = (
            f"suite_id=eq.{suite_id}"
            "&type=eq.OFFICE"
            "&status=neq.ARCHIVED"
            f"{'&unread=is.true' if unread_only else ''}"
            "&order=updated_at.desc"
            f"&limit={limit}"
        )

        rows = await supabase_select("inbox_items", filters)
        emails: list[dict[str, Any]] = []
        for row in rows or []:
            md = row.get("metadata") or {}
            emails.append({
                "id": row.get("id"),
                "title": row.get("title"),
                "preview": row.get("preview"),
                "priority": row.get("priority"),
                "status": row.get("status"),
                "unread": row.get("unread"),
                "timestamp": row.get("updated_at") or row.get("created_at"),
                "sender_suite_id": md.get("sender_suite_id"),
                "sender_office_id": md.get("sender_office_id"),
                "recipient_suite_id": md.get("recipient_suite_id"),
                "recipient_office_id": md.get("recipient_office_id"),
                "body": md.get("body"),
                "is_draft": bool(md.get("is_draft", False)),
            })

        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.SUCCESS,
            reason_code="EXECUTED",
        )
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={"messages": emails, "message_count": len(emails)},
            receipt_data=receipt,
        )
    except SupabaseClientError as e:
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="SUPABASE_ERROR",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=str(e),
            receipt_data=receipt,
        )


async def execute_office_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    **kwargs: Any,
) -> ToolExecutionResult:
    tool_id = "internal.office.create"
    recipient_suite_id = str(payload.get("recipient_suite_id", "")).strip()
    recipient_office_id = str(payload.get("recipient_office_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    priority = _normalize_priority(payload.get("priority"))

    if not all([recipient_suite_id, recipient_office_id, title, body]):
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameters: recipient_suite_id, recipient_office_id, title, body",
            receipt_data=receipt,
        )

    try:
        row = _build_office_item(
            recipient_suite_id=recipient_suite_id,
            recipient_office_id=recipient_office_id,
            sender_suite_id=suite_id,
            sender_office_id=office_id,
            title=title,
            body=body,
            priority=priority,
            status="NEW",
            is_draft=False,
        )
        created = await supabase_insert("inbox_items", row)
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.SUCCESS,
            reason_code="EXECUTED",
        )
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={"message_id": created.get("id"), "status": "sent"},
            receipt_data=receipt,
        )
    except SupabaseClientError as e:
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="SUPABASE_ERROR",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=str(e),
            receipt_data=receipt,
        )


async def execute_office_draft(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    **kwargs: Any,
) -> ToolExecutionResult:
    tool_id = "internal.office.draft"
    recipient_suite_id = str(payload.get("recipient_suite_id", "")).strip()
    recipient_office_id = str(payload.get("recipient_office_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    priority = _normalize_priority(payload.get("priority"))

    if not all([recipient_suite_id, recipient_office_id, title, body]):
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameters: recipient_suite_id, recipient_office_id, title, body",
            receipt_data=receipt,
        )

    try:
        row = _build_office_item(
            recipient_suite_id=recipient_suite_id,
            recipient_office_id=recipient_office_id,
            sender_suite_id=suite_id,
            sender_office_id=office_id,
            title=title,
            body=body,
            priority=priority,
            status="WAITING",
            is_draft=True,
        )
        created = await supabase_insert("inbox_items", row)
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.SUCCESS,
            reason_code="EXECUTED",
        )
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={
                "draft_id": created.get("id"),
                "status": "draft",
                "recipient_suite_id": recipient_suite_id,
                "recipient_office_id": recipient_office_id,
            },
            receipt_data=receipt,
        )
    except SupabaseClientError as e:
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="SUPABASE_ERROR",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=str(e),
            receipt_data=receipt,
        )


async def execute_office_send(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    **kwargs: Any,
) -> ToolExecutionResult:
    tool_id = "internal.office.send"
    draft_id = str(payload.get("draft_id", "")).strip()
    if not draft_id:
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error="Missing required parameter: draft_id",
            receipt_data=receipt,
        )

    try:
        updated = await supabase_update(
            "inbox_items",
            f"id=eq.{draft_id}&type=eq.OFFICE",
            {
                "status": "NEW",
                "unread": True,
            },
        )
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.SUCCESS,
            reason_code="EXECUTED",
        )
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id=tool_id,
            data={"message_id": updated.get("id", draft_id), "status": "sent"},
            receipt_data=receipt,
        )
    except SupabaseClientError as e:
        receipt = _mk_receipt(
            tool_id=tool_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            outcome=Outcome.FAILED,
            reason_code="SUPABASE_ERROR",
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id=tool_id,
            error=str(e),
            receipt_data=receipt,
        )
