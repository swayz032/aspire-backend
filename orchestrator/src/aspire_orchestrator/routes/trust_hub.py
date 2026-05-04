"""Trust Hub KYB intake REST API — Wave 3.

Routes:
  POST /v1/trust-hub/kyb              — Yellow, scope trust_hub:kyb_submit
  GET  /v1/trust-hub/status           — Green, no capability token
  POST /v1/trust-hub/dispute          — Yellow, scope trust_hub:resubmit
  POST /v1/trust-hub/status-callback  — Public webhook, Twilio HMAC validated

Law compliance:
  Law #2 — receipts cut on every state change (Yellow tier, hash-chained).
  Law #3 — fail closed: missing token → 401, vault down → 503, profile exists → 409.
  Law #5 — capability tokens validated server-side before any Supabase/Twilio call.
  Law #6 — tenant scope from X- headers only, never from request body.
  Law #9 — EIN/DOB/SSN/email/phone NEVER in receipts, logs, or response bodies.
           Vault secret names use {tenant_id}:{field_type} (W1 R-004).
           Old vault secrets deleted before overwrite (W1 R-005).

Table assumptions (migrations 109–114):
  tenant_trust_profiles  — main KYB table; suite_id UNIQUE constraint (1:1)
  tenant_authorized_reps — 1–2 per profile; dob_vault_secret_id / ssn_last4_vault_secret_id
  trust_state_transitions — append-only audit ledger
  tenant_a2p_brands      — queried for a2p_approved milestone in GET /status

ARQ queue: 'arq:trust_onboarding' — enqueue advance_trust_state after write.

Author: Aspire — Wave 3 (per docs/plans/per-tenant-trust-hub-cnam.md §III W3)
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date as date_cls, datetime, timezone
from typing import Any, Final, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.routes.front_desk import (
    _cap_token_id,
    _resolve_scope,
    _validate_cap_token,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_rpc,
    supabase_select,
    supabase_update,
)
from aspire_orchestrator.services.twilio_voice import verify_twilio_signature
from aspire_orchestrator.workers.trust_onboarding.trust_receipts import (
    TrustReceiptError,
    cut_trust_receipt,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/trust-hub", tags=["trust-hub"])


# ---------------------------------------------------------------------------
# Helpers used at module load time (security-reviewer R-002, R-003, R-006)
# ---------------------------------------------------------------------------


def _build_twilio_sid_re() -> re.Pattern[str]:
    """Compile once at module load. Twilio SIDs are 34 chars: 2-letter prefix + 32 hex.

    Used by /status-callback to validate `ResourceSid` BEFORE interpolating
    into the PostgREST filter. Without this, a Twilio webhook with a crafted
    `ResourceSid` containing `&` could break out and inject additional filter
    clauses against `tenant_trust_profiles` — leading to cross-tenant reads.
    """
    return re.compile(r"^[A-Z]{2}[0-9a-fA-F]{32}$")


# Module-level compiled regex (security-reviewer R-W5-006). Compiling on every
# webhook hit is pure waste under high volume; this constant is referenced by
# the status-callback handler instead of recompiling per request.
_TWILIO_SID_RE: Final[re.Pattern[str]] = _build_twilio_sid_re()


# Allowlist of Twilio Trust Hub status values we are prepared to dispatch on
# (security-reviewer R-W5-001). Any other status (e.g., `twilio-pending`,
# `twilio-under-review`) is logged + receipt-written, but does NOT advance
# state. Without this allowlist, intermediate statuses fall through to
# `is_approved=False` and write `profile_rejected`/`failed` to DB —
# locking tenants out for what should be benign Twilio progress events.
_KNOWN_TWILIO_STATUSES: Final[frozenset[str]] = frozenset({
    "twilio-approved",
    "twilio-rejected",
})


# Sanitization for Twilio's `FailureReason` text (security-reviewer R-W5-002).
# Twilio rejection reasons may include rep names or other tenant-submitted
# document strings. Storing them raw and serving them back in GET /status
# leaks PII. We strip to a safe character class and truncate.
_FAILURE_REASON_SAFE_CHARS: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9 .,;:!?\-]")
_FAILURE_REASON_MAX_LEN: Final[int] = 256


def _sanitize_failure_reason(raw: str | None) -> str | None:
    """Strip non-safe chars + truncate FailureReason for safe DB / API echo.

    The raw text is preserved separately on the receipt
    (`twilio_rejection_reason`) so the audit ledger is complete (Law #2),
    but the DB column + GET /status response carry only the sanitized form.
    """
    if not raw:
        return None
    cleaned = _FAILURE_REASON_SAFE_CHARS.sub(" ", raw)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_FAILURE_REASON_MAX_LEN] if cleaned else None


# Field names whose values must NEVER appear in 422 validation error
# responses (security-reviewer R-003). FastAPI's default RequestValidationError
# handler echoes the offending input back to the client, which puts raw
# EIN/DOB/SSN values into HTTP response bodies — captured by SIEM, proxies,
# CloudFlare logs, etc. We replace `input` with "<REDACTED>" for these fields.
_PII_FIELD_NAMES: frozenset[str] = frozenset({
    "ein",
    "dob",
    "date_of_birth",
    "ssn_last4",
    "ssn",
    "phone_e164",
    "phone_number",
    "email",
    "first_name",
    "last_name",
})


def _redact_pii_from_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the `input` field from any validation error whose location names a PII field."""
    redacted: list[dict[str, Any]] = []
    for err in errors:
        new_err = dict(err)
        loc = err.get("loc", ())
        if any(isinstance(part, str) and part in _PII_FIELD_NAMES for part in loc):
            if "input" in new_err:
                new_err["input"] = "<REDACTED>"
            if "ctx" in new_err and isinstance(new_err["ctx"], dict):
                # Pydantic sometimes includes the value in ctx too (e.g., enum/regex contexts)
                ctx = dict(new_err["ctx"])
                for k in list(ctx.keys()):
                    if isinstance(ctx[k], str) and len(ctx[k]) > 0:
                        ctx[k] = "<REDACTED>"
                new_err["ctx"] = ctx
        redacted.append(new_err)
    return redacted


def register_trust_hub_validation_handler(app: Any) -> None:
    """Mount a router-scoped 422 handler that redacts PII from Pydantic errors.

    The orchestrator's server.py calls this immediately after include_router
    so the handler is scoped to /v1/trust-hub paths only. Other routers keep
    FastAPI's default 422 echo behavior (no PII fields in those routes).
    """
    @app.exception_handler(RequestValidationError)
    async def trust_hub_validation_handler(  # type: ignore[misc]
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Only redact for /v1/trust-hub/* paths; pass through otherwise.
        if request.url.path.startswith("/v1/trust-hub/"):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"detail": _redact_pii_from_validation_errors(list(exc.errors()))},
            )
        # Default behavior for all other routes
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": list(exc.errors())},
        )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ROUTE_DISPUTES: int = 5  # route-layer cap (DB allows 10 via CHECK in migration 113)
_CALLBACK_QUEUE_NAME: str = "arq:trust_onboarding"


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class AuthorizedRepInput(BaseModel):
    first_name: str
    last_name: str
    title: str
    email: str
    phone_e164: str = Field(..., pattern=r"^\+1\d{10}$")
    dob: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")     # YYYY-MM-DD — encrypted immediately
    ssn_last4: str = Field(..., pattern=r"^\d{4}$")            # encrypted immediately

    @field_validator("dob")
    @classmethod
    def _validate_dob_semantics(cls, v: str) -> str:
        """Reject pattern-valid but semantically-invalid dates.

        Security-reviewer R-006: pattern `^\\d{4}-\\d{2}-\\d{2}$` accepts
        "9999-99-99", "0000-00-00", "2000-13-45", etc. These pass regex
        but are not real dates. They'd encrypt into Vault, get rejected by
        Twilio at customer_profile_submitted, and consume one of the
        tenant's 5 disputes on a form-validation issue we should have
        caught at intake.
        """
        try:
            d = date_cls.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"dob is not a real calendar date: {exc}") from exc
        # Reasonable bounds: DOB must be 18-120 years ago. Twilio's KYB also
        # implicitly requires the rep to be an adult.
        today = date_cls.today()
        if d.year < today.year - 120:
            raise ValueError("dob is too far in the past (>120 years)")
        if d > today.replace(year=today.year - 18):
            raise ValueError("authorized representative must be at least 18 years old")
        return v


class KYBSubmitRequest(BaseModel):
    legal_business_name: str = Field(..., min_length=2, max_length=120)
    dba_name: str | None = None
    business_type: Literal[
        "sole_proprietor", "partnership", "llc", "corporation",
        "nonprofit", "government", "other"
    ]
    address_street: str
    address_city: str
    address_state: str = Field(..., pattern=r"^[A-Z]{2}$")
    address_zip: str = Field(..., pattern=r"^\d{5}(-\d{4})?$")
    ein: str = Field(..., pattern=r"^\d{2}-\d{7}$")            # encrypted immediately
    authorized_reps: list[AuthorizedRepInput] = Field(..., min_length=1, max_length=2)
    capability_token: dict[str, Any] | None = None


class KYBDisputeRequest(BaseModel):
    legal_business_name: str | None = None
    dba_name: str | None = None
    business_type: Literal[
        "sole_proprietor", "partnership", "llc", "corporation",
        "nonprofit", "government", "other"
    ] | None = None
    address_street: str | None = None
    address_city: str | None = None
    address_state: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    address_zip: str | None = Field(None, pattern=r"^\d{5}(-\d{4})?$")
    ein: str | None = Field(None, pattern=r"^\d{2}-\d{7}$")
    authorized_reps: list[AuthorizedRepInput] | None = None
    capability_token: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Vault helpers (Law #9 — PII never touches logs or receipts)
# ---------------------------------------------------------------------------


async def _vault_create_secret(
    value: str,
    *,
    name: str,
    description: str,
) -> str:
    """Encrypt value in Supabase Vault; return the UUID secret_id.

    Vault secret names MUST use {tenant_id}:{field_type} convention (W1 R-004)
    to prevent cross-tenant collision.

    Raises SupabaseClientError on vault failure.
    """
    result = await supabase_rpc(
        "create_vault_secret",
        {
            "secret": value,
            "name": name,
            "description": description,
        },
    )
    secret_id = result.get("id") or result.get("secret_id") or result.get("uuid")
    if not secret_id:
        raise SupabaseClientError(
            "rpc/create_vault_secret",
            detail="Vault returned no secret_id",
        )
    return str(secret_id)


async def _vault_delete_secret(secret_id: str | None) -> None:
    """Delete a vault secret by UUID (W1 R-005 — cleanup before overwrite).

    Best-effort: logs warning on failure but does NOT raise so the caller
    can continue with creating the replacement secret.
    """
    if not secret_id:
        return
    try:
        await supabase_rpc("delete_vault_secret", {"secret_id": str(secret_id)})
    except SupabaseClientError as exc:
        logger.warning(
            "trust_hub vault_delete_failed secret_id=%s err=%s",
            secret_id, exc,
        )


# ---------------------------------------------------------------------------
# ARQ enqueue helper
# ---------------------------------------------------------------------------


async def _enqueue_advance_trust_state(trust_profile_id: str) -> None:
    """Push an advance_trust_state job onto the ARQ queue (0s delay).

    Deduplication key: trust:{trust_profile_id}:{current_state} ensures ARQ
    won't re-enqueue a job that's already in flight for this tenant+state.

    Fails gracefully: if Redis is unreachable we log but do NOT fail the HTTP
    response — the trust profile is already written and the cron job (W9) will
    recover. The job_id format must match what the worker expects.
    """
    try:
        from arq.connections import RedisSettings, create_pool  # type: ignore[import-not-found]

        redis_url = settings.redis_url or "redis://localhost:6379"
        redis_settings = RedisSettings.from_dsn(redis_url)
        pool = await create_pool(redis_settings)
        try:
            job_id = f"trust:{trust_profile_id}:kyb_collected"
            await pool.enqueue_job(
                "advance_trust_state",
                trust_profile_id,
                _queue_name=_CALLBACK_QUEUE_NAME,
                _job_id=job_id,
                _defer_by=0,
            )
        finally:
            await pool.aclose()
    except Exception as exc:  # noqa: BLE001 — best-effort; W9 cron recovers
        logger.warning(
            "trust_hub arq_enqueue_failed trust_profile_id=%s err=%s",
            trust_profile_id, exc,
        )


# ---------------------------------------------------------------------------
# POST /v1/trust-hub/kyb — Yellow tier, scope trust_hub:kyb_submit
# ---------------------------------------------------------------------------


@router.post("/kyb", status_code=status.HTTP_201_CREATED)
async def kyb_submit(
    body: KYBSubmitRequest,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Receive KYB wizard form (W8 frontend) and kick off state machine.

    Steps:
      1. Resolve scope from Gateway headers.
      2. Validate capability token (scope = trust_hub:kyb_submit).
      3. Encrypt EIN via vault.create_secret — store UUID.
      4. Encrypt DOB + SSN-last4 for each rep.
      5. Upsert tenant_trust_profiles (fail 409 if suite_id already exists).
      6. Upsert tenant_authorized_reps.
      7. Cut kyb_collected receipt (NO PII in redacted_inputs).
      8. Enqueue ARQ job advance_trust_state.
      9. Return {trust_profile_id, trust_state, receipt_id}.
    """
    # --- 1. Scope resolution (Law #6) ---
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)

    # --- 2. Capability token validation (Law #5) ---
    _validate_cap_token(body.capability_token, scope, "trust_hub:kyb_submit")
    cap_token_id = _cap_token_id(body.capability_token)

    tenant_id = str(scope.tenant_id)
    suite_id = str(scope.suite_id)
    office_id = str(scope.office_id)

    # --- 3. Check for duplicate (409 — 1:1 constraint on suite_id) ---
    try:
        existing = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("trust_hub kyb_submit select_existing failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
        ) from exc

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PROFILE_ALREADY_EXISTS",
                "trust_profile_id": str(existing[0].get("id", "")),
                "trust_state": existing[0].get("trust_state", ""),
            },
        )

    # --- 4. Encrypt EIN (Law #9 — value never logged) ---
    try:
        ein_vault_id = await _vault_create_secret(
            body.ein,
            name=f"{tenant_id}:ein",
            description=f"EIN for tenant {tenant_id}",
        )
    except SupabaseClientError as exc:
        logger.error("trust_hub kyb_submit vault_ein_encrypt failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
        ) from exc

    # --- 5. Encrypt DOB + SSN-last4 for each rep ---
    # Policy-gate W3 finding F-P1: if EIN encryption succeeded but rep
    # encryption fails, we MUST clean up the orphaned EIN vault secret
    # before raising 503. Otherwise a client retry creates a SECOND EIN
    # vault entry under the same name and the first leaks forever.
    # Track all vault IDs created so we can roll them back as a unit.
    rep_vault_ids: list[dict[str, str]] = []
    created_vault_ids: list[str] = [ein_vault_id]
    try:
        for idx, rep in enumerate(body.authorized_reps):
            dob_vault_id = await _vault_create_secret(
                rep.dob,
                name=f"{tenant_id}:rep_{idx}_dob",
                description=f"DOB for tenant {tenant_id} rep index {idx}",
            )
            created_vault_ids.append(dob_vault_id)
            ssn_vault_id = await _vault_create_secret(
                rep.ssn_last4,
                name=f"{tenant_id}:rep_{idx}_ssn_last4",
                description=f"SSN-last4 for tenant {tenant_id} rep index {idx}",
            )
            created_vault_ids.append(ssn_vault_id)
            rep_vault_ids.append({"dob_vault_id": dob_vault_id, "ssn_vault_id": ssn_vault_id})
    except SupabaseClientError as exc:
        logger.error(
            "trust_hub kyb_submit vault_rep_encrypt_failed err=%s — rolling back %d vault secrets",
            exc, len(created_vault_ids),
        )
        # Best-effort cleanup of every vault secret we created so far.
        # _vault_delete_secret already swallows its own errors, so a
        # second-round failure here can't mask the original 503.
        for vid in created_vault_ids:
            try:
                await _vault_delete_secret(vid)
            except Exception as cleanup_exc:  # noqa: BLE001 — log + continue
                logger.warning(
                    "trust_hub kyb_submit vault_cleanup_partial_failure vid=%s err=%s",
                    vid, cleanup_exc,
                )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
        ) from exc

    # --- 6. Insert tenant_trust_profiles ---
    trust_profile_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    profile_row: dict[str, Any] = {
        "id": trust_profile_id,
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "legal_business_name": body.legal_business_name,
        "dba_name": body.dba_name,
        "business_type": body.business_type,
        "address_street": body.address_street,
        "address_city": body.address_city,
        "address_state": body.address_state,
        "address_zip": body.address_zip,
        "address_country": "US",
        "ein_vault_secret_id": ein_vault_id,
        "trust_state": "kyb_collected",
        "kyb_collected_at": now_iso,
        "dispute_count": 0,
    }
    try:
        inserted_profile = await supabase_insert("tenant_trust_profiles", profile_row)
    except SupabaseClientError as exc:
        detail_str = str(exc.detail or "").lower()
        if "duplicate" in detail_str or "unique" in detail_str or exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "PROFILE_ALREADY_EXISTS"},
            ) from exc
        logger.error("trust_hub kyb_submit profile_insert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
        ) from exc

    # --- 7. Insert tenant_authorized_reps ---
    for idx, (rep, vault_ids) in enumerate(zip(body.authorized_reps, rep_vault_ids)):
        rep_row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "trust_profile_id": trust_profile_id,
            "suite_id": suite_id,
            "tenant_id": tenant_id,
            "rep_index": idx + 1,
            "first_name": rep.first_name,
            "last_name": rep.last_name,
            "business_title": rep.title,
            "email": rep.email,
            "phone_e164": rep.phone_e164,
            "dob_vault_secret_id": vault_ids["dob_vault_id"],
            "ssn_last4_vault_secret_id": vault_ids["ssn_vault_id"],
        }
        try:
            await supabase_insert("tenant_authorized_reps", rep_row)
        except SupabaseClientError as exc:
            logger.error(
                "trust_hub kyb_submit rep_insert_failed idx=%d err=%s", idx, exc
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
            ) from exc

    # --- 8. Cut kyb_collected receipt (Law #2) — NO PII in redacted_inputs ---
    trust_profile_for_receipt: dict[str, Any] = {
        "id": trust_profile_id,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": office_id,
    }
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type="kyb_collected",
            trust_profile=trust_profile_for_receipt,
            outcome="success",
            from_state="<initial>",
            to_state="kyb_collected",
            redacted_inputs={
                "trust_profile_id": trust_profile_id,
                "step_name": "kyb_form_submit",
                "rep_count": len(body.authorized_reps),
                "business_type": body.business_type,
                "address_state": body.address_state,
                "vault_secret_count": 1 + (len(body.authorized_reps) * 2),
            },
            capability_token_id=cap_token_id or None,
        )
    except TrustReceiptError as exc:
        logger.error("trust_hub kyb_submit cut_trust_receipt failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "RECEIPT_FAILED", "reason_code": exc.code},
        ) from exc

    # --- 9. Enqueue ARQ job (best-effort; W9 cron recovers if Redis down) ---
    await _enqueue_advance_trust_state(trust_profile_id)

    return {
        "trust_profile_id": trust_profile_id,
        "trust_state": "kyb_collected",
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# GET /v1/trust-hub/status — Green tier, no capability token
# ---------------------------------------------------------------------------


@router.get("/status", status_code=status.HTTP_200_OK)
async def kyb_status(
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Return the tenant's current Trust Hub onboarding state.

    Used by the W8 frontend status dashboard. No cap token required (read-only).
    Returns 404 if the tenant hasn't started onboarding yet.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    suite_id = str(scope.suite_id)

    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("trust_hub status select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_TRUST_PROFILE", "suite_id": suite_id},
        )

    profile = rows[0]

    # A2P milestone: look up brand_status from tenant_a2p_brands
    a2p_approved = False
    try:
        a2p_rows = await supabase_select(
            "tenant_a2p_brands",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
        if a2p_rows:
            a2p_approved = a2p_rows[0].get("brand_status") == "approved"
    except SupabaseClientError:
        pass  # best-effort; A2P table may not exist in early deploys

    trust_state = profile.get("trust_state") or ""

    # F-11: `shaken_approved_at` column does not exist in migration 109.
    # We derive the shaken_approved milestone from `trust_state` itself —
    # any state at or past `shaken_approved` (rank 6) means SHAKEN is live.
    _SHAKEN_APPROVED_STATES: frozenset[str] = frozenset({
        "shaken_approved", "cnam_created", "cnam_submitted",
        "cnam_approved", "number_attached", "branded_calling_pending",
    })

    milestones: dict[str, bool] = {
        "kyb_collected": bool(profile.get("kyb_collected_at")),
        "profile_approved": bool(profile.get("profile_approved_at")),
        "shaken_approved": trust_state in _SHAKEN_APPROVED_STATES,
        "cnam_approved": bool(profile.get("cnam_approved_at")),
        "branded_calling_live": bool(profile.get("branded_calling_enabled")),
        "a2p_approved": a2p_approved,
    }

    return {
        "trust_state": trust_state,
        "kyb_collected_at": profile.get("kyb_collected_at"),
        "profile_approved_at": profile.get("profile_approved_at"),
        "cnam_approved_at": profile.get("cnam_approved_at"),
        "rejection_reason": profile.get("rejection_reason"),
        "rejection_code": profile.get("rejection_code"),
        "cnam_display_name": profile.get("cnam_display_name"),
        "branded_calling_enabled": bool(profile.get("branded_calling_enabled")),
        "branded_calling_display_name": profile.get("branded_calling_display_name"),
        "milestones": milestones,
    }


# ---------------------------------------------------------------------------
# POST /v1/trust-hub/dispute — Yellow tier, scope trust_hub:resubmit
# ---------------------------------------------------------------------------


@router.post("/dispute", status_code=status.HTTP_200_OK)
async def kyb_dispute(
    body: KYBDisputeRequest,
    x_tenant_id: str | None = Header(None),
    x_suite_id: str | None = Header(None),
    x_office_id: str | None = Header(None),
) -> dict[str, Any]:
    """Accept corrected KYB fields, re-encrypt PII, reset state to kyb_collected.

    Steps:
      1. Resolve scope + validate cap token.
      2. Load existing trust profile.
      3. Check dispute_count cap (route-layer: 5; DB allows 10).
      4. For each re-submitted PII field: delete old vault secret, create new.
      5. Update tenant_trust_profiles (increments dispute_count, resets state).
      6. If authorized_reps supplied: re-encrypt and update each rep row.
      7. Cut kyb_collected receipt.
      8. Enqueue ARQ job.
      9. Return {trust_profile_id, trust_state, dispute_count, receipt_id}.
    """
    scope = _resolve_scope(x_tenant_id, x_suite_id, x_office_id)
    _validate_cap_token(body.capability_token, scope, "trust_hub:resubmit")
    cap_token_id = _cap_token_id(body.capability_token)

    tenant_id = str(scope.tenant_id)
    suite_id = str(scope.suite_id)

    # Load existing profile
    try:
        rows = await supabase_select(
            "tenant_trust_profiles",
            f"suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.error("trust_hub dispute select failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NO_TRUST_PROFILE"},
        )

    profile = rows[0]
    trust_profile_id = str(profile["id"])
    current_dispute_count: int = int(profile.get("dispute_count") or 0)

    # Route-layer dispute cap (even though DB allows up to 10)
    if current_dispute_count >= _MAX_ROUTE_DISPUTES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "MAX_DISPUTES_REACHED",
                "dispute_count": current_dispute_count,
                "max_disputes": _MAX_ROUTE_DISPUTES,
            },
        )

    update_payload: dict[str, Any] = {
        "trust_state": "kyb_collected",
        "kyb_collected_at": datetime.now(timezone.utc).isoformat(),
        "dispute_count": current_dispute_count + 1,
        "rejection_reason": None,
        "rejection_code": None,
    }

    # Plaintext KYB fields (no PII in these)
    if body.legal_business_name is not None:
        update_payload["legal_business_name"] = body.legal_business_name
    if body.dba_name is not None:
        update_payload["dba_name"] = body.dba_name
    if body.business_type is not None:
        update_payload["business_type"] = body.business_type
    if body.address_street is not None:
        update_payload["address_street"] = body.address_street
    if body.address_city is not None:
        update_payload["address_city"] = body.address_city
    if body.address_state is not None:
        update_payload["address_state"] = body.address_state
    if body.address_zip is not None:
        update_payload["address_zip"] = body.address_zip

    # EIN re-encryption (W1 R-005: delete old secret BEFORE creating new)
    if body.ein is not None:
        old_ein_vault_id: str | None = profile.get("ein_vault_secret_id")
        await _vault_delete_secret(old_ein_vault_id)
        try:
            new_ein_vault_id = await _vault_create_secret(
                body.ein,
                name=f"{tenant_id}:ein",
                description=f"EIN for tenant {tenant_id} (resubmit {current_dispute_count + 1})",
            )
        except SupabaseClientError as exc:
            logger.error("trust_hub dispute vault_ein_encrypt failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
            ) from exc
        update_payload["ein_vault_secret_id"] = new_ein_vault_id

    # Update trust profile
    try:
        await supabase_update(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            update_payload,
        )
    except SupabaseClientError as exc:
        logger.error("trust_hub dispute profile_update failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "DB_UNAVAILABLE"},
        ) from exc

    # Re-encrypt reps if supplied
    if body.authorized_reps is not None:
        for idx, rep in enumerate(body.authorized_reps):
            # Load existing rep row for old vault secret IDs
            try:
                rep_rows = await supabase_select(
                    "tenant_authorized_reps",
                    f"trust_profile_id=eq.{trust_profile_id}&rep_index=eq.{idx + 1}",
                    limit=1,
                )
            except SupabaseClientError as exc:
                logger.error("trust_hub dispute rep_select failed idx=%d err=%s", idx, exc)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": "DB_UNAVAILABLE"},
                ) from exc

            rep_update: dict[str, Any] = {
                "first_name": rep.first_name,
                "last_name": rep.last_name,
                "business_title": rep.title,
                "email": rep.email,
                "phone_e164": rep.phone_e164,
            }

            # Delete old DOB vault secret, create new
            if rep_rows:
                old_dob_vault = rep_rows[0].get("dob_vault_secret_id")
                old_ssn_vault = rep_rows[0].get("ssn_last4_vault_secret_id")
                await _vault_delete_secret(old_dob_vault)
                await _vault_delete_secret(old_ssn_vault)

            try:
                dob_vault_id = await _vault_create_secret(
                    rep.dob,
                    name=f"{tenant_id}:rep_{idx}_dob",
                    description=f"DOB tenant {tenant_id} rep {idx} (resubmit {current_dispute_count + 1})",
                )
                ssn_vault_id = await _vault_create_secret(
                    rep.ssn_last4,
                    name=f"{tenant_id}:rep_{idx}_ssn_last4",
                    description=f"SSN-last4 tenant {tenant_id} rep {idx} (resubmit {current_dispute_count + 1})",
                )
            except SupabaseClientError as exc:
                logger.error(
                    "trust_hub dispute vault_rep_encrypt_failed idx=%d err=%s", idx, exc
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
                ) from exc

            rep_update["dob_vault_secret_id"] = dob_vault_id
            rep_update["ssn_last4_vault_secret_id"] = ssn_vault_id

            if rep_rows:
                # Update existing row
                try:
                    await supabase_update(
                        "tenant_authorized_reps",
                        f"trust_profile_id=eq.{trust_profile_id}&rep_index=eq.{idx + 1}",
                        rep_update,
                    )
                except SupabaseClientError as exc:
                    logger.error(
                        "trust_hub dispute rep_update_failed idx=%d err=%s", idx, exc
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": "DB_UNAVAILABLE"},
                    ) from exc
            else:
                # Insert new row (rep added on dispute)
                rep_update.update({
                    "id": str(uuid.uuid4()),
                    "trust_profile_id": trust_profile_id,
                    "suite_id": suite_id,
                    "tenant_id": tenant_id,
                    "rep_index": idx + 1,
                })
                try:
                    await supabase_insert("tenant_authorized_reps", rep_update)
                except SupabaseClientError as exc:
                    logger.error(
                        "trust_hub dispute rep_insert_failed idx=%d err=%s", idx, exc
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"error": "DB_UNAVAILABLE"},
                    ) from exc

    # Cut receipt (Law #2)
    trust_profile_for_receipt: dict[str, Any] = {
        "id": trust_profile_id,
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "office_id": str(scope.office_id),
    }
    try:
        receipt_id = await cut_trust_receipt(
            receipt_type="kyb_collected",
            trust_profile=trust_profile_for_receipt,
            outcome="success",
            from_state="kyb_disputed",
            to_state="kyb_collected",
            redacted_inputs={
                "trust_profile_id": trust_profile_id,
                "step_name": "kyb_dispute_resubmit",
                "dispute_count": current_dispute_count + 1,
                "business_type": body.business_type,
                "address_state": body.address_state,
                "vault_secret_count": (1 if body.ein else 0) + (
                    len(body.authorized_reps) * 2 if body.authorized_reps else 0
                ),
            },
            capability_token_id=cap_token_id or None,
        )
    except TrustReceiptError as exc:
        logger.error("trust_hub dispute cut_trust_receipt failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "RECEIPT_FAILED", "reason_code": exc.code},
        ) from exc

    await _enqueue_advance_trust_state(trust_profile_id)

    return {
        "trust_profile_id": trust_profile_id,
        "trust_state": "kyb_collected",
        "dispute_count": current_dispute_count + 1,
        "receipt_id": receipt_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/trust-hub/status-callback — Public webhook, Twilio HMAC validated
# ---------------------------------------------------------------------------

# TODO (W5): implement full state-advancement dispatch after Twilio status arrives.
# Currently: validate HMAC, log, cut webhook_received receipt, return 200.
# W5 will add: look up profile by SID, enqueue advance_trust_state with new state.


@router.post("/status-callback", status_code=status.HTTP_200_OK)
async def status_callback(
    request: Request,
    x_twilio_signature: str | None = Header(None, alias="X-Twilio-Signature"),
) -> dict[str, Any]:
    """Accept async Twilio Trust Hub status updates.

    Always returns 200 to Twilio (non-2xx causes Twilio to retry indefinitely).
    W5 TODO: full dispatch — currently logs + receipt only.

    Security:
      - Twilio HMAC signature validated before any processing.
      - Invalid HMAC → 401 (Twilio will not retry on 401 for Trust Hub callbacks).
    """
    # Parse raw form body (Twilio posts as application/x-www-form-urlencoded)
    try:
        form = dict(await request.form())
    except Exception as exc:  # noqa: BLE001
        logger.warning("trust_hub status_callback form_parse_failed: %s", exc)
        # Return 200 anyway so Twilio doesn't retry malformed posts
        return {"status": "ignored", "reason": "form_parse_error"}

    # Validate Twilio HMAC signature (Law #3 — fail closed on forged webhooks)
    request_url = str(request.url)
    signature = x_twilio_signature or ""

    if not verify_twilio_signature(
        request_url=request_url,
        form_params={k: str(v) for k, v in form.items()},
        signature_header=signature,
    ):
        logger.warning(
            "trust_hub status_callback invalid_hmac remote=%s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_TWILIO_SIGNATURE"},
        )

    resource_sid: str = str(form.get("ResourceSid", ""))
    twilio_status: str = str(form.get("Status", ""))

    # Security-reviewer R-002: even after HMAC passes, validate the
    # `ResourceSid` format BEFORE interpolating into the PostgREST filter.
    # Twilio SIDs are exactly `[A-Z]{2}[0-9a-f]{32}`. A value containing
    # `&` or PostgREST operator suffixes would inject extra filter clauses
    # against `tenant_trust_profiles` (e.g., `BUaaa...&suite_id=neq.null`
    # would return rows from OTHER tenants). HMAC authenticates Twilio's
    # signing key; it does NOT vouch for SID format.
    # R-W5-006: regex compiled at module load (`_TWILIO_SID_RE`) — no
    # per-request recompilation in the hot path.
    sid_valid = bool(resource_sid) and bool(_TWILIO_SID_RE.match(resource_sid))

    logger.info(
        "trust_hub status_callback received ResourceSid=%s Status=%s sid_valid=%s",
        resource_sid[:34] if resource_sid else "<empty>",
        twilio_status, sid_valid,
    )

    # Look up trust profile by matching any bundle SID column —
    # ONLY when SID format is valid (R-002).
    trust_profile: dict[str, Any] | None = None
    matched_sid_column: str | None = None
    if sid_valid:
        for sid_column in (
            "twilio_secondary_profile_sid",
            "twilio_shaken_bundle_sid",
            "twilio_cnam_bundle_sid",
            "twilio_branded_calling_sid",  # W6 — Branded Calling enrollment SID
        ):
            try:
                rows = await supabase_select(
                    "tenant_trust_profiles",
                    f"{sid_column}=eq.{resource_sid}",
                    limit=1,
                )
                if rows:
                    trust_profile = rows[0]
                    matched_sid_column = sid_column
                    break
            except SupabaseClientError:
                pass  # continue searching other columns
    elif resource_sid:
        # SID was present but malformed — log + skip lookup. Still return
        # 200 to Twilio so it doesn't retry; the receipt will reflect the
        # validation skip.
        logger.warning(
            "trust_hub status_callback malformed_sid sid_prefix=%s — skipping DB lookup",
            resource_sid[:6] if resource_sid else "<empty>",
        )

    if not trust_profile:
        logger.warning(
            "trust_hub status_callback no_profile_found ResourceSid=%s", resource_sid
        )
        return {"status": "received"}

    # --- W5: Full dispatch logic ---
    trust_profile_id: str = str(trust_profile.get("id", ""))
    trust_profile_for_receipt: dict[str, Any] = {
        "id": trust_profile_id,
        "suite_id": str(trust_profile.get("suite_id", "")),
        "tenant_id": str(trust_profile.get("tenant_id", "")),
        "office_id": str(trust_profile.get("office_id", "")),
    }
    current_state: str = str(trust_profile.get("trust_state", "unknown"))

    # Step 4: Determine bundle type from matched column
    _COLUMN_TO_BUNDLE: dict[str, str] = {
        "twilio_secondary_profile_sid": "profile",
        "twilio_shaken_bundle_sid": "shaken",
        "twilio_cnam_bundle_sid": "cnam",
        # W6 — Branded Calling enrollment SID (only populated when
        # branded_calling_enabled=True AND state machine reaches the
        # `_transition_number_attached` enrollment step).
        "twilio_branded_calling_sid": "branded_calling",
    }
    bundle_type: str = _COLUMN_TO_BUNDLE.get(matched_sid_column or "", "unknown")

    # Step 5: Determine new trust_state based on bundle + Twilio status.
    #
    # security-reviewer R-W5-001 (IMMEDIATE) — only dispatch on KNOWN Twilio
    # statuses. Twilio sends intermediate statuses (e.g., `twilio-pending`,
    # `twilio-under-review`) that previously fell through to `is_approved=False`
    # and wrote `profile_rejected` / `failed` to DB — corrupting tenant state
    # for benign progress events. Unknown statuses now route to the
    # `new_state is None` path (cut webhook_received receipt + return 200).
    new_state: str | None = None
    is_approved: bool = False
    if twilio_status in _KNOWN_TWILIO_STATUSES:
        is_approved = twilio_status == "twilio-approved"
        _STATE_MAP: dict[tuple[str, bool], str] = {
            ("profile", True): "profile_approved",
            ("profile", False): "profile_rejected",
            ("shaken", True): "shaken_approved",
            ("shaken", False): "failed",   # SHAKEN rejection is rare and terminal
            ("cnam", True): "cnam_approved",
            ("cnam", False): "failed",     # CNAM display-name rejection; tenant can dispute
            # W6 — Branded Calling: approval lights up `branded_calling_live`,
            # rejection terminates at `failed`. Tenants can re-enroll via the
            # admin endpoint after Twilio resolves the rejection reason.
            ("branded_calling", True): "branded_calling_live",
            ("branded_calling", False): "failed",
        }
        new_state = _STATE_MAP.get((bundle_type, is_approved))
    else:
        # Truncate for log safety in case Twilio adds long status strings
        logger.warning(
            "trust_hub status_callback unknown_status status=%s bundle=%s — no DB write",
            twilio_status[:64], bundle_type,
        )

    if new_state is None:
        # Unknown bundle type or unrecognised status — cut inbound marker and return
        logger.warning(
            "trust_hub status_callback unrecognised_bundle_or_status bundle=%s status=%s",
            bundle_type, twilio_status,
        )
        try:
            await cut_trust_receipt(
                receipt_type="webhook_received",
                trust_profile=trust_profile_for_receipt,
                outcome="success",
                from_state="<webhook>",
                to_state=current_state,
                redacted_inputs={
                    "trust_profile_id": trust_profile_id,
                    "step_name": "status_callback_unrecognised",
                    "twilio_resource_sid": resource_sid,
                },
                redacted_outputs={
                    "twilio_resource_sid": resource_sid,
                    "twilio_status": twilio_status,
                },
                twilio_resource_sid=resource_sid,
                twilio_status=twilio_status,
            )
        except TrustReceiptError as exc:
            logger.error("trust_hub status_callback cut_receipt_failed err=%s", exc)
        return {"status": "received"}

    # Step 6: Idempotency check — state ordering table for forward-progress guard
    # If we're already AT or PAST the target state, this is a duplicate callback.
    _STATE_RANK: dict[str, int] = {
        "kyb_collected": 0,
        # R-W5-004: kyb_disputed is a real `from_state` (set by the dispute
        # endpoint when a tenant resubmits). Without this entry, .get(...)
        # returns the -1 default and the idempotency guard
        # (`current_rank >= target_rank`) is bypassed for any disputing
        # tenant — letting a stale Twilio retry advance them straight to
        # `profile_approved` without the resubmit completing.
        "kyb_disputed": 0,
        "profile_drafted": 1,
        "profile_submitted": 2,
        "profile_approved": 3,
        "profile_rejected": 3,
        "shaken_created": 4,
        "shaken_submitted": 5,
        "shaken_approved": 6,
        "cnam_created": 7,
        "cnam_submitted": 8,
        "cnam_approved": 9,
        "number_attached": 10,
        "branded_calling_pending": 11,
        "branded_calling_live": 12,  # W6 — terminal happy state when feature flag is on
        "failed": 99,
        "suspended": 99,
    }
    current_rank = _STATE_RANK.get(current_state, -1)
    target_rank = _STATE_RANK.get(new_state, -1)
    if current_rank >= target_rank and new_state != "failed":
        # Already at or past this state — silent no-op to handle Twilio retries
        logger.info(
            "trust_hub status_callback idempotent_skip profile=%s current=%s target=%s",
            trust_profile_id, current_state, new_state,
        )
        return {"status": "received"}

    # Special case: if already in a terminal failure state, don't overwrite
    if current_state in ("failed", "suspended") and new_state == "failed":
        logger.info(
            "trust_hub status_callback already_failed profile=%s", trust_profile_id
        )
        return {"status": "received"}

    # Step 7: Determine receipt type
    _RECEIPT_MAP: dict[tuple[str, bool], str] = {
        ("profile", True): "customer_profile_approved",
        ("profile", False): "customer_profile_rejected",
        ("shaken", True): "shaken_trust_product_approved",
        ("shaken", False): "shaken_trust_product_rejected",
        ("cnam", True): "cnam_trust_product_approved",
        ("cnam", False): "cnam_trust_product_rejected",
        # W6 — Branded Calling status callback receipts
        ("branded_calling", True): "branded_calling_approved",
        ("branded_calling", False): "branded_calling_rejected",
    }
    receipt_type: str = _RECEIPT_MAP[(bundle_type, is_approved)]

    # Step 8: Extract rejection reason from Twilio payload (rejection events only).
    #
    # security-reviewer R-W5-002 — `FailureReason` may include rep names or
    # other tenant document strings. We sanitize before storing in
    # `tenant_trust_profiles.rejection_reason` (which is served back via
    # GET /status). The RAW value still flows to the receipt's
    # `twilio_rejection_reason` field for audit completeness (see R-W5-003);
    # that field lives in `trust_state_transitions` which is service-role-only.
    failure_reason_raw: str | None = None
    failure_reason_sanitized: str | None = None
    error_code: str | None = None
    if not is_approved:
        failure_reason_raw = str(form.get("FailureReason", "")) or None
        failure_reason_sanitized = _sanitize_failure_reason(failure_reason_raw)
        error_code_raw = str(form.get("ErrorCode", "")) or None
        # Error codes are short alphanumeric Twilio codes (e.g., "30450"); cap to 32 chars.
        error_code = error_code_raw[:32] if error_code_raw else None

    try:
        # Step 9: Update trust_state + timestamps in DB
        update_payload: dict[str, Any] = {"trust_state": new_state}
        if is_approved and bundle_type == "profile":
            update_payload["profile_approved_at"] = datetime.now(timezone.utc).isoformat()
        elif is_approved and bundle_type == "cnam":
            update_payload["cnam_approved_at"] = datetime.now(timezone.utc).isoformat()
        elif is_approved and bundle_type == "branded_calling":
            # W6 — flip the feature flag on the trust profile so GET /status
            # surfaces branded_calling_live=true in the milestones response.
            # The display name itself was set during enrollment.
            update_payload["branded_calling_enabled"] = True
        # F-11: `shaken_approved_at` column does not exist in migration 109.
        # The shaken_approved milestone is derived from `trust_state` rank
        # in GET /status; do NOT attempt to write it here.
        if not is_approved and failure_reason_sanitized is not None:
            update_payload["rejection_reason"] = failure_reason_sanitized
        if not is_approved and error_code is not None:
            update_payload["rejection_code"] = error_code

        await supabase_update(
            "tenant_trust_profiles",
            f"id=eq.{trust_profile_id}",
            update_payload,
        )

        # Step 10: Cut the matching receipt.
        # R-W5-003 — forward the RAW `FailureReason` and `ErrorCode` to the
        # receipt for audit completeness (Law #2). `cut_trust_receipt`
        # writes them to `trust_state_transitions.twilio_rejection_*`,
        # which is service-role-only. They are NOT written to
        # `redacted_inputs` / `redacted_outputs` — those are served back
        # in wider audit views and must remain PII-clean.
        await cut_trust_receipt(
            receipt_type=receipt_type,
            trust_profile=trust_profile_for_receipt,
            outcome="success" if is_approved else "denied",
            from_state=current_state,
            to_state=new_state,
            redacted_inputs={
                "trust_profile_id": trust_profile_id,
                "step_name": f"twilio_{receipt_type}",
                "twilio_resource_sid": resource_sid,
            },
            redacted_outputs={
                "twilio_resource_sid": resource_sid,
                "twilio_status": twilio_status,
            },
            twilio_resource_sid=resource_sid,
            twilio_status=twilio_status,
            twilio_rejection_code=error_code,
            twilio_rejection_reason=failure_reason_raw,
        )

        # Step 11: Approval → enqueue; rejection → do NOT enqueue (tenant must dispute)
        if is_approved:
            await _enqueue_advance_trust_state(trust_profile_id)

    except (SupabaseClientError, TrustReceiptError, Exception) as exc:  # noqa: BLE001
        # Always return 200 to Twilio; internal errors get a processing_failed receipt
        logger.error(
            "trust_hub status_callback processing_error profile=%s err=%s",
            trust_profile_id, exc,
        )
        try:
            await cut_trust_receipt(
                receipt_type="webhook_processing_failed",
                trust_profile=trust_profile_for_receipt,
                outcome="failed",
                from_state=current_state,
                to_state=current_state,
                redacted_inputs={
                    "trust_profile_id": trust_profile_id,
                    "step_name": "status_callback_processing_failed",
                    "twilio_resource_sid": resource_sid,
                },
                redacted_outputs={
                    "twilio_resource_sid": resource_sid,
                    "twilio_status": twilio_status,
                    "error": type(exc).__name__,
                },
                twilio_resource_sid=resource_sid,
                twilio_status=twilio_status,
            )
        except TrustReceiptError as receipt_exc:
            logger.error(
                "trust_hub status_callback processing_failed_receipt_also_failed err=%s",
                receipt_exc,
            )

    return {"status": "received"}


__all__ = ["router"]
