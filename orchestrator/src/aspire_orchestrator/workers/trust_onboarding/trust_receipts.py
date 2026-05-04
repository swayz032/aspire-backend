"""Trust-onboarding receipt taxonomy + hash-chain helpers (W2-E).

Defines the 21 trust-related receipt types and the helper that cuts each
receipt while:
    1. Hash-chaining via `previous_receipt_id` per `trust_profile_id`
    2. Enforcing PII redaction (email/phone/DOB/SSN never appear)
    3. Writing the matching `trust_state_transitions` audit row in the
       same logical operation

The state machine (W2-D) calls `cut_trust_receipt(...)` on every transition.
Status-callback handler (Wave 5) also calls it when Twilio approves/rejects.

Receipt types (Yellow tier, hash-chained):

    State machine (15):
        kyb_collected
        customer_profile_created
        customer_profile_submitted
        customer_profile_approved
        customer_profile_rejected
        shaken_trust_product_created
        shaken_trust_product_approved
        shaken_trust_product_rejected
        cnam_trust_product_created
        cnam_display_name_set
        cnam_trust_product_approved
        cnam_trust_product_rejected
        branded_calling_enrolled
        number_attached_to_profile
        caller_id_lookup_enabled

    A2P 10DLC (W7, 2):
        a2p_brand_registered
        a2p_campaign_approved

    Number swap (W11, 6):
        number_swap_initiated
        number_detached_from_profile
        caller_id_lookup_disabled
        front_desk_phone_switched
        phone_number_released
        number_swap_complete

PII redaction enforcement (Law #9 + W1 verification mandate R-006):
    Allowed in `redacted_inputs`:  suite_id, trust_profile_id, step_name,
                                   phone_number_id, rep_index, brand_id,
                                   campaign_id, twilio_resource_sid
    Allowed in `redacted_outputs`: twilio_resource_sid, twilio_status,
                                   bundle_sid, end_user_sid, channel_endpoint_sid,
                                   cnam_display_name (already public-facing),
                                   caller_id_e164_redacted,
                                   latency_seconds, version_no
    BLOCKED:                       email, phone_e164, phone_number,
                                   first_name, last_name, dob, ssn, ssn_last4,
                                   ein, raw_business_name (only on cnam_display_name_set
                                   is the business_name allowed — and only the
                                   already-sanitized display_name)

Hash chain:
    trust_state_transitions.receipt_id = receipts.receipt_id (TEXT — worker
    pre-generates the human-readable receipt_id and reuses the same value
    here). previous_receipt_id is the most recent transition's receipt_id
    for this trust_profile_id.

Author: Aspire — Wave 2-E (per docs/plans/per-tenant-trust-hub-cnam.md §III)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Final

from aspire_orchestrator.middleware.correlation import get_correlation_id, get_trace_id
from aspire_orchestrator.services import receipt_store
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert,
    supabase_select,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Receipt-type registry
# ---------------------------------------------------------------------------


# Tier of every trust receipt — all Yellow per architect plan §II.E.
RECEIPT_TIER: Final[str] = "yellow"

# Tool used (constant for all trust receipts).
RECEIPT_TOOL: Final[str] = "twilio_trust_hub"

RECEIPT_TYPES: Final[frozenset[str]] = frozenset({
    # State machine (15)
    "kyb_collected",
    "customer_profile_created",
    "customer_profile_submitted",
    "customer_profile_approved",
    "customer_profile_rejected",
    "shaken_trust_product_created",
    "shaken_trust_product_submitted",
    "shaken_trust_product_approved",
    "shaken_trust_product_rejected",
    "cnam_trust_product_created",
    "cnam_trust_product_submitted",
    "cnam_display_name_set",
    "cnam_trust_product_approved",
    "cnam_trust_product_rejected",
    "branded_calling_enrolled",
    "number_attached_to_profile",
    "caller_id_lookup_enabled",
    # A2P 10DLC (W7)
    "a2p_brand_registered",
    "a2p_campaign_approved",
    # Number swap (W11)
    "number_swap_initiated",
    "number_detached_from_profile",
    "caller_id_lookup_disabled",
    "front_desk_phone_switched",
    "phone_number_released",
    "number_swap_complete",
    # Webhook ingestion (Wave 3 skeleton + Wave 5 dispatch)
    "webhook_received",
    "webhook_processing_failed",
})


# ---------------------------------------------------------------------------
# PII guardrails (W1 verification mandate R-006)
# ---------------------------------------------------------------------------


# Field names that are NEVER allowed in redacted_inputs / redacted_outputs.
# If a receipt is cut with one of these keys, we raise immediately —
# fail-closed on PII leak attempts (Law #9, Law #3).
_FORBIDDEN_PII_KEYS: Final[frozenset[str]] = frozenset({
    "email",
    "phone_e164",
    "phone_number",
    "first_name",
    "last_name",
    "full_name",
    "dob",
    "date_of_birth",
    "ssn",
    "ssn_last4",
    "ein",
    "tax_id",
    "address_street",
    "raw_business_name",  # only allowed inside cnam_display_name_set as the source name
    "owner_name",
})


class TrustReceiptError(Exception):
    """Raised when a trust receipt fails validation or write."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _assert_no_pii(payload: dict[str, Any], *, label: str, receipt_type: str) -> None:
    """Raise TrustReceiptError if any PII key appears in the payload.

    Allowance: cnam_display_name_set may include the SANITIZED display name
    (already public-facing — it's literally the caller-ID display string).
    """
    if not isinstance(payload, dict):
        return
    for key in payload.keys():
        if key.lower() in _FORBIDDEN_PII_KEYS:
            # The only allowed exception: nothing — even the sanitized CNAM
            # name lives under `cnam_display_name`, not under any forbidden key.
            raise TrustReceiptError(
                "PII_LEAK_BLOCKED",
                f"Receipt {receipt_type!r} attempted to write forbidden PII key "
                f"{key!r} in {label}. Forbidden keys: {sorted(_FORBIDDEN_PII_KEYS)}",
            )


