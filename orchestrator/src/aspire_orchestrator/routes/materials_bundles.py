"""POST|GET /v1/materials/bundles/* — bundle CRUD + push-to-estimate (Pass D).

Law compliance:
  Law #2 — immutable receipt on every outcome (success, denied, error).
  Law #3 — fail closed: missing scope headers -> 401, empty project_id -> 400.
  Law #4 — GREEN tier for reads/add/remove/update/clear; YELLOW for push-to-estimate.
  Law #5 — capability token validated server-side before execution.
  Law #6 — tenant isolation: suite_id / office_id enforced; cross-project isolation.
  Law #7 — adapter never retries; returns result to orchestrator.
  Law #9 — product_payload stored as-is (PII-free retail data); no secrets logged.

Endpoints:
  POST /v1/materials/bundles/add
  POST /v1/materials/bundles/remove
  POST /v1/materials/bundles/update-quantity
  POST /v1/materials/bundles/clear
  GET  /v1/materials/bundles
  POST /v1/materials/bundles/push-to-estimate
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.routes._scope import _resolve_scope
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_delete,
    supabase_insert,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.services.token_service import validate_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/materials/bundles", tags=["materials-bundles"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ProductPayload(BaseModel):
    """Minimal Product snapshot — full object stored; this validates required fields."""

    id: str
    title: str
    price: float
    fetched_at: str
    # Accept arbitrary extra fields (brand, sku, imageUrl, store, etc.)
    model_config = {"extra": "allow"}


class AddToBundleRequest(BaseModel):
    project_id: str
    product: ProductPayload
    quantity: float = 1.0
    store_id: str | None = None
    category_hint: str | None = None
    idempotency_key: str | None = None
    capability_token: str | None = None


class RemoveFromBundleRequest(BaseModel):
    project_id: str
    bundle_item_id: str
    capability_token: str | None = None


class UpdateQuantityRequest(BaseModel):
    project_id: str
    bundle_item_id: str
    quantity: float
    capability_token: str | None = None


class ClearBundleRequest(BaseModel):
    project_id: str
    capability_token: str | None = None


class PushToEstimateRequest(BaseModel):
    project_id: str
    capability_token: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_REQUIRED_TOKEN_WRITE_SCOPE = "materials:bundles.write"
_REQUIRED_TOKEN_READ_SCOPE = "materials:bundles.read"
_REQUIRED_TOKEN_PUSH_SCOPE = "materials:bundles.push"


def _parse_cap_token(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


def _cap_token_id(cap_token: dict[str, Any] | None) -> str | None:
    if not cap_token:
        return None
    if cap_token.get("id"):
        return str(cap_token["id"])
    sig = cap_token.get("signature") or cap_token.get("token") or ""
    if sig:
        import hashlib
        return hashlib.sha256(str(sig).encode()).hexdigest()[:16]
    return None


def _emit_receipt(
    *,
    receipt_type: str,
    action_type: str,
    risk_tier: str,
    suite_id: str,
    office_id: str,
    tenant_id: str,
    outcome: str,
    reason_code: str,
    correlation_id: str,
    trace_id: str,
    capability_token_id: str | None,
    redacted_inputs: dict[str, Any] | None = None,
    redacted_outputs: dict[str, Any] | None = None,
) -> str:
    """Emit immutable receipt; return its ID (Law #2)."""
    receipt_id = str(uuid.uuid4())
    receipt: dict[str, Any] = {
        "id": receipt_id,
        "receipt_type": receipt_type,
        "suite_id": suite_id,
        "office_id": office_id,
        "tenant_id": tenant_id,
        "outcome": outcome,
        "action_type": action_type,
        "tool_used": "materials_bundles",
        "risk_tier": risk_tier,
        "reason_code": reason_code,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "capability_token_id": capability_token_id or "",
        "created_at": _now_iso(),
    }
    if redacted_inputs:
        receipt["redacted_inputs"] = redacted_inputs
    if redacted_outputs:
        receipt["redacted_outputs"] = redacted_outputs
    receipt_store.store_receipts([receipt])
    return receipt_id


def _require_token(
    raw_token: str | None,
    *,
    suite_id: str,
    office_id: str,
    tenant_id: str,
    required_scope: str,
    action_type: str,
    risk_tier: str,
    correlation_id: str,
    trace_id: str,
) -> dict[str, Any]:
    """Validate capability token. Raises HTTP 401 on failure (Law #3 / Law #5)."""
    cap_token = _parse_cap_token(raw_token)
    if cap_token is None:
        receipt_id = _emit_receipt(
            receipt_type="materials_bundle_denied",
            action_type=action_type,
            risk_tier=risk_tier,
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="denied", reason_code="MISSING_CAPABILITY_TOKEN",
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_CAPABILITY_TOKEN", "receipt_id": receipt_id},
        )

    result = validate_token(
        cap_token,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope=required_scope,
    )
    if not result.valid:
        err_code = result.error.value if result.error else "INVALID_TOKEN"
        receipt_id = _emit_receipt(
            receipt_type="materials_bundle_denied",
            action_type=action_type,
            risk_tier=risk_tier,
            suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
            outcome="denied", reason_code=err_code,
            correlation_id=correlation_id, trace_id=trace_id,
            capability_token_id=None,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": err_code, "receipt_id": receipt_id},
        )
    return cap_token


def _validate_project_id(project_id: str, *, receipt_kwargs: dict[str, Any]) -> None:
    """Reject empty or overly-long project_id (Law #3)."""
    if not project_id or not project_id.strip():
        receipt_id = _emit_receipt(
            reason_code="INVALID_INPUT",
            outcome="failed",
            **receipt_kwargs,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "project_id is required", "receipt_id": receipt_id},
        )
    if len(project_id) > 500:
        receipt_id = _emit_receipt(
            reason_code="INVALID_INPUT",
            outcome="failed",
            **receipt_kwargs,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "project_id too long", "receipt_id": receipt_id},
        )


