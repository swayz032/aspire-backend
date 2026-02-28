"""Supabase vendor adapter for secret rotation.

Supabase Management API: regenerate service role key + JWT secret.
90-day rotation cycle. Risk tier RED (service role has full DB access).

Note: Supabase service_role key regeneration requires the Management API
(api.supabase.com) with a management token. If unavailable, falls back
to manual rotation (alarm + import-key.sh).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from .base_adapter import VendorAdapter, KeyResult, TestResult, RevokeResult

logger = logging.getLogger(__name__)

SUPABASE_MGMT_API = "https://api.supabase.com"


class SupabaseAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "supabase"

    @property
    def supports_dual_key(self) -> bool:
        # Supabase does NOT support dual service role keys — regeneration
        # invalidates the old key immediately. This makes it a higher-risk
        # rotation that requires careful overlap handling.
        return False

    @property
    def risk_tier(self) -> str:
        return "red"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        """Regenerate Supabase service role key via Management API."""
        try:
            mgmt_token = current_secret.get("management_token", "")
            project_ref = current_secret.get("project_ref", "qtuehjqlcmfcascqjjhc")

            if not mgmt_token:
                return KeyResult(
                    success=False,
                    error="Supabase management token not available — manual rotation required",
                    error_code="SUPABASE_MGMT_TOKEN_MISSING",
                )

            # Regenerate API keys
            resp = requests.post(
                f"{SUPABASE_MGMT_API}/v1/projects/{project_ref}/api-keys/regenerate",
                headers={
                    "Authorization": f"Bearer {mgmt_token}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )

            if resp.status_code == 200:
                keys = resp.json()
                # Find the service_role key in the response
                service_key = next(
                    (k for k in keys if k.get("name") == "service_role"),
                    None,
                )
                if service_key:
                    return KeyResult(
                        success=True,
                        key_id=f"supabase-{int(time.time())}",
                        key_value=service_key["api_key"],
                        metadata={"project_ref": project_ref},
                    )
                else:
                    return KeyResult(
                        success=False,
                        error="service_role key not found in regeneration response",
                        error_code="SUPABASE_KEY_NOT_FOUND",
                    )
            elif resp.status_code in (401, 403):
                return KeyResult(
                    success=False,
                    error="Supabase management token unauthorized — manual rotation required",
                    error_code="SUPABASE_MGMT_UNAUTHORIZED",
                )
            else:
                return KeyResult(
                    success=False,
                    error=f"Supabase API error: {resp.status_code}",
                    error_code="SUPABASE_CREATE_FAILED",
                )

        except Exception as e:
            logger.error("Supabase create_key failed: %s", e)
            return KeyResult(success=False, error=str(e), error_code="SUPABASE_CREATE_EXCEPTION")

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        """Test new Supabase service role key with a health check."""
        try:
            key_value = new_key_data.get("service_role_key", new_key_data.get("key_value", ""))
            project_ref = new_key_data.get("metadata", {}).get("project_ref", "qtuehjqlcmfcascqjjhc")

            start = time.monotonic()
            resp = requests.get(
                f"https://{project_ref}.supabase.co/rest/v1/",
                headers={
                    "apikey": key_value,
                    "Authorization": f"Bearer {key_value}",
                },
                timeout=15,
            )
            latency = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                return TestResult(success=True, latency_ms=latency, test_name="supabase.rest.health")
            elif resp.status_code == 401:
                return TestResult(
                    success=False, test_name="supabase.rest.health",
                    error="Authentication failed", retryable=False,
                )
            else:
                return TestResult(
                    success=False, test_name="supabase.rest.health",
                    error=f"HTTP {resp.status_code}", retryable=True,
                )

        except Exception as e:
            return TestResult(
                success=False, test_name="supabase.rest.health",
                error=str(e), retryable=True,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        """Supabase key regeneration already invalidated the old key.

        No separate revocation step needed — the old service_role JWT
        is invalid as soon as the new one is generated.
        """
        return RevokeResult(
            success=True,
            revoked_key_id=old_key_id or "auto-revoked-on-regenerate",
            revocation_immediate=True,
        )
