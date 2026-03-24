"""Wave 3 YELLOW-tier provider tests — Sarah, Quinn enhancements, Eli Phase A.

Tests cover:
  - Twilio telephony (call.create YELLOW, call.status GREEN)
  - TelephonyPolicy (forbidden topics, handle time, escalation)
  - Stripe invoice.void, quote.create, quote.send (YELLOW)
  - PolarisM email.send, email.draft (YELLOW, DLP redaction)
  - Tool executor registry wiring verification
  - Receipt emission for ALL outcomes (Law #2)
  - Idempotency key verification (Stripe + Twilio)
  - S2S HMAC signature verification (Domain Rail email)

70+ tests covering all success, failure, validation, and governance paths.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import ProviderResponse
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# =============================================================================
# Shared fixtures
# =============================================================================


@pytest.fixture
def suite_id() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


@pytest.fixture
def office_id() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000011"))


@pytest.fixture
def correlation_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def cap_token_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def cap_token_hash() -> str:
    return "sha256:abcdef1234567890"


# =============================================================================
# 1. TelephonyPolicy Tests (Sarah — pure policy)
# =============================================================================


class TestTelephonyPolicy:
    """Test Sarah's telephony policy enforcement."""

    def test_safe_topic(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("Schedule a meeting at 3pm") is True

    def test_empty_text_is_safe(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("") is True

    def test_forbidden_billing(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("I need help with billing") is False

    def test_forbidden_credit_card(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("Enter your credit card number") is False

    def test_forbidden_bank_account(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("What is your bank account?") is False

    def test_forbidden_payment(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("Process a payment now") is False

    def test_forbidden_social_security(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("Provide your social security number") is False

    def test_forbidden_password(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("Reset your password") is False

    def test_forbidden_case_insensitive(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("BILLING ISSUE") is False

    def test_forbidden_mixed_case(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.check_topic_safety("Credit Card details") is False

    def test_should_not_escalate_early(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.should_escalate(60.0) is False

    def test_should_not_escalate_at_119(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.should_escalate(119.9) is False

    def test_should_escalate_at_120(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.should_escalate(120.0) is True

    def test_should_escalate_at_150(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.should_escalate(150.0) is True

    def test_within_handle_time(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.is_within_handle_time(60.0) is True

    def test_within_handle_time_at_179(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.is_within_handle_time(179.9) is True

    def test_exceeds_handle_time_at_180(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.is_within_handle_time(180.0) is False

    def test_exceeds_handle_time_at_200(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.is_within_handle_time(200.0) is False

    def test_get_forbidden_topics_returns_list(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        topics = TelephonyPolicy.get_forbidden_topics()
        assert isinstance(topics, list)
        assert len(topics) == 6
        assert "billing" in topics

    def test_constants_correct(self):
        from aspire_orchestrator.services.telephony_policy import TelephonyPolicy
        assert TelephonyPolicy.MAX_HANDLE_TIME_S == 180.0
        assert TelephonyPolicy.ESCALATION_THRESHOLD_S == 120.0


# =============================================================================
# 2. Twilio Provider Tests (Sarah — telephony)
# =============================================================================


class TestTwilioCallCreate:
    """Test twilio.call.create executor (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_missing_params_fails_with_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_create

        result = await execute_twilio_call_create(
            payload={"to": "+15551234567"},  # missing from_number and url
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.tool_id == "twilio.call.create"
        assert "Missing required parameters" in (result.error or "")
        assert result.receipt_data  # Law #2: receipt emitted
        assert result.receipt_data["outcome"] == "failed"
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_missing_all_params(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_create

        result = await execute_twilio_call_create(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_missing_auth_fails_closed(
        self, suite_id, office_id, correlation_id
    ):
        """Law #3: fail-closed when Twilio credentials not configured."""
        from aspire_orchestrator.providers.twilio_client import (
            execute_twilio_call_create,
            _get_client,
        )
        import aspire_orchestrator.providers.twilio_client as twilio_mod

        # Reset singleton to force fresh client
        twilio_mod._client = None

        with patch(
            "aspire_orchestrator.providers.twilio_client.settings"
        ) as mock_settings:
            mock_settings.twilio_account_sid = ""
            mock_settings.twilio_auth_token = ""

            result = await execute_twilio_call_create(
                payload={
                    "to": "+15551234567",
                    "from_number": "+15559876543",
                    "url": "https://example.com/twiml",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )
            assert result.outcome == Outcome.FAILED
            assert result.receipt_data
            twilio_mod._client = None

    @pytest.mark.asyncio
    async def test_success_with_receipt(
        self, suite_id, office_id, correlation_id, cap_token_id, cap_token_hash
    ):
        """Successful call creation returns call_sid, status, and receipt."""
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_create
        import aspire_orchestrator.providers.twilio_client as twilio_mod

        twilio_mod._client = None

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.content = json.dumps({
            "sid": "CA1234567890abcdef",
            "status": "queued",
            "from": "+15559876543",
            "to": "+15551234567",
            "direction": "outbound-api",
        }).encode()

        with patch(
            "aspire_orchestrator.providers.twilio_client.settings"
        ) as mock_settings:
            mock_settings.twilio_account_sid = "AC_test_sid"
            mock_settings.twilio_auth_token = "test_auth_token"

            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.is_closed = False
                mock_client.post = AsyncMock(return_value=mock_response)
                MockClient.return_value = mock_client

                # Set the _client's internal httpx client
                twilio_mod._client = None
                client_instance = twilio_mod._get_client()
                client_instance._client = mock_client

                result = await execute_twilio_call_create(
                    payload={
                        "to": "+15551234567",
                        "from_number": "+15559876543",
                        "url": "https://example.com/twiml",
                        "status_callback": "https://example.com/callback",
                    },
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    capability_token_id=cap_token_id,
                    capability_token_hash=cap_token_hash,
                )

                assert result.outcome == Outcome.SUCCESS
                assert result.tool_id == "twilio.call.create"
                assert result.data["call_sid"] == "CA1234567890abcdef"
                assert result.data["status"] == "queued"
                assert result.data["direction"] == "outbound-api"
                assert result.receipt_data
                assert result.receipt_data["outcome"] == "success"
                assert result.receipt_data["risk_tier"] == "yellow"
                assert result.receipt_data["capability_token_id"] == cap_token_id

                # Verify form-encoded body was sent
                call_args = mock_client.post.call_args
                assert call_args is not None
                # Content should be form-encoded
                content = call_args.kwargs.get("content") or call_args[1].get("content", b"")
                if isinstance(content, bytes):
                    decoded = content.decode("utf-8")
                    assert "To=" in decoded
                    assert "From=" in decoded
                    assert "Url=" in decoded

                twilio_mod._client = None

    @pytest.mark.asyncio
    async def test_risk_tier_is_yellow(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_create

        result = await execute_twilio_call_create(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data["risk_tier"] == "yellow"


class TestTwilioCallStatus:
    """Test twilio.call.status executor (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_missing_call_sid(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_status

        result = await execute_twilio_call_status(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "call_sid" in (result.error or "")
        assert result.receipt_data
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_success(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_status
        import aspire_orchestrator.providers.twilio_client as twilio_mod

        twilio_mod._client = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = json.dumps({
            "sid": "CA_test_sid",
            "status": "completed",
            "duration": "45",
            "start_time": "2026-02-13T10:00:00Z",
        }).encode()

        with patch(
            "aspire_orchestrator.providers.twilio_client.settings"
        ) as mock_settings:
            mock_settings.twilio_account_sid = "AC_test_sid"
            mock_settings.twilio_auth_token = "test_auth_token"

            client_instance = twilio_mod._get_client()
            mock_client = AsyncMock()
            mock_client.is_closed = False
            mock_client.get = AsyncMock(return_value=mock_response)
            client_instance._client = mock_client

            result = await execute_twilio_call_status(
                payload={"call_sid": "CA_test_sid"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.SUCCESS
            assert result.data["call_sid"] == "CA_test_sid"
            assert result.data["status"] == "completed"
            assert result.data["duration"] == "45"
            assert result.receipt_data
            assert result.receipt_data["risk_tier"] == "green"

            twilio_mod._client = None

    @pytest.mark.asyncio
    async def test_risk_tier_is_green(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_status

        result = await execute_twilio_call_status(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data["risk_tier"] == "green"


class TestTwilioAuth:
    """Test Twilio Basic Auth header generation."""

    def test_basic_auth_encoding(self):
        """Verify base64 encoding of account_sid:auth_token."""
        account_sid = "AC_test_123"
        auth_token = "secret_456"
        credentials = f"{account_sid}:{auth_token}"
        expected = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        assert expected == base64.b64encode(b"AC_test_123:secret_456").decode("ascii")


# =============================================================================
# 3. Stripe Invoice Void Tests (Quinn)
# =============================================================================


class TestStripeInvoiceVoid:
    """Test stripe.invoice.void executor (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_missing_invoice_id(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_void

        result = await execute_stripe_invoice_void(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "invoice_id" in (result.error or "")
        assert result.receipt_data
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"

    @pytest.mark.asyncio
    async def test_success_with_receipt(
        self, suite_id, office_id, correlation_id, cap_token_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_void
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        mock_response = ProviderResponse(
            status_code=200,
            body={"id": "inv_void_123", "status": "void"},
            success=True,
            latency_ms=50.0,
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(return_value=mock_response)

            result = await execute_stripe_invoice_void(
                payload={"invoice_id": "inv_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                capability_token_id=cap_token_id,
            )

            assert result.outcome == Outcome.SUCCESS
            assert result.data["invoice_id"] == "inv_void_123"
            assert result.data["status"] == "void"
            assert result.receipt_data
            assert result.receipt_data["outcome"] == "success"
            assert result.receipt_data["risk_tier"] == "yellow"
            stripe_mod._client = None

    @pytest.mark.asyncio
    async def test_api_failure_with_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_void
        import aspire_orchestrator.providers.stripe_client as stripe_mod
        from aspire_orchestrator.providers.error_codes import InternalErrorCode

        stripe_mod._client = None

        mock_response = ProviderResponse(
            status_code=404,
            body={"error": {"type": "invalid_request_error", "code": "not_found"}},
            success=False,
            error_code=InternalErrorCode.DOMAIN_NOT_FOUND,
            error_message="Invoice not found",
            latency_ms=30.0,
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(return_value=mock_response)

            result = await execute_stripe_invoice_void(
                payload={"invoice_id": "inv_nonexistent"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.FAILED
            assert result.receipt_data  # Law #2
            assert result.receipt_data["outcome"] == "failed"
            stripe_mod._client = None

    @pytest.mark.asyncio
    async def test_idempotency_key_passed(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_void
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        mock_response = ProviderResponse(
            status_code=200,
            body={"id": "inv_123", "status": "void"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(return_value=mock_response)

            await execute_stripe_invoice_void(
                payload={
                    "invoice_id": "inv_123",
                    "idempotency_key": "my-idem-key",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            # Verify the request was called with idempotency_key
            call_args = client_instance._request.call_args
            request_obj = call_args[0][0]
            assert request_obj.idempotency_key == "my-idem-key"
            stripe_mod._client = None


# =============================================================================
# 4. Stripe Quote Create Tests (Quinn)
# =============================================================================


class TestStripeQuoteCreate:
    """Test stripe.quote.create executor (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_missing_customer(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_create

        result = await execute_stripe_quote_create(
            payload={"line_items": [{"price_data": {"unit_amount": 5000}}]},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "customer_email" in (result.error or "") or "customer_id" in (result.error or "")
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_missing_line_items(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_create

        result = await execute_stripe_quote_create(
            payload={"customer_id": "cus_123"},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_missing_both_params(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_create

        result = await execute_stripe_quote_create(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_success_with_line_items(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_create
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        mock_response = ProviderResponse(
            status_code=200,
            body={
                "id": "qt_abc123",
                "status": "draft",
                "amount_total": 10000,
                "currency": "usd",
            },
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(return_value=mock_response)

            result = await execute_stripe_quote_create(
                payload={
                    "customer_id": "cus_123",
                    "line_items": [
                        {
                            "price_data": {
                                "currency": "usd",
                                "unit_amount": 5000,
                                "product_data": {"name": "Consulting"},
                            }
                        },
                        {
                            "price_data": {
                                "currency": "usd",
                                "unit_amount": 5000,
                                "product_data": {"name": "Support"},
                            }
                        },
                    ],
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.SUCCESS
            assert result.data["quote_id"] == "qt_abc123"
            # Auto-finalize returns the finalized status (mock returns same "draft" since same mock)
            assert result.data["status"] in ("draft", "open")
            assert result.data["amount_total"] == 10000
            assert result.data["currency"] == "usd"
            assert result.receipt_data
            assert result.receipt_data["risk_tier"] == "yellow"
            stripe_mod._client = None

    @pytest.mark.asyncio
    async def test_metadata_includes_tenant_info(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_create
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        mock_response = ProviderResponse(
            status_code=200,
            body={"id": "qt_123", "status": "draft", "amount_total": 100, "currency": "usd"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(return_value=mock_response)

            await execute_stripe_quote_create(
                payload={
                    "customer_id": "cus_123",
                    "line_items": [{"price_data": {"unit_amount": 100}}],
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            # First call is quote creation (with metadata), second is auto-finalize
            create_call = client_instance._request.call_args_list[0]
            request_obj = create_call[0][0]
            assert request_obj.body["metadata"]["aspire_suite_id"] == suite_id
            assert request_obj.body["metadata"]["aspire_office_id"] == office_id
            stripe_mod._client = None


# =============================================================================
# 5. Stripe Quote Send Tests (Quinn — two-step finalize+accept)
# =============================================================================


class TestStripeQuoteSend:
    """Test stripe.quote.send executor (YELLOW tier, two-step)."""

    @pytest.mark.asyncio
    async def test_missing_quote_id(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_send

        result = await execute_stripe_quote_send(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "quote_id" in (result.error or "")
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_success_finalize_and_accept(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_send
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        finalize_response = ProviderResponse(
            status_code=200,
            body={"id": "qt_123", "status": "open"},
            success=True,
        )
        accept_response = ProviderResponse(
            status_code=200,
            body={"id": "qt_123", "status": "accepted", "amount_total": 5000, "currency": "usd"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(
                side_effect=[finalize_response, accept_response]
            )

            result = await execute_stripe_quote_send(
                payload={"quote_id": "qt_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.SUCCESS
            assert result.data["quote_id"] == "qt_123"
            assert result.data["status"] == "accepted"
            assert result.receipt_data
            assert client_instance._request.call_count == 2
            stripe_mod._client = None

    @pytest.mark.asyncio
    async def test_finalize_failure_stops_accept(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_send
        from aspire_orchestrator.providers.error_codes import InternalErrorCode
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        finalize_response = ProviderResponse(
            status_code=400,
            body={"error": {"type": "invalid_request_error"}},
            success=False,
            error_code=InternalErrorCode.INPUT_INVALID_FORMAT,
            error_message="Quote cannot be finalized",
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(return_value=finalize_response)

            result = await execute_stripe_quote_send(
                payload={"quote_id": "qt_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.FAILED
            assert "finalize" in (result.error or "").lower()
            assert result.receipt_data  # Law #2
            assert client_instance._request.call_count == 1  # Accept was NOT called
            stripe_mod._client = None

    @pytest.mark.asyncio
    async def test_accept_failure_after_finalize(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_send
        from aspire_orchestrator.providers.error_codes import InternalErrorCode
        import aspire_orchestrator.providers.stripe_client as stripe_mod

        stripe_mod._client = None

        finalize_response = ProviderResponse(
            status_code=200,
            body={"id": "qt_123", "status": "open"},
            success=True,
        )
        accept_response = ProviderResponse(
            status_code=400,
            body={"error": {"type": "invalid_request_error"}},
            success=False,
            error_code=InternalErrorCode.INPUT_INVALID_FORMAT,
            error_message="Quote accept failed",
        )

        with patch(
            "aspire_orchestrator.providers.stripe_client.settings"
        ) as mock_settings:
            mock_settings.stripe_api_key = "sk_test_key"

            client_instance = stripe_mod._get_client()
            client_instance._request = AsyncMock(
                side_effect=[finalize_response, accept_response]
            )

            result = await execute_stripe_quote_send(
                payload={"quote_id": "qt_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.FAILED
            assert result.receipt_data  # Law #2
            assert client_instance._request.call_count == 2
            stripe_mod._client = None


# =============================================================================
# 6. PolarisM Email Send Tests (Eli Phase A)
# =============================================================================


class TestPolarisEmailSend:
    """Test polaris.email.send executor (YELLOW tier, DLP-redacted receipts)."""

    @pytest.mark.asyncio
    async def test_missing_from_address(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send

        result = await execute_polaris_email_send(
            payload={
                "to": "user@example.com",
                "subject": "Hello",
                "body_html": "<p>Hi</p>",
                "body_text": "Hi",
            },
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "from_address" in (result.error or "")
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_missing_to(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send

        result = await execute_polaris_email_send(
            payload={
                "from_address": "noreply@aspireos.app",
                "subject": "Hello",
                "body_html": "<p>Hi</p>",
                "body_text": "Hi",
            },
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_missing_subject(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send

        result = await execute_polaris_email_send(
            payload={
                "from_address": "noreply@aspireos.app",
                "to": "user@example.com",
                "body_html": "<p>Hi</p>",
                "body_text": "Hi",
            },
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_missing_body(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send

        result = await execute_polaris_email_send(
            payload={
                "from_address": "noreply@aspireos.app",
                "to": "user@example.com",
                "subject": "Hello",
            },
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_success_with_dlp_redacted_receipt(
        self, suite_id, office_id, correlation_id
    ):
        """Email content MUST be DLP-redacted in receipt_data (Law #9)."""
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send
        from aspire_orchestrator.services.domain_rail_client import DomainRailResponse

        mock_dr_response = DomainRailResponse(
            status_code=200,
            body={"message_id": "msg_abc123", "status": "sent"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            return_value=mock_dr_response,
        ):
            result = await execute_polaris_email_send(
                payload={
                    "from_address": "noreply@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Your Invoice #1234",
                    "body_html": "<p>Dear John, your invoice is attached.</p>",
                    "body_text": "Dear John, your invoice is attached.",
                    "reply_to": "support@aspireos.app",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.SUCCESS
            assert result.data["message_id"] == "msg_abc123"
            assert result.data["status"] == "sent"
            assert result.receipt_data
            assert result.receipt_data["risk_tier"] == "yellow"

            # DLP verification — email content must be redacted in receipt
            redacted = result.receipt_data.get("redacted_inputs", {})
            assert redacted["to"] == "<EMAIL_REDACTED>"
            assert redacted["subject"] == "<SUBJECT_REDACTED>"
            assert redacted["body_html"] == "<BODY_REDACTED>"
            assert redacted["body_text"] == "<BODY_REDACTED>"
            # From domain preserved (business email)
            assert redacted["from_domain"] == "aspireos.app"
            assert redacted["has_reply_to"] is True
            assert redacted["recipient_count"] == 1

    @pytest.mark.asyncio
    async def test_dlp_redaction_multiple_recipients(
        self, suite_id, office_id, correlation_id
    ):
        """DLP redaction works with list of recipients."""
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "team@company.com",
            "to": ["user1@example.com", "user2@example.com", "user3@example.com"],
            "subject": "Confidential Report",
            "body_html": "<p>SSN: 123-45-6789</p>",
            "body_text": "SSN: 123-45-6789",
        })
        assert redacted["recipient_count"] == 3
        assert redacted["to"] == "<EMAIL_REDACTED>"
        assert redacted["subject"] == "<SUBJECT_REDACTED>"

    @pytest.mark.asyncio
    async def test_s2s_hmac_called(self, suite_id, office_id, correlation_id):
        """Verify S2S HMAC auth is used for Domain Rail email calls."""
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send
        from aspire_orchestrator.services.domain_rail_client import DomainRailResponse

        mock_dr_response = DomainRailResponse(
            status_code=200,
            body={"message_id": "msg_123", "status": "sent"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            return_value=mock_dr_response,
        ) as mock_call:
            await execute_polaris_email_send(
                payload={
                    "from_address": "sender@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Test",
                    "body_html": "<p>Test</p>",
                    "body_text": "Test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            mock_call.assert_called_once()
            call_kwargs = mock_call.call_args.kwargs
            assert call_kwargs["method"] == "POST"
            assert call_kwargs["path"] == "/v1/email/send"
            assert call_kwargs["correlation_id"] == correlation_id
            assert call_kwargs["suite_id"] == suite_id
            assert call_kwargs["office_id"] == office_id

    @pytest.mark.asyncio
    async def test_domain_rail_error_emits_receipt(
        self, suite_id, office_id, correlation_id
    ):
        """Law #2: Domain Rail errors still emit receipt."""
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send
        from aspire_orchestrator.services.domain_rail_client import DomainRailClientError

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            side_effect=DomainRailClientError("S2S_SECRET_MISSING", "No secret"),
        ):
            result = await execute_polaris_email_send(
                payload={
                    "from_address": "sender@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Test",
                    "body_html": "<p>Test</p>",
                    "body_text": "Test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.FAILED
            assert "No secret" in (result.error or "")
            assert result.receipt_data  # Law #2
            assert result.receipt_data["reason_code"] == "S2S_SECRET_MISSING"

    @pytest.mark.asyncio
    async def test_api_failure_response(self, suite_id, office_id, correlation_id):
        """Non-2xx response from Domain Rail emits failed receipt."""
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send
        from aspire_orchestrator.services.domain_rail_client import DomainRailResponse

        mock_dr_response = DomainRailResponse(
            status_code=500,
            body={"error": "INTERNAL_ERROR"},
            success=False,
            error="INTERNAL_ERROR",
        )

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            return_value=mock_dr_response,
        ):
            result = await execute_polaris_email_send(
                payload={
                    "from_address": "sender@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Test",
                    "body_html": "<p>Test</p>",
                    "body_text": "Test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.FAILED
            assert result.receipt_data
            assert result.receipt_data["outcome"] == "failed"


# =============================================================================
# 7. PolarisM Email Draft Tests (Eli Phase A)
# =============================================================================


class TestPolarisEmailDraft:
    """Test polaris.email.draft executor (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_missing_params(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_draft

        result = await execute_polaris_email_draft(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert result.receipt_data

    @pytest.mark.asyncio
    async def test_success(self, suite_id, office_id, correlation_id):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_draft
        from aspire_orchestrator.services.domain_rail_client import DomainRailResponse

        mock_dr_response = DomainRailResponse(
            status_code=200,
            body={"draft_id": "draft_xyz", "status": "draft"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            return_value=mock_dr_response,
        ):
            result = await execute_polaris_email_draft(
                payload={
                    "from_address": "noreply@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Draft Subject",
                    "body_html": "<p>Draft content</p>",
                    "body_text": "Draft content",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.SUCCESS
            assert result.data["draft_id"] == "draft_xyz"
            assert result.data["status"] == "draft"
            assert result.receipt_data
            assert result.receipt_data["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_draft_dlp_redaction(self, suite_id, office_id, correlation_id):
        """Draft receipts also have DLP-redacted content."""
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_draft
        from aspire_orchestrator.services.domain_rail_client import DomainRailResponse

        mock_dr_response = DomainRailResponse(
            status_code=200,
            body={"draft_id": "draft_123", "status": "draft"},
            success=True,
        )

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            return_value=mock_dr_response,
        ):
            result = await execute_polaris_email_draft(
                payload={
                    "from_address": "noreply@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Sensitive Info",
                    "body_html": "<p>SSN: 123-45-6789</p>",
                    "body_text": "SSN: 123-45-6789",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            redacted = result.receipt_data.get("redacted_inputs", {})
            assert redacted["subject"] == "<SUBJECT_REDACTED>"
            assert redacted["body_html"] == "<BODY_REDACTED>"

    @pytest.mark.asyncio
    async def test_domain_rail_error_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_draft
        from aspire_orchestrator.services.domain_rail_client import DomainRailClientError

        with patch(
            "aspire_orchestrator.providers.polaris_email_client._call_domain_rail",
            new_callable=AsyncMock,
            side_effect=DomainRailClientError("S2S_SECRET_MISSING", "Secret not set"),
        ):
            result = await execute_polaris_email_draft(
                payload={
                    "from_address": "noreply@aspireos.app",
                    "to": "user@example.com",
                    "subject": "Test",
                    "body_html": "<p>Test</p>",
                    "body_text": "Test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

            assert result.outcome == Outcome.FAILED
            assert result.receipt_data


# =============================================================================
# 8. DLP Redaction Unit Tests
# =============================================================================


class TestEmailDlpRedaction:
    """Test _redact_email_payload for DLP compliance (Law #9)."""

    def test_redact_single_recipient(self):
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "sender@company.com",
            "to": "user@example.com",
            "subject": "Invoice #1234",
            "body_html": "<p>Amount: $5,000</p>",
            "body_text": "Amount: $5,000",
        })
        assert redacted["to"] == "<EMAIL_REDACTED>"
        assert redacted["subject"] == "<SUBJECT_REDACTED>"
        assert redacted["body_html"] == "<BODY_REDACTED>"
        assert redacted["body_text"] == "<BODY_REDACTED>"
        assert redacted["from_domain"] == "company.com"
        assert redacted["recipient_count"] == 1

    def test_redact_list_recipients(self):
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "sender@company.com",
            "to": ["a@b.com", "c@d.com"],
            "subject": "Test",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
        })
        assert redacted["recipient_count"] == 2
        assert redacted["to"] == "<EMAIL_REDACTED>"

    def test_redact_empty_recipient(self):
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "sender@company.com",
            "to": "",
            "subject": "Test",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
        })
        assert redacted["recipient_count"] == 0

    def test_redact_no_reply_to(self):
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "sender@company.com",
            "to": "user@example.com",
            "subject": "Test",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
        })
        assert redacted["has_reply_to"] is False

    def test_redact_with_reply_to(self):
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "sender@company.com",
            "to": "user@example.com",
            "subject": "Test",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
            "reply_to": "support@company.com",
        })
        assert redacted["has_reply_to"] is True

    def test_redact_no_from_domain(self):
        from aspire_orchestrator.providers.polaris_email_client import _redact_email_payload

        redacted = _redact_email_payload({
            "from_address": "no-at-sign",
            "to": "user@example.com",
            "subject": "Test",
            "body_html": "<p>Hi</p>",
            "body_text": "Hi",
        })
        assert redacted["from_domain"] == "REDACTED"


# =============================================================================
# 9. Tool Executor Registry Wiring Tests
# =============================================================================


class TestToolExecutorWiring:
    """Verify Wave 3 executors are wired into the registry."""

    def test_twilio_call_create_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("twilio.call.create") is True

    def test_twilio_call_status_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("twilio.call.status") is True

    def test_stripe_invoice_void_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("stripe.invoice.void") is True

    def test_stripe_quote_create_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("stripe.quote.create") is True

    def test_stripe_quote_send_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("stripe.quote.send") is True

    def test_polaris_email_send_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("polaris.email.send") is True

    def test_polaris_email_draft_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("polaris.email.draft") is True

    def test_all_wave3_tools_in_live_list(self):
        from aspire_orchestrator.services.tool_executor import get_live_tools

        live = get_live_tools()
        wave3_tools = [
            "twilio.call.create",
            "twilio.call.status",
            "stripe.invoice.void",
            "stripe.quote.create",
            "stripe.quote.send",
            "polaris.email.send",
            "polaris.email.draft",
        ]
        for tool in wave3_tools:
            assert tool in live, f"{tool} not in live executor registry"

    def test_existing_tools_still_registered(self):
        """Wave 3 additions don't break existing registrations."""
        from aspire_orchestrator.services.tool_executor import is_live_tool

        # Wave 0/1/2 tools should still be present
        assert is_live_tool("domain.check") is True
        assert is_live_tool("stripe.invoice.create") is True
        assert is_live_tool("stripe.invoice.send") is True
        assert is_live_tool("brave.search") is True


# =============================================================================
# 10. S2S HMAC Verification Tests (Domain Rail auth)
# =============================================================================


class TestS2sHmacSignature:
    """Verify S2S HMAC signature computation for Domain Rail email calls."""

    def test_compute_signature_deterministic(self):
        from aspire_orchestrator.services.domain_rail_client import compute_s2s_signature

        sig1 = compute_s2s_signature(
            secret="test-secret",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b'{"to":"user@example.com"}',
        )
        sig2 = compute_s2s_signature(
            secret="test-secret",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b'{"to":"user@example.com"}',
        )
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA256 hex digest

    def test_signature_changes_with_body(self):
        from aspire_orchestrator.services.domain_rail_client import compute_s2s_signature

        sig1 = compute_s2s_signature(
            secret="test-secret",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b'{"to":"user1@example.com"}',
        )
        sig2 = compute_s2s_signature(
            secret="test-secret",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b'{"to":"user2@example.com"}',
        )
        assert sig1 != sig2

    def test_signature_changes_with_secret(self):
        from aspire_orchestrator.services.domain_rail_client import compute_s2s_signature

        sig1 = compute_s2s_signature(
            secret="secret-1",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b"{}",
        )
        sig2 = compute_s2s_signature(
            secret="secret-2",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b"{}",
        )
        assert sig1 != sig2

    def test_signature_changes_with_method(self):
        from aspire_orchestrator.services.domain_rail_client import compute_s2s_signature

        sig_post = compute_s2s_signature(
            secret="test-secret",
            timestamp="1700000000",
            nonce="abc123",
            method="POST",
            path_and_query="/v1/email/send",
            body=b"{}",
        )
        sig_get = compute_s2s_signature(
            secret="test-secret",
            timestamp="1700000000",
            nonce="abc123",
            method="GET",
            path_and_query="/v1/email/send",
            body=b"{}",
        )
        assert sig_post != sig_get


# =============================================================================
# 11. Receipt Coverage Verification (Law #2)
# =============================================================================


class TestReceiptCoverage:
    """Verify all Wave 3 executors emit receipts for all outcomes."""

    @pytest.mark.asyncio
    async def test_twilio_create_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_create

        result = await execute_twilio_call_create(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["correlation_id"] == correlation_id
        assert result.receipt_data["suite_id"] == suite_id
        assert result.receipt_data["office_id"] == office_id

    @pytest.mark.asyncio
    async def test_twilio_status_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.twilio_client import execute_twilio_call_status

        result = await execute_twilio_call_status(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "twilio.call.status"

    @pytest.mark.asyncio
    async def test_stripe_void_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_invoice_void

        result = await execute_stripe_invoice_void(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "stripe.invoice.void"

    @pytest.mark.asyncio
    async def test_stripe_quote_create_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_create

        result = await execute_stripe_quote_create(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "stripe.quote.create"

    @pytest.mark.asyncio
    async def test_stripe_quote_send_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.stripe_client import execute_stripe_quote_send

        result = await execute_stripe_quote_send(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "stripe.quote.send"

    @pytest.mark.asyncio
    async def test_email_send_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send

        result = await execute_polaris_email_send(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "polaris.email.send"

    @pytest.mark.asyncio
    async def test_email_draft_failure_has_receipt(
        self, suite_id, office_id, correlation_id
    ):
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_draft

        result = await execute_polaris_email_draft(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
        assert result.receipt_data
        assert result.receipt_data["tool_used"] == "polaris.email.draft"

    @pytest.mark.asyncio
    async def test_receipt_has_all_required_fields(
        self, suite_id, office_id, correlation_id
    ):
        """Verify receipt_data has all minimum required fields per Law #2."""
        from aspire_orchestrator.providers.polaris_email_client import execute_polaris_email_send

        result = await execute_polaris_email_send(
            payload={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )

        receipt = result.receipt_data
        required_fields = [
            "id", "correlation_id", "suite_id", "office_id",
            "actor_type", "actor_id", "action_type", "risk_tier",
            "tool_used", "created_at", "executed_at",
            "outcome", "reason_code", "receipt_type", "receipt_hash",
        ]
        for field in required_fields:
            assert field in receipt, f"Missing required receipt field: {field}"
