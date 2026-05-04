"""Number-swap REST API — Wave 11.

Routes:
  POST /v1/twilio/swap-number  — Yellow tier, scope telephony:swap_number

Law compliance:
  Law #1  — no autonomous decisions; worker runs swap, route only validates + enqueues.
  Law #2  — receipt cut by swap state machine on every step.
  Law #3  — fail closed: missing token → 401; profile not ready → 409.
  Law #5  — capability token validated server-side (Yellow-gated route).
  Law #6  — tenant scope from X- headers only, never from request body.
  Law #9  — phone_e164 never in receipts or logs (only Twilio SIDs).

Table assumptions (migrations 109-115):
  tenant_phone_numbers   — active number queried to find old_phone_number_id
  tenant_trust_profiles  — trust_state must be 'number_attached' before swap
  tenant_phone_swaps     — swap job row created here, executed by ARQ worker
  front_desk_configs     — updated atomically in step 7 of state machine

ARQ queue: 'arq:trust_onboarding' — enqueue advance_number_swap job.

Author: Aspire — Wave 11
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Request, status
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
from aspire_orchestrator.services.twilio_provisioning import search_available_numbers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/twilio", tags=["twilio-swap"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SWAP_QUEUE_NAME: str = "arq:trust_onboarding"

# Trust states where a swap is permitted (number must be fully onboarded).
_SWAP_ALLOWED_STATES: frozenset[str] = frozenset({
    "number_attached",
    "branded_calling_pending",
    "branded_calling_live",
})

# Estimated completion: 60 seconds p95 SLO (plan §14).
_ESTIMATED_COMPLETION_SECONDS: int = 60

# Supported number types for search.
_VALID_NUMBER_TYPES: frozenset[str] = frozenset({"LOCAL", "TOLLFREE"})


# ---------------------------------------------------------------------------
# ARQ enqueue helper
# ---------------------------------------------------------------------------


async def _enqueue_advance_swap(swap_job_id: str) -> None:
    """Push advance_number_swap onto the ARQ trust_onboarding queue.

    Best-effort: if Redis is unreachable we log but do NOT fail the HTTP
    response — the swap row is written, ops can manually enqueue.
    """
    try:
        from arq.connections import RedisSettings, create_pool  # type: ignore[import-not-found]

        redis_url = settings.redis_url or "redis://localhost:6379"
        redis_settings = RedisSettings.from_dsn(redis_url)
        pool = await create_pool(redis_settings)
        try:
            job_id = f"swap:{swap_job_id}:advance"
            await pool.enqueue_job(
                "advance_number_swap",
                swap_job_id,
                _queue_name=_SWAP_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=0,
            )
        finally:
            await pool.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "swap_routes arq_enqueue_failed swap_job_id=%s err=%s", swap_job_id, exc,
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class NumberSearchParams(BaseModel):
    area_code: str = Field(..., pattern=r"^\d{3}$", description="US 3-digit area code")
    number_type: Literal["LOCAL", "TOLLFREE"] = "LOCAL"


class SwapNumberRequest(BaseModel):
    """POST /v1/twilio/swap-number request body.

    Exactly one of new_number_search or new_number_e164 must be provided.
    """

    new_number_search: NumberSearchParams | None = None
    new_number_e164: str | None = Field(
        default=None,
        pattern=r"^\+1\d{10}$",
        description="E.164 number to swap to (if pre-selected)",
    )
    release_old_number: bool = Field(
        default=True,
        description="If true, release the old number from the Twilio account after swap.",
    )
    capability_token: dict[str, Any] | None = None

    @field_validator("new_number_search", "new_number_e164", mode="before")
    @classmethod
    def _at_least_one(cls, v: Any) -> Any:
        # Individual field validation — combined check in the endpoint itself.
        return v


# ---------------------------------------------------------------------------
# POST /v1/twilio/swap-number — Yellow tier, scope telephony:swap_number
# ---------------------------------------------------------------------------


@router.post("/swap-number", status_code=status.HTTP_202_ACCEPTED)
async def swap_number(
    body: SwapNumberRequest,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Initiate a number-swap job for the tenant.

    Validates:
      1. Capability token (Yellow, scope telephony:swap_number).
      2. Exactly one of new_number_search / new_number_e164 provided.
      3. Tenant has an active phone number.
      4. Trust profile state is 'number_attached' (onboarding must be complete).

    Creates:
      - Resolves new_number_e164 from search if only area_code provided.
      - INSERT tenant_phone_swaps row (status='pending').
      - Enqueues advance_number_swap ARQ job.

    Returns:
      {
        "swap_job_id": "uuid",
        "old_number_e164": "+14482885386",
        "new_number_e164": "+14155550199",
        "estimated_completion": "2026-05-04T18:00:00Z"
      }
    """
    # --- 1. Scope resolution (Law #6) ---
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # --- 2. Capability token (Law #5, Yellow tier) ---
    _validate_cap_token(body.capability_token, scope, "telephony:swap_number")
    cap_token_id = _cap_token_id(body.capability_token)

    suite_id = str(scope.suite_id)
    tenant_id = str(scope.tenant_id)
    office_id = str(scope.office_id)

    # --- 3. Validate exactly one search mode ---
    if body.new_number_search is None and body.new_number_e164 is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "MISSING_NUMBER_TARGET",
                "message": "Provide either new_number_search or new_number_e164.",
            },
        )
    if body.new_number_search is not None and body.new_number_e164 is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "AMBIGUOUS_NUMBER_TARGET",
                "message": "Provide exactly one of new_number_search or new_number_e164, not both.",
            },
        )

    # --- 4. Load trust profile (must exist + be in number_attached state) ---
    try:
        profile_rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("swap_routes trust_profile_select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not profile_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "NO_TRUST_PROFILE",
                "reason_code": "NO_TRUST_PROFILE",
                "message": (
                    "No trust profile found. Complete KYB onboarding and Trust Hub "
                    "verification before swapping your number."
                ),
            },
        )

    trust_state = str(profile_rows[0].get("trust_state", ""))
    if trust_state not in _SWAP_ALLOWED_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PROFILE_NOT_READY_FOR_SWAP",
                "reason_code": "PROFILE_NOT_READY_FOR_SWAP",
                "trust_state": trust_state,
                "message": (
                    f"Number swap requires trust_state='number_attached'. "
                    f"Current state: {trust_state!r}. "
                    "Complete Trust Hub verification first."
                ),
            },
        )

    # --- 5. Load current active phone number ---
    try:
        phone_rows = await supabase_select(
            "tenant_phone_numbers",
            f"suite_id=eq.{suite_id}&status=eq.active",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("swap_routes phone_number_select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not phone_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "NO_ACTIVE_NUMBER",
                "reason_code": "NO_ACTIVE_NUMBER",
                "message": "No active phone number found. Purchase a number first.",
            },
        )

    old_phone_row = phone_rows[0]
    old_phone_number_id = str(old_phone_row["id"])
    old_number_e164 = str(old_phone_row.get("phone_number", ""))

    # --- 6. Check for in-flight swap (prevent double-swap) ---
    try:
        in_flight = await supabase_select(
            "tenant_phone_swaps",
            f"suite_id=eq.{suite_id}&status=eq.pending",
            limit=1,
        )
    except SupabaseClientError:
        in_flight = []  # best-effort; allow creation

    if in_flight:
        existing_swap = in_flight[0]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "SWAP_ALREADY_IN_PROGRESS",
                "reason_code": "SWAP_ALREADY_IN_PROGRESS",
                "swap_job_id": str(existing_swap.get("id", "")),
                "message": "A number swap is already in progress for this tenant.",
            },
        )

    # --- 7. Resolve new_number_e164 (search or use provided) ---
    new_number_e164: str
    if body.new_number_e164:
        new_number_e164 = body.new_number_e164
    else:
        # Search for an available number matching area_code + number_type.
        search_params = body.new_number_search  # type: ignore[union-attr]
        # search_available_numbers uses "Local" / "TollFree" casing internally
        _type_map: dict[str, str] = {"LOCAL": "Local", "TOLLFREE": "TollFree"}
        mapped_type = _type_map.get(search_params.number_type, "Local")
        try:
            available = await search_available_numbers(
                area_code=search_params.area_code,
                number_type=mapped_type,
            )
        except Exception as exc:
            logger.error("swap_routes number_search failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "NUMBER_SEARCH_FAILED",
                    "message": f"Could not find available numbers: {exc}",
                },
            ) from exc

        if not available:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "NO_NUMBERS_AVAILABLE",
                    "reason_code": "NO_NUMBERS_AVAILABLE",
                    "message": (
                        f"No available numbers found for area code "
                        f"{search_params.area_code}. Try a different area code."
                    ),
                },
            )
        new_number_e164 = available[0].phone_number

    # --- 8. Prevent self-swap ---
    if new_number_e164 == old_number_e164:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "SAME_NUMBER",
                "message": "New number is the same as the current number.",
            },
        )

    # --- 9. Create tenant_phone_swaps row ---
    swap_job_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    estimated_completion = (
        datetime.now(timezone.utc) + timedelta(seconds=_ESTIMATED_COMPLETION_SECONDS)
    ).isoformat()

    swap_row: dict[str, Any] = {
        "id": swap_job_id,
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "old_phone_number_id": old_phone_number_id,
        "new_number_e164": new_number_e164,
        "release_old_number": body.release_old_number,
        "status": "pending",
        "capability_token_id": cap_token_id or None,
        "progress": {},
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        await supabase_insert("tenant_phone_swaps", swap_row)
    except SupabaseClientError as exc:
        detail_str = str(exc.detail or "").lower()
        if "duplicate" in detail_str or "unique" in detail_str or exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "SWAP_ALREADY_IN_PROGRESS"},
            ) from exc
        logger.error("swap_routes swap_insert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    # --- 10. Enqueue ARQ job (best-effort) ---
    await _enqueue_advance_swap(swap_job_id)

    logger.info(
        "swap_routes swap_created swap_job_id=%s suite=%s old=%s new=%s cap_token=%s",
        swap_job_id, suite_id,
        old_number_e164[:6] + "***",
        new_number_e164[:6] + "***",
        cap_token_id or "<none>",
    )

    return {
        "swap_job_id": swap_job_id,
        "old_number_e164": old_number_e164,
        "new_number_e164": new_number_e164,
        "estimated_completion": estimated_completion,
    }


# ---------------------------------------------------------------------------
# GET /v1/twilio/swap-number/{swap_job_id} — Green tier, Bearer-required
#
# Frontend polls this every 30s while the swap is in flight. We mirror the
# A2P /status pattern: presence-only Bearer check (full JWT validation lives
# in the desktop-server proxy) + suite isolation via X-Suite-Id header to
# defend against UUID enumeration attacks.
# ---------------------------------------------------------------------------


def _require_bearer_token(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
        logger.warning(
            "swap_routes unauthenticated_read path=%s remote=%s",
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


@router.get("/swap-number/{swap_job_id}", status_code=status.HTTP_200_OK)
async def get_swap_status(
    request: Request,
    swap_job_id: str = Path(..., min_length=8, max_length=64),
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Return the live status of an in-flight number swap.

    Green tier — no capability token. Bearer presence is required (Law #6
    defense-in-depth), and the swap row is filtered by `suite_id` resolved
    from the X-Suite-Id header so a tenant can never read another tenant's
    swap state by guessing the swap_job_id UUID.

    Response shape:
      {
        "swap_job_id": "<uuid>",
        "status": "pending" | "in_progress" | "succeeded" | "failed"
                | "partial_success",
        "current_step": "<step_<n>_<name>>" | null,
        "completed_steps": ["step_1_initiated", "step_2_purchased", ...],
        "reason_code": "<machine_code>" | null,
        "old_number_e164": "+1...",
        "new_number_e164": "+1...",
        "created_at": "<iso8601>",
        "updated_at": "<iso8601>",
        "completed_at": "<iso8601>" | null
      }

    404 — swap not found OR not owned by the suite (404 not 403 to avoid
          confirming existence of cross-tenant swaps).
    """
    _require_bearer_token(request)
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    try:
        rows = await supabase_select(
            "tenant_phone_swaps",
            f"id=eq.{swap_job_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error(
            "swap_routes status_select_failed swap_job_id=%s err=%s",
            swap_job_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "SWAP_NOT_FOUND",
                "reason_code": "SWAP_NOT_FOUND",
            },
        )

    swap = rows[0]
    progress: dict[str, Any] = swap.get("progress") or {}

    # Derive completed_steps from the step_<n>_* keys in progress JSONB.
    # Order matters for the UI's progress chain — sort by step number prefix.
    completed_steps: list[str] = sorted(
        (k for k in progress if k.startswith("step_")),
        key=lambda k: int(k.split("_", 2)[1]) if k.split("_", 2)[1].isdigit() else 99,
    )

    # The "current step" is the next un-completed step, derived from the
    # highest-numbered completed step + 1. Returns None on terminal status.
    current_step: str | None = None
    swap_status = str(swap.get("status", "pending"))
    if swap_status in ("pending", "in_progress") and completed_steps:
        try:
            last_n = max(
                int(k.split("_", 2)[1])
                for k in completed_steps
                if k.split("_", 2)[1].isdigit()
            )
            current_step = f"step_{last_n + 1}"
        except (ValueError, IndexError):
            current_step = None

    # Resolve old_number_e164 from the linked tenant_phone_numbers row.
    old_number_e164: str | None = None
    old_phone_id = swap.get("old_phone_number_id")
    if old_phone_id:
        try:
            phone_rows = await supabase_select(
                "tenant_phone_numbers",
                f"id=eq.{old_phone_id}",
                limit=1,
            )
            if phone_rows:
                old_number_e164 = phone_rows[0].get("phone_number")
        except SupabaseClientError:
            pass  # best-effort; status returns null on failure

    return {
        "swap_job_id": swap_job_id,
        "status": swap_status,
        "current_step": current_step,
        "completed_steps": completed_steps,
        "reason_code": swap.get("reason_code"),
        "old_number_e164": old_number_e164,
        "new_number_e164": swap.get("new_number_e164"),
        "created_at": swap.get("created_at"),
        "updated_at": swap.get("updated_at"),
        "completed_at": swap.get("completed_at"),
    }


__all__ = ["router"]
