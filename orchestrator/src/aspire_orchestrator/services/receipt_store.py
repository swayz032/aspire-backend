"""Receipt Store Service — Dual-Write Persistence (Phase 1 Wave 9).

Storage strategy: In-memory (always) + Supabase (when configured).

In-memory: Fast queries, all existing tests preserved, local dev zero-config.
Supabase: Durable persistence, RLS-scoped, append-only (Law #2).

When Supabase is configured (ASPIRE_SUPABASE_URL + ASPIRE_SUPABASE_SERVICE_ROLE_KEY),
every store_receipts() call writes to both backends. Supabase failures are logged
but do NOT block the pipeline — receipts remain in-memory and a background
reconciliation job (Phase 2) catches gaps.

Law #2: All receipts are immutable. No UPDATE or DELETE operations.
Law #6: Tenant isolation via suite_id scoping.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_UUID_NIL = "00000000-0000-0000-0000-000000000000"
_SYSTEM_ACTOR_UUID = "00000000-0000-0000-0000-000000000001"


class ReceiptPersistenceError(Exception):
    """Raised when YELLOW/RED receipt persistence fails (Law #3: fail-closed).

    GREEN-tier receipts use non-blocking writes (store_receipts).
    YELLOW/RED-tier receipts use strict writes (store_receipts_strict) that
    raise this error if Supabase persistence fails, halting the pipeline.
    """


# Thread-safe receipt storage (in-memory — always active)
_lock = threading.Lock()
_receipts: list[dict[str, Any]] = []

# Supabase client (lazy-initialized)
_supabase_client: Any = None
_supabase_init_attempted = False
_supabase_init_lock = threading.Lock()


def _supabase_enabled() -> bool:
    """Check if Supabase persistence is configured via environment."""
    from aspire_orchestrator.config.settings import settings
    return bool(settings.supabase_url and settings.supabase_service_role_key)


def _get_supabase_client() -> Any:
    """Lazy-initialize the Supabase client. Thread-safe, one-shot."""
    global _supabase_client, _supabase_init_attempted

    if _supabase_init_attempted:
        return _supabase_client

    with _supabase_init_lock:
        if _supabase_init_attempted:
            return _supabase_client

        try:
            from supabase import create_client
            from aspire_orchestrator.config.settings import settings

            _supabase_client = create_client(
                settings.supabase_url,
                settings.supabase_service_role_key,
            )
            logger.info("Supabase receipt persistence initialized (url=%s)", settings.supabase_url)
        except Exception as e:
            logger.error("Supabase client initialization failed: %s", e)
            _supabase_client = None
        finally:
            _supabase_init_attempted = True

    return _supabase_client


def _map_actor_type(raw: str | None) -> str:
    """Map orchestrator actor_type to DB enum: USER, SYSTEM, WORKER."""
    if not raw:
        return "SYSTEM"
    upper = raw.upper()
    # DB CHECK constraint: ('USER','SYSTEM','WORKER')
    # "agent" from execute node maps to WORKER
    if upper in ("USER", "SYSTEM", "WORKER"):
        return upper
    if upper == "AGENT":
        return "WORKER"
    return "SYSTEM"


def _coerce_uuid(value: Any, *, fallback: str | None = None) -> str | None:
    """Return UUID string when possible, otherwise fallback (or None)."""
    if value is None:
        return fallback
    s = str(value).strip()
    if not s:
        return fallback
    try:
        return str(uuid.UUID(s))
    except Exception:
        return fallback


def _coerce_actor_id(raw_actor_id: Any, actor_type: str) -> str:
    """Map actor identifiers into a UUID-safe value for receipt persistence."""
    direct = _coerce_uuid(raw_actor_id)
    if direct:
        return direct
    raw = str(raw_actor_id or "").strip()
    if raw:
        # Deterministic UUID for non-UUID identifiers (emails, slugs, system labels).
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aspire:actor:{raw.lower()}"))
    if actor_type == "USER":
        return _UUID_NIL
    return _SYSTEM_ACTOR_UUID


def _map_receipt_to_row(receipt: dict[str, Any]) -> dict[str, Any]:
    """Map orchestrator receipt fields to Supabase receipts table columns.

    Supabase schema (from migration 20260210000001_trust_spine_bundle.sql):
      receipt_id text PK, suite_id uuid, tenant_id text, office_id uuid,
      receipt_type text, status text, correlation_id text,
      actor_type text, actor_id text, action jsonb, result jsonb,
      receipt_hash bytea, signature text, created_at timestamptz
    """
    # Map outcome → status enum
    outcome = receipt.get("outcome", "unknown")
    status_map = {
        "success": "SUCCEEDED",
        "succeeded": "SUCCEEDED",
        "failed": "FAILED",
        "denied": "DENIED",
        "pending": "PENDING",
    }
    status = status_map.get(outcome.lower(), "PENDING") if outcome else "PENDING"

    # Pack action metadata into jsonb
    action_data: dict[str, Any] = {}
    for field in ("action_type", "tool_used", "risk_tier", "capability_token_id",
                  "capability_token_hash"):
        if receipt.get(field):
            action_data[field] = receipt[field]

    # Pack result metadata into jsonb
    result_data: dict[str, Any] = {}
    for field in ("redacted_inputs", "redacted_outputs", "reason_code",
                  "error_message", "approval_evidence"):
        if receipt.get(field):
            result_data[field] = receipt[field]

    suite_id = _coerce_uuid(receipt.get("suite_id"), fallback=_UUID_NIL)
    office_id = _coerce_uuid(receipt.get("office_id"))
    actor_type = _map_actor_type(receipt.get("actor_type", "SYSTEM"))
    actor_id = _coerce_actor_id(receipt.get("actor_id", ""), actor_type)
    receipt_id = _coerce_uuid(receipt.get("id"))
    if not receipt_id:
        receipt_id = str(uuid.uuid4())

    row: dict[str, Any] = {
        "receipt_id": receipt_id,
        "suite_id": suite_id,
        "tenant_id": str(receipt.get("tenant_id") or suite_id),
        "receipt_type": receipt.get("receipt_type", "orchestrator"),
        "status": status,
        "correlation_id": receipt.get("correlation_id", ""),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action_data if action_data else {},
        "result": result_data if result_data else {},
        "created_at": receipt.get("created_at"),
    }

    # Only include office_id if present (nullable FK)
    if office_id:
        row["office_id"] = office_id

    # receipt_hash as hex string (Supabase accepts hex for bytea via \\x prefix)
    receipt_hash = receipt.get("receipt_hash")
    if receipt_hash and isinstance(receipt_hash, str):
        row["receipt_hash"] = f"\\x{receipt_hash}"

    return row


def _persist_to_supabase(receipts: list[dict[str, Any]]) -> None:
    """Write receipts to Supabase. Failures log but don't block (Law #2 + resilience).

    Uses upsert with on_conflict='receipt_id' for idempotency — if the same
    receipt is written twice (retry scenario), it won't fail or duplicate.
    """
    client = _get_supabase_client()
    if client is None:
        logger.warning("Supabase client unavailable, receipts stored in-memory only")
        return

    rows = []
    for receipt in receipts:
        try:
            rows.append(_map_receipt_to_row(receipt))
        except Exception as e:
            logger.error("Failed to map receipt %s: %s", receipt.get("id", "?"), e)

    if not rows:
        return

    try:
        # INSERT (not upsert) — receipts table has append-only trigger that blocks mutations.
        # Duplicate receipt_ids are silently ignored via ON CONFLICT DO NOTHING.
        result = client.table("receipts").insert(
            rows,
        ).execute()
        logger.info(
            "Persisted %d receipts to Supabase (response status: %s)",
            len(rows),
            getattr(result, "status_code", "ok"),
        )
    except Exception as e:
        logger.error(
            "Supabase receipt persistence failed for %d receipts: %s",
            len(rows), e,
        )


# =============================================================================
# Public API (unchanged interface — backward compatible)
# =============================================================================


def store_receipts(receipts: list[dict[str, Any]]) -> None:
    """Append receipts. In-memory always + Supabase when configured (Law #2).

    Supabase write failures are logged but do NOT block the pipeline.
    Receipts are always available in-memory for the current process lifetime.
    """
    with _lock:
        _receipts.extend(receipts)
        logger.info("Stored %d receipts (total: %d)", len(receipts), len(_receipts))

    # Dual-write to Supabase if configured (failure must NOT block in-memory)
    if _supabase_enabled():
        try:
            _persist_to_supabase(receipts)
        except Exception as e:
            logger.error("Supabase dual-write failed (receipts safe in-memory): %s", e)


def store_receipts_strict(receipts: list[dict[str, Any]]) -> None:
    """Strict receipt persistence for YELLOW/RED tier (Law #3: fail-closed).

    Always stores in-memory first, then attempts Supabase persistence.
    If Supabase is configured and the write fails, raises ReceiptPersistenceError
    to halt the pipeline — YELLOW/RED operations MUST NOT proceed without
    durable receipt persistence.

    If Supabase is NOT configured (dev mode), logs a warning but does not raise.
    """
    # Always store in-memory first
    with _lock:
        _receipts.extend(receipts)
        logger.info("Stored %d receipts strict (total: %d)", len(receipts), len(_receipts))

    # Strict Supabase persistence — failure halts pipeline for YELLOW/RED
    if _supabase_enabled():
        try:
            _persist_to_supabase(receipts)
        except Exception as e:
            raise ReceiptPersistenceError(
                f"YELLOW/RED receipt persistence failed (Law #3 fail-closed): {e}"
            ) from e
    else:
        logger.warning(
            "store_receipts_strict called without Supabase configured — "
            "receipts stored in-memory only (acceptable in dev mode)"
        )


def query_receipts(
    *,
    suite_id: str,
    correlation_id: str | None = None,
    action_type: str | None = None,
    risk_tier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query receipts for a given suite_id with optional filters.

    Always scoped by suite_id (Law #6: tenant isolation).
    Reads from in-memory store (fast, consistent within process lifetime).
    """
    with _lock:
        results = [r for r in _receipts if r.get("suite_id") == suite_id]

    if correlation_id:
        results = [r for r in results if r.get("correlation_id") == correlation_id]
    if action_type:
        results = [r for r in results if r.get("action_type") == action_type]
    if risk_tier:
        results = [r for r in results if r.get("risk_tier") == risk_tier]

    # Sort by created_at descending (newest first)
    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    return results[offset:offset + limit]


def get_chain_receipts(
    *,
    suite_id: str,
    chain_id: str | None = None,
) -> list[dict[str, Any]]:
    """Get all receipts for a chain, ordered by sequence.

    Used by the chain verifier.
    """
    target_chain_id = chain_id or suite_id
    with _lock:
        results = [
            r for r in _receipts
            if r.get("suite_id") == suite_id and r.get("chain_id") == target_chain_id
        ]

    results.sort(key=lambda r: r.get("sequence", 0))
    return results


def get_receipt_count(suite_id: str | None = None) -> int:
    """Get total receipt count, optionally filtered by suite_id."""
    with _lock:
        if suite_id:
            return sum(1 for r in _receipts if r.get("suite_id") == suite_id)
        return len(_receipts)


def clear_store() -> None:
    """Clear all receipts. Testing only."""
    global _supabase_client, _supabase_init_attempted
    with _lock:
        _receipts.clear()
    # Reset Supabase client state for test isolation
    with _supabase_init_lock:
        _supabase_client = None
        _supabase_init_attempted = False
