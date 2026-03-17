"""ElevenLabs Provider Client — Text-to-Speech for Nora (Conference) skill pack.

Provider: ElevenLabs (https://api.elevenlabs.io/v1)
Auth: API key in `xi-api-key` header
Risk tier: GREEN (TTS is read-only processing, generates audio)
Idempotency: N/A (stateless processing)

Tools:
  - elevenlabs.speak: Convert text to speech via ElevenLabs TTS
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


class ElevenLabsClient(BaseProviderClient):
    """ElevenLabs TTS API client."""

    provider_id = "elevenlabs"
    base_url = "https://api.elevenlabs.io/v1"
    timeout_seconds = 15.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.elevenlabs_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="ElevenLabs API key not configured (ASPIRE_ELEVENLABS_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"xi-api-key": api_key}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 422:
            return InternalErrorCode.INPUT_CONSTRAINT_VIOLATED
        return super()._parse_error(status_code, body)

    def _parse_response(self, raw_body: bytes) -> dict[str, Any]:
        """Override: ElevenLabs TTS returns audio bytes, not JSON.

        For successful responses (audio), we return metadata.
        For error responses, we attempt JSON parsing.
        """
        import json
        try:
            return json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, ValueError):
            # Likely binary audio data — return metadata about it
            return {
                "audio_generated": True,
                "audio_size_bytes": len(raw_body) if raw_body else 0,
            }


_client: ElevenLabsClient | None = None


def _get_client() -> ElevenLabsClient:
    global _client
    if _client is None:
        _client = ElevenLabsClient()
    return _client


async def execute_elevenlabs_speak(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute elevenlabs.speak — convert text to speech.

    Required payload:
      - text: str — text to synthesize

    Optional payload:
      - voice_id: str — ElevenLabs voice ID (default "21m00Tcm4TlvDq8ikWAM" — Rachel)
      - model_id: str — model (default "eleven_flash_v2_5")
      - stability: float — voice stability 0.0-1.0 (default 0.5)
      - similarity_boost: float — similarity 0.0-1.0 (default 0.75)
    """
    client = _get_client()

    text = payload.get("text", "")
    if not text:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="elevenlabs.speak",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="elevenlabs.speak",
            error="Missing required parameter: text",
            receipt_data=receipt,
        )

    # S4-L4: Text input size validation (ElevenLabs limit: 5000 chars)
    _MAX_TEXT_LENGTH = 5000
    if len(text) > _MAX_TEXT_LENGTH:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="elevenlabs.speak",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_TOO_LARGE",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="elevenlabs.speak",
            error=f"Text exceeds max length: {len(text)} > {_MAX_TEXT_LENGTH} chars",
            receipt_data=receipt,
        )

    voice_id = payload.get("voice_id", "21m00Tcm4TlvDq8ikWAM")
    model_id = payload.get("model_id", "eleven_flash_v2_5")

    body: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": payload.get("stability", 0.5),
            "similarity_boost": payload.get("similarity_boost", 0.75),
        },
    }

    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/text-to-speech/{voice_id}",
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
        tool_id="elevenlabs.speak",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        # S4-L4: Guard against oversized audio responses (max 25MB)
        _MAX_AUDIO_BYTES = 25 * 1024 * 1024
        audio_size = response.body.get("audio_size_bytes", 0)
        if isinstance(audio_size, (int, float)) and audio_size > _MAX_AUDIO_BYTES:
            logger.warning(
                "ElevenLabs audio response too large: %d bytes > %d max",
                audio_size, _MAX_AUDIO_BYTES,
            )

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="elevenlabs.speak",
            data={
                "voice_id": voice_id,
                "model_id": model_id,
                "text_length": len(text),
                "audio_generated": True,
                "audio_size_bytes": audio_size,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="elevenlabs.speak",
            error=response.error_message or f"ElevenLabs API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
