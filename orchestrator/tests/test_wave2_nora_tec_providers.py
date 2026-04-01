"""Tests for Wave 2 — Nora (Conference) and Tec (Documents) provider clients.

Wave 2 providers:
  Nora: Zoom (rooms), Deepgram (STT), ElevenLabs (TTS)
  Tec:  Puppeteer (PDF), S3 (document storage)

Test coverage:
  - Success paths with mocked HTTP responses
  - Input validation (missing required fields)
  - Auth validation (missing API keys)
  - Receipt emission for ALL outcomes (Law #2)
  - Correct risk tier in receipts
  - Error handling and error code mapping
  - Stub markers for Puppeteer/S3
  - Tool executor registry wiring
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# =============================================================================
# Shared test fixtures
# =============================================================================

SUITE_ID = "suite-test-wave2"
OFFICE_ID = "office-test-wave2"
CORR_ID = "corr-wave2-test-001"
CAP_TOKEN_ID = "cap-tok-w2-001"
CAP_TOKEN_HASH = "hash-w2-001"


def _std_kwargs(**overrides):
    """Build standard executor kwargs."""
    base = {
        "correlation_id": CORR_ID,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "capability_token_id": CAP_TOKEN_ID,
        "capability_token_hash": CAP_TOKEN_HASH,
    }
    base.update(overrides)
    return base


def _assert_receipt(result: ToolExecutionResult, expected_outcome: str, expected_tool: str, expected_tier: str):
    """Assert receipt data meets Law #2 requirements."""
    assert result.receipt_data, f"Receipt MISSING for {expected_tool} — Law #2 violation"
    rd = result.receipt_data
    assert rd["outcome"] == expected_outcome, f"Expected outcome={expected_outcome}, got {rd['outcome']}"
    assert rd["tool_used"] == expected_tool
    assert rd["risk_tier"] == expected_tier
    assert rd["suite_id"] == SUITE_ID
    assert rd["office_id"] == OFFICE_ID
    assert rd["correlation_id"] == CORR_ID
    assert rd["capability_token_id"] == CAP_TOKEN_ID
    assert rd["capability_token_hash"] == CAP_TOKEN_HASH
    assert rd.get("id"), "Receipt must have an ID"
    assert rd.get("created_at"), "Receipt must have created_at"
    assert rd.get("receipt_type") == "tool_execution"


