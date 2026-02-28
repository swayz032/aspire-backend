"""Internal keys adapter for secret rotation.

Handles rotation of internally-generated secrets:
  - TOKEN_SIGNING_SECRET (JWT/capability token signing)
  - TOKEN_ENCRYPTION_KEY (AES-256-GCM for IMAP credentials)
  - N8N_HMAC_SECRET (+ per-agent HMAC secrets)
  - DOMAIN_RAIL_HMAC_SECRET
  - GATEWAY_INTERNAL_KEY

These don't require vendor API calls — we generate new random keys,
test that services can use them, and drop the old ones after a grace period.

Risk tier: RED (signing/encryption keys affect auth and data integrity).
Dual-key overlap: 5 minutes (services use cache TTL to pick up new keys).
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any

from .base_adapter import VendorAdapter, KeyResult, TestResult, RevokeResult

logger = logging.getLogger(__name__)


class InternalAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "internal"

    @property
    def supports_dual_key(self) -> bool:
        # We implement dual-key in application code (accept both old + new
        # during the overlap window)
        return True

    @property
    def risk_tier(self) -> str:
        return "red"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        """Generate new random keys for all internal secrets.

        Each key is 256-bit (32 bytes) hex-encoded.
        """
        try:
            new_keys = {}
            for key_name in [
                "token_signing_secret",
                "token_encryption_key",
                "n8n_hmac_secret",
                "n8n_eli_webhook_secret",
                "n8n_sarah_webhook_secret",
                "n8n_nora_webhook_secret",
                "domain_rail_hmac_secret",
                "gateway_internal_key",
            ]:
                new_keys[key_name] = secrets.token_hex(32)

            return KeyResult(
                success=True,
                key_id=f"internal-{int(time.time())}",
                key_value="[multiple-keys]",  # Actual values in metadata
                metadata={"new_keys": new_keys},
            )

        except Exception as e:
            logger.error("Internal create_key failed: %s", e)
            return KeyResult(success=False, error=str(e), error_code="INTERNAL_CREATE_EXCEPTION")

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        """Test internal keys by verifying they meet entropy requirements.

        We can't do a live service test from Lambda (services are on Railway),
        so we validate the keys themselves are properly formed.
        Real cutover verification happens in the CutoverVerification step
        by hitting the health endpoints.
        """
        try:
            # Handle both formats:
            # 1. Direct from create_key: {"metadata": {"new_keys": {...}}}
            # 2. From SM AWSPENDING (flattened): {"token_signing_secret": "...", ...}
            new_keys = new_key_data.get("metadata", {}).get("new_keys", {})
            if not new_keys:
                # Try flattened format — keys are at top level in the SM secret
                internal_key_names = {
                    "token_signing_secret", "token_encryption_key",
                    "n8n_hmac_secret", "n8n_eli_webhook_secret",
                    "n8n_sarah_webhook_secret", "n8n_nora_webhook_secret",
                    "domain_rail_hmac_secret", "gateway_internal_key",
                }
                new_keys = {
                    k: v for k, v in new_key_data.items()
                    if k in internal_key_names and v and not k.startswith("_")
                }

            if not new_keys:
                return TestResult(
                    success=False,
                    test_name="internal.entropy_check",
                    error="No keys found in metadata",
                    retryable=False,
                )

            for key_name, key_value in new_keys.items():
                # Verify 256-bit hex (64 chars)
                if len(key_value) != 64:
                    return TestResult(
                        success=False,
                        test_name=f"internal.entropy_check.{key_name}",
                        error=f"Key {key_name} wrong length: {len(key_value)} (expected 64)",
                        retryable=False,
                    )
                # Verify valid hex
                try:
                    bytes.fromhex(key_value)
                except ValueError:
                    return TestResult(
                        success=False,
                        test_name=f"internal.entropy_check.{key_name}",
                        error=f"Key {key_name} is not valid hex",
                        retryable=False,
                    )

            return TestResult(
                success=True,
                test_name="internal.entropy_check",
                latency_ms=0.0,
            )

        except Exception as e:
            return TestResult(
                success=False,
                test_name="internal.entropy_check",
                error=str(e),
                retryable=False,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        """Internal keys don't need vendor-side revocation.

        The old key simply stops working once services reload with the new one.
        The SM version stage change (AWSCURRENT → new version) is the revocation.
        """
        return RevokeResult(
            success=True,
            revoked_key_id=old_key_id or "n/a",
            revocation_immediate=False,  # Depends on service cache TTL
        )
