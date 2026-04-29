"""POST /v1/tools/* — Bounded tool endpoints invoked by the Desktop client.

Currently exposes:
  - POST /v1/tools/enrich_product — Lazy SerpApi `home_depot_product` fetch.

Why lazy? The basic SerpApi `home_depot` search returns enough fields for the
voice carousel. Detail fields (gallery, specs, bay/aisle) cost an extra SerpApi
unit per product; we only spend that unit when the user opens the modal.

Law compliance:
  - Law #1: This endpoint executes a bounded tool. Decisions stay with the
    orchestrator — the Desktop only calls this AFTER the user explicitly opens
    a product card.
  - Law #2: Every call emits a receipt (success or failure) via the SerpApi
    client's `make_receipt_data`.
  - Law #3: Missing auth headers -> 401 + denial receipt.
  - Law #5: Capability token required (validated via token_service).
  - Law #6: suite_id/office_id from request headers; mismatch -> 401.
  - Risk tier: GREEN (read-only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aspire_orchestrator.providers.serpapi_homedepot_product_client import (
    fetch_product_details,
)
from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.token_service import (
    compute_token_hash,
    validate_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_TOOL_ID = "serpapi_home_depot_product.fetch"
_REQUIRED_SCOPE = "research.product.enrich"


class EnrichProductRequest(BaseModel):
    product_id: str = Field(min_length=1)
    store_id: str | None = None
    capability_token: dict[str, Any] | None = None


def _denial_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    actor_id: str,
    reason_code: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": "system",
        "actor_id": actor_id,
        "action_type": "tool.enrich_product",
        "risk_tier": "green",
        "tool_used": _TOOL_ID,
        "outcome": "denied",
        "reason_code": reason_code,
        "created_at": now,
        "receipt_type": "tool",
        "receipt_hash": "",
        "redacted_inputs": None,
        "redacted_outputs": details,
    }
    canonical = json.dumps(
        {k: str(v) for k, v in receipt.items() if k != "receipt_hash"},
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt["receipt_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return receipt


def _error(
    *,
    error: str,
    message: str,
    correlation_id: str,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": error,
            "message": message,
            "correlation_id": correlation_id,
        },
    )


@router.post("/v1/tools/enrich_product")
async def enrich_product(request: Request) -> JSONResponse:
    """Lazy product enrichment — calls SerpApi `home_depot_product` engine.

    Headers (required, Law #3):
      - X-Suite-Id, X-Office-Id, X-Actor-Id, X-Correlation-Id

    Body:
      - product_id: str (required) — Home Depot internet/product number
      - store_id: str (optional) — for bay/aisle/local stock fields
      - capability_token: dict (required, Law #5) — must scope research.product.enrich
    """
    suite_id = request.headers.get("x-suite-id") or ""
    office_id = request.headers.get("x-office-id") or ""
    actor_id = request.headers.get("x-actor-id") or ""
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())

    if not suite_id or not office_id or not actor_id:
        missing = [
            h
            for h, v in (
                ("X-Suite-Id", suite_id),
                ("X-Office-Id", office_id),
                ("X-Actor-Id", actor_id),
            )
            if not v
        ]
        store_receipts(
            [
                _denial_receipt(
                    correlation_id=correlation_id,
                    suite_id=suite_id or "unknown",
                    office_id=office_id or "unknown",
                    actor_id="fail_closed_guard",
                    reason_code="AUTH_REQUIRED",
                    details={"missing_headers": missing},
                )
            ]
        )
        return _error(
            error="AUTH_REQUIRED",
            message=f"Missing required auth headers: {', '.join(missing)}",
            correlation_id=correlation_id,
            status_code=401,
        )

    try:
        body_raw = await request.json()
    except Exception:
        return _error(
            error="SCHEMA_VALIDATION_FAILED",
            message="Invalid JSON body",
            correlation_id=correlation_id,
        )

    try:
        req = EnrichProductRequest(**body_raw)
    except Exception as exc:
        return _error(
            error="SCHEMA_VALIDATION_FAILED",
            message=f"Request validation failed: {exc}",
            correlation_id=correlation_id,
        )

    # -- Capability token validation (Law #5) --------------------------------
    if not req.capability_token:
        store_receipts(
            [
                _denial_receipt(
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    actor_id=actor_id,
                    reason_code="CAPABILITY_TOKEN_REQUIRED",
                )
            ]
        )
        return _error(
            error="CAPABILITY_TOKEN_REQUIRED",
            message="capability_token is required",
            correlation_id=correlation_id,
            status_code=401,
        )

    token_validation = validate_token(
        req.capability_token,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        required_scope=_REQUIRED_SCOPE,
    )
    if not token_validation.valid:
        reason = (
            token_validation.error.value
            if token_validation.error is not None
            else "TOKEN_INVALID"
        )
        store_receipts(
            [
                _denial_receipt(
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    actor_id=actor_id,
                    reason_code=reason,
                    details={"error_message": token_validation.error_message},
                )
            ]
        )
        return _error(
            error="CAPABILITY_TOKEN_INVALID",
            message=token_validation.error_message or "Capability token rejected",
            correlation_id=correlation_id,
            status_code=403,
        )

    capability_token_id = req.capability_token.get("token_id")
    capability_token_hash = compute_token_hash(req.capability_token)

    # -- Execute SerpApi product fetch (receipt is emitted by the client) ----
    result = await fetch_product_details(
        product_id=req.product_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        store_id=req.store_id,
    )

    if result.receipt_data is not None:
        store_receipts([result.receipt_data])

    if result.outcome.value != "success":
        return JSONResponse(
            status_code=502,
            content={
                "error": "PROVIDER_FAILED",
                "message": result.error or "Product enrichment failed",
                "correlation_id": correlation_id,
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "correlation_id": correlation_id,
            "product": result.data,
        },
    )