def _scope_headers(
    x_tenant_id: str | None,
    x_suite_id: str | None,
    x_office_id: str | None,
) -> tuple[str, str, str, str, str]:
    """Resolve scope headers + correlation/trace IDs."""
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)
    tenant_id = str(scope.tenant_id)
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    trace_id = get_trace_id() or correlation_id
    return suite_id, office_id, tenant_id, correlation_id, trace_id


async def _list_bundle(project_id: str, suite_id: str, office_id: str) -> list[dict[str, Any]]:
    """Fetch active (not pushed) bundle rows for a project/suite."""
    rows = await supabase_select(
        "material_bundles",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&pushed_to_estimate=eq.false",
        order_by="created_at.asc",
    )
    return rows or []


def _bundle_stats(rows: list[dict[str, Any]]) -> tuple[float, int]:
    """Return (subtotal, supplier_count) from bundle rows."""
    subtotal = 0.0
    store_ids: set[str] = set()
    for row in rows:
        payload = row.get("product_payload") or {}
        unit_price = row.get("unit_price") or payload.get("price") or 0.0
        qty = float(row.get("quantity") or 1)
        subtotal += float(unit_price) * qty
        sid = row.get("store_id") or ""
        if sid:
            store_ids.add(sid)
    return round(subtotal, 2), len(store_ids)


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a DB row for API output."""
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "product": row.get("product_payload"),
        "store_id": row.get("store_id"),
        "category_hint": row.get("category_hint"),
        "quantity": float(row.get("quantity") or 1),
        "unit_price": float(row.get("unit_price") or 0),
        "fetched_at": row.get("fetched_at"),
        "pushed_to_estimate": row.get("pushed_to_estimate", False),
        "estimate_draft_id": row.get("estimate_draft_id"),
        "created_at": row.get("created_at"),
    }


# ---------------------------------------------------------------------------
# GET /v1/materials/bundles  — list bundle for a project
# ---------------------------------------------------------------------------


@router.get("")
async def list_bundle(
    project_id: str = Query(..., description="Project address / ID"),
    capability_token: str | None = Query(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """List active bundle items for a project. GREEN tier."""
    suite_id, office_id, tenant_id, correlation_id, trace_id = _scope_headers(
        x_tenant_id, x_suite_id, x_office_id
    )
    _action = "materials.bundle.list"
    _tier = "green"

    cap_token = _require_token(
        capability_token,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        required_scope=_REQUIRED_TOKEN_READ_SCOPE,
        action_type=_action, risk_tier=_tier,
        correlation_id=correlation_id, trace_id=trace_id,
    )
    cap_token_id = _cap_token_id(cap_token)

    _receipt_kw: dict[str, Any] = dict(
        receipt_type="materials_bundle_list",
        action_type=_action, risk_tier=_tier,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
    )
    _validate_project_id(project_id, receipt_kwargs=_receipt_kw)

    try:
        rows = await _list_bundle(project_id, suite_id, office_id)
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR", **_receipt_kw,
        )
        logger.error("bundle list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    subtotal, supplier_count = _bundle_stats(rows)
    receipt_id = _emit_receipt(
        outcome="success", reason_code="EXECUTED",
        redacted_outputs={"item_count": len(rows), "bundle_subtotal": subtotal},
        **_receipt_kw,
    )
    return {
        "success": True,
        "items": [_serialize_row(r) for r in rows],
        "bundle_subtotal": subtotal,
        "bundle_supplier_count": supplier_count,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/materials/bundles/add
# ---------------------------------------------------------------------------


@router.post("/add")
async def add_to_bundle(
    body: AddToBundleRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Add a product to the bundle (dedup: same product_id -> increment quantity). GREEN tier."""
    suite_id, office_id, tenant_id, correlation_id, trace_id = _scope_headers(
        x_tenant_id, x_suite_id, x_office_id
    )
    _action = "materials.bundle.add"
    _tier = "green"

    cap_token = _require_token(
        body.capability_token,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        required_scope=_REQUIRED_TOKEN_WRITE_SCOPE,
        action_type=_action, risk_tier=_tier,
        correlation_id=correlation_id, trace_id=trace_id,
    )
    cap_token_id = _cap_token_id(cap_token)

    _receipt_kw: dict[str, Any] = dict(
        receipt_type="materials_bundle_add",
        action_type=_action, risk_tier=_tier,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
    )
    _validate_project_id(body.project_id, receipt_kwargs=_receipt_kw)

    product_id = body.product.id
    product_dict = body.product.model_dump()

    # -- Idempotency dedup: if same idempotency_key already exists, return cached list
    if body.idempotency_key:
        try:
            existing_idem = await supabase_select(
                "material_bundles",
                f"suite_id=eq.{suite_id}&project_id=eq.{body.project_id}",
                limit=200,
            )
            # Check for a row that matches this idempotency_key stored in product_payload meta
            idem_match = next(
                (r for r in (existing_idem or [])
                 if (r.get("product_payload") or {}).get("_idempotency_key") == body.idempotency_key),
                None,
            )
            if idem_match:
                rows = await _list_bundle(body.project_id, suite_id, office_id)
                subtotal, supplier_count = _bundle_stats(rows)
                receipt_id = _emit_receipt(
                    outcome="success", reason_code="IDEMPOTENCY_REPLAY", **_receipt_kw,
                )
                return {
                    "success": True,
                    "items": [_serialize_row(r) for r in rows],
                    "bundle_subtotal": subtotal,
                    "bundle_supplier_count": supplier_count,
                    "receipt_id": receipt_id,
                }
        except SupabaseClientError:
            pass  # Non-fatal — fall through to normal add

    try:
        # Check if this product is already in the bundle
        existing_rows = await supabase_select(
            "material_bundles",
            f"project_id=eq.{body.project_id}&suite_id=eq.{suite_id}&pushed_to_estimate=eq.false",
            limit=200,
        )
        existing_rows = existing_rows or []
        dup = next(
            (r for r in existing_rows
             if (r.get("product_payload") or {}).get("id") == product_id),
            None,
        )

        if dup:
            # Increment quantity on existing row
            new_qty = float(dup.get("quantity") or 1) + float(body.quantity)
            await supabase_update(
                "material_bundles",
                f"id=eq.{dup['id']}",
                {"quantity": new_qty},
            )
        else:
            # Tag idempotency key into payload meta if provided
            if body.idempotency_key:
                product_dict["_idempotency_key"] = body.idempotency_key

            await supabase_insert(
                "material_bundles",
                {
                    "id": str(uuid.uuid4()),
                    "project_id": body.project_id,
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "product_payload": product_dict,
                    "store_id": body.store_id or (product_dict.get("store") or {}).get("id") or "",
                    "category_hint": body.category_hint or "",
                    "quantity": float(body.quantity),
                    "unit_price": float(body.product.price),
                    "fetched_at": body.product.fetched_at,
                    "pushed_to_estimate": False,
                    "created_at": _now_iso(),
                },
            )

        rows = await _list_bundle(body.project_id, suite_id, office_id)
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR", **_receipt_kw,
        )
        logger.error("bundle add failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    subtotal, supplier_count = _bundle_stats(rows)
    receipt_id = _emit_receipt(
        outcome="success", reason_code="EXECUTED",
        redacted_inputs={"product_id": product_id, "quantity": body.quantity},
        redacted_outputs={"item_count": len(rows), "bundle_subtotal": subtotal},
        **_receipt_kw,
    )
    logger.info("bundle add suite=%s project=%s product=%s qty=%s items=%d",
                suite_id[:8], body.project_id[:40], product_id[:20], body.quantity, len(rows))
    return {
        "success": True,
        "items": [_serialize_row(r) for r in rows],
        "bundle_subtotal": subtotal,
        "bundle_supplier_count": supplier_count,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/materials/bundles/remove
# ---------------------------------------------------------------------------


@router.post("/remove")
async def remove_from_bundle(
    body: RemoveFromBundleRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Remove one item by ID. GREEN tier."""
    suite_id, office_id, tenant_id, correlation_id, trace_id = _scope_headers(
        x_tenant_id, x_suite_id, x_office_id
    )
    _action = "materials.bundle.remove"
    _tier = "green"

    cap_token = _require_token(
        body.capability_token,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        required_scope=_REQUIRED_TOKEN_WRITE_SCOPE,
        action_type=_action, risk_tier=_tier,
        correlation_id=correlation_id, trace_id=trace_id,
    )
    cap_token_id = _cap_token_id(cap_token)

    _receipt_kw: dict[str, Any] = dict(
        receipt_type="materials_bundle_remove",
        action_type=_action, risk_tier=_tier,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
    )
    _validate_project_id(body.project_id, receipt_kwargs=_receipt_kw)

    # Enforce tenant isolation: only delete rows owned by this suite
    try:
        await supabase_delete(
            "material_bundles",
            f"id=eq.{body.bundle_item_id}&suite_id=eq.{suite_id}",
        )
        rows = await _list_bundle(body.project_id, suite_id, office_id)
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR", **_receipt_kw,
        )
        logger.error("bundle remove failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    subtotal, supplier_count = _bundle_stats(rows)
    receipt_id = _emit_receipt(
        outcome="success", reason_code="EXECUTED",
        redacted_inputs={"bundle_item_id": body.bundle_item_id},
        redacted_outputs={"item_count": len(rows), "bundle_subtotal": subtotal},
        **_receipt_kw,
    )
    return {
        "success": True,
        "items": [_serialize_row(r) for r in rows],
        "bundle_subtotal": subtotal,
        "bundle_supplier_count": supplier_count,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/materials/bundles/update-quantity
# ---------------------------------------------------------------------------


@router.post("/update-quantity")
async def update_bundle_quantity(
    body: UpdateQuantityRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Update item quantity. If quantity <= 0, removes the item. GREEN tier."""
    suite_id, office_id, tenant_id, correlation_id, trace_id = _scope_headers(
        x_tenant_id, x_suite_id, x_office_id
    )
    _action = "materials.bundle.update_quantity"
    _tier = "green"

    cap_token = _require_token(
        body.capability_token,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        required_scope=_REQUIRED_TOKEN_WRITE_SCOPE,
        action_type=_action, risk_tier=_tier,
        correlation_id=correlation_id, trace_id=trace_id,
    )
    cap_token_id = _cap_token_id(cap_token)

    _receipt_kw: dict[str, Any] = dict(
        receipt_type="materials_bundle_update_quantity",
        action_type=_action, risk_tier=_tier,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
    )
    _validate_project_id(body.project_id, receipt_kwargs=_receipt_kw)

    if body.quantity < 0:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="INVALID_INPUT",
            redacted_inputs={"quantity": body.quantity},
            **_receipt_kw,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_INPUT", "message": "quantity must be >= 0", "receipt_id": receipt_id},
        )

    try:
        if body.quantity == 0:
            await supabase_delete(
                "material_bundles",
                f"id=eq.{body.bundle_item_id}&suite_id=eq.{suite_id}",
            )
        else:
            await supabase_update(
                "material_bundles",
                f"id=eq.{body.bundle_item_id}&suite_id=eq.{suite_id}",
                {"quantity": body.quantity},
            )
        rows = await _list_bundle(body.project_id, suite_id, office_id)
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR", **_receipt_kw,
        )
        logger.error("bundle update-quantity failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    subtotal, supplier_count = _bundle_stats(rows)
    receipt_id = _emit_receipt(
        outcome="success", reason_code="EXECUTED",
        redacted_inputs={"bundle_item_id": body.bundle_item_id, "quantity": body.quantity},
        redacted_outputs={"item_count": len(rows), "bundle_subtotal": subtotal},
        **_receipt_kw,
    )
    return {
        "success": True,
        "items": [_serialize_row(r) for r in rows],
        "bundle_subtotal": subtotal,
        "bundle_supplier_count": supplier_count,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/materials/bundles/clear
# ---------------------------------------------------------------------------


@router.post("/clear")
async def clear_bundle(
    body: ClearBundleRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Clear all non-pushed items for a project. GREEN tier."""
    suite_id, office_id, tenant_id, correlation_id, trace_id = _scope_headers(
        x_tenant_id, x_suite_id, x_office_id
    )
    _action = "materials.bundle.clear"
    _tier = "green"

    cap_token = _require_token(
        body.capability_token,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        required_scope=_REQUIRED_TOKEN_WRITE_SCOPE,
        action_type=_action, risk_tier=_tier,
        correlation_id=correlation_id, trace_id=trace_id,
    )
    cap_token_id = _cap_token_id(cap_token)

    _receipt_kw: dict[str, Any] = dict(
        receipt_type="materials_bundle_clear",
        action_type=_action, risk_tier=_tier,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
    )
    _validate_project_id(body.project_id, receipt_kwargs=_receipt_kw)

    try:
        # Delete all non-pushed items for this project scoped to this suite
        await supabase_delete(
            "material_bundles",
            f"project_id=eq.{body.project_id}&suite_id=eq.{suite_id}&pushed_to_estimate=eq.false",
        )
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR", **_receipt_kw,
        )
        logger.error("bundle clear failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    receipt_id = _emit_receipt(
        outcome="success", reason_code="EXECUTED",
        redacted_inputs={"project_id": body.project_id[:40]},
        **_receipt_kw,
    )
    return {
        "success": True,
        "items": [],
        "bundle_subtotal": 0.0,
        "bundle_supplier_count": 0,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/materials/bundles/push-to-estimate  — YELLOW tier
# ---------------------------------------------------------------------------


@router.post("/push-to-estimate")
async def push_to_estimate(
    body: PushToEstimateRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
    x_suite_id: str | None = Header(None, alias="X-Suite-Id"),
    x_office_id: str | None = Header(None, alias="X-Office-Id"),
) -> dict[str, Any]:
    """Push current bundle to an estimate draft. YELLOW tier (state change with money implications).

    Law #4 — YELLOW: explicit user confirmation required before this endpoint is called.
             The Express proxy will enforce this with a Yellow-tier gate before minting.
    Law #2 — immutable receipt with action_type=materials.bundle.push_to_estimate.
    """
    suite_id, office_id, tenant_id, correlation_id, trace_id = _scope_headers(
        x_tenant_id, x_suite_id, x_office_id
    )
    _action = "materials.bundle.push_to_estimate"
    _tier = "yellow"

    cap_token = _require_token(
        body.capability_token,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        required_scope=_REQUIRED_TOKEN_PUSH_SCOPE,
        action_type=_action, risk_tier=_tier,
        correlation_id=correlation_id, trace_id=trace_id,
    )
    cap_token_id = _cap_token_id(cap_token)

    _receipt_kw: dict[str, Any] = dict(
        receipt_type="materials_bundle_push_to_estimate",
        action_type=_action, risk_tier=_tier,
        suite_id=suite_id, office_id=office_id, tenant_id=tenant_id,
        correlation_id=correlation_id, trace_id=trace_id,
        capability_token_id=cap_token_id,
    )
    _validate_project_id(body.project_id, receipt_kwargs=_receipt_kw)

    try:
        rows = await _list_bundle(body.project_id, suite_id, office_id)
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR", **_receipt_kw,
        )
        logger.error("push-to-estimate list failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    if not rows:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="BUNDLE_EMPTY",
            redacted_inputs={"project_id": body.project_id[:40]},
            **_receipt_kw,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "BUNDLE_EMPTY", "message": "Cannot push an empty bundle to estimate", "receipt_id": receipt_id},
        )

    subtotal, supplier_count = _bundle_stats(rows)
    estimate_draft_id = str(uuid.uuid4())

    # Create estimate_drafts row
    draft_row = {
        "id": estimate_draft_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "project_id": body.project_id,
        "items": [_serialize_row(r) for r in rows],
        "subtotal": subtotal,
        "supplier_count": supplier_count,
        "item_count": sum(int(r.get("quantity") or 1) for r in rows),
        "source": "materials_bundle",
        "status": "draft",
        "correlation_id": correlation_id,
        "created_at": _now_iso(),
    }
    # Mark all rows as pushed (atomic-ish via two operations)
    item_ids = [r["id"] for r in rows if r.get("id")]

    try:
        await supabase_insert("estimate_drafts", draft_row)
        # Mark each item pushed_to_estimate = true
        for item_id in item_ids:
            await supabase_update(
                "material_bundles",
                f"id=eq.{item_id}&suite_id=eq.{suite_id}",
                {"pushed_to_estimate": True, "estimate_draft_id": estimate_draft_id},
            )
    except SupabaseClientError as exc:
        receipt_id = _emit_receipt(
            outcome="failed", reason_code="PROVIDER_INTERNAL_ERROR",
            redacted_inputs={"item_count": len(rows), "bundle_subtotal": subtotal},
            **_receipt_kw,
        )
        logger.error("push-to-estimate write failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "PROVIDER_INTERNAL_ERROR", "receipt_id": receipt_id},
        )

    receipt_id = _emit_receipt(
        outcome="success", reason_code="EXECUTED",
        redacted_inputs={
            "project_id": body.project_id[:40],
            "item_count": len(rows),
        },
        redacted_outputs={
            "estimate_draft_id": estimate_draft_id,
            "bundle_subtotal": subtotal,
            "supplier_count": supplier_count,
        },
        **_receipt_kw,
    )
    logger.info(
        "push-to-estimate suite=%s project=%s items=%d draft=%s",
        suite_id[:8], body.project_id[:40], len(rows), estimate_draft_id[:8],
    )
    return {
        "success": True,
        "estimate_draft_id": estimate_draft_id,
        "bundle_subtotal": subtotal,
        "bundle_supplier_count": supplier_count,
        "item_count": len(rows),
        "receipt_id": receipt_id,
    }
