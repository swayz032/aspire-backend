"""ElevenLabs vendor adapter for secret rotation.

Automation is available through Service Account API keys. This requires
workspace service accounts to be enabled and the active key to belong to a
service account.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import requests

from .base_adapter import KeyResult, RevokeResult, TestResult, VendorAdapter

logger = logging.getLogger(__name__)

ELEVENLABS_API = "https://api.elevenlabs.io/v1"


class ElevenLabsAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "elevenlabs"

    @property
    def supports_dual_key(self) -> bool:
        return True

    @property
    def risk_tier(self) -> str:
        return "yellow"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        api_key = str(current_secret.get("elevenlabs_key") or "").strip()
        if not api_key:
            return KeyResult(
                success=False,
                error="ElevenLabs key missing from current secret",
                error_code="ELEVENLABS_KEY_MISSING",
            )

        try:
            service_account = self._resolve_service_account(api_key, current_secret)
            if not service_account:
                return KeyResult(
                    success=False,
                    error="Workspace service accounts are required for automated ElevenLabs rotation",
                    error_code="ELEVENLABS_SERVICE_ACCOUNT_REQUIRED",
                )

            service_account_user_id = service_account["service_account_user_id"]
            permissions = service_account["permissions"]
            if not permissions:
                return KeyResult(
                    success=False,
                    error="Unable to determine ElevenLabs API key permissions",
                    error_code="ELEVENLABS_PERMISSIONS_UNKNOWN",
                )

            response = requests.post(
                f"{ELEVENLABS_API}/service-accounts/{service_account_user_id}/api-keys",
                headers=self._headers(api_key),
                json={
                    "name": f"aspire-auto-rotated-{int(time.time())}",
                    "permissions": permissions,
                },
                timeout=30,
            )

            if response.status_code != 200:
                return KeyResult(
                    success=False,
                    error=f"ElevenLabs API error: HTTP {response.status_code}",
                    error_code="ELEVENLABS_CREATE_FAILED",
                )

            payload = response.json()
            return KeyResult(
                success=True,
                key_id=str(payload.get("key_id") or ""),
                key_value=str(payload.get("xi-api-key") or ""),
                metadata={
                    "service_account_user_id": service_account_user_id,
                    "permissions": permissions,
                    "current_key_id": service_account.get("current_key_id", ""),
                },
            )
        except Exception as exc:
            logger.error("ElevenLabs create_key failed: %s", exc)
            return KeyResult(
                success=False,
                error=str(exc),
                error_code="ELEVENLABS_CREATE_EXCEPTION",
            )

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        api_key = str(new_key_data.get("elevenlabs_key") or new_key_data.get("key_value") or "").strip()
        if not api_key:
            return TestResult(
                success=False,
                test_name="elevenlabs.models.list",
                error="ElevenLabs key missing",
                retryable=False,
            )

        try:
            start = time.monotonic()
            response = requests.get(
                f"{ELEVENLABS_API}/models",
                headers=self._headers(api_key),
                timeout=15,
            )
            latency_ms = (time.monotonic() - start) * 1000

            if response.status_code == 200:
                return TestResult(success=True, latency_ms=latency_ms, test_name="elevenlabs.models.list")
            if response.status_code == 401:
                return TestResult(
                    success=False,
                    latency_ms=latency_ms,
                    test_name="elevenlabs.models.list",
                    error="Authentication failed",
                    retryable=False,
                )
            return TestResult(
                success=False,
                latency_ms=latency_ms,
                test_name="elevenlabs.models.list",
                error=f"HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        except Exception as exc:
            return TestResult(
                success=False,
                test_name="elevenlabs.models.list",
                error=str(exc),
                retryable=True,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        api_key = str(current_secret.get("elevenlabs_key") or "").strip()
        if not api_key:
            return RevokeResult(success=False, revoked_key_id=old_key_id, error="ElevenLabs key missing")
        if not old_key_id:
            return RevokeResult(success=False, revoked_key_id="", error="Old ElevenLabs key id unavailable")

        service_account_user_id = str(
            current_secret.get("_elevenlabs_service_account_user_id")
            or current_secret.get("elevenlabs_service_account_user_id")
            or ""
        ).strip()
        if not service_account_user_id:
            service_account = self._resolve_service_account(api_key, current_secret)
            service_account_user_id = service_account.get("service_account_user_id", "") if service_account else ""
        if not service_account_user_id:
            return RevokeResult(
                success=False,
                revoked_key_id=old_key_id,
                error="ElevenLabs service account unavailable",
            )

        try:
            response = requests.delete(
                f"{ELEVENLABS_API}/service-accounts/{service_account_user_id}/api-keys/{old_key_id}",
                headers=self._headers(api_key),
                timeout=15,
            )
            if response.status_code in (200, 204, 404):
                return RevokeResult(success=True, revoked_key_id=old_key_id)
            return RevokeResult(
                success=False,
                revoked_key_id=old_key_id,
                error=f"ElevenLabs revoke failed: HTTP {response.status_code}",
            )
        except Exception as exc:
            logger.error("ElevenLabs revoke_key failed: %s", exc)
            return RevokeResult(success=False, revoked_key_id=old_key_id, error=str(exc))

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _resolve_service_account(self, api_key: str, current_secret: dict[str, Any]) -> dict[str, Any] | None:
        configured_service_account = str(
            current_secret.get("elevenlabs_service_account_user_id")
            or current_secret.get("_elevenlabs_service_account_user_id")
            or ""
        ).strip()
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

        response = requests.get(
            f"{ELEVENLABS_API}/service-accounts",
            headers=self._headers(api_key),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        service_accounts = payload.get("service-accounts") if isinstance(payload, dict) else None
        if not isinstance(service_accounts, list) or not service_accounts:
            return None

        candidate_accounts = service_accounts
        if configured_service_account:
            candidate_accounts = [
                item for item in service_accounts
                if str(item.get("service_account_user_id") or "") == configured_service_account
            ]

        for account in candidate_accounts:
            service_account_user_id = str(account.get("service_account_user_id") or "").strip()
            if not service_account_user_id:
                continue
            key_listing = requests.get(
                f"{ELEVENLABS_API}/service-accounts/{service_account_user_id}/api-keys",
                headers=self._headers(api_key),
                timeout=20,
            )
            key_listing.raise_for_status()
            listing_payload = key_listing.json()
            api_keys = listing_payload.get("api-keys") if isinstance(listing_payload, dict) else None
            if not isinstance(api_keys, list):
                continue

            if len(api_keys) == 1 and not configured_service_account:
                only_key = api_keys[0]
                return {
                    "service_account_user_id": service_account_user_id,
                    "permissions": list(only_key.get("permissions") or []),
                    "current_key_id": str(only_key.get("key_id") or ""),
                }

            for item in api_keys:
                if str(item.get("hashed_xi_api_key") or "").strip() == key_hash:
                    return {
                        "service_account_user_id": service_account_user_id,
                        "permissions": list(item.get("permissions") or []),
                        "current_key_id": str(item.get("key_id") or ""),
                    }

        return None
