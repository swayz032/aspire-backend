"""A2P 10DLC registration REST API — Wave 7.

Routes:
  POST /v1/a2p/start         — Yellow, scope a2p:register
  POST /v1/a2p/verify-otp    — Yellow, scope a2p:register
  GET  /v1/a2p/status        — Green, no capability token

Law compliance:
  Law #2 — receipts cut by state machine on every state change.
  Law #3 — fail closed: missing token → 401; profile not approved → 409.
  Law #5 — capability tokens validated server-side (Yellow-gated routes).
  Law #6 — tenant scope from X- headers only, never from request body.
  Law #9 — phone_e164 (OTP target) NEVER in receipts, logs, or responses.
           otp_code is NEVER logged.

Table assumptions (migration 111 + 113):
  tenant_a2p_brands       — 1:1 per suite, brand_status drives state machine
  tenant_a2p_campaigns    — 1 per brand (Sole Prop), campaign_status tracks Twilio
  tenant_trust_profiles   — checked for profile_approved before /start

ARQ queue: 'arq:trust_onboarding' — enqueue advance_a2p_registration after write.

Author: Aspire — Wave 7
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.routes.front_desk import (
    _cap_token_id,
    _resolve_scope,
    _validate_cap_token,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)
from aspire_orchestrator.workers.trust_onboarding.a2p_state_machine import (
    submit_a2p_otp,
)
from aspire_orchestrator.workers.trust_onboarding.trust_receipts import TrustReceiptError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/a2p", tags=["a2p"])


# ---------------------------------------------------------------------------
# Defense-in-depth: Bearer-token presence guard for read endpoints.
#
# policy-gate W7-H1 — without this, GET /v1/a2p/status only validates the
# X-Suite-Id header and returns brand status / OTP attempt counts / rejection
# reasons to anyone who can guess a tenant's UUID. The orchestrator runs
# behind a desktop-server proxy that validates the JWT and forwards X-headers,
# but defense-in-depth requires the orchestrator itself to also reject
# anonymous reads.
#
# This is a presence check only — full JWT verification lives in the proxy.
# Any caller without `Authorization: Bearer ...` gets a 401 immediately,
# before any DB lookup or PostgREST filter that might leak existence info.
# ---------------------------------------------------------------------------


def _require_bearer_token(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
        logger.warning(
            "a2p_routes unauthenticated_read path=%s remote=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "UNAUTHENTICATED",
                "reason_code": "MISSING_BEARER_TOKEN",
            },
        )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_A2P_QUEUE_NAME: str = "arq:trust_onboarding"

# Profile states where A2P is safe to start (Customer Profile must be approved).
_PROFILE_APPROVED_STATES: frozenset[str] = frozenset({
    "profile_approved",
    "shaken_created",
    "shaken_submitted",
    "shaken_approved",
    "cnam_created",
    "cnam_submitted",
    "cnam_approved",
    "number_attached",
    "branded_calling_pending",
    "branded_calling_live",
})

# Valid campaign use cases (mirrors migration 111 CHECK constraint).
_VALID_USE_CASES: frozenset[str] = frozenset({
    "MIXED",
    "2FA",
    "ACCOUNT_NOTIFICATION",
    "CUSTOMER_CARE",
    "DELIVERY_NOTIFICATION",
    "FRAUD_ALERT",
    "HIGHER_EDUCATION",
    "LOW_VOLUME",
    "MARKETING",
    "POLLING_VOTING",
    "PUBLIC_SERVICE_ANNOUNCEMENT",
})


# ---------------------------------------------------------------------------
# ARQ enqueue helper
# ---------------------------------------------------------------------------


async def _enqueue_advance_a2p(suite_id: str) -> None:
    """Push advance_a2p_registration onto the ARQ trust_onboarding queue.

    Best-effort: if Redis is unreachable we log but do NOT fail the HTTP
    response — the brand row is written, W9 cron will recover.
    """
    try:
        from arq.connections import RedisSettings, create_pool  # type: ignore[import-not-found]

        redis_url = settings.redis_url or "redis://localhost:6379"
        redis_settings = RedisSettings.from_dsn(redis_url)
        pool = await create_pool(redis_settings)
        try:
            job_id = f"a2p:{suite_id}:advance"
            await pool.enqueue_job(
                "advance_a2p_registration",
                suite_id,
                _queue_name=_A2P_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=0,
            )
        finally:
            await pool.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "a2p_routes arq_enqueue_failed suite_id=%s err=%s", suite_id, exc,
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class A2PStartRequest(BaseModel):
    brand_type: Literal["sole_proprietor", "standard"] = "sole_proprietor"
    campaign_use_case: str = "MIXED"
    campaign_description: str = Field(..., min_length=10, max_length=500)
    sample_messages: list[str] = Field(..., min_length=1, max_length=5)
    has_embedded_links: bool = False
    has_embedded_phone: bool = False
    capability_token: dict[str, Any] | None = None

    @field_validator("campaign_use_case")
    @classmethod
    def _validate_use_case(cls, v: str) -> str:
        if v not in _VALID_USE_CASES:
            raise ValueError(
                f"campaign_use_case must be one of: {sorted(_VALID_USE_CASES)}"
            )
        return v

    @field_validator("sample_messages")
    @classmethod
    def _validate_samples(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 sample_message required")
        for msg in v:
            if len(msg.strip()) < 5:
                raise ValueError("Each sample_message must be at least 5 characters")
        return v


class A2PVerifyOTPRequest(BaseModel):
    otp_code: str = Field(..., pattern=r"^\d{6}$")
    capability_token: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# POST /v1/a2p/start — Yellow tier, scope a2p:register
# ---------------------------------------------------------------------------


@router.post("/start", status_code=status.HTTP_200_OK)
async def a2p_start(
    body: A2PStartRequest,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Initiate A2P 10DLC registration for the tenant.

    Validates:
      1. Capability token (Yellow, scope a2p:register).
      2. Customer Profile must be approved (trust_state in approved set).
      3. No existing brand (409 if already started).

    Creates:
      - tenant_a2p_brands row in draft state.
      - tenant_a2p_campaigns row in draft state.
      - Enqueues advance_a2p_registration ARQ job.

    Returns:
      { brand_id, campaign_id, brand_status: "draft" }
    """
    # --- 1. Scope resolution (Law #6) ---
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # --- 2. Capability token (Law #5, Yellow tier) ---
    _validate_cap_token(body.capability_token, scope, "a2p:register")
    cap_token_id = _cap_token_id(body.capability_token)

    suite_id = str(scope.suite_id)
    tenant_id = str(scope.tenant_id)
    office_id = str(scope.office_id)

    # --- 3. Check Customer Profile is approved ---
    try:
        profile_rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("a2p_routes start profile_select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not profile_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PROFILE_NOT_READY",
                "reason_code": "PROFILE_NOT_READY",
                "message": (
                    "No trust profile found. Complete KYB onboarding first."
                ),
            },
        )

    trust_state = str(profile_rows[0].get("trust_state", ""))
    if trust_state not in _PROFILE_APPROVED_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PROFILE_NOT_READY",
                "reason_code": "PROFILE_NOT_READY",
                "trust_state": trust_state,
                "message": (
                    f"Customer Profile must be approved before A2P registration. "
                    f"Current state: {trust_state}"
                ),
            },
        )

    # --- 4. Check for existing brand (idempotent 409) ---
    try:
        existing_brand = await supabase_select(
            "tenant_a2p_brands",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("a2p_routes start brand_select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if existing_brand:
        brand = existing_brand[0]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "A2P_ALREADY_STARTED",
                "brand_id": str(brand.get("id", "")),
                "brand_status": brand.get("brand_status", ""),
            },
        )

    # --- 5. Create brand row in draft ---
    brand_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    brand_row: dict[str, Any] = {
        "id": brand_id,
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "brand_type": body.brand_type,
        "brand_status": "draft",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        await supabase_insert("tenant_a2p_brands", brand_row)
    except SupabaseClientError as exc:
        detail_str = str(exc.detail or "").lower()
        if "duplicate" in detail_str or "unique" in detail_str or exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "A2P_ALREADY_STARTED"},
            ) from exc
        logger.error("a2p_routes start brand_insert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    # --- 6. Create campaign row in draft ---
    campaign_id = str(uuid.uuid4())
    campaign_row: dict[str, Any] = {
        "id": campaign_id,
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "brand_id": brand_id,
        "campaign_use_case": body.campaign_use_case,
        "campaign_description": body.campaign_description,
        "sample_messages": body.sample_messages,
        "has_embedded_links": body.has_embedded_links,
        "has_embedded_phone": body.has_embedded_phone,
        "campaign_status": "draft",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        await supabase_insert("tenant_a2p_campaigns", campaign_row)
    except SupabaseClientError as exc:
        logger.error("a2p_routes start campaign_insert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    # --- 7. Enqueue ARQ job (best-effort) ---
    await _enqueue_advance_a2p(suite_id)

    logger.info(
        "a2p_routes start brand_id=%s campaign_id=%s suite_id=%s cap_token=%s",
        brand_id, campaign_id, suite_id, cap_token_id or "<none>",
    )

    return {
        "brand_id": brand_id,
        "campaign_id": campaign_id,
        "brand_status": "draft",
        "campaign_status": "draft",
    }


# ---------------------------------------------------------------------------
# POST /v1/a2p/verify-otp — Yellow tier, scope a2p:register
# ---------------------------------------------------------------------------


@router.post("/verify-otp", status_code=status.HTTP_200_OK)
async def a2p_verify_otp(
    body: A2PVerifyOTPRequest,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Submit the OTP code Twilio sent to the authorized rep's phone.

    Validates:
      1. Capability token (Yellow, scope a2p:register).
      2. Brand must exist with a pending brand_registration_sid.
      3. Submits OTP to Twilio via state machine helper.

    Returns:
      { brand_id, brand_status, otp_attempts, locked_out, receipt_id }

    Error codes:
      400 INVALID_OTP    — wrong code, attempts < 3
      429 OTP_LOCKED_OUT — wrong code, 3rd failure (brand suspended)
      409 NO_BRAND_RECORD — no brand row found
    """
    # --- 1. Scope + token ---
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(body.capability_token, scope, "a2p:register")
    cap_token_id = _cap_token_id(body.capability_token)

    suite_id = str(scope.suite_id)

    # --- 2. Delegate to state machine OTP helper (Law #1 — no logic in route) ---
    # Law #9: body.otp_code never logged here; state machine also never logs it.
    # policy-gate W7-H2: cap_token_id flows into receipts for audit chain.
    result = await submit_a2p_otp(
        suite_id=suite_id,
        otp_code=body.otp_code,
        capability_token_id=cap_token_id,
    )

    reason_code = result.get("reason_code", "")

    if not result.get("success"):
        if reason_code == "OTP_LOCKED_OUT" or result.get("locked_out"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "OTP_LOCKED_OUT",
                    "reason_code": "OTP_LOCKED_OUT",
                    "brand_id": result.get("brand_id", ""),
                    "otp_attempts": result.get("otp_attempts", 0),
                },
            )
        if reason_code in ("INVALID_OTP", "MISSING_BRAND_REGISTRATION_SID"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "INVALID_OTP",
                    "reason_code": "INVALID_OTP",
                    "brand_id": result.get("brand_id", ""),
                    "otp_attempts": result.get("otp_attempts", 0),
                },
            )
        if reason_code == "NO_BRAND_RECORD":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "NO_BRAND_RECORD", "reason_code": "NO_BRAND_RECORD"},
            )
        # policy-gate W7-M1: re-submitting OTP after success returns a stable
        # 409 instead of falling through to the generic 500 path. This
        # prevents duplicate ARQ jobs / duplicate vetting POSTs.
        if reason_code == "OTP_ALREADY_CONFIRMED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "OTP_ALREADY_CONFIRMED",
                    "reason_code": "OTP_ALREADY_CONFIRMED",
                    "brand_id": result.get("brand_id", ""),
                    "brand_status": result.get("brand_status", "otp_confirmed"),
                },
            )
        # Fallback
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": reason_code or "OTP_VERIFY_FAILED"},
        )

    # OTP accepted — enqueue next step (otp_confirmed → vetting POST).
    await _enqueue_advance_a2p(suite_id)

    return {
        "brand_id": result.get("brand_id", ""),
        "brand_status": result.get("brand_status", "otp_confirmed"),
        "otp_attempts": result.get("otp_attempts", 0),
        "locked_out": False,
        "receipt_id": result.get("receipt_id"),
    }


# ---------------------------------------------------------------------------
# GET /v1/a2p/status — Green tier, no capability token
# ---------------------------------------------------------------------------


@router.get("/status", status_code=status.HTTP_200_OK)
async def a2p_status(
    request: Request,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Return current A2P brand + campaign status for the tenant.

    Green tier — no capability token required (read-only). However, a
    Bearer token IS required: without it, anyone who guesses a tenant's
    suite UUID could read brand status, OTP-attempt counters, and
    rejection reasons (policy-gate W7-H1).

    Returns 404 if A2P has not been started yet.
    """
    _require_bearer_token(request)
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    try:
        brand_rows = await supabase_select(
            "tenant_a2p_brands",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("a2p_routes status brand_select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not brand_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_A2P_REGISTRATION", "suite_id": suite_id},
        )

    brand = brand_rows[0]
    brand_id = str(brand.get("id", ""))
    brand_status = str(brand.get("brand_status", ""))

    # Load campaign
    campaign_status: str | None = None
    campaign_id: str | None = None
    otp_required = False

    try:
        campaign_rows = await supabase_select(
            "tenant_a2p_campaigns",
            f"brand_id=eq.{brand_id}",
            limit=1,
        )
        if campaign_rows:
            campaign_id = str(campaign_rows[0].get("id", ""))
            campaign_status = str(campaign_rows[0].get("campaign_status", ""))
    except SupabaseClientError:
        pass  # best-effort; brand is the source of truth

    # Compute otp_required: brand is pending AND otp_verified_at is null
    if brand_status == "pending" and not brand.get("otp_verified_at"):
        otp_required = True

    return {
        "brand_id": brand_id,
        "brand_status": brand_status,
        "brand_type": brand.get("brand_type", "sole_proprietor"),
        "campaign_id": campaign_id,
        "campaign_status": campaign_status,
        "otp_required": otp_required,
        "otp_verified_at": brand.get("otp_verified_at"),
        "submitted_at": brand.get("submitted_at"),
        "approved_at": brand.get("approved_at"),
        "rejection_reason": (
            brand.get("rejection_reason")
            if brand_status in ("rejected", "suspended")
            else None
        ),
    }


__all__ = ["router"]
