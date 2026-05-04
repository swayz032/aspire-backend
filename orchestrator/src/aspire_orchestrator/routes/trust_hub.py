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
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

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
    rep_vault_ids: list[dict[str, str]] = []
    for idx, rep in enumerate(body.authorized_reps):
        try:
            dob_vault_id = await _vault_create_secret(
                rep.dob,
                name=f"{tenant_id}:rep_{idx}_dob",
                description=f"DOB for tenant {tenant_id} rep index {idx}",
            )
            ssn_vault_id = await _vault_create_secret(
                rep.ssn_last4,
                name=f"{tenant_id}:rep_{idx}_ssn_last4",
                description=f"SSN-last4 for tenant {tenant_id} rep index {idx}",
            )
        except SupabaseClientError as exc:
            logger.error(
                "trust_hub kyb_submit vault_rep_encrypt_failed idx=%d err=%s", idx, exc
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "VAULT_UNAVAILABLE", "reason_code": "VAULT_UNAVAILABLE"},
            ) from exc
        rep_vault_ids.append({"dob_vault_id": dob_vault_id, "ssn_vault_id": ssn_vault_id})

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

    milestones: dict[str, bool] = {
        "kyb_collected": bool(profile.get("kyb_collected_at")),
        "profile_approved": bool(profile.get("profile_approved_at")),
        "shaken_approved": bool(profile.get("shaken_approved_at")),
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

    logger.info(
        "trust_hub status_callback received ResourceSid=%s Status=%s",
        resource_sid, twilio_status,
    )

    # Look up trust profile by matching any bundle SID column
    trust_profile: dict[str, Any] | None = None
    if resource_sid:
        for sid_column in (
            "twilio_secondary_profile_sid",
            "twilio_shaken_bundle_sid",
            "twilio_cnam_bundle_sid",
        ):
            try:
                rows = await supabase_select(
                    "tenant_trust_profiles",
                    f"{sid_column}=eq.{resource_sid}",
                    limit=1,
                )
                if rows:
                    trust_profile = rows[0]
                    break
            except SupabaseClientError:
                pass  # continue searching other columns

    # W3 skeleton: cut webhook_received receipt and return 200.
    # W5 will add full dispatch (advance state machine based on twilio_status).
    #
    # `webhook_received` is an inbound-event marker, NOT a state transition.
    # We pair it with from_state="<webhook>" → to_state=current_state so the
    # trust_state_transitions append-only ledger has a row, but the existing
    # `tst_no_self_loop CHECK (from_state != to_state)` constraint is satisfied
    # because "<webhook>" is a sentinel that no real state ever takes.
    if trust_profile:
        trust_profile_for_receipt: dict[str, Any] = {
            "id": str(trust_profile.get("id", "")),
            "suite_id": str(trust_profile.get("suite_id", "")),
            "tenant_id": str(trust_profile.get("tenant_id", "")),
            "office_id": str(trust_profile.get("office_id", "")),
        }
        current_state = str(trust_profile.get("trust_state", "unknown"))

        try:
            await cut_trust_receipt(
                receipt_type="webhook_received",
                trust_profile=trust_profile_for_receipt,
                outcome="success",
                from_state="<webhook>",
                to_state=current_state,
                redacted_inputs={
                    "trust_profile_id": str(trust_profile.get("id", "")),
                    "step_name": "status_callback_received",
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
            # W3: do not let receipt failure block the 200 return to Twilio
            logger.error(
                "trust_hub status_callback cut_receipt_failed err=%s", exc
            )
    else:
        logger.warning(
            "trust_hub status_callback no_profile_found ResourceSid=%s", resource_sid
        )

    # TODO (W5): dispatch advance_trust_state based on twilio_status
    # W5 implements full dispatch — currently logs only

    return {"status": "received"}


__all__ = ["router"]
