"""Deepgram Provider Client — Speech-to-Text for Nora (Conference) skill pack.

Provider: Deepgram (https://api.deepgram.com/v1)
Auth: API key as `Authorization: Token {key}` header
Risk tier: GREEN (transcription is read-only processing)
Idempotency: N/A (stateless processing)

Tools:
  - deepgram.transcribe: Transcribe audio via Deepgram Nova-3
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


class DeepgramClient(BaseProviderClient):
    """Deepgram STT API client."""

    provider_id = "deepgram"
    base_url = "https://api.deepgram.com/v1"
    timeout_seconds = 30.0  # Audio processing can be slow
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.deepgram_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Deepgram API key not configured (ASPIRE_DEEPGRAM_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"Authorization": f"Token {api_key}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if status_code == 400:
            return InternalErrorCode.INPUT_INVALID_FORMAT
        return super()._parse_error(status_code, body)


_client: DeepgramClient | None = None


def _get_client() -> DeepgramClient:
    global _client
    if _client is None:
        _client = DeepgramClient()
    return _client


async def execute_deepgram_transcribe(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute deepgram.transcribe — transcribe audio via Deepgram Nova-3.

    Required payload (one of):
      - audio_url: str — URL to the audio file (preferred)
      - audio_data: str — base64-encoded audio data

    Optional payload:
      - model: str — Deepgram model (default "nova-3")
      - smart_format: bool — enable smart formatting (default true)
      - language: str — BCP-47 language code (default "en")
    """
    client = _get_client()

    audio_url = payload.get("audio_url", "")
    audio_data = payload.get("audio_data", "")

    if not audio_url and not audio_data:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="deepgram.transcribe",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="deepgram.transcribe",
            error="Missing required parameter: audio_url or audio_data",
            receipt_data=receipt,
        )

    # S4-L5: Whitelist allowed models — reject invalid before API call
    _ALLOWED_MODELS = {"nova-3", "nova-2", "enhanced", "base"}
    model = payload.get("model", "nova-3")
    if model not in _ALLOWED_MODELS:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="deepgram.transcribe",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INVALID_MODEL",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="deepgram.transcribe",
            error=f"Invalid model '{model}'. Allowed: {', '.join(sorted(_ALLOWED_MODELS))}",
            receipt_data=receipt,
        )
    smart_format = "true" if payload.get("smart_format", True) else "false"
    language = payload.get("language", "en")

    query_params: dict[str, str] = {
        "model": model,
        "smart_format": smart_format,
        "language": language,
    }

    # Build request body — URL mode sends JSON, data mode would send binary
    body: dict[str, Any] = {}
    if audio_url:
        body = {"url": audio_url}

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/listen",
            body=body,
            query_params=query_params,
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
        tool_id="deepgram.transcribe",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        results = response.body.get("results", {})
        channels = results.get("channels", [])

        transcript = ""
        confidence = 0.0
        words_count = 0

        if channels:
            alternatives = channels[0].get("alternatives", [])
            if alternatives:
                best = alternatives[0]
                transcript = best.get("transcript", "")
                confidence = best.get("confidence", 0.0)
                words = best.get("words", [])
                words_count = len(words)

        metadata = response.body.get("metadata", {})
        duration = metadata.get("duration", 0.0)

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="deepgram.transcribe",
            data={
                "transcript": transcript,
                "confidence": confidence,
                "words_count": words_count,
                "duration": duration,
                "model": model,
                "language": language,
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="deepgram.transcribe",
            error=response.error_message or f"Deepgram API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
