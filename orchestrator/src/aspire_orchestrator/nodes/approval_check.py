"""Approval Check Node — Yellow/Red tier governance (Law #4).

Responsibilities:
1. GREEN tier: auto-approve (no user confirmation needed)
2. YELLOW tier: verify approval binding (payload_hash, replay defense)
3. RED tier: verify approval binding + presence token
4. If approval missing: return ApprovalRequest with payload_hash
5. If approval expired/rejected: deny with receipt
6. Emit approval_requested or approval_granted/denied receipt

Per approval_binding_spec.md:
  - payload_hash = SHA-256 of canonical JSON of execution payload
  - Binding: suite_id + request_id + payload_hash + policy_version
  - Reject mismatched payload_hash (approve-then-swap defense)
  - Reject expired approvals
  - Reject reused request_id

Per presence_sessions.md:
  - RED tier requires presence_token (TTL <=5min, nonce bound to payload_hash)
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.types import interrupt

from aspire_orchestrator.models import (
    AspireErrorCode,
    Outcome,
    ReceiptType,
    RiskTier,
)
from aspire_orchestrator.services.approval_service import (
    ApprovalBinding,
    ApprovalBindingError,
    CURRENT_POLICY_VERSION,
    compute_payload_hash,
    verify_approval_binding,
)
from aspire_orchestrator.services.presence_service import (
    PresenceError,
    verify_presence_token,
)
from aspire_orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)


def _extract_execution_payload(state: OrchestratorState) -> dict[str, Any]:
    """Extract the execution payload for approval hash binding.

    The payload includes the exact parameters that will be executed,
    bound to the tenant context. This prevents approve-then-swap attacks.
    """
    request = state.get("request")
    payload: dict[str, Any] = {}

    if request is not None:
        if hasattr(request, "payload"):
            payload = request.payload if isinstance(request.payload, dict) else {}
        elif isinstance(request, dict):
            payload = request.get("payload", {})

    return {
        "task_type": state.get("task_type", "unknown"),
        "parameters": payload,
        "suite_id": state.get("suite_id", ""),
        "office_id": state.get("office_id", ""),
    }


# --- PII Redaction (Law #9 — never persist raw PII in drafts) ---

_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# Keys whose values should be redacted in execution_payload before Supabase persistence
_PII_KEYS = frozenset({
    "customer_email", "email", "to", "cc", "bcc", "from_email",
    "phone", "phone_number", "mobile", "customer_phone",
    "ssn", "social_security", "tax_id",
    "card_number", "account_number", "routing_number",
    "address", "street", "zip_code", "postal_code",
})

# Keys that are safe to keep (needed for execution but not PII)
_SAFE_KEYS = frozenset({
    "amount_cents", "amount", "currency", "description", "title",
    "start_time", "end_time", "duration_minutes", "event_type",
    "invoice_id", "quote_id", "room_name", "query",
    "customer_name", "client_name", "subject",
    "due_days", "expiry_days", "location", "participants",
})


def _redact_pii(params: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy params and redact PII fields for safe persistence.

    Execution_payload (with real values) is stored separately for resume execution.
    This redacted version is used for draft_summary and audit display.
    """
    if not params:
        return {}

    redacted = copy.deepcopy(params)

    def _redact_value(key: str, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _redact_value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [_redact_value(key, item) for item in value]
        if not isinstance(value, str):
            return value

        lower_key = key.lower()
        # Redact known PII keys
        if lower_key in _PII_KEYS:
            if _EMAIL_RE.search(value):
                return "<EMAIL_REDACTED>"
            if _PHONE_RE.search(value):
                return "<PHONE_REDACTED>"
            if _SSN_RE.search(value):
                return "<SSN_REDACTED>"
            if _CC_RE.search(value):
                return "<CC_REDACTED>"
            return "<PII_REDACTED>"

        # Scan string values for embedded PII patterns even in non-PII keys
        value = _SSN_RE.sub("<SSN_REDACTED>", value)
        value = _CC_RE.sub("<CC_REDACTED>", value)
        return value

    for key, value in redacted.items():
        redacted[key] = _redact_value(key, value)

    return redacted


def _mask_email(email: str) -> str:
    """Partially mask email for display: j***@acme.com."""
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if len(local) <= 1:
        masked_local = local + "***"
    else:
        masked_local = local[0] + "***"
    return f"{masked_local}@{domain}"


def _build_draft_summary(task_type: str, execution_params: dict[str, Any]) -> str:
    """Build human-readable draft summary for Authority Queue display."""
    tt = task_type.lower()
    p = execution_params or {}

    name = p.get("customer_name") or p.get("client_name") or p.get("contact_name") or "client"

    if "invoice" in tt:
        cents = p.get("amount_cents")
        if isinstance(cents, (int, float)) and cents > 0:
            return f"Invoice for {name} — ${cents / 100:.2f}"
        return f"Invoice for {name}"

    if "email" in tt:
        to = p.get("to", "recipient")
        # Partial mask email for draft_summary (PII protection — Law #9)
        # Full email stored in execution_payload for resume execution
        masked_to = _mask_email(to) if "@" in str(to) else to
        subject_text = p.get("subject", "")
        return f"Email to {masked_to}" + (f" — Re: {subject_text}" if subject_text else "")

    if "calendar" in tt:
        title = p.get("title", "event")
        start = p.get("start_time", "")
        return f"Calendar: {title}" + (f" on {start}" if start else "")

    if "quote" in tt or "proposal" in tt:
        return f"Quote for {name}"

    if "contract" in tt:
        return f"Contract for {name}"

    if "payment" in tt or "transfer" in tt:
        cents = p.get("amount_cents")
        if isinstance(cents, (int, float)) and cents > 0:
            return f"Payment — ${cents / 100:.2f}"
        return f"Payment — review required"

    return f"{task_type} — review required"


def _make_receipt(
    *,
    correlation_id: str,
    suite_id: str,
    office_id: str,
    actor_type: str,
    actor_id: str,
    action_type: str,
    risk_tier: str,
    outcome: str,
    reason_code: str,
    receipt_type: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a receipt dict with standard fields."""
    receipt: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action_type": action_type,
        "risk_tier": risk_tier,
        "tool_used": "orchestrator.approval_check",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "reason_code": reason_code,
        "receipt_type": receipt_type,
        "receipt_hash": "",
    }
    if details:
        receipt["details"] = details
    return receipt


def _append_receipt(
    receipts: list[dict[str, Any]],
    receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    if any(existing.get("id") == receipt.get("id") for existing in receipts):
        return receipts
    return [*receipts, receipt]


def _build_approval_request_id(
    *,
    suite_id: str,
    request_id: str,
    payload_hash: str,
    task_type: str,
) -> str:
    raw = f"aspire:approval:{suite_id}:{request_id}:{payload_hash}:{task_type}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _build_approval_receipt_id(approval_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aspire:approval-receipt:{approval_id}"))


def _build_pending_interrupt_payload(
    state: OrchestratorState,
    pending_update: dict[str, Any],
) -> dict[str, Any]:
    from aspire_orchestrator.nodes.respond import respond_node

    pending_state = {
        **state,
        **pending_update,
    }
    response = respond_node(pending_state).get("response", {})
    if isinstance(response, dict):
        pending_update = {
            **pending_update,
            "receipt_ids": list(response.get("receipt_ids", pending_update.get("receipt_ids", []))),
        }
    return {
        "response": response,
        "state_update": pending_update,
    }


def _map_binding_error_to_error_code(
    error: ApprovalBindingError,
) -> AspireErrorCode:
    """Map approval binding errors to Aspire error codes."""
    if error == ApprovalBindingError.APPROVAL_EXPIRED:
        return AspireErrorCode.APPROVAL_EXPIRED
    if error in (
        ApprovalBindingError.SUITE_MISMATCH,
        ApprovalBindingError.OFFICE_MISMATCH,
    ):
        return AspireErrorCode.TENANT_ISOLATION_VIOLATION
    # payload_hash mismatch, request_id reused, policy_version mismatch
    return AspireErrorCode.APPROVAL_BINDING_FAILED


def _map_presence_error_to_error_code(
    error: PresenceError,
) -> AspireErrorCode:
    """Map presence verification errors to Aspire error codes."""
    if error == PresenceError.TOKEN_MISSING:
        return AspireErrorCode.PRESENCE_REQUIRED
    if error in (
        PresenceError.SUITE_MISMATCH,
        PresenceError.OFFICE_MISMATCH,
    ):
        return AspireErrorCode.TENANT_ISOLATION_VIOLATION
    return AspireErrorCode.PRESENCE_INVALID




def approval_check_node(state: OrchestratorState) -> dict[str, Any]:
    """Check approval status for the current request.

    GREEN tier: auto-approve
    YELLOW tier: require user approval evidence with payload_hash binding
    RED tier: require user approval + presence token verification
    """
    if state.get("error_code"):
        return {"approval_status": "denied"}

    risk_tier = state.get("risk_tier", RiskTier.YELLOW)
    correlation_id = state.get("correlation_id", str(uuid.uuid4()))
    suite_id = state.get("suite_id", "unknown")
    office_id = state.get("office_id", "unknown")
    request_id = state.get("request_id", str(uuid.uuid4()))
    risk_tier_value = risk_tier.value if isinstance(risk_tier, RiskTier) else risk_tier

    # GREEN tier: auto-approve with receipt (Law #2)
    if risk_tier == RiskTier.GREEN:
        logger.info(
            "GREEN tier auto-approve: correlation=%s, suite=%s",
            correlation_id[:8], suite_id[:8],
        )
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator.approval_check",
            action_type="approval.auto_approve",
            risk_tier="green",
            outcome=Outcome.SUCCESS.value,
            reason_code="GREEN_AUTO_APPROVED",
            receipt_type=ReceiptType.APPROVAL_GRANTED.value,
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)
        return {
            "approval_status": "approved",
            "approval_evidence": None,
            "pipeline_receipts": existing_receipts,
        }

    # --- YELLOW/RED tier: Compute payload hash for approval binding ---
    execution_payload = _extract_execution_payload(state)
    payload_hash = compute_payload_hash(execution_payload)

    # Check if approval evidence exists in the request
    approval_evidence = state.get("approval_evidence")

    if approval_evidence is None:
        # --- Safe Mode (AVA_SAFE_MODE=1 → all operations draft-only) ---
        from aspire_orchestrator.config.settings import settings

        if settings.ava_safe_mode:
            safe_receipt = _make_receipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                actor_type="system",
                actor_id="orchestrator.approval_check",
                action_type="approval.safe_mode",
                risk_tier=risk_tier_value,
                outcome=Outcome.PENDING.value,
                reason_code="SAFE_MODE",
                receipt_type=ReceiptType.APPROVAL_REQUESTED.value,
            )
            existing_receipts = list(state.get("pipeline_receipts", []))
            existing_receipts.append(safe_receipt)
            return {
                "approval_status": "pending",
                "outcome": Outcome.PENDING,
                "error_code": "SAFE_MODE",
                "error_message": "Safe mode active — all operations are draft-only",
                "pipeline_receipts": existing_receipts,
            }

        # No approval yet — return approval request to client.
        #
        # Ecosystem interaction states (Law #8):
        #   YELLOW tier → WARM state (inline voice/chat confirmation)
        #     Ava presents the draft and asks "Should I proceed?"
        #     User confirms in the conversation → re-submit with approval_evidence
        #   RED tier → HOT state (video presence required)
        #     Ava escalates to video: "This requires your face-to-face approval"
        #     User appears on camera → approval binding verified → execute
        #
        # Authority Queue is for ASYNC items only — NOT for inline approvals.
        is_red = risk_tier == RiskTier.RED

        logger.info(
            "Approval required: tier=%s, state=%s, correlation=%s, suite=%s",
            risk_tier_value,
            "HOT (video)" if is_red else "WARM (inline)",
            correlation_id[:8], suite_id[:8],
        )

        # Use distinct error codes: APPROVAL_REQUIRED (YELLOW inline) vs
        # PRESENCE_REQUIRED (RED video). The Desktop handles them differently.
        error_code = (
            AspireErrorCode.PRESENCE_REQUIRED if is_red
            else AspireErrorCode.APPROVAL_REQUIRED
        )

        approval_id = _build_approval_request_id(
            suite_id=suite_id,
            request_id=request_id,
            payload_hash=payload_hash,
            task_type=state.get("task_type", "unknown"),
        )

        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator",
            action_type="approval.request",
            risk_tier=risk_tier_value,
            outcome=Outcome.PENDING.value,
            reason_code=error_code.value,
            receipt_type=ReceiptType.APPROVAL_REQUESTED.value,
            details={"payload_hash": payload_hash},
        )
        receipt["id"] = _build_approval_receipt_id(approval_id)
        existing_receipts = _append_receipt(list(state.get("pipeline_receipts", [])), receipt)

        if is_red:
            error_msg = "Red-tier action requires video presence approval"
        else:
            error_msg = "Yellow-tier action — Ava is asking for your confirmation"

        # --- Draft-First: Persist draft to Supabase for Authority Queue ---
        draft_id = None
        draft_persistence_status = "failed"
        execution_params = state.get("execution_params")
        approval_row: dict[str, Any] = {
            "approval_id": approval_id,
            "tenant_id": suite_id,
            "run_id": correlation_id,
            "request_id": request_id,
            "thread_id": state.get("thread_id"),
            "session_id": state.get("session_id"),
            "tool": state.get("tool_used", "unknown"),
            "operation": state.get("task_type", "unknown"),
            "risk_tier": risk_tier_value,
            "policy_version": CURRENT_POLICY_VERSION,
            "approval_hash": payload_hash,
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "assigned_agent": state.get("assigned_agent", "ava"),
            "draft_summary": _build_draft_summary(
                state.get("task_type", "unknown"),
                execution_params or {},
            ),
        }
        if execution_params is not None:
            approval_row["execution_payload"] = execution_params
            approval_row["execution_params_hash"] = compute_payload_hash(execution_params)
            approval_row["payload_redacted"] = _redact_pii(execution_params)

        try:
            from aspire_orchestrator.services.supabase_client import supabase_upsert_sync

            supabase_upsert_sync(
                "approval_requests",
                approval_row,
                on_conflict="approval_id",
            )
            draft_id = approval_id
            draft_persistence_status = "success"
            logger.info("Draft upserted: draft_id=%s suite=%s", draft_id, suite_id[:8])
        except Exception as e:
            logger.warning(
                "Draft persistence failed: %s (suite=%s, task=%s)",
                e,
                suite_id[:8],
                state.get("task_type", "?"),
            )

        pending_update: dict[str, Any] = {
            "approval_status": "pending",
            "approval_payload_hash": payload_hash,
            "error_code": error_code.value,
            "error_message": error_msg,
            "required_approvals": ["owner_approval"] + (["presence_verification"] if is_red else []),
            "presence_required": is_red,
            "outcome": Outcome.PENDING,
            "draft_id": draft_id,
            "draft_persistence_status": draft_persistence_status,
            "pipeline_receipts": existing_receipts,
            "receipt_ids": [],
        }

        try:
            resumed = interrupt(_build_pending_interrupt_payload(state, pending_update))
        except RuntimeError as exc:
            if "runnable context" in str(exc).lower():
                return pending_update
            raise
        state = {
            **state,
            **pending_update,
        }
        if isinstance(resumed, dict):
            approval_evidence = resumed.get("approval_evidence")
            if "presence_token" in resumed:
                state["presence_token"] = resumed.get("presence_token")
        else:
            approval_evidence = None

    # --- Approval evidence exists — verify binding ---

    if approval_evidence is None:
        logger.warning(
            "Approval binding REJECTED: missing approval_evidence after resume, correlation=%s",
            correlation_id[:8],
        )
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator",
            action_type="approval.deny",
            risk_tier=risk_tier_value,
            outcome=Outcome.DENIED.value,
            reason_code=AspireErrorCode.APPROVAL_BINDING_FAILED.value,
            receipt_type=ReceiptType.APPROVAL_DENIED.value,
            details={"reason": "Approval resume missing approval evidence"},
        )
        existing_receipts = _append_receipt(list(state.get("pipeline_receipts", [])), receipt)
        return {
            "approval_status": "rejected",
            "error_code": AspireErrorCode.APPROVAL_BINDING_FAILED.value,
            "error_message": "Approval evidence missing",
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # Extract fields from ApprovalEvidence (Pydantic model or dict)
    if hasattr(approval_evidence, "approver_id"):
        approver_id = approval_evidence.approver_id
        approved_at_raw = approval_evidence.approved_at
    elif isinstance(approval_evidence, dict):
        approver_id = approval_evidence.get("approver_id", "unknown")
        approved_at_raw = approval_evidence.get("approved_at")
    else:
        # Fail closed: unrecognized evidence format
        logger.warning(
            "Approval binding REJECTED: unrecognized evidence format, correlation=%s",
            correlation_id[:8],
        )
        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="system",
            actor_id="orchestrator",
            action_type="approval.deny",
            risk_tier=risk_tier_value,
            outcome=Outcome.DENIED.value,
            reason_code=AspireErrorCode.APPROVAL_BINDING_FAILED.value,
            receipt_type=ReceiptType.APPROVAL_DENIED.value,
            details={"reason": "Unrecognized approval evidence format"},
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)
        return {
            "approval_status": "rejected",
            "error_code": AspireErrorCode.APPROVAL_BINDING_FAILED.value,
            "error_message": "Unrecognized approval evidence format",
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # Parse approved_at to datetime
    if isinstance(approved_at_raw, str):
        approved_at = datetime.fromisoformat(approved_at_raw)
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
    elif isinstance(approved_at_raw, datetime):
        approved_at = approved_at_raw
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
    else:
        approved_at = datetime.now(timezone.utc)

    # Build ApprovalBinding from the evidence
    # The evidence should contain the payload_hash that was computed at approval time
    evidence_payload_hash = ""
    if hasattr(approval_evidence, "payload_hash"):
        evidence_payload_hash = approval_evidence.payload_hash
    elif isinstance(approval_evidence, dict):
        evidence_payload_hash = approval_evidence.get("payload_hash", payload_hash)
    else:
        evidence_payload_hash = payload_hash

    evidence_policy_version = ""
    if hasattr(approval_evidence, "policy_version"):
        evidence_policy_version = approval_evidence.policy_version
    elif isinstance(approval_evidence, dict):
        evidence_policy_version = approval_evidence.get(
            "policy_version", CURRENT_POLICY_VERSION
        )
    else:
        evidence_policy_version = CURRENT_POLICY_VERSION

    # Get request_id from evidence or use current
    evidence_request_id = ""
    if hasattr(approval_evidence, "request_id"):
        evidence_request_id = approval_evidence.request_id
    elif isinstance(approval_evidence, dict):
        evidence_request_id = approval_evidence.get("request_id", request_id)
    else:
        evidence_request_id = request_id

    binding = ApprovalBinding(
        suite_id=suite_id,
        office_id=office_id,
        request_id=evidence_request_id,
        payload_hash=evidence_payload_hash,
        policy_version=evidence_policy_version,
        approved_at=approved_at,
        expires_at=approved_at,  # Will be replaced below
        approver_id=approver_id,
    )

    # Compute actual expiry from evidence or default (5 minutes)
    from aspire_orchestrator.services.approval_service import (
        DEFAULT_APPROVAL_EXPIRY_SECONDS,
    )

    if hasattr(approval_evidence, "expires_at") and approval_evidence.expires_at:
        expires_at = approval_evidence.expires_at
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
    elif isinstance(approval_evidence, dict) and "expires_at" in approval_evidence:
        expires_at = approval_evidence["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = approved_at + timedelta(seconds=DEFAULT_APPROVAL_EXPIRY_SECONDS)

    # Reconstruct with correct expires_at (frozen dataclass)
    binding = ApprovalBinding(
        suite_id=binding.suite_id,
        office_id=binding.office_id,
        request_id=binding.request_id,
        payload_hash=binding.payload_hash,
        policy_version=binding.policy_version,
        approved_at=binding.approved_at,
        expires_at=expires_at,
        approver_id=binding.approver_id,
    )

    # Verify the approval binding (7-check defense)
    binding_result = verify_approval_binding(
        binding,
        expected_suite_id=suite_id,
        expected_office_id=office_id,
        expected_request_id=request_id,
        expected_payload_hash=payload_hash,
    )

    if not binding_result.valid:
        # Approval binding verification FAILED — deny (Law #3: fail closed)
        error_code = _map_binding_error_to_error_code(binding_result.error)
        logger.warning(
            "Approval binding REJECTED: error=%s, msg=%s, correlation=%s",
            binding_result.error.value if binding_result.error else "unknown",
            binding_result.error_message,
            correlation_id[:8],
        )

        receipt = _make_receipt(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            actor_type="user",
            actor_id=approver_id,
            action_type="approval.deny",
            risk_tier=risk_tier_value,
            outcome=Outcome.DENIED.value,
            reason_code=error_code.value,
            receipt_type=ReceiptType.APPROVAL_DENIED.value,
            details={
                "binding_error": binding_result.error.value if binding_result.error else "unknown",
                "binding_message": binding_result.error_message or "",
            },
        )
        existing_receipts = list(state.get("pipeline_receipts", []))
        existing_receipts.append(receipt)

        return {
            "approval_status": "rejected",
            "approval_payload_hash": payload_hash,
            "error_code": error_code.value,
            "error_message": binding_result.error_message or "Approval binding verification failed",
            "outcome": Outcome.DENIED,
            "pipeline_receipts": existing_receipts,
        }

    # --- Approval binding verified ---

    # RED tier: also verify presence token
    if risk_tier == RiskTier.RED:
        presence_token = state.get("presence_token")

        if presence_token is None:
            # No presence token — deny (Law #3: fail closed for RED tier)
            logger.warning(
                "Presence token MISSING for RED-tier action: correlation=%s, suite=%s",
                correlation_id[:8], suite_id[:8],
            )
            receipt = _make_receipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                actor_type="system",
                actor_id="orchestrator",
                action_type="presence.check",
                risk_tier="red",
                outcome=Outcome.DENIED.value,
                reason_code=AspireErrorCode.PRESENCE_REQUIRED.value,
                receipt_type=ReceiptType.PRESENCE_MISSING.value,
            )
            existing_receipts = list(state.get("pipeline_receipts", []))
            existing_receipts.append(receipt)

            return {
                "approval_status": "rejected",
                "approval_payload_hash": payload_hash,
                "presence_required": True,
                "error_code": AspireErrorCode.PRESENCE_REQUIRED.value,
                "error_message": "Red-tier action requires presence verification",
                "outcome": Outcome.DENIED,
                "pipeline_receipts": existing_receipts,
            }

        # Verify presence token (6-check)
        presence_result = verify_presence_token(
            presence_token,
            expected_suite_id=suite_id,
            expected_office_id=office_id,
            expected_payload_hash=payload_hash,
        )

        if not presence_result.valid:
            error_code = _map_presence_error_to_error_code(presence_result.error)
            logger.warning(
                "Presence token REJECTED: error=%s, msg=%s, correlation=%s",
                presence_result.error.value if presence_result.error else "unknown",
                presence_result.error_message,
                correlation_id[:8],
            )

            receipt = _make_receipt(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                actor_type="system",
                actor_id="orchestrator",
                action_type="presence.check",
                risk_tier="red",
                outcome=Outcome.DENIED.value,
                reason_code=error_code.value,
                receipt_type=ReceiptType.PRESENCE_MISSING.value,
                details={
                    "presence_error": presence_result.error.value if presence_result.error else "unknown",
                    "presence_message": presence_result.error_message or "",
                },
            )
            existing_receipts = list(state.get("pipeline_receipts", []))
            existing_receipts.append(receipt)

            return {
                "approval_status": "rejected",
                "approval_payload_hash": payload_hash,
                "error_code": error_code.value,
                "error_message": presence_result.error_message or "Presence verification failed",
                "outcome": Outcome.DENIED,
                "pipeline_receipts": existing_receipts,
            }

        # Presence verified
        logger.info(
            "Presence token VERIFIED for RED-tier action: correlation=%s, suite=%s",
            correlation_id[:8], suite_id[:8],
        )

    # --- Approval GRANTED ---
    logger.info(
        "Approval GRANTED: tier=%s, correlation=%s, suite=%s, approver=%s",
        risk_tier_value, correlation_id[:8], suite_id[:8], approver_id[:8],
    )

    receipt = _make_receipt(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        actor_type="user",
        actor_id=approver_id,
        action_type="approval.grant",
        risk_tier=risk_tier_value,
        outcome=Outcome.SUCCESS.value,
        reason_code="APPROVED",
        receipt_type=ReceiptType.APPROVAL_GRANTED.value,
        details={
            "payload_hash": payload_hash,
            "policy_version": binding.policy_version,
        },
    )
    existing_receipts = list(state.get("pipeline_receipts", []))
    existing_receipts.append(receipt)

    return {
        "approval_status": "approved",
        "approval_payload_hash": payload_hash,
        "approval_evidence": approval_evidence,
        "pipeline_receipts": existing_receipts,
    }
