"""Incident Writer — Automated incident creation from failed receipts.

When a receipt is written with status FAILED, BLOCKED, or DENIED, this module
derives an incident and upserts it into the Supabase `incidents` table via the
existing admin_store.upsert_incident() pattern.

Fingerprint-based dedup ensures the same class of failure doesn't spam incidents:
if an open incident with the same fingerprint already exists, the existing row is
updated (failure count bumped, description refreshed) instead of inserting a duplicate.

Design:
- Called as a post-write hook from receipt_store.store_receipts()
- Runs in a background thread to avoid blocking the receipt pipeline
- All errors are caught and logged — incident writer failure NEVER blocks receipt writing
- Uses admin_store.upsert_incident() for actual DB operations (single code path)

Law #2: Incidents are append-only (new or update-in-place via fingerprint dedup).
Law #3: Fail-closed — if severity cannot be determined, defaults to 'medium'.
Law #6: tenant_id is propagated from the receipt.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity derivation rules
# ---------------------------------------------------------------------------

# receipt_type + action_type prefix -> severity
# Order matters: first match wins. More specific rules first.
_SEVERITY_RULES: list[tuple[str, str, str]] = [
    # Critical: Stripe token expiry, payment failures
    ("stripe", "expired", "critical"),
    ("stripe", "payment.fail", "critical"),
    ("stripe", "charge.fail", "critical"),
    # High: Orchestrator-level failures, Stripe general failures
    ("orchestrator", "", "high"),
    ("stripe", "", "high"),
    ("approval", "", "high"),
    ("governance", "", "high"),
    # Medium: Tool execution, parameter extraction, mail onboarding
    ("mail", "onboarding", "medium"),
    ("tool_execution", "", "medium"),
    ("param_extraction", "", "medium"),
    ("n8n_ops", "", "medium"),
    ("n8n_agent", "", "medium"),
    ("agent", "", "medium"),
    # Low: Domain lookups, informational
    ("domain", "", "low"),
    ("search", "", "low"),
    ("calendar", "", "low"),
]

# Statuses that trigger incident creation
_INCIDENT_STATUSES = {"FAILED", "BLOCKED", "DENIED", "failed", "blocked", "denied"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def maybe_create_incident(receipts: list[dict[str, Any]]) -> None:
    """Inspect receipts and create incidents for failed/blocked/denied ones.

    Runs synchronously but is designed to be called from a background thread.
    All exceptions are caught internally — caller is never impacted.
    """
    for receipt in receipts:
        try:
            _process_single_receipt(receipt)
        except Exception as e:
            logger.error(
                "Incident writer: failed to process receipt %s: %s",
                receipt.get("id", "?"),
                e,
                exc_info=True,
            )


def maybe_create_incident_async(receipts: list[dict[str, Any]]) -> None:
    """Fire-and-forget incident creation in a daemon thread.

    This is the main entry point called from receipt_store.store_receipts().
    It spawns a daemon thread so the receipt pipeline is never blocked.
    """
    failed = [r for r in receipts if _is_incident_worthy(r)]
    if not failed:
        return

    thread = threading.Thread(
        target=maybe_create_incident,
        args=(failed,),
        name="incident-writer",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_incident_worthy(receipt: dict[str, Any]) -> bool:
    """Return True if this receipt warrants an incident."""
    outcome = receipt.get("outcome", "")
    status = receipt.get("status", "")
    # Check both outcome (raw receipt) and status (mapped receipt)
    return str(outcome).upper() in _INCIDENT_STATUSES or str(status).upper() in _INCIDENT_STATUSES


def _derive_fingerprint(receipt: dict[str, Any]) -> str:
    """Derive a deterministic fingerprint for dedup.

    Fingerprint = sha256(receipt_type + action_type + tool_used)[:32]
    This groups the same class of failure into one incident.
    """
    receipt_type = str(receipt.get("receipt_type", "unknown"))
    action = receipt.get("action", {})
    if isinstance(action, dict):
        action_type = str(action.get("action_type", receipt.get("action_type", "")))
        tool_used = str(action.get("tool_used", receipt.get("tool_used", "")))
    else:
        action_type = str(receipt.get("action_type", ""))
        tool_used = str(receipt.get("tool_used", ""))

    raw = f"{receipt_type}:{action_type}:{tool_used}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _derive_severity(receipt: dict[str, Any]) -> str:
    """Map receipt fields to incident severity (critical/high/medium/low).

    Matches receipt_type and action_type against _SEVERITY_RULES.
    Default: 'medium' (Law #3: when in doubt, don't under-classify).
    """
    receipt_type = str(receipt.get("receipt_type", "")).lower()
    action = receipt.get("action", {})
    if isinstance(action, dict):
        action_type = str(action.get("action_type", receipt.get("action_type", ""))).lower()
    else:
        action_type = str(receipt.get("action_type", "")).lower()

    for rule_type, rule_action, severity in _SEVERITY_RULES:
        if rule_type and rule_type not in receipt_type:
            continue
        if rule_action and rule_action not in action_type:
            continue
        return severity

    return "medium"


def _derive_component(receipt: dict[str, Any]) -> str:
    """Extract the component name from the receipt."""
    action = receipt.get("action", {})
    if isinstance(action, dict):
        tool = action.get("tool_used", receipt.get("tool_used", ""))
        if tool:
            return str(tool)
    receipt_type = receipt.get("receipt_type", "")
    if receipt_type:
        return str(receipt_type)
    return "orchestrator"


def _derive_provider(receipt: dict[str, Any]) -> str | None:
    """Extract provider name if identifiable from the receipt."""
    action = receipt.get("action", {})
    if isinstance(action, dict):
        tool = str(action.get("tool_used", "")).lower()
    else:
        tool = str(receipt.get("tool_used", "")).lower()

    receipt_type = str(receipt.get("receipt_type", "")).lower()

    # Map known providers
    for provider in ("stripe", "openai", "twilio", "deepgram", "elevenlabs", "supabase", "pandadoc"):
        if provider in tool or provider in receipt_type:
            return provider
    return None


def _derive_title(receipt: dict[str, Any], severity: str) -> str:
    """Generate a human-readable incident title."""
    outcome = str(receipt.get("outcome", receipt.get("status", "FAILED"))).upper()
    receipt_type = receipt.get("receipt_type", "unknown")
    action = receipt.get("action", {})
    if isinstance(action, dict):
        action_type = action.get("action_type", receipt.get("action_type", ""))
    else:
        action_type = receipt.get("action_type", "")

    parts = [f"[{severity.upper()}]", f"{outcome}:"]
    if receipt_type:
        parts.append(str(receipt_type))
    if action_type:
        parts.append(f"/ {action_type}")

    return " ".join(parts)


def _derive_description(receipt: dict[str, Any]) -> str:
    """Generate a description combining human context and machine details."""
    lines: list[str] = []

    # Error message from result
    result = receipt.get("result", {})
    if isinstance(result, dict):
        error_msg = result.get("error_message", "")
        reason = result.get("reason_code", "")
    else:
        error_msg = receipt.get("error_message", "")
        reason = receipt.get("reason_code", "")

    if error_msg:
        lines.append(f"Error: {error_msg}")
    if reason:
        lines.append(f"Reason: {reason}")

    # Receipt context
    receipt_type = receipt.get("receipt_type", "")
    if receipt_type:
        lines.append(f"Receipt type: {receipt_type}")

    correlation_id = receipt.get("correlation_id", "")
    if correlation_id:
        lines.append(f"Correlation ID: {correlation_id}")

    suite_id = receipt.get("suite_id", "")
    if suite_id:
        lines.append(f"Suite ID: {suite_id}")

    if not lines:
        lines.append("Receipt failed with no additional context.")

    return "\n".join(lines)


def _process_single_receipt(receipt: dict[str, Any]) -> None:
    """Derive incident fields from a single receipt and upsert."""
    from aspire_orchestrator.services.admin_store import get_admin_store

    fingerprint = _derive_fingerprint(receipt)
    severity = _derive_severity(receipt)
    title = _derive_title(receipt, severity)
    description = _derive_description(receipt)
    component = _derive_component(receipt)
    provider = _derive_provider(receipt)
    tenant_id = str(receipt.get("tenant_id", receipt.get("suite_id", "system")))
    correlation_id = receipt.get("correlation_id", "")

    # Build metadata with receipt details for traceability
    meta: dict[str, Any] = {
        "source_receipt_id": receipt.get("id", ""),
        "receipt_type": receipt.get("receipt_type", ""),
        "outcome": receipt.get("outcome", receipt.get("status", "")),
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    action = receipt.get("action", {})
    if isinstance(action, dict):
        if action.get("action_type"):
            meta["action_type"] = action["action_type"]
        if action.get("tool_used"):
            meta["tool_used"] = action["tool_used"]
        if action.get("risk_tier"):
            meta["risk_tier"] = action["risk_tier"]
    else:
        if receipt.get("action_type"):
            meta["action_type"] = receipt["action_type"]
        if receipt.get("tool_used"):
            meta["tool_used"] = receipt["tool_used"]
        if receipt.get("risk_tier"):
            meta["risk_tier"] = receipt["risk_tier"]

    store = get_admin_store()
    incident, deduped, sb_ok = store.upsert_incident(
        tenant_id=tenant_id,
        title=title,
        severity=severity,
        source="backend",
        description=description,
        component=component,
        provider=provider,
        fingerprint=fingerprint,
        correlation_id=correlation_id,
        metadata=meta,
    )

    action_str = "deduped" if deduped else "created"
    backend = "supabase" if sb_ok else "in-memory"
    logger.info(
        "Incident writer: %s incident %s (severity=%s, fingerprint=%s, backend=%s)",
        action_str,
        incident.get("incident_id", "?"),
        severity,
        fingerprint[:12],
        backend,
    )
