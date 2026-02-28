"""Twilio vendor adapter for secret rotation.

Twilio REST API: POST /Keys.json (create) + DELETE /Keys/{Sid}.json (revoke).
Supports dual-key (multiple API keys active simultaneously).
90-day rotation cycle. Revocation is IMMEDIATE.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .base_adapter import VendorAdapter, KeyResult, TestResult, RevokeResult

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "twilio"

    @property
    def supports_dual_key(self) -> bool:
        return True

    @property
    def risk_tier(self) -> str:
        return "yellow"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        """Create a new Twilio API key."""
        try:
            account_sid = current_secret["account_sid"]
            auth_token = current_secret["auth_token"]

            resp = requests.post(
                f"{TWILIO_API_BASE}/Accounts/{account_sid}/Keys.json",
                auth=(account_sid, auth_token),
                data={"FriendlyName": f"aspire-auto-rotated-{int(time.time())}"},
                timeout=30,
            )

            if resp.status_code != 201:
                return KeyResult(
                    success=False,
                    error=f"Twilio API error: {resp.status_code}",
                    error_code="TWILIO_CREATE_FAILED",
                )

            key_data = resp.json()
            return KeyResult(
                success=True,
                key_id=key_data["sid"],
                key_value=key_data["secret"],
                metadata={"friendly_name": key_data.get("friendly_name", "")},
            )

        except Exception as e:
            logger.error("Twilio create_key failed: %s", e)
            return KeyResult(success=False, error=str(e), error_code="TWILIO_CREATE_EXCEPTION")

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        """Test new Twilio key with a read-only accounts list call."""
        try:
            api_key = new_key_data.get("api_key", new_key_data.get("key_id", ""))
            api_secret = new_key_data.get("api_secret", new_key_data.get("key_value", ""))

            start = time.monotonic()
            resp = requests.get(
                f"{TWILIO_API_BASE}/Accounts.json",
                auth=(api_key, api_secret),
                timeout=15,
            )
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return TestResult(success=True, latency_ms=latency, test_name="twilio.Accounts.list")
            elif resp.status_code == 401:
                return TestResult(
                    success=False, test_name="twilio.Accounts.list",
                    error="Authentication failed", retryable=False,
                )
            else:
                return TestResult(
                    success=False, test_name="twilio.Accounts.list",
                    error=f"HTTP {resp.status_code}", retryable=True,
                )

        except Exception as e:
            return TestResult(
                success=False, test_name="twilio.Accounts.list",
                error=str(e), retryable=True,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        """Revoke old Twilio API key. Revocation is IMMEDIATE."""
        if not old_key_id:
            return RevokeResult(success=True, revoked_key_id="")

        try:
            account_sid = current_secret["account_sid"]
            auth_token = current_secret["auth_token"]

            resp = requests.delete(
                f"{TWILIO_API_BASE}/Accounts/{account_sid}/Keys/{old_key_id}.json",
                auth=(account_sid, auth_token),
                timeout=15,
            )

            if resp.status_code in (204, 404):
                return RevokeResult(success=True, revoked_key_id=old_key_id, revocation_immediate=True)
            else:
                return RevokeResult(
                    success=False, revoked_key_id=old_key_id,
                    error=f"Twilio revoke failed: {resp.status_code}",
                )

        except Exception as e:
            logger.error("Twilio revoke_key failed: %s", e)
            return RevokeResult(success=False, revoked_key_id=old_key_id, error=str(e))
