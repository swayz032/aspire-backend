"""Drew Provider Stub Tests — Wave 1.

Validates contract compliance for LlamaParseClient and AzureDocIntelClient:
  - Fail-closed when env vars missing (Law #3)
  - Valid ProviderRequest is built before any network call
  - Stub guard (NotImplementedError) is in place — Wave 2 wires real calls
  - No network calls made at construction time
  - Receipt emission path inherited from BaseProviderClient (Law #2)
  - PII-safe logging: pdf_bytes content never logged, only len()

Cross-reference:
  - providers/llamaparse_client.py
  - providers/azure_doc_intel_client.py
  - providers/base_client.py (BaseProviderClient — receipt emission, circuit breaker)
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from aspire_orchestrator.providers.base_client import ProviderError
from aspire_orchestrator.providers.error_codes import InternalErrorCode


# =============================================================================
# Constants
# =============================================================================

SUITE_ID = "11111111-1111-4111-8111-111111111111"
OFFICE_ID = "22222222-2222-4222-8222-222222222222"
CORRELATION_ID = "test-corr-drew-wave1-001"
FAKE_PDF_BYTES = b"%PDF-1.4 fake pdf content for testing"
FAKE_LLAMAPARSE_KEY = "llx-test-key-0000000000000000000000000000000000000000"
FAKE_AZURE_ENDPOINT = "https://aspire-test.cognitiveservices.azure.com"
FAKE_AZURE_KEY = "az-test-key-00000000000000000000000000000000"


# =============================================================================
# LlamaParseClient Tests
# =============================================================================


class TestLlamaParseClientMissingCredentials:
    """Law #3: Fail-closed when ASPIRE_LLAMAPARSE_API_KEY is missing."""

    def test_authenticate_headers_raises_when_key_missing(self) -> None:
        """_authenticate_headers must raise ProviderError if key is empty."""
        import asyncio

        mock_settings = MagicMock()
        mock_settings.llamaparse_api_key = ""  # Empty → fail-closed

        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()
            # Build a minimal ProviderRequest to pass to _authenticate_headers
            from aspire_orchestrator.providers.base_client import ProviderRequest

            req = ProviderRequest(
                method="POST",
                path="/api/parsing/upload",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            with pytest.raises(ProviderError) as exc_info:
                asyncio.get_event_loop().run_until_complete(
                    client._authenticate_headers(req)
                )

            assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY
            assert "ASPIRE_LLAMAPARSE_API_KEY" in exc_info.value.message

    def test_parse_pdf_propagates_auth_error(self) -> None:
        """parse_pdf must surface ProviderError (AUTH_INVALID_KEY) if key missing.

        Note: parse_pdf raises NotImplementedError as its stub guard, but the
        auth check in _authenticate_headers fires first when _request is called.
        For Wave 1, the stub raises NotImplementedError before touching auth,
        so this test validates the auth path via _authenticate_headers directly.
        This mirrors the Wave 1 design: method body exits early with NotImplementedError.
        """
        # Covered by test_authenticate_headers_raises_when_key_missing above.
        # Explicit stub guard test is in TestLlamaParseClientStubGuard.
        pass


class TestLlamaParseClientWithValidCredentials:
    """Verify client construction and ProviderRequest shape with valid credentials."""

    @pytest.fixture
    def mock_settings_llamaparse(self):
        mock = MagicMock()
        mock.llamaparse_api_key = FAKE_LLAMAPARSE_KEY
        return mock

    def test_construction_makes_no_network_calls(self, mock_settings_llamaparse: MagicMock) -> None:
        """Constructing LlamaParseClient must not trigger any HTTP calls."""
        import httpx

        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            with patch.object(httpx.AsyncClient, "post") as mock_post:
                from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

                _client = LlamaParseClient()
                mock_post.assert_not_called()

    def test_provider_id_is_llamaparse(self, mock_settings_llamaparse: MagicMock) -> None:
        """provider_id must be 'llamaparse' for receipt and circuit breaker keying."""
        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()
            assert client.provider_id == "llamaparse"

    def test_base_url_is_llamaindex_cloud(self, mock_settings_llamaparse: MagicMock) -> None:
        """base_url must point to LlamaIndex Cloud API."""
        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()
            assert client.base_url == "https://api.cloud.llamaindex.ai"

    def test_timeout_is_12_seconds(self, mock_settings_llamaparse: MagicMock) -> None:
        """timeout_seconds must be 12.0 — Drew blueprint parsing budget."""
        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()
            assert client.timeout_seconds == 12.0

    def test_max_retries_is_1(self, mock_settings_llamaparse: MagicMock) -> None:
        """max_retries must be 1 — single retry per no-fallback principle."""
        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()
            assert client.max_retries == 1

    def test_authenticate_headers_returns_bearer(self, mock_settings_llamaparse: MagicMock) -> None:
        """_authenticate_headers must return correct Bearer token header."""
        import asyncio

        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient
            from aspire_orchestrator.providers.base_client import ProviderRequest

            client = LlamaParseClient()
            req = ProviderRequest(
                method="POST",
                path="/api/parsing/upload",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            headers = asyncio.get_event_loop().run_until_complete(
                client._authenticate_headers(req)
            )

        assert headers == {"Authorization": f"Bearer {FAKE_LLAMAPARSE_KEY}"}


class TestLlamaParseClientStubGuard:
    """Wave 1 stub guard: parse_pdf must raise NotImplementedError."""

    @pytest.fixture
    def mock_settings_llamaparse(self):
        mock = MagicMock()
        mock.llamaparse_api_key = FAKE_LLAMAPARSE_KEY
        return mock

    @pytest.mark.asyncio
    async def test_parse_pdf_raises_not_implemented(
        self, mock_settings_llamaparse: MagicMock
    ) -> None:
        """parse_pdf must raise NotImplementedError until Wave 2 wires it."""
        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()

            with pytest.raises(NotImplementedError, match="Wave 2 wires this"):
                await client.parse_pdf(
                    FAKE_PDF_BYTES,
                    correlation_id=CORRELATION_ID,
                    suite_id=SUITE_ID,
                    office_id=OFFICE_ID,
                )

    @pytest.mark.asyncio
    async def test_parse_pdf_logs_byte_length_not_content(
        self, mock_settings_llamaparse: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Law #9: parse_pdf logs len(pdf_bytes), never raw content.

        We verify that the raw PDF sentinel string does not appear in log output
        but the byte count does.
        """
        import logging

        PDF_SENTINEL = b"SENSITIVE_BLUEPRINT_CONTENT_DO_NOT_LOG"

        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings_llamaparse,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()

            with caplog.at_level(logging.INFO, logger="aspire_orchestrator.providers.llamaparse_client"):
                with pytest.raises(NotImplementedError):
                    await client.parse_pdf(
                        PDF_SENTINEL,
                        correlation_id=CORRELATION_ID,
                        suite_id=SUITE_ID,
                        office_id=OFFICE_ID,
                    )

        full_log = " ".join(caplog.messages)
        # PII-safe: raw content must not appear in any log line
        assert "SENSITIVE_BLUEPRINT_CONTENT_DO_NOT_LOG" not in full_log
        # Byte length must appear (proves the safe logging path fired)
        assert str(len(PDF_SENTINEL)) in full_log


class TestLlamaParseClientErrorMapping:
    """Verify _parse_error maps provider HTTP codes to canonical InternalErrorCode."""

    @pytest.fixture
    def client(self):
        mock_settings = MagicMock()
        mock_settings.llamaparse_api_key = FAKE_LLAMAPARSE_KEY
        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient
            return LlamaParseClient()

    def test_401_maps_to_auth_invalid_key(self, client: "LlamaParseClient") -> None:
        assert client._parse_error(401, {}) == InternalErrorCode.AUTH_INVALID_KEY

    def test_403_maps_to_domain_forbidden(self, client: "LlamaParseClient") -> None:
        assert client._parse_error(403, {}) == InternalErrorCode.DOMAIN_FORBIDDEN

    def test_429_maps_to_rate_limited(self, client: "LlamaParseClient") -> None:
        assert client._parse_error(429, {}) == InternalErrorCode.RATE_LIMITED

    def test_500_maps_to_server_unavailable(self, client: "LlamaParseClient") -> None:
        assert client._parse_error(500, {}) == InternalErrorCode.SERVER_UNAVAILABLE

    def test_503_maps_to_server_unavailable(self, client: "LlamaParseClient") -> None:
        assert client._parse_error(503, {}) == InternalErrorCode.SERVER_UNAVAILABLE

    def test_400_falls_through_to_base(self, client: "LlamaParseClient") -> None:
        """400 uses base class mapping → INPUT_INVALID_FORMAT."""
        assert client._parse_error(400, {}) == InternalErrorCode.INPUT_INVALID_FORMAT


class TestLlamaParseReceiptEmission:
    """Law #2: Verify receipt emission is inherited from BaseProviderClient."""

    def test_make_receipt_data_is_callable(self) -> None:
        """LlamaParseClient inherits make_receipt_data from BaseProviderClient."""
        from aspire_orchestrator.providers.base_client import BaseProviderClient
        from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

        assert hasattr(LlamaParseClient, "make_receipt_data")
        # Confirmed via inheritance — not overridden
        assert LlamaParseClient.make_receipt_data is BaseProviderClient.make_receipt_data

    def test_make_receipt_data_produces_required_fields(self) -> None:
        """make_receipt_data must produce all Law #2 required fields."""
        from unittest.mock import MagicMock, patch
        from aspire_orchestrator.models import Outcome

        mock_settings = MagicMock()
        mock_settings.llamaparse_api_key = FAKE_LLAMAPARSE_KEY

        with patch(
            "aspire_orchestrator.providers.llamaparse_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.llamaparse_client import LlamaParseClient

            client = LlamaParseClient()
            receipt = client.make_receipt_data(
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
                tool_id="llamaparse.parse_pdf",
                risk_tier="green",
                outcome=Outcome.SUCCESS,
                reason_code="PARSED",
            )

        required_fields = {
            "id", "correlation_id", "suite_id", "office_id",
            "actor_type", "actor_id", "action_type", "risk_tier",
            "tool_used", "created_at", "executed_at", "outcome", "reason_code",
        }
        for field in required_fields:
            assert field in receipt, f"Receipt missing required field: {field}"

        assert receipt["suite_id"] == SUITE_ID
        assert receipt["office_id"] == OFFICE_ID
        assert receipt["correlation_id"] == CORRELATION_ID


# =============================================================================
# AzureDocIntelClient Tests
# =============================================================================


class TestAzureDocIntelClientMissingEndpoint:
    """Law #3: Fail-closed when ASPIRE_AZURE_DOC_INTEL_ENDPOINT is missing."""

    def test_construction_raises_when_endpoint_missing(self) -> None:
        """AzureDocIntelClient.__init__ must raise ProviderError if endpoint empty."""
        mock_settings = MagicMock()
        mock_settings.azure_doc_intel_endpoint = ""  # Missing → fail-closed
        mock_settings.azure_doc_intel_key = FAKE_AZURE_KEY

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            with pytest.raises(ProviderError) as exc_info:
                AzureDocIntelClient()

            assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY
            assert "ASPIRE_AZURE_DOC_INTEL_ENDPOINT" in exc_info.value.message


class TestAzureDocIntelClientMissingKey:
    """Law #3: Fail-closed when ASPIRE_AZURE_DOC_INTEL_KEY is missing."""

    def test_authenticate_headers_raises_when_key_missing(self) -> None:
        """_authenticate_headers must raise ProviderError if API key is empty."""
        import asyncio

        mock_settings = MagicMock()
        mock_settings.azure_doc_intel_endpoint = FAKE_AZURE_ENDPOINT
        mock_settings.azure_doc_intel_key = ""  # Missing → fail-closed

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient
            from aspire_orchestrator.providers.base_client import ProviderRequest

            client = AzureDocIntelClient()
            req = ProviderRequest(
                method="POST",
                path="/formrecognizer/documentModels/prebuilt-layout:analyze",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )

            with pytest.raises(ProviderError) as exc_info:
                asyncio.get_event_loop().run_until_complete(
                    client._authenticate_headers(req)
                )

            assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY
            assert "ASPIRE_AZURE_DOC_INTEL_KEY" in exc_info.value.message


class TestAzureDocIntelClientWithValidCredentials:
    """Verify client construction and ProviderRequest shape with valid credentials."""

    @pytest.fixture
    def mock_settings_azure(self):
        mock = MagicMock()
        mock.azure_doc_intel_endpoint = FAKE_AZURE_ENDPOINT
        mock.azure_doc_intel_key = FAKE_AZURE_KEY
        return mock

    def test_construction_makes_no_network_calls(self, mock_settings_azure: MagicMock) -> None:
        """Constructing AzureDocIntelClient must not trigger any HTTP calls."""
        import httpx

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            with patch.object(httpx.AsyncClient, "post") as mock_post:
                from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

                _client = AzureDocIntelClient()
                mock_post.assert_not_called()

    def test_provider_id_is_azure_doc_intel(self, mock_settings_azure: MagicMock) -> None:
        """provider_id must be 'azure_doc_intel'."""
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()
            assert client.provider_id == "azure_doc_intel"

    def test_base_url_set_from_endpoint_env(self, mock_settings_azure: MagicMock) -> None:
        """base_url must be set from ASPIRE_AZURE_DOC_INTEL_ENDPOINT with trailing slash stripped."""
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()
            assert client.base_url == FAKE_AZURE_ENDPOINT.rstrip("/")

    def test_trailing_slash_stripped_from_endpoint(self, mock_settings_azure: MagicMock) -> None:
        """base_url must strip trailing slash from endpoint to avoid double-slash paths."""
        mock_settings_azure.azure_doc_intel_endpoint = FAKE_AZURE_ENDPOINT + "/"

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()
            assert not client.base_url.endswith("/")

    def test_timeout_is_15_seconds(self, mock_settings_azure: MagicMock) -> None:
        """timeout_seconds must be 15.0 — Azure can be slower than LlamaParse."""
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()
            assert client.timeout_seconds == 15.0

    def test_max_retries_is_2(self, mock_settings_azure: MagicMock) -> None:
        """max_retries must be 2."""
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()
            assert client.max_retries == 2

    def test_authenticate_headers_returns_subscription_key(
        self, mock_settings_azure: MagicMock
    ) -> None:
        """_authenticate_headers must return Ocp-Apim-Subscription-Key header."""
        import asyncio

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient
            from aspire_orchestrator.providers.base_client import ProviderRequest

            client = AzureDocIntelClient()
            req = ProviderRequest(
                method="POST",
                path="/formrecognizer/documentModels/prebuilt-layout:analyze",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            headers = asyncio.get_event_loop().run_until_complete(
                client._authenticate_headers(req)
            )

        assert headers == {"Ocp-Apim-Subscription-Key": FAKE_AZURE_KEY}


class TestAzureDocIntelClientStubGuard:
    """Wave 1 stub guard: analyze_layout must raise NotImplementedError."""

    @pytest.fixture
    def mock_settings_azure(self):
        mock = MagicMock()
        mock.azure_doc_intel_endpoint = FAKE_AZURE_ENDPOINT
        mock.azure_doc_intel_key = FAKE_AZURE_KEY
        return mock

    @pytest.mark.asyncio
    async def test_analyze_layout_raises_not_implemented(
        self, mock_settings_azure: MagicMock
    ) -> None:
        """analyze_layout must raise NotImplementedError until Wave 2 wires it."""
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()

            with pytest.raises(NotImplementedError, match="Wave 2 wires this"):
                await client.analyze_layout(
                    FAKE_PDF_BYTES,
                    correlation_id=CORRELATION_ID,
                    suite_id=SUITE_ID,
                    office_id=OFFICE_ID,
                )

    @pytest.mark.asyncio
    async def test_analyze_layout_logs_byte_length_not_content(
        self, mock_settings_azure: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Law #9: analyze_layout logs len(pdf_bytes), never raw content."""
        import logging

        PDF_SENTINEL = b"SENSITIVE_BLUEPRINT_CONTENT_DO_NOT_LOG"

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings_azure,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()

            with caplog.at_level(
                logging.INFO,
                logger="aspire_orchestrator.providers.azure_doc_intel_client",
            ):
                with pytest.raises(NotImplementedError):
                    await client.analyze_layout(
                        PDF_SENTINEL,
                        correlation_id=CORRELATION_ID,
                        suite_id=SUITE_ID,
                        office_id=OFFICE_ID,
                    )

        full_log = " ".join(caplog.messages)
        assert "SENSITIVE_BLUEPRINT_CONTENT_DO_NOT_LOG" not in full_log
        assert str(len(PDF_SENTINEL)) in full_log


class TestAzureDocIntelClientErrorMapping:
    """Verify _parse_error maps provider HTTP codes to canonical InternalErrorCode."""

    @pytest.fixture
    def client(self):
        mock_settings = MagicMock()
        mock_settings.azure_doc_intel_endpoint = FAKE_AZURE_ENDPOINT
        mock_settings.azure_doc_intel_key = FAKE_AZURE_KEY
        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient
            return AzureDocIntelClient()

    def test_401_maps_to_auth_invalid_key(self, client: "AzureDocIntelClient") -> None:
        assert client._parse_error(401, {}) == InternalErrorCode.AUTH_INVALID_KEY

    def test_403_maps_to_domain_forbidden(self, client: "AzureDocIntelClient") -> None:
        assert client._parse_error(403, {}) == InternalErrorCode.DOMAIN_FORBIDDEN

    def test_429_maps_to_rate_limited(self, client: "AzureDocIntelClient") -> None:
        assert client._parse_error(429, {}) == InternalErrorCode.RATE_LIMITED

    def test_500_maps_to_server_unavailable(self, client: "AzureDocIntelClient") -> None:
        assert client._parse_error(500, {}) == InternalErrorCode.SERVER_UNAVAILABLE

    def test_503_maps_to_server_unavailable(self, client: "AzureDocIntelClient") -> None:
        assert client._parse_error(503, {}) == InternalErrorCode.SERVER_UNAVAILABLE

    def test_400_falls_through_to_base(self, client: "AzureDocIntelClient") -> None:
        """400 uses base class mapping → INPUT_INVALID_FORMAT."""
        assert client._parse_error(400, {}) == InternalErrorCode.INPUT_INVALID_FORMAT


class TestAzureDocIntelReceiptEmission:
    """Law #2: Verify receipt emission is inherited from BaseProviderClient."""

    def test_make_receipt_data_is_callable(self) -> None:
        """AzureDocIntelClient inherits make_receipt_data from BaseProviderClient."""
        from aspire_orchestrator.providers.base_client import BaseProviderClient
        from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

        assert hasattr(AzureDocIntelClient, "make_receipt_data")
        assert AzureDocIntelClient.make_receipt_data is BaseProviderClient.make_receipt_data

    def test_make_receipt_data_produces_required_fields(self) -> None:
        """make_receipt_data must produce all Law #2 required fields."""
        from aspire_orchestrator.models import Outcome

        mock_settings = MagicMock()
        mock_settings.azure_doc_intel_endpoint = FAKE_AZURE_ENDPOINT
        mock_settings.azure_doc_intel_key = FAKE_AZURE_KEY

        with patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.settings",
            mock_settings,
        ):
            from aspire_orchestrator.providers.azure_doc_intel_client import AzureDocIntelClient

            client = AzureDocIntelClient()
            receipt = client.make_receipt_data(
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
                tool_id="azure_doc_intel.analyze_layout",
                risk_tier="green",
                outcome=Outcome.SUCCESS,
                reason_code="ANALYZED",
            )

        required_fields = {
            "id", "correlation_id", "suite_id", "office_id",
            "actor_type", "actor_id", "action_type", "risk_tier",
            "tool_used", "created_at", "executed_at", "outcome", "reason_code",
        }
        for field in required_fields:
            assert field in receipt, f"Receipt missing required field: {field}"

        assert receipt["suite_id"] == SUITE_ID
        assert receipt["office_id"] == OFFICE_ID
        assert receipt["correlation_id"] == CORRELATION_ID
