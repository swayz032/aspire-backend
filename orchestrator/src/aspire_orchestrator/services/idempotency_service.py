"""Idempotency Service — Phase 3 Wave 5.

Generates and enforces idempotency keys for state-changing operations.
Pattern: (suite_id, idempotency_key) unique constraint.

A double-click on "send $10,000 payment" must not send $20,000.
Idempotency is enforced BEFORE tool execution.

Key generation: UUID v7 (time-ordered) for uniqueness.
Storage: In-memory (Phase 3), Supabase (future).

Law compliance:
  - Law #2: Idempotency violations produce receipts.
  - Law #3: Fail-closed on duplicate keys (reject re-execution).
  - Law #4: RED-tier operations MUST have idempotency keys.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# In-memory store (Phase 3 — will be replaced with Redis/Supabase)
# =============================================================================

_lock = threading.Lock()
_executed_keys: dict[str, dict[str, Any]] = {}  # (suite_id:key) -> metadata


def _make_store_key(suite_id: str, idempotency_key: str) -> str:
    """Create a composite key for the store."""
    return f"{suite_id}:{idempotency_key}"


# =============================================================================
# Idempotency Service
# =============================================================================


class IdempotencyService:
    """Idempotency key generation and enforcement.

    Thread-safe. Enforced on all state-changing ops before tool execution.
    """

    def generate_key(
        self,
        *,
        suite_id: str,
        action_type: str,
        params_hash: str | None = None,
    ) -> str:
        """Generate a unique idempotency key.

        Keys are UUID v4 (random) for uniqueness guarantee.
        The params_hash is optional context for deduplication.

        Args:
            suite_id: Tenant identifier
            action_type: The action being performed
            params_hash: Optional hash of input parameters

        Returns:
            A unique idempotency key string.
        """
        key = str(uuid.uuid4())
        logger.debug(
            "Generated idempotency key: suite=%s action=%s key=%s",
            suite_id[:8], action_type, key[:8],
        )
        return key

    def check_and_reserve(
        self,
        *,
        suite_id: str,
        idempotency_key: str,
        action_type: str,
    ) -> IdempotencyResult:
        """Check if a key has been used and reserve it if not.

        This is an atomic check-and-set operation.

        Args:
            suite_id: Tenant identifier
            idempotency_key: The idempotency key to check
            action_type: The action being performed

        Returns:
            IdempotencyResult indicating if the operation should proceed.
        """
        store_key = _make_store_key(suite_id, idempotency_key)

        with _lock:
            if store_key in _executed_keys:
                existing = _executed_keys[store_key]
                logger.warning(
                    "Idempotency key already used: suite=%s key=%s action=%s (original: %s at %s)",
                    suite_id[:8], idempotency_key[:8], action_type,
                    existing.get("action_type"), existing.get("reserved_at"),
                )
                return IdempotencyResult(
                    should_execute=False,
                    already_executed=True,
                    original_receipt_id=existing.get("receipt_id"),
                    original_action=existing.get("action_type"),
                )

            # Reserve the key
            _executed_keys[store_key] = {
                "suite_id": suite_id,
                "idempotency_key": idempotency_key,
                "action_type": action_type,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
                "status": "reserved",
                "receipt_id": None,
            }

        return IdempotencyResult(
            should_execute=True,
            already_executed=False,
        )

    def mark_completed(
        self,
        *,
        suite_id: str,
        idempotency_key: str,
        receipt_id: str,
    ) -> None:
        """Mark an idempotency key as completed with its receipt ID."""
        store_key = _make_store_key(suite_id, idempotency_key)

        with _lock:
            if store_key in _executed_keys:
                _executed_keys[store_key]["status"] = "completed"
                _executed_keys[store_key]["receipt_id"] = receipt_id
                _executed_keys[store_key]["completed_at"] = datetime.now(timezone.utc).isoformat()

    def mark_failed(
        self,
        *,
        suite_id: str,
        idempotency_key: str,
        error: str,
    ) -> None:
        """Mark an idempotency key as failed (allows retry with same key)."""
        store_key = _make_store_key(suite_id, idempotency_key)

        with _lock:
            if store_key in _executed_keys:
                # Remove the reservation so the operation can be retried
                del _executed_keys[store_key]
                logger.info(
                    "Idempotency key released after failure: suite=%s key=%s error=%s",
                    suite_id[:8], idempotency_key[:8], error[:100],
                )

    def clear_store(self) -> None:
        """Clear all stored keys (for testing only)."""
        with _lock:
            _executed_keys.clear()


class IdempotencyResult:
    """Result of an idempotency check."""

    def __init__(
        self,
        *,
        should_execute: bool,
        already_executed: bool,
        original_receipt_id: str | None = None,
        original_action: str | None = None,
    ):
        self.should_execute = should_execute
        self.already_executed = already_executed
        self.original_receipt_id = original_receipt_id
        self.original_action = original_action


# =============================================================================
# Module-level singleton
# =============================================================================

_service: IdempotencyService | None = None


def get_idempotency_service(*, reload: bool = False) -> IdempotencyService:
    """Get the cached IdempotencyService singleton."""
    global _service
    if _service is None or reload:
        _service = IdempotencyService()
    return _service