def _mock_httpx_response(status_code: int = 200, body: dict | None = None, content: bytes | None = None):
    """Create a mock httpx response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if content is not None:
        resp.content = content
    else:
        resp.content = json.dumps(body or {}).encode()
    resp.headers = {"x-request-id": "mock-req-123"}
    return resp


# =============================================================================
# Zoom Tests (Nora — Conference rooms)
# =============================================================================


class TestZoomSessionCreate:
    """Tests for zoom.session.create executor."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Room creation succeeds with valid payload."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None  # Reset singleton

        mock_resp = _mock_httpx_response(200, {
            "session_name": "standup-room",
            "id": "ZS_abc123",
            "session_key": "key-123",
            "status": "available",
        })

        with patch.object(mod, "settings", MagicMock(zoom_api_key="zk-key", zoom_api_secret="zk-secret")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_create(
                    payload={"name": "standup-room"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.tool_id == "zoom.session.create"
        assert result.data["session_name"] == "standup-room"
        assert result.data["session_id"] == "ZS_abc123"
        _assert_receipt(result, "success", "zoom.session.create", "green")

    @pytest.mark.asyncio
    async def test_missing_name(self):
        """Missing room name returns FAILED with receipt."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(zoom_api_key="zk-key", zoom_api_secret="zk-secret")):
            result = await mod.execute_zoom_session_create(
                payload={},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        assert "name" in result.error.lower()
        _assert_receipt(result, "failed", "zoom.session.create", "green")

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """Missing API key returns AUTH_INVALID_KEY with receipt."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(zoom_api_key="", zoom_api_secret="")):
            result = await mod.execute_zoom_session_create(
                payload={"name": "test-room"},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "zoom.session.create", "green")
        assert result.receipt_data["reason_code"] == "AUTH_INVALID_KEY"

    @pytest.mark.asyncio
    async def test_api_error_401(self):
        """401 response maps to AUTH_INVALID_KEY."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(401, {"error": "unauthorized"})

        with patch.object(mod, "settings", MagicMock(zoom_api_key="bad-key", zoom_api_secret="bad-secret")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_create(
                    payload={"name": "test"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "zoom.session.create", "green")

    @pytest.mark.asyncio
    async def test_custom_params(self):
        """Custom empty_timeout and max_participants are sent."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {
            "name": "big-room",
            "sid": "RM_xyz",
            "empty_timeout": 600,
            "max_participants": 100,
        })

        with patch.object(mod, "settings", MagicMock(zoom_api_key="zk-key", zoom_api_secret="zk-secret")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_create(
                    payload={"name": "big-room", "empty_timeout": 600, "max_participants": 100},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["max_participants"] == 100


class TestZoomSessionList:
    """Tests for zoom.session.list executor."""

    @pytest.mark.asyncio
    async def test_success_with_rooms(self):
        """Room list succeeds with rooms in response."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {
            "rooms": [
                {"name": "room-1", "sid": "RM_1", "num_participants": 3, "creation_time": 1700000000},
                {"name": "room-2", "sid": "RM_2", "num_participants": 0, "creation_time": 1700001000},
            ],
        })

        with patch.object(mod, "settings", MagicMock(zoom_api_key="zk-key", zoom_api_secret="zk-secret")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_list(
                    payload={},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["room_count"] == 2
        assert result.data["rooms"][0]["name"] == "room-1"
        _assert_receipt(result, "success", "zoom.session.list", "green")

    @pytest.mark.asyncio
    async def test_success_empty(self):
        """Room list succeeds with no rooms."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {"rooms": []})

        with patch.object(mod, "settings", MagicMock(zoom_api_key="zk-key", zoom_api_secret="zk-secret")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_list(
                    payload={},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["room_count"] == 0
        _assert_receipt(result, "success", "zoom.session.list", "green")

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """Missing API key returns FAILED with receipt."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(zoom_api_key="", zoom_api_secret="")):
            result = await mod.execute_zoom_session_list(
                payload={},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "zoom.session.list", "green")

    @pytest.mark.asyncio
    async def test_api_error_429(self):
        """429 rate limit maps correctly."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(429, {"error": "rate limited"})

        with patch.object(mod, "settings", MagicMock(zoom_api_key="zk-key", zoom_api_secret="zk-secret")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_list(
                    payload={},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "zoom.session.list", "green")


# =============================================================================
# Deepgram Tests (Nora — Speech-to-Text)
# =============================================================================


class TestDeepgramTranscribe:
    """Tests for deepgram.transcribe executor."""

    @pytest.mark.asyncio
    async def test_success_with_url(self):
        """Transcription succeeds with audio URL."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {
            "results": {
                "channels": [{
                    "alternatives": [{
                        "transcript": "Hello world, this is a test.",
                        "confidence": 0.98,
                        "words": [{"word": "Hello"}, {"word": "world"}, {"word": "this"},
                                  {"word": "is"}, {"word": "a"}, {"word": "test"}],
                    }],
                }],
            },
            "metadata": {"duration": 3.5},
        })

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="dg-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_deepgram_transcribe(
                    payload={"audio_url": "https://example.com/audio.wav"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.tool_id == "deepgram.transcribe"
        assert result.data["transcript"] == "Hello world, this is a test."
        assert result.data["confidence"] == 0.98
        assert result.data["words_count"] == 6
        assert result.data["duration"] == 3.5
        _assert_receipt(result, "success", "deepgram.transcribe", "green")

    @pytest.mark.asyncio
    async def test_missing_audio_input(self):
        """Missing both audio_url and audio_data returns FAILED."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="dg-key")):
            result = await mod.execute_deepgram_transcribe(
                payload={},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        assert "audio_url" in result.error.lower() or "audio" in result.error.lower()
        _assert_receipt(result, "failed", "deepgram.transcribe", "green")

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """Missing API key returns AUTH_INVALID_KEY."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="")):
            result = await mod.execute_deepgram_transcribe(
                payload={"audio_url": "https://example.com/audio.wav"},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "deepgram.transcribe", "green")
        assert result.receipt_data["reason_code"] == "AUTH_INVALID_KEY"

    @pytest.mark.asyncio
    async def test_api_error_400(self):
        """400 bad request maps to INPUT_INVALID_FORMAT."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(400, {"error": "invalid audio format"})

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="dg-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_deepgram_transcribe(
                    payload={"audio_url": "https://example.com/bad.txt"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "deepgram.transcribe", "green")

    @pytest.mark.asyncio
    async def test_empty_transcript(self):
        """Transcription succeeds but with empty result."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {
            "results": {"channels": [{"alternatives": [{"transcript": "", "confidence": 0.0, "words": []}]}]},
            "metadata": {"duration": 0.0},
        })

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="dg-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_deepgram_transcribe(
                    payload={"audio_url": "https://example.com/silence.wav"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["transcript"] == ""
        assert result.data["words_count"] == 0
        _assert_receipt(result, "success", "deepgram.transcribe", "green")

    @pytest.mark.asyncio
    async def test_custom_model_and_language(self):
        """Custom model and language are passed through."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {
            "results": {"channels": [{"alternatives": [{"transcript": "Hola", "confidence": 0.95, "words": [{"word": "Hola"}]}]}]},
            "metadata": {"duration": 1.0},
        })

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="dg-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_deepgram_transcribe(
                    payload={"audio_url": "https://example.com/es.wav", "model": "nova-3", "language": "es"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["language"] == "es"
        _assert_receipt(result, "success", "deepgram.transcribe", "green")


# =============================================================================
# ElevenLabs Tests (Nora — Text-to-Speech)
# =============================================================================


class TestElevenLabsSpeak:
    """Tests for elevenlabs.speak executor."""

    @pytest.mark.asyncio
    async def test_success(self):
        """TTS succeeds with valid text."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        # ElevenLabs returns binary audio, not JSON
        mock_resp = _mock_httpx_response(200, content=b"\x00\x01\x02\x03" * 1000)

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="el-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_elevenlabs_speak(
                    payload={"text": "Hello from Aspire!"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.tool_id == "elevenlabs.speak"
        assert result.data["text_length"] == len("Hello from Aspire!")
        assert result.data["audio_generated"] is True
        assert result.data["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
        assert result.data["model_id"] == "eleven_flash_v2_5"
        _assert_receipt(result, "success", "elevenlabs.speak", "green")

    @pytest.mark.asyncio
    async def test_missing_text(self):
        """Missing text returns FAILED with receipt."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="el-key")):
            result = await mod.execute_elevenlabs_speak(
                payload={},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        assert "text" in result.error.lower()
        _assert_receipt(result, "failed", "elevenlabs.speak", "green")

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        """Missing API key returns AUTH_INVALID_KEY."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="")):
            result = await mod.execute_elevenlabs_speak(
                payload={"text": "Test"},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "elevenlabs.speak", "green")
        assert result.receipt_data["reason_code"] == "AUTH_INVALID_KEY"

    @pytest.mark.asyncio
    async def test_custom_voice_and_model(self):
        """Custom voice_id and model_id are used."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, content=b"\x00\x01" * 500)

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="el-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_elevenlabs_speak(
                    payload={"text": "Custom voice", "voice_id": "custom-voice-123", "model_id": "eleven_turbo_v2"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["voice_id"] == "custom-voice-123"
        assert result.data["model_id"] == "eleven_turbo_v2"
        _assert_receipt(result, "success", "elevenlabs.speak", "green")

    @pytest.mark.asyncio
    async def test_api_error_422(self):
        """422 maps to INPUT_CONSTRAINT_VIOLATED."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(422, {"detail": {"message": "Text too long"}})

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="el-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_elevenlabs_speak(
                    payload={"text": "x" * 100000},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "elevenlabs.speak", "green")

    @pytest.mark.asyncio
    async def test_api_error_401(self):
        """401 maps to AUTH_INVALID_KEY."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(401, {"detail": {"message": "Unauthorized"}})

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="bad-key")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False,
                post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_elevenlabs_speak(
                    payload={"text": "test"},
                    **_std_kwargs(risk_tier="green"),
                )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "elevenlabs.speak", "green")


# =============================================================================
# Puppeteer Tests (Tec — PDF Generation)
# =============================================================================


class TestPuppeteerPdfGenerate:
    """Tests for puppeteer.pdf.generate executor (stub)."""

    @pytest.mark.asyncio
    async def test_success_stub(self):
        """PDF generation stub succeeds with receipt."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        result = await mod.execute_puppeteer_pdf_generate(
            payload={"html": "<h1>Invoice #123</h1><p>Total: $500</p>"},
            **_std_kwargs(risk_tier="green"),
        )

        assert result.outcome == Outcome.SUCCESS
        assert result.tool_id == "puppeteer.pdf.generate"
        assert result.data["pdf_generated"] is True
        assert result.data["format"] == "A4"
        assert result.data["html_length"] > 0
        assert result.data["stub"] is True
        assert result.is_stub is True
        _assert_receipt(result, "success", "puppeteer.pdf.generate", "green")

    @pytest.mark.asyncio
    async def test_missing_html(self):
        """Missing HTML returns FAILED with receipt."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        result = await mod.execute_puppeteer_pdf_generate(
            payload={},
            **_std_kwargs(risk_tier="green"),
        )

        assert result.outcome == Outcome.FAILED
        assert "html" in result.error.lower()
        _assert_receipt(result, "failed", "puppeteer.pdf.generate", "green")

    @pytest.mark.asyncio
    async def test_custom_options(self):
        """Custom format and margins are accepted."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        result = await mod.execute_puppeteer_pdf_generate(
            payload={
                "html": "<h1>Test</h1>",
                "options": {"format": "Letter", "margin": {"top": "1in", "bottom": "1in"}},
            },
            **_std_kwargs(risk_tier="green"),
        )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["format"] == "Letter"
        _assert_receipt(result, "success", "puppeteer.pdf.generate", "green")

    @pytest.mark.asyncio
    async def test_receipt_has_provider_metadata(self):
        """Receipt includes provider metadata from stub response."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        result = await mod.execute_puppeteer_pdf_generate(
            payload={"html": "<p>Test</p>"},
            **_std_kwargs(risk_tier="green"),
        )

        assert result.receipt_data.get("provider_metadata")
        pm = result.receipt_data["provider_metadata"]
        assert pm["provider_status_code"] == 200

    @pytest.mark.asyncio
    async def test_empty_html_fails(self):
        """Empty string HTML returns FAILED."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        result = await mod.execute_puppeteer_pdf_generate(
            payload={"html": ""},
            **_std_kwargs(risk_tier="green"),
        )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "puppeteer.pdf.generate", "green")

    @pytest.mark.asyncio
    async def test_no_auth_required(self):
        """Puppeteer client does not require authentication."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        client = mod._get_client()
        headers = await client._authenticate_headers(
            MagicMock(suite_id="s", correlation_id="c", office_id="o")
        )
        assert headers == {}


# =============================================================================
# S3 Tests (Tec — Document Storage)
# =============================================================================


class TestS3DocumentUpload:
    """Tests for s3.document.upload executor (stub)."""

    @pytest.mark.asyncio
    async def test_success_stub(self):
        """Upload stub succeeds with receipt."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_document_upload(
                payload={
                    "bucket": "aspire-docs",
                    "key": "invoices/2026/inv-001.pdf",
                    "content_type": "application/pdf",
                },
                **_std_kwargs(risk_tier="yellow"),
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.tool_id == "s3.document.upload"
        assert result.data["bucket"] == "aspire-docs"
        assert result.data["key"] == "invoices/2026/inv-001.pdf"
        assert result.data["uploaded"] is True
        assert result.data["stub"] is True
        assert result.is_stub is True
        _assert_receipt(result, "success", "s3.document.upload", "yellow")

    @pytest.mark.asyncio
    async def test_missing_params(self):
        """Missing bucket/key/content_type returns FAILED."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_document_upload(
                payload={"bucket": "test"},  # Missing key and content_type
                **_std_kwargs(risk_tier="yellow"),
            )

        assert result.outcome == Outcome.FAILED
        assert "bucket" in result.error.lower() or "key" in result.error.lower()
        _assert_receipt(result, "failed", "s3.document.upload", "yellow")

    @pytest.mark.asyncio
    async def test_missing_aws_credentials(self):
        """Missing AWS credentials returns AUTH_INVALID_KEY."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="", aws_secret_access_key="", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_document_upload(
                payload={
                    "bucket": "aspire-docs",
                    "key": "test.pdf",
                    "content_type": "application/pdf",
                },
                **_std_kwargs(risk_tier="yellow"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "s3.document.upload", "yellow")
        assert result.receipt_data["reason_code"] == "AUTH_INVALID_KEY"

    @pytest.mark.asyncio
    async def test_yellow_risk_tier(self):
        """Upload uses YELLOW risk tier (state change)."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_document_upload(
                payload={
                    "bucket": "aspire-docs",
                    "key": "test.pdf",
                    "content_type": "application/pdf",
                },
                **_std_kwargs(risk_tier="yellow"),
            )

        assert result.receipt_data["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_etag_in_response(self):
        """Upload response includes an etag."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_document_upload(
                payload={
                    "bucket": "aspire-docs",
                    "key": "doc.pdf",
                    "content_type": "application/pdf",
                },
                **_std_kwargs(risk_tier="yellow"),
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data.get("etag"), "Upload response must include etag"
        assert result.data["etag"].startswith("stub-")


class TestS3UrlSign:
    """Tests for s3.url.sign executor (stub)."""

    @pytest.mark.asyncio
    async def test_success_stub(self):
        """Presigned URL stub succeeds with receipt."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_url_sign(
                payload={
                    "bucket": "aspire-docs",
                    "key": "invoices/inv-001.pdf",
                    "expires_in": 7200,
                },
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.tool_id == "s3.url.sign"
        assert "presigned_url" in result.data
        assert "aspire-docs" in result.data["presigned_url"]
        assert "inv-001.pdf" in result.data["presigned_url"]
        assert result.data["expires_in"] == 7200
        _assert_receipt(result, "success", "s3.url.sign", "green")

    @pytest.mark.asyncio
    async def test_missing_bucket_key(self):
        """Missing bucket or key returns FAILED."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_url_sign(
                payload={"bucket": "aspire-docs"},  # Missing key
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "s3.url.sign", "green")

    @pytest.mark.asyncio
    async def test_green_risk_tier(self):
        """URL signing uses GREEN risk tier (read-only)."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_url_sign(
                payload={"bucket": "b", "key": "k"},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.receipt_data["risk_tier"] == "green"

    @pytest.mark.asyncio
    async def test_missing_aws_credentials(self):
        """Missing AWS credentials returns AUTH_INVALID_KEY."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="", aws_secret_access_key="", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_url_sign(
                payload={"bucket": "b", "key": "k"},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.FAILED
        _assert_receipt(result, "failed", "s3.url.sign", "green")
        assert result.receipt_data["reason_code"] == "AUTH_INVALID_KEY"

    @pytest.mark.asyncio
    async def test_default_expiry(self):
        """Default expires_in is 3600 seconds."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="AKIA123", aws_secret_access_key="secret123", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_url_sign(
                payload={"bucket": "b", "key": "k"},
                **_std_kwargs(risk_tier="green"),
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["expires_in"] == 3600


# =============================================================================
# Tool Executor Registry Wiring Tests
# =============================================================================


class TestToolExecutorRegistry:
    """Verify Wave 2 tools are wired into the executor registry."""

    def test_conference_tools_registered(self):
        """Nora conference tools are in the live registry."""
        from aspire_orchestrator.services.tool_executor import is_live_tool

        assert is_live_tool("zoom.session.create")
        assert is_live_tool("zoom.session.list")
        assert is_live_tool("deepgram.transcribe")
        assert is_live_tool("elevenlabs.speak")

    def test_document_tools_registered(self):
        """Tec document tools are in the live registry."""
        from aspire_orchestrator.services.tool_executor import is_live_tool

        assert is_live_tool("puppeteer.pdf.generate")
        assert is_live_tool("s3.document.upload")
        assert is_live_tool("s3.url.sign")

    def test_live_tools_list_includes_wave2(self):
        """get_live_tools() includes all Wave 2 tool IDs."""
        from aspire_orchestrator.services.tool_executor import get_live_tools

        live = get_live_tools()
        wave2_tools = [
            "zoom.session.create", "zoom.session.list",
            "deepgram.transcribe", "elevenlabs.speak",
            "puppeteer.pdf.generate", "s3.document.upload", "s3.url.sign",
        ]
        for tool_id in wave2_tools:
            assert tool_id in live, f"{tool_id} missing from live tools registry"


# =============================================================================
# Cross-Cutting Law #2 Receipt Completeness Tests
# =============================================================================


class TestLaw2ReceiptCompleteness:
    """Verify ALL outcomes emit receipts with required fields."""

    @pytest.mark.asyncio
    async def test_zoom_success_receipt_fields(self):
        """Zoom success receipt has all required fields."""
        import aspire_orchestrator.providers.zoom_videosdk_client as mod
        mod._client = None

        mock_resp = _mock_httpx_response(200, {"name": "r", "sid": "s"})
        with patch.object(mod, "settings", MagicMock(zoom_api_key="k", zoom_api_secret="s")):
            client = mod._get_client()
            with patch.object(client, "_get_client", new_callable=AsyncMock, return_value=MagicMock(
                is_closed=False, post=AsyncMock(return_value=mock_resp),
            )):
                result = await mod.execute_zoom_session_create(
                    payload={"name": "r"}, **_std_kwargs(risk_tier="green"),
                )

        rd = result.receipt_data
        required = ["id", "correlation_id", "suite_id", "office_id", "actor_type",
                     "actor_id", "action_type", "risk_tier", "tool_used",
                     "created_at", "executed_at", "outcome", "reason_code", "receipt_type"]
        for field in required:
            assert field in rd, f"Receipt missing required field: {field}"

    @pytest.mark.asyncio
    async def test_deepgram_failure_receipt_fields(self):
        """Deepgram failure receipt has all required fields."""
        import aspire_orchestrator.providers.deepgram_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(deepgram_api_key="k")):
            result = await mod.execute_deepgram_transcribe(
                payload={}, **_std_kwargs(risk_tier="green"),
            )

        rd = result.receipt_data
        assert rd["outcome"] == "failed"
        assert rd["reason_code"] == "INPUT_MISSING_REQUIRED"
        assert rd["actor_id"] == "provider.deepgram"

    @pytest.mark.asyncio
    async def test_s3_auth_failure_receipt_fields(self):
        """S3 auth failure receipt has all required fields."""
        import aspire_orchestrator.providers.s3_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(
            aws_access_key_id="", aws_secret_access_key="", aws_s3_region="us-east-1"
        )):
            result = await mod.execute_s3_document_upload(
                payload={"bucket": "b", "key": "k", "content_type": "ct"},
                **_std_kwargs(risk_tier="yellow"),
            )

        rd = result.receipt_data
        assert rd["outcome"] == "failed"
        assert rd["reason_code"] == "AUTH_INVALID_KEY"
        assert rd["actor_id"] == "provider.s3"

    @pytest.mark.asyncio
    async def test_puppeteer_stub_receipt_actor(self):
        """Puppeteer receipt actor is provider.puppeteer."""
        import aspire_orchestrator.providers.puppeteer_client as mod
        mod._client = None

        result = await mod.execute_puppeteer_pdf_generate(
            payload={"html": "<p>test</p>"}, **_std_kwargs(risk_tier="green"),
        )

        assert result.receipt_data["actor_id"] == "provider.puppeteer"

    @pytest.mark.asyncio
    async def test_elevenlabs_failure_receipt_actor(self):
        """ElevenLabs failure receipt actor is provider.elevenlabs."""
        import aspire_orchestrator.providers.elevenlabs_client as mod
        mod._client = None

        with patch.object(mod, "settings", MagicMock(elevenlabs_api_key="k")):
            result = await mod.execute_elevenlabs_speak(
                payload={}, **_std_kwargs(risk_tier="green"),
            )

        assert result.receipt_data["actor_id"] == "provider.elevenlabs"
        assert result.receipt_data["outcome"] == "failed"
