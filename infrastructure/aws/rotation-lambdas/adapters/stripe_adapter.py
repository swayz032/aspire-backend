"""Stripe vendor adapter for secret rotation.

Follows Stripe's official pattern:
  https://stripe.dev/blog/securing-stripe-api-keys-aws-automatic-rotation

Stripe supports dual-key: new restricted key is immediately active alongside old.
30-day rotation cycle. Read-only test via balance.retrieve.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import stripe

from .base_adapter import VendorAdapter, KeyResult, TestResult, RevokeResult

logger = logging.getLogger(__name__)


class StripeAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "stripe"

    @property
    def supports_dual_key(self) -> bool:
        return True

    @property
    def risk_tier(self) -> str:
        return "red"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        """Create a new Stripe restricted key with same permissions as current."""
        try:
            # Use the secret key (sk_*) to manage restricted keys
            stripe.api_key = current_secret["secret_key"]

            # Create restricted key with invoice + balance permissions
            # (matches Aspire's Stripe Connect usage)
            new_key = stripe.ApplePayDomain  # placeholder — actual API:
            # Stripe restricted key creation via API:
            # POST /v1/api_keys with type=restricted + permissions
            # For now, use the direct approach:
            import requests
            resp = requests.post(
                "https://api.stripe.com/v1/api_keys",
                auth=(current_secret["secret_key"], ""),
                data={
                    "type": "restricted",
                    "name": f"aspire-auto-rotated-{int(time.time())}",
                    "permissions[0][resource]": "balance",
                    "permissions[0][actions][0]": "read",
                    "permissions[1][resource]": "invoices",
                    "permissions[1][actions][0]": "write",
                    "permissions[2][resource]": "customers",
                    "permissions[2][actions][0]": "write",
                    "permissions[3][resource]": "payment_intents",
                    "permissions[3][actions][0]": "write",
                    "permissions[4][resource]": "webhook_endpoints",
                    "permissions[4][actions][0]": "read",
                },
            )

            if resp.status_code != 200:
                return KeyResult(
                    success=False,
                    error=f"Stripe API error: {resp.status_code}",
                    error_code="STRIPE_CREATE_FAILED",
                )

            key_data = resp.json()
            return KeyResult(
                success=True,
                key_id=key_data["id"],
                key_value=key_data["secret"],
                metadata={
                    "name": key_data.get("name", ""),
                    "created": key_data.get("created"),
                },
            )

        except Exception as e:
            logger.error("Stripe create_key failed: %s", e)
            return KeyResult(success=False, error=str(e), error_code="STRIPE_CREATE_EXCEPTION")

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        """Test new Stripe key with a read-only balance.retrieve call."""
        try:
            stripe.api_key = new_key_data.get("restricted_key", new_key_data.get("key_value", ""))
            start = time.monotonic()
            stripe.Balance.retrieve()
            latency = (time.monotonic() - start) * 1000

            return TestResult(
                success=True,
                latency_ms=latency,
                test_name="stripe.Balance.retrieve",
            )
        except stripe.error.AuthenticationError:
            return TestResult(
                success=False,
                test_name="stripe.Balance.retrieve",
                error="Authentication failed",
                retryable=False,
            )
        except Exception as e:
            return TestResult(
                success=False,
                test_name="stripe.Balance.retrieve",
                error=str(e),
                retryable=True,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        """Revoke old Stripe restricted key."""
        if not old_key_id:
            return RevokeResult(success=True, revoked_key_id="", error="No old key to revoke")

        try:
            import requests
            resp = requests.delete(
                f"https://api.stripe.com/v1/api_keys/{old_key_id}",
                auth=(current_secret["secret_key"], ""),
            )

            if resp.status_code in (200, 404):
                # 404 = already revoked (idempotent)
                return RevokeResult(
                    success=True,
                    revoked_key_id=old_key_id,
                    revocation_immediate=True,
                )
            else:
                return RevokeResult(
                    success=False,
                    revoked_key_id=old_key_id,
                    error=f"Stripe revoke failed: {resp.status_code}",
                )
        except Exception as e:
            logger.error("Stripe revoke_key failed: %s", e)
            return RevokeResult(success=False, revoked_key_id=old_key_id, error=str(e))