# ---------------------------------------------------------------------------
# Hash-chain lookup
# ---------------------------------------------------------------------------


async def _get_previous_receipt_id(trust_profile_id: str) -> str | None:
    """Return the receipt_id (TEXT) of the most recent transition for this profile.

    Used to set `previous_receipt_id` on the new transition row, building
    the per-tenant audit hash chain.
    """
    try:
        rows = await supabase_select(
            "trust_state_transitions",
            f"trust_profile_id=eq.{trust_profile_id}&receipt_id=not.is.null",
            order_by="created_at.desc",
            limit=1,
        )
    except SupabaseClientError as exc:
        logger.warning(
            "trust_receipts previous_receipt_id_lookup_failed trust_profile_id=%s: %s",
            trust_profile_id, exc,
        )
        return None
    if not rows:
        return None
    rid = rows[0].get("receipt_id")
    return str(rid) if rid else None


# ---------------------------------------------------------------------------
# Public API — cut_trust_receipt
# ---------------------------------------------------------------------------


async def cut_trust_receipt(
    *,
    receipt_type: str,
    trust_profile: dict[str, Any],
    outcome: str,
    from_state: str,
    to_state: str,
    redacted_inputs: dict[str, Any] | None = None,
    redacted_outputs: dict[str, Any] | None = None,
    reason_code: str | None = None,
    twilio_resource_sid: str | None = None,
    twilio_status: str | None = None,
    twilio_rejection_code: str | None = None,
    twilio_rejection_reason: str | None = None,
    capability_token_id: str | None = None,
    worker_job_id: str | None = None,
    retry_count: int = 0,
) -> str:
    """Cut a Yellow-tier trust receipt + write the matching trust_state_transitions row.

    Args:
        receipt_type: One of RECEIPT_TYPES.
        trust_profile: dict with at least suite_id, tenant_id, office_id, id (the
                       trust_profile_id). Most state-machine call sites pass the
                       full row.
        outcome: "success" | "failed" | "denied" | "pending"
        from_state: prior trust_state value
        to_state:   new trust_state value
        redacted_inputs: dict of safe identifiers (NO PII — see _FORBIDDEN_PII_KEYS)
        redacted_outputs: dict of Twilio SIDs / status / latency (NO PII)
        reason_code: e.g. "MISSING_FIELDS", "TWILIO_REJECT", "VAULT_UNAVAILABLE"
        twilio_resource_sid: BU... / RA... / IT... — the Twilio bundle/assignment
                             that drove this transition
        twilio_status: "draft" | "pending-review" | "twilio-approved" | "twilio-rejected"
        twilio_rejection_code, twilio_rejection_reason: only set when Twilio rejects
        capability_token_id: cap-token UUID if this receipt was driven by a
                             gated route (KYB submit, dispute, swap)
        worker_job_id: ARQ job ID for traceability
        retry_count: ARQ retry count

    Returns:
        The receipt_id (TEXT) of the cut receipt — caller stores this on the
        trust profile if needed.

    Raises:
        TrustReceiptError: on receipt-type validation failure, PII leak attempt,
            or Supabase insert failure for the audit row. Receipt store failures
            (downstream of the audit row) do NOT raise — they are logged and
            the transition still records.
    """
    if receipt_type not in RECEIPT_TYPES:
        raise TrustReceiptError(
            "UNKNOWN_RECEIPT_TYPE",
            f"Unknown trust receipt_type {receipt_type!r}. Add to RECEIPT_TYPES first.",
        )

    suite_id = trust_profile.get("suite_id", "")
    tenant_id = trust_profile.get("tenant_id", "")
    office_id = trust_profile.get("office_id", "")
    trust_profile_id = trust_profile.get("id", "")
    if not all([suite_id, tenant_id, office_id, trust_profile_id]):
        raise TrustReceiptError(
            "MISSING_SCOPE",
            f"Receipt {receipt_type!r} requires suite_id+tenant_id+office_id+trust_profile_id "
            f"on trust_profile arg (got suite_id={suite_id!r} tenant_id={tenant_id!r} "
            f"office_id={office_id!r} trust_profile_id={trust_profile_id!r}).",
        )

    # PII guardrails — fail-closed (R-006)
    _assert_no_pii(redacted_inputs or {}, label="redacted_inputs", receipt_type=receipt_type)
    _assert_no_pii(redacted_outputs or {}, label="redacted_outputs", receipt_type=receipt_type)

    receipt_id = f"trust_{receipt_type}_{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).isoformat()
    correlation_id = get_correlation_id() or ""
    trace_id = get_trace_id() or ""

    previous_receipt_id = await _get_previous_receipt_id(str(trust_profile_id))

    # 1. Write the receipts row (Yellow tier, fail-closed)
    receipt_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "receipt_id": receipt_id,  # human-readable text key — also reused as state-transition reference
        "receipt_type": receipt_type,
        "outcome": outcome,
        "action_type": receipt_type,
        "tool_used": RECEIPT_TOOL,
        "risk_tier": RECEIPT_TIER,
        "suite_id": str(suite_id),
        "tenant_id": str(tenant_id),
        "office_id": str(office_id),
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "created_at": now_iso,
    }
    if reason_code:
        receipt_row["reason_code"] = reason_code
    if redacted_inputs:
        receipt_row["redacted_inputs"] = redacted_inputs
    if redacted_outputs:
        receipt_row["redacted_outputs"] = redacted_outputs
    if capability_token_id:
        receipt_row["capability_token_id"] = capability_token_id

    try:
        receipt_store.store_receipts_strict([receipt_row])
    except Exception as exc:  # noqa: BLE001 — fail-closed downstream of audit
        logger.error(
            "trust_receipts store_receipts_strict failed receipt_type=%s receipt_id=%s err=%s",
            receipt_type, receipt_id, exc,
        )
        raise TrustReceiptError("RECEIPT_STORE_FAILED", str(exc)) from exc

    # 2. Write the trust_state_transitions audit row (append-only, immutability
    #    trigger from migration 114 enforces no-update / no-delete)
    transition_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "suite_id": str(suite_id),
        "trust_profile_id": str(trust_profile_id),
        "from_state": from_state,
        "to_state": to_state,
        "event_type": receipt_type,
        "twilio_resource_sid": twilio_resource_sid,
        "twilio_status": twilio_status,
        "twilio_rejection_code": twilio_rejection_code,
        "twilio_rejection_reason": twilio_rejection_reason,
        "receipt_id": receipt_id,
        "previous_receipt_id": previous_receipt_id,
        "worker_job_id": worker_job_id,
        "retry_count": retry_count,
        "created_at": now_iso,
    }
    try:
        await supabase_insert("trust_state_transitions", transition_row)
    except SupabaseClientError as exc:
        # The receipt is already written — this is an audit-row write failure.
        # Log loud but do NOT raise on the second failure (the receipt itself
        # is the durable governance record; transitions table is operational).
        # That said, if this happens we want to know.
        logger.error(
            "trust_receipts state_transition_insert_failed trust_profile_id=%s "
            "receipt_id=%s err=%s",
            trust_profile_id, receipt_id, exc,
        )
        raise TrustReceiptError("STATE_TRANSITION_INSERT_FAILED", str(exc)) from exc

    logger.info(
        "trust_receipts cut receipt_type=%s trust_profile_id=%s from=%s to=%s "
        "outcome=%s receipt_id=%s",
        receipt_type, trust_profile_id, from_state, to_state, outcome, receipt_id,
    )
    return receipt_id


__all__ = [
    "RECEIPT_TYPES",
    "RECEIPT_TIER",
    "RECEIPT_TOOL",
    "TrustReceiptError",
    "cut_trust_receipt",
]
