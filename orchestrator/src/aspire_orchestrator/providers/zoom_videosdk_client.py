"""Zoom Video SDK Provider Client — Conference sessions for Nora (Conference) skill pack.

Provider: Zoom Video SDK API (https://api.zoom.us/v2)
Auth: JWT signed with API Key + Secret — Bearer token in Authorization header
Risk tier: GREEN (session management is non-destructive)
Idempotency: N/A (read + create operations)

Tools:
  - zoom.session.create: Create a video conference session
  - zoom.session.list: List active sessions
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


class ZoomVideoSDKClient(BaseProviderClient):
    """Zoom Video SDK API client."""

    provider_id = "zoom"
    base_url = "https://api.zoom.us/v2"
    timeout_seconds = 5.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        import time as _time
        import jwt

        api_key = settings.zoom_api_key
        api_secret = settings.zoom_api_secret
        if not api_key or not api_secret:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Zoom API key/secret not configured (ASPIRE_ZOOM_API_KEY, ASPIRE_ZOOM_API_SECRET)",
                provider_id=self.provider_id,
            )
        now = _time.time()
        token = jwt.encode(
            {
                "iss": api_key,
                "exp": int(now) + 600,
                "iat": int(now),
            },
            api_secret,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


_client: ZoomVideoSDKClient | None = None


def _get_client() -> ZoomVideoSDKClient:
    global _client
    if _client is None:
        _client = ZoomVideoSDKClient()
    return _client


async def execute_zoom_session_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute zoom.session.create — create a video conference session.

    Required payload:
      - name: str — session topic name

    Optional payload:
      - session_key: str — custom session key for joining
      - settings: dict — session settings (auto_recording, etc.)
    """
    client = _get_client()

    name = payload.get("name", "")
    if not name:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="zoom.session.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="zoom.session.create",
            error="Missing required parameter: name",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "session_name": name,
        "type": 1,  # Instant session
    }
    session_key = payload.get("session_key")
    if session_key:
        body["session_key"] = session_key
    session_settings = payload.get("settings")
    if session_settings:
        body["settings"] = session_settings

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/videosdk/sessions",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="zoom.session.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        session = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="zoom.session.create",
            data={
                "session_name": session.get("session_name", name),
                "session_id": session.get("id", ""),
                "session_key": session.get("session_key", ""),
                "status": session.get("status", ""),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="zoom.session.create",
            error=response.error_message or f"Zoom API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_zoom_session_list(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute zoom.session.list — list active video conference sessions.

    No required payload fields.
    """
    client = _get_client()

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/videosdk/sessions",
            body=None,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="zoom.session.list",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        sessions = response.body.get("sessions", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="zoom.session.list",
            data={
                "sessions": [
                    {
                        "session_name": s.get("session_name", ""),
                        "session_id": s.get("id", ""),
                        "session_key": s.get("session_key", ""),
                        "status": s.get("status", ""),
                    }
                    for s in sessions
                ],
                "session_count": len(sessions),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="zoom.session.list",
            error=response.error_message or f"Zoom API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
