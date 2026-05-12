"""Shared scope resolution helper — extracted from front_desk.py (architect R5).

All routes that need X-Tenant-Id / X-Suite-Id / X-Office-Id header resolution
import _resolve_scope from here rather than duplicating the logic.

Law #3: Fail closed — missing or malformed headers raise HTTP 401/422.
Law #6: Tenant isolation — all three IDs are required and validated as UUIDs.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity


def _resolve_scope(
    x_tenant_id: str | None,
    x_suite_id: str | None,
    x_office_id: str | None,
) -> ScopedIdentity:
    """Validate and parse Gateway-trusted scope headers into a ScopedIdentity.

    Raises HTTP 401 if any header is missing.
    Raises HTTP 422 if any header is not a valid UUID.
    Never silently degrades (Law #3).
    """
    from uuid import UUID

    if not x_tenant_id or not x_suite_id or not x_office_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_SCOPE_HEADERS"},
        )
    try:
        return ScopedIdentity(
            tenant_id=UUID(x_tenant_id),
            suite_id=UUID(x_suite_id),
            office_id=UUID(x_office_id),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_SCOPE_HEADERS", "message": str(exc)},
        ) from exc
