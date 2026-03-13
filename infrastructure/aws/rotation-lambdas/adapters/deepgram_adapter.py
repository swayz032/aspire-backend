"""Deepgram vendor adapter for secret rotation.

Deepgram Management API supports creating and deleting project API keys.
Automation requires the active key to have either administrator role or
`keys:read` + `keys:write` scopes.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .base_adapter import KeyResult, RevokeResult, TestResult, VendorAdapter

logger = logging.getLogger(__name__)

DEEPGRAM_API = "https://api.deepgram.com/v1"


class DeepgramAdapter(VendorAdapter):

    @property
    def provider_name(self) -> str:
        return "deepgram"

    @property
    def supports_dual_key(self) -> bool:
        return True

    @property
    def risk_tier(self) -> str:
        return "yellow"

    def create_key(self, current_secret: dict[str, Any]) -> KeyResult:
        api_key = str(current_secret.get("deepgram_key") or "").strip()
        if not api_key:
            return KeyResult(
                success=False,
                error="Deepgram key missing from current secret",
                error_code="DEEPGRAM_KEY_MISSING",
            )

        try:
            auth_info = self._auth_details(api_key)
            scopes = list(auth_info.get("scopes") or [])
            if "admin" not in scopes and not {"keys:read", "keys:write"}.issubset(set(scopes)):
                return KeyResult(
                    success=False,
                    error="Deepgram key lacks keys:read and keys:write permissions",
                    error_code="DEEPGRAM_KEYS_SCOPE_REQUIRED",
                )

            project_id = self._resolve_project_id(api_key, current_secret)
            if not project_id:
                return KeyResult(
                    success=False,
                    error="Deepgram project_id unavailable",
                    error_code="DEEPGRAM_PROJECT_ID_MISSING",
                )

            expiration = (datetime.now(timezone.utc) + timedelta(days=90)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            response = requests.post(
                f"{DEEPGRAM_API}/projects/{project_id}/keys",
                headers=self._headers(api_key),
                json={
                    "comment": f"aspire-auto-rotated-{int(time.time())}",
                    "scopes": scopes,
                    "tags": ["aspire", "rotation"],
                    "expiration_date": expiration,
                },
                timeout=30,
            )

            if response.status_code != 200:
                return KeyResult(
                    success=False,
                    error=f"Deepgram API error: HTTP {response.status_code}",
                    error_code="DEEPGRAM_CREATE_FAILED",
                )

            payload = response.json()
            return KeyResult(
                success=True,
                key_id=str(payload.get("api_key_id") or ""),
                key_value=str(payload.get("key") or ""),
                metadata={
                    "project_id": project_id,
                    "current_accessor": str(auth_info.get("accessor") or ""),
                    "scopes": scopes,
                },
            )
        except Exception as exc:
            logger.error("Deepgram create_key failed: %s", exc)
            return KeyResult(
                success=False,
                error=str(exc),
                error_code="DEEPGRAM_CREATE_EXCEPTION",
            )

    def test_key(self, new_key_data: dict[str, Any]) -> TestResult:
        api_key = str(new_key_data.get("deepgram_key") or new_key_data.get("key_value") or "").strip()
        if not api_key:
            return TestResult(
                success=False,
                test_name="deepgram.auth.token",
                error="Deepgram key missing",
                retryable=False,
            )

        try:
            start = time.monotonic()
            response = requests.get(
                f"{DEEPGRAM_API}/auth/token",
                headers=self._headers(api_key),
                timeout=15,
            )
            latency_ms = (time.monotonic() - start) * 1000

            if response.status_code == 200:
                return TestResult(
                    success=True,
                    latency_ms=latency_ms,
                    test_name="deepgram.auth.token",
                )
            if response.status_code == 401:
                return TestResult(
                    success=False,
                    latency_ms=latency_ms,
                    test_name="deepgram.auth.token",
                    error="Authentication failed",
                    retryable=False,
                )
            return TestResult(
                success=False,
                latency_ms=latency_ms,
                test_name="deepgram.auth.token",
                error=f"HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        except Exception as exc:
            return TestResult(
                success=False,
                test_name="deepgram.auth.token",
                error=str(exc),
                retryable=True,
            )

    def revoke_key(self, old_key_id: str, current_secret: dict[str, Any]) -> RevokeResult:
        api_key = str(current_secret.get("deepgram_key") or "").strip()
        if not api_key:
            return RevokeResult(success=False, revoked_key_id=old_key_id, error="Deepgram key missing")
        if not old_key_id:
            return RevokeResult(success=False, revoked_key_id="", error="Old Deepgram key id unavailable")

        project_id = self._resolve_project_id(api_key, current_secret)
        if not project_id:
            return RevokeResult(success=False, revoked_key_id=old_key_id, error="Deepgram project_id unavailable")

        try:
            response = requests.delete(
                f"{DEEPGRAM_API}/projects/{project_id}/keys/{old_key_id}",
                headers=self._headers(api_key),
                timeout=15,
            )
            if response.status_code in (200, 204, 404):
                return RevokeResult(success=True, revoked_key_id=old_key_id)
            return RevokeResult(
                success=False,
                revoked_key_id=old_key_id,
                error=f"Deepgram revoke failed: HTTP {response.status_code}",
            )
        except Exception as exc:
            logger.error("Deepgram revoke_key failed: %s", exc)
            return RevokeResult(success=False, revoked_key_id=old_key_id, error=str(exc))

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _auth_details(self, api_key: str) -> dict[str, Any]:
        response = requests.get(
            f"{DEEPGRAM_API}/auth/token",
            headers=self._headers(api_key),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Deepgram auth response")
        return payload

    def _resolve_project_id(self, api_key: str, current_secret: dict[str, Any]) -> str:
        explicit = str(current_secret.get("deepgram_project_id") or current_secret.get("_deepgram_project_id") or "").strip()
        if explicit:
            return explicit

        response = requests.get(
            f"{DEEPGRAM_API}/projects",
            headers=self._headers(api_key),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if isinstance(projects, list) and len(projects) == 1:
            return str(projects[0].get("project_id") or "").strip()
        return ""
