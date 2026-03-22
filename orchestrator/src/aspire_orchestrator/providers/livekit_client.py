"""LiveKit Provider Client — Conference rooms for Nora (Conference) skill pack.

Provider: LiveKit Server API (https://cloud-api.livekit.io)
Auth: API key + secret — Bearer token in Authorization header
Risk tier: GREEN (room management is non-destructive)
Idempotency: N/A (read + create operations)

Tools:
  - livekit.room.create: Create a conference room
  - livekit.room.list: List active rooms
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


class LiveKitClient(BaseProviderClient):
    """LiveKit Server API client."""

    provider_id = "livekit"
    base_url = "https://cloud-api.livekit.io"
    timeout_seconds = 5.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        import time as _time
        import jwt

        api_key = settings.livekit_api_key
        api_secret = settings.livekit_api_secret
        if not api_key or not api_secret:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="LiveKit API key/secret not configured (ASPIRE_LIVEKIT_API_KEY, ASPIRE_LIVEKIT_API_SECRET)",
                provider_id=self.provider_id,
            )
        # LiveKit requires a JWT signed with api_key (issuer) + api_secret
        now = _time.time()
        token = jwt.encode(
            {
                "iss": api_key,
                "sub": api_key,
                "exp": int(now) + 600,
                "nbf": int(now),
                "video": {
                    "roomCreate": True,
                    "roomList": True,
                    "roomAdmin": True,
                },
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


_client: LiveKitClient | None = None


def _get_client() -> LiveKitClient:
    global _client
    if _client is None:
        _client = LiveKitClient()
    return _client


async def execute_livekit_room_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute livekit.room.create — create a conference room.

    Required payload:
      - name: str — room name

    Optional payload:
      - empty_timeout: int — seconds before empty room is closed (default 300)
      - max_participants: int — max participants allowed (default 20)
    """
    client = _get_client()

    name = payload.get("name", "")
    if not name:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="livekit.room.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="livekit.room.create",
            error="Missing required parameter: name",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "name": name,
        "empty_timeout": payload.get("empty_timeout", 300),
        "max_participants": payload.get("max_participants", 20),
    }

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/twirp/livekit.RoomService/CreateRoom",
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
        tool_id="livekit.room.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        room = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="livekit.room.create",
            data={
                "room_name": room.get("name", name),
                "sid": room.get("sid", ""),
                "empty_timeout": room.get("empty_timeout", 300),
                "max_participants": room.get("max_participants", 20),
                "creation_time": room.get("creation_time"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="livekit.room.create",
            error=response.error_message or f"LiveKit API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_livekit_room_list(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute livekit.room.list — list active conference rooms.

    No required payload fields.
    """
    client = _get_client()

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/twirp/livekit.RoomService/ListRooms",
            body={},
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
        tool_id="livekit.room.list",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        rooms = response.body.get("rooms", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="livekit.room.list",
            data={
                "rooms": [
                    {
                        "name": r.get("name", ""),
                        "sid": r.get("sid", ""),
                        "num_participants": r.get("num_participants", 0),
                        "creation_time": r.get("creation_time"),
                    }
                    for r in rooms
                ],
                "room_count": len(rooms),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="livekit.room.list",
            error=response.error_message or f"LiveKit API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
