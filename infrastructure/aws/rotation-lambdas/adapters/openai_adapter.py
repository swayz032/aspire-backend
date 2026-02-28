"""OpenAI vendor adapter for secret rotation.

OpenAI API: POST /organization/api_keys (create) + DELETE /organization/api_keys/{id} (revoke).
Supports dual-key. 90-day rotation cycle.
Read-only test via GET /v1/models.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .base_adapter import VendorAdapter, KeyResult, TestResult, RevokeResult

logger = logging.getLogger(__name__)


class OpenAIAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def supports_dual_key(self) -> bool:
        return True

    @property
    def risk_tier(self) -> str:
        return "yellow"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        """Create a new OpenAI API key.

        Note: OpenAI's admin API for key creation requires organization-level
        permissions. If unavailable, this adapter returns a marker indicating
        manual creation is needed (alarm + import-key.sh workflow).
        """
        try:
            # OpenAI admin key management API
            admin_key = current_secret.get("admin_key", current_secret.get("api_key", ""))

            resp = requests.post(
                "https://api.openai.com/v1/organization/api_keys",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "name": f"aspire-auto-rotated-{int(time.time())}",
                    "scopes": ["model.read", "model.request"],
                },
                timeout=30,
            )

            if resp.status_code == 200:
                key_data = resp.json()
                return KeyResult(
                    success=True,
                    key_id=key_data.get("id", ""),
                    key_value=key_data.get("key", key_data.get("secret", "")),
                    metadata={"name": key_data.get("name", "")},
                )
            elif resp.status_code in (403, 404):
                # Admin API not available — fall back to manual rotation
                return KeyResult(
                    success=False,
                    error="OpenAI admin key API not available — manual rotation required",
                    error_code="OPENAI_ADMIN_API_UNAVAILABLE",
                )
            else:
                return KeyResult(
                    success=False,
                    error=f"OpenAI API error: {resp.status_code}",
                    error_code="OPENAI_CREATE_FAILED",
                )

        except Exception as e:
            logger.error("OpenAI create_key failed: %s", e)
            return KeyResult(success=False, error=str(e), error_code="OPENAI_CREATE_EXCEPTION")

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        """Test new OpenAI key with a read-only models.list call."""
        try:
            api_key = new_key_data.get("api_key", new_key_data.get("key_value", ""))

            start = time.monotonic()
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return TestResult(success=True, latency_ms=latency, test_name="openai.models.list")
            elif resp.status_code == 401:
                return TestResult(
                    success=False, test_name="openai.models.list",
                    error="Authentication failed", retryable=False,
                )
            else:
                return TestResult(
                    success=False, test_name="openai.models.list",
                    error=f"HTTP {resp.status_code}", retryable=True,
                )

        except Exception as e:
            return TestResult(
                success=False, test_name="openai.models.list",
                error=str(e), retryable=True,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        """Revoke old OpenAI API key."""
        if not old_key_id:
            return RevokeResult(success=True, revoked_key_id="")

        try:
            admin_key = current_secret.get("admin_key", current_secret.get("api_key", ""))
            resp = requests.delete(
                f"https://api.openai.com/v1/organization/api_keys/{old_key_id}",
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=15,
            )

            if resp.status_code in (200, 204, 404):
                return RevokeResult(success=True, revoked_key_id=old_key_id)
            else:
                return RevokeResult(
                    success=False, revoked_key_id=old_key_id,
                    error=f"OpenAI revoke failed: {resp.status_code}",
                )

        except Exception as e:
            logger.error("OpenAI revoke_key failed: %s", e)
            return RevokeResult(success=False, revoked_key_id=old_key_id, error=str(e))
