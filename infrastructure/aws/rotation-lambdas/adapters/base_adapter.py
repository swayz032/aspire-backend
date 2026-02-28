"""Base vendor adapter interface for secret rotation.

Every provider adapter implements three operations:
  - create_key() — Create a new API key via vendor API
  - test_key()   — Verify the new key works (synthetic API call)
  - revoke_key() — Revoke/delete the old key

Adapters NEVER read from or write to Secrets Manager directly.
The rotation handler (Step Functions Lambda) manages SM operations.
Adapters only interact with vendor APIs.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class KeyResult:
    """Result of a key operation."""
    success: bool
    key_id: str = ""
    key_value: str = ""  # The actual secret value — NEVER log this
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_code: str = ""


@dataclass
class TestResult:
    """Result of a key test."""
    success: bool
    latency_ms: float = 0.0
    test_name: str = ""
    error: str = ""
    retryable: bool = False


@dataclass
class RevokeResult:
    """Result of a key revocation."""
    success: bool
    revoked_key_id: str = ""
    error: str = ""
    # Some vendors have delayed revocation — key may still work briefly
    revocation_immediate: bool = True


class VendorAdapter(ABC):
    """Abstract base for vendor-specific key rotation adapters."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Vendor identifier (e.g., 'stripe', 'twilio')."""
        ...

    @property
    @abstractmethod
    def supports_dual_key(self) -> bool:
        """Whether the vendor allows two active keys simultaneously."""
        ...

    @property
    def risk_tier(self) -> str:
        """Risk tier for this rotation (green/yellow/red)."""
        return "yellow"

    @abstractmethod
    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        """Create a new API key via the vendor's API.

        Args:
            current_secret: The current secret dict from SM (contains existing key + metadata).

        Returns:
            KeyResult with the new key value and vendor-assigned key ID.
        """
        ...

    @abstractmethod
    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        """Test that a newly created key works.

        This MUST be a read-only, non-destructive operation.
        Examples: Stripe balance.retrieve, Twilio accounts.list, OpenAI models.list.

        Args:
            new_key_data: Dict containing the new key value(s) to test.

        Returns:
            TestResult indicating pass/fail.
        """
        ...

    @abstractmethod
    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        """Revoke/delete the old key via vendor API.

        Called AFTER the new key is promoted to AWSCURRENT and overlap window has passed.

        Args:
            old_key_id: Vendor-assigned ID of the old key to revoke.
            current_secret: Current secret dict (may contain auth token needed for revocation).

        Returns:
            RevokeResult indicating success/failure.
        """
        ...

    def generate_correlation_id(self) -> str:
        """Generate a correlation ID for this rotation run."""
        return f"rotation-{self.provider_name}-{uuid.uuid4().hex[:12]}"

    def build_receipt_data(
        self,
        correlation_id: str,
        outcome: str,
        old_key_id: str = "",
        new_key_id: str = "",
        error: str = "",
        test_latency_ms: float = 0.0,
    ) -> dict[str, Any]:
        """Build an immutable receipt for this rotation event (Law #2)."""
        return {
            "receipt_id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            # Tenant fields — system sentinel for infrastructure operations
            "suite_id": "ffffffff-0000-0000-0000-system000000",
            "office_id": "ffffffff-0000-0000-0000-system000000",
            "tenant_id": "system",
            "actor_type": "SYSTEM",
            "action_type": f"secret.rotation.{self.provider_name}",
            "risk_tier": self.risk_tier,
            "actor": "system/rotation-lambda",
            "tool_used": f"aws.secretsmanager.rotation.{self.provider_name}",
            "outcome": outcome,
            "provider": self.provider_name,
            "old_key_id": old_key_id,
            "new_key_id": new_key_id,
            "supports_dual_key": self.supports_dual_key,
            "error": error if error else None,
            "test_latency_ms": test_latency_ms,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "timestamps": {
                "created": datetime.now(timezone.utc).isoformat(),
            },
        }
