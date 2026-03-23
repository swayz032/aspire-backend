"""Wave 5 RED Provider Tests — Plaid + Gusto (Milo Payroll).

Tests for Plaid and Gusto provider clients.
RED tier operations require explicit authority + strong confirmation UX.

Per CLAUDE.md:
  - Law #2: Every outcome generates a receipt (100% coverage)
  - Law #3: Fail-closed (missing credentials, params, tokens → deny)
  - Law #4: RED tier — explicit authority, binding fields in receipts
  - Law #7: Tools are hands — execute bounded commands, no decisions
  - Law #9: Never log secrets, PII redacted

Test categories:
  - Plaid: accounts.get, transactions.get, transfer.create (RED)
  - Gusto: read_company, read_payrolls, payroll.run (RED)
  - Each category tests: success, validation, receipt emission, fail-closed
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    CircuitBreaker,
    CircuitState,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.providers.plaid_client import (
    PlaidClient,
    execute_plaid_accounts_get,
    execute_plaid_transactions_get,
    execute_plaid_transfer_create,
    _get_client as plaid_get_client,
)
from aspire_orchestrator.providers.gusto_client import (
    GustoClient,
    execute_gusto_read_company,
    execute_gusto_read_payrolls,
    execute_gusto_payroll_run,
    _get_client as gusto_get_client,
)
from aspire_orchestrator.providers.oauth2_manager import OAuth2Config, OAuth2Manager, OAuth2Token
from aspire_orchestrator.services.tool_types import ToolExecutionResult


# =============================================================================
# Shared fixtures
# =============================================================================


@pytest.fixture
def suite_id():
    return "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def office_id():
    return "00000000-0000-0000-0000-000000000011"


@pytest.fixture
def correlation_id():
    return str(uuid.uuid4())


def _mock_success_response(body: dict, status_code: int = 200) -> ProviderResponse:
    """Build a mock successful ProviderResponse."""
    return ProviderResponse(
        status_code=status_code,
        body=body,
        success=True,
        provider_request_id="req_test_123",
        latency_ms=42.0,
    )


def _mock_error_response(
    body: dict,
    status_code: int = 400,
    error_code: InternalErrorCode = InternalErrorCode.INPUT_INVALID_FORMAT,
) -> ProviderResponse:
    """Build a mock error ProviderResponse."""
    return ProviderResponse(
        status_code=status_code,
        body=body,
        success=False,
        error_code=error_code,
        error_message=body.get("error", f"HTTP {status_code}"),
    )


# =============================================================================
# Plaid Client Tests (25+)
# =============================================================================


class TestPlaidClient:
    """Test Plaid client configuration and body-based auth."""

    def test_provider_config(self):
        client = PlaidClient()
        assert client.provider_id == "plaid"
        assert client.base_url == "https://production.plaid.com"
        assert client.timeout_seconds == 30.0
        assert client.max_retries == 1
        assert client.idempotency_support is False

    @pytest.mark.asyncio
    async def test_auth_headers_empty_with_creds(self):
        """Plaid auth is in body, NOT headers — headers must be empty dict."""
        client = PlaidClient()
        with patch("aspire_orchestrator.providers.plaid_client.settings") as mock_settings:
            mock_settings.plaid_client_id = "client_123"
            mock_settings.plaid_secret = "secret_456"
            req = ProviderRequest(method="POST", path="/test")
            headers = await client._authenticate_headers(req)
            assert headers == {}  # CRITICAL: Plaid uses body auth

    @pytest.mark.asyncio
    async def test_auth_missing_client_id_fails_closed(self):
        """Law #3: Missing Plaid credentials -> fail-closed."""
        client = PlaidClient()
        with patch("aspire_orchestrator.providers.plaid_client.settings") as mock_settings:
            mock_settings.plaid_client_id = ""
            mock_settings.plaid_secret = "has_secret"
            req = ProviderRequest(method="POST", path="/test")
            with pytest.raises(ProviderError) as exc_info:
                await client._authenticate_headers(req)
            assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY

    @pytest.mark.asyncio
    async def test_auth_missing_secret_fails_closed(self):
        client = PlaidClient()
        with patch("aspire_orchestrator.providers.plaid_client.settings") as mock_settings:
            mock_settings.plaid_client_id = "has_id"
            mock_settings.plaid_secret = ""
            req = ProviderRequest(method="POST", path="/test")
            with pytest.raises(ProviderError) as exc_info:
                await client._authenticate_headers(req)
            assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY

    def test_inject_auth_adds_credentials(self):
        client = PlaidClient()
        with patch("aspire_orchestrator.providers.plaid_client.settings") as mock_settings:
            mock_settings.plaid_client_id = "cid_test"
            mock_settings.plaid_secret = "secret_test"
            body = client._inject_auth({"access_token": "at_123"})
            assert body["client_id"] == "cid_test"
            assert body["secret"] == "secret_test"
            assert body["access_token"] == "at_123"

    def test_inject_auth_empty_body(self):
        client = PlaidClient()
        with patch("aspire_orchestrator.providers.plaid_client.settings") as mock_settings:
            mock_settings.plaid_client_id = "cid"
            mock_settings.plaid_secret = "sec"
            body = client._inject_auth(None)
            assert body["client_id"] == "cid"
            assert body["secret"] == "sec"

    def test_parse_error_invalid_access_token(self):
        client = PlaidClient()
        code = client._parse_error(400, {
            "error_type": "INVALID_REQUEST",
            "error_code": "INVALID_ACCESS_TOKEN",
        })
        assert code == InternalErrorCode.AUTH_EXPIRED_TOKEN

    def test_parse_error_item_login_required(self):
        client = PlaidClient()
        code = client._parse_error(400, {
            "error_type": "ITEM_ERROR",
            "error_code": "ITEM_LOGIN_REQUIRED",
        })
        assert code == InternalErrorCode.AUTH_EXPIRED_TOKEN

    def test_parse_error_rate_limit(self):
        client = PlaidClient()
        code = client._parse_error(429, {"error_type": "RATE_LIMIT_EXCEEDED"})
        assert code == InternalErrorCode.RATE_LIMITED


class TestPlaidAccountsGet:
    """Test plaid.accounts.get executor."""

    @pytest.mark.asyncio
    async def test_success(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({
            "accounts": [
                {
                    "account_id": "plaid_acct_1",
                    "name": "Checking",
                    "type": "depository",
                    "balances": {"current": 15000.50},
                },
                {
                    "account_id": "plaid_acct_2",
                    "name": "Savings",
                    "type": "depository",
                    "balances": {"current": 50000.00},
                },
            ],
        })

        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client._request = AsyncMock(return_value=mock_response)
            client._inject_auth = MagicMock(side_effect=lambda b: {**b, "client_id": "c", "secret": "s"})
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_accounts_get(
                payload={"access_token": "access-sandbox-123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert len(result.data["accounts"]) == 2
        assert result.data["accounts"][0]["account_id"] == "plaid_acct_1"
        assert result.data["accounts"][0]["balance"] == 15000.50

    @pytest.mark.asyncio
    async def test_missing_access_token(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_accounts_get(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "access_token" in result.error

    @pytest.mark.asyncio
    async def test_receipt_emission_on_success(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({"accounts": []})

        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client._request = AsyncMock(return_value=mock_response)
            client._inject_auth = MagicMock(side_effect=lambda b: {**b, "client_id": "c", "secret": "s"})
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_accounts_get(
                payload={"access_token": "at_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.receipt_data["outcome"] == "success"
        assert result.receipt_data["risk_tier"] == "green"
        assert result.receipt_data["tool_used"] == "plaid.accounts.get"


class TestPlaidTransactionsGet:
    """Test plaid.transactions.get executor."""

    @pytest.mark.asyncio
    async def test_success_with_date_range(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({
            "transactions": [
                {
                    "transaction_id": "txn_1",
                    "name": "Coffee Shop",
                    "amount": 5.50,
                    "date": "2026-02-10",
                    "category": ["Food", "Coffee"],
                },
            ],
            "total_transactions": 1,
        })

        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client._request = AsyncMock(return_value=mock_response)
            client._inject_auth = MagicMock(side_effect=lambda b: {**b, "client_id": "c", "secret": "s"})
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transactions_get(
                payload={
                    "access_token": "at_123",
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-13",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert len(result.data["transactions"]) == 1
        assert result.data["transactions"][0]["name"] == "Coffee Shop"
        assert result.data["total"] == 1

    @pytest.mark.asyncio
    async def test_missing_start_date(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transactions_get(
                payload={"access_token": "at_123", "end_date": "2026-02-13"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "start_date" in result.error

    @pytest.mark.asyncio
    async def test_missing_access_token(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transactions_get(
                payload={"start_date": "2026-01-01", "end_date": "2026-02-13"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "access_token" in result.error

    @pytest.mark.asyncio
    async def test_missing_all_params(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transactions_get(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "access_token" in result.error


class TestPlaidTransferCreate:
    """Test plaid.transfer.create executor (RED tier)."""

    @pytest.mark.asyncio
    async def test_success(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({
            "transfer": {
                "id": "plaid_xfer_1",
                "status": "pending",
                "amount": "100.50",
            },
        })

        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client._request = AsyncMock(return_value=mock_response)
            client._inject_auth = MagicMock(side_effect=lambda b: {**b, "client_id": "c", "secret": "s"})
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "account_id": "plaid_acct_1",
                    "amount": "100.50",
                    "description": "ACH transfer",
                    "idempotency_key": "idem_plaid_1",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["transfer_id"] == "plaid_xfer_1"
        assert result.receipt_data["risk_tier"] == "red"

    @pytest.mark.asyncio
    async def test_binding_fields_in_receipt(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({"transfer": {"id": "x", "status": "pending", "amount": "50.00"}})

        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client._request = AsyncMock(return_value=mock_response)
            client._inject_auth = MagicMock(side_effect=lambda b: {**b, "client_id": "c", "secret": "s"})
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "account_id": "acct_bind_test",
                    "amount": "50.00",
                    "description": "binding test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        binding = result.receipt_data["binding_fields"]
        assert binding["account_id"] == "acct_bind_test"
        assert binding["amount"] == "50.00"

    @pytest.mark.asyncio
    async def test_missing_access_token(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "account_id": "acct_1",
                    "amount": "100",
                    "description": "test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "access_token" in result.error

    @pytest.mark.asyncio
    async def test_missing_account_id(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "amount": "100",
                    "description": "test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "account_id" in result.error

    @pytest.mark.asyncio
    async def test_negative_amount_rejected(self, suite_id, office_id, correlation_id):
        """Evil: negative amount -> INPUT_INVALID_FORMAT."""
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "account_id": "acct_1",
                    "amount": "-50.00",
                    "description": "evil negative",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["reason_code"] == "INPUT_INVALID_FORMAT"

    @pytest.mark.asyncio
    async def test_zero_amount_rejected(self, suite_id, office_id, correlation_id):
        """Evil: zero amount -> INPUT_INVALID_FORMAT."""
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "account_id": "acct_1",
                    "amount": "0",
                    "description": "evil zero",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["reason_code"] == "INPUT_INVALID_FORMAT"

    @pytest.mark.asyncio
    async def test_non_numeric_amount_rejected(self, suite_id, office_id, correlation_id):
        """Evil: non-numeric amount -> INPUT_INVALID_FORMAT."""
        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            result = await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "account_id": "acct_1",
                    "amount": "not_a_number",
                    "description": "evil format",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["reason_code"] == "INPUT_INVALID_FORMAT"

    @pytest.mark.asyncio
    async def test_body_auth_verification(self, suite_id, office_id, correlation_id):
        """Verify Plaid credentials are injected into request BODY, not headers."""
        mock_response = _mock_success_response({"transfer": {"id": "t1", "status": "ok", "amount": "10"}})
        captured_body = {}

        def inject_and_capture(b):
            result = {**b, "client_id": "test_cid", "secret": "test_sec"}
            captured_body.update(result)
            return result

        with patch("aspire_orchestrator.providers.plaid_client._get_client") as mock_gc:
            client = MagicMock(spec=PlaidClient)
            client._request = AsyncMock(return_value=mock_response)
            client._inject_auth = MagicMock(side_effect=inject_and_capture)
            client.make_receipt_data = PlaidClient.make_receipt_data.__get__(client)
            client.provider_id = "plaid"
            mock_gc.return_value = client

            await execute_plaid_transfer_create(
                payload={
                    "access_token": "at_123",
                    "account_id": "acct_1",
                    "amount": "10.00",
                    "description": "body auth test",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert "client_id" in captured_body
        assert "secret" in captured_body
        assert captured_body["client_id"] == "test_cid"


# =============================================================================
# Gusto Client Tests (25+)
# =============================================================================


class TestGustoClient:
    """Test Gusto client configuration and OAuth2 integration."""

    def test_provider_config(self):
        client = GustoClient()
        assert client.provider_id == "gusto"
        assert client.base_url == "https://api.gusto.com/v1"
        assert client.timeout_seconds == 15.0
        assert client.max_retries == 2
        assert client.idempotency_support is True

    def test_oauth2_manager_lazy_init(self):
        client = GustoClient()
        assert client._oauth2 is None
        with patch("aspire_orchestrator.providers.gusto_client.settings") as mock_settings:
            mock_settings.gusto_client_id = "gci"
            mock_settings.gusto_client_secret = "gcs"
            manager = client.oauth2
            assert manager is not None
            assert isinstance(manager, OAuth2Manager)
            # Second access returns same instance
            assert client.oauth2 is manager

    @pytest.mark.asyncio
    async def test_auth_headers_missing_creds_fails_closed(self):
        """Law #3: Missing Gusto OAuth2 credentials -> fail-closed."""
        client = GustoClient()
        with patch("aspire_orchestrator.providers.gusto_client.settings") as mock_settings:
            mock_settings.gusto_client_id = ""
            mock_settings.gusto_client_secret = ""
            req = ProviderRequest(method="GET", path="/test", suite_id="suite-1")
            with pytest.raises(ProviderError) as exc_info:
                await client._authenticate_headers(req)
            assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY

    @pytest.mark.asyncio
    async def test_auth_missing_suite_id_fails(self):
        """Law #6: suite_id required for per-suite OAuth2 token."""
        client = GustoClient()
        with patch("aspire_orchestrator.providers.gusto_client.settings") as mock_settings:
            mock_settings.gusto_client_id = "gci"
            mock_settings.gusto_client_secret = "gcs"
            req = ProviderRequest(method="GET", path="/test", suite_id="")
            with pytest.raises(ProviderError) as exc_info:
                await client._authenticate_headers(req)
            assert exc_info.value.code == InternalErrorCode.AUTH_SCOPE_INSUFFICIENT

    @pytest.mark.asyncio
    async def test_auth_with_valid_token(self):
        """OAuth2 token returns Bearer header."""
        client = GustoClient()
        mock_manager = MagicMock(spec=OAuth2Manager)
        mock_token = OAuth2Token(
            access_token="gusto_access_valid",
            refresh_token="gusto_refresh",
            expires_at=time.time() + 3600,
        )
        mock_manager.get_token = AsyncMock(return_value=mock_token)
        client._oauth2 = mock_manager

        with patch("aspire_orchestrator.providers.gusto_client.settings") as mock_settings:
            mock_settings.gusto_client_id = "gci"
            mock_settings.gusto_client_secret = "gcs"
            req = ProviderRequest(method="GET", path="/test", suite_id="suite-1")
            headers = await client._authenticate_headers(req)

        assert headers == {"Authorization": "Bearer gusto_access_valid"}

    @pytest.mark.asyncio
    async def test_auth_no_token_for_suite_fails(self):
        """Law #6: No token for suite -> AUTH_EXPIRED_TOKEN."""
        client = GustoClient()
        mock_manager = MagicMock(spec=OAuth2Manager)
        mock_manager.get_token = AsyncMock(
            side_effect=ProviderError(
                code=InternalErrorCode.AUTH_EXPIRED_TOKEN,
                message="No token",
                provider_id="gusto",
            )
        )
        client._oauth2 = mock_manager

        with patch("aspire_orchestrator.providers.gusto_client.settings") as mock_settings:
            mock_settings.gusto_client_id = "gci"
            mock_settings.gusto_client_secret = "gcs"
            req = ProviderRequest(method="GET", path="/test", suite_id="suite-no-token")
            with pytest.raises(ProviderError) as exc_info:
                await client._authenticate_headers(req)
            assert exc_info.value.code == InternalErrorCode.AUTH_EXPIRED_TOKEN

    def test_parse_error_expired_token(self):
        client = GustoClient()
        assert client._parse_error(401, {}) == InternalErrorCode.AUTH_EXPIRED_TOKEN

    def test_parse_error_conflict(self):
        client = GustoClient()
        assert client._parse_error(409, {}) == InternalErrorCode.DOMAIN_CONFLICT

    def test_parse_error_rate_limit(self):
        client = GustoClient()
        assert client._parse_error(429, {}) == InternalErrorCode.RATE_LIMITED


class TestGustoReadCompany:
    """Test gusto.read_company executor."""

    @pytest.mark.asyncio
    async def test_success(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({
            "id": "company_123",
            "name": "Acme Corp",
            "ein": "12-3456789",
            "company_status": "active",
        })

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(return_value=mock_response)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_read_company(
                payload={"company_id": "company_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["company_id"] == "company_123"
        assert result.data["name"] == "Acme Corp"
        assert result.data["ein"] == "<EIN_REDACTED>"  # Law #9: PII redacted

    @pytest.mark.asyncio
    async def test_missing_company_id(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_read_company(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "company_id" in result.error

    @pytest.mark.asyncio
    async def test_green_tier_receipt(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_read_company(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.receipt_data["risk_tier"] == "green"


class TestGustoReadPayrolls:
    """Test gusto.read_payrolls executor."""

    @pytest.mark.asyncio
    async def test_success_list_format(self, suite_id, office_id, correlation_id):
        """Gusto can return payrolls as a top-level list."""
        mock_response = _mock_success_response([
            {
                "payroll_id": "pr_1",
                "pay_period": {"start_date": "2026-01-01", "end_date": "2026-01-15"},
                "check_date": "2026-01-20",
                "totals": {"net_pay": "45000.00", "total_tax": "12000.00"},
                "employee_count": 15,
            },
        ])

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(return_value=mock_response)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_read_payrolls(
                payload={"company_id": "company_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert len(result.data["payrolls"]) == 1
        assert result.data["payrolls"][0]["id"] == "pr_1"
        assert result.data["payrolls"][0]["employee_count"] == 15

    @pytest.mark.asyncio
    async def test_success_dict_format(self, suite_id, office_id, correlation_id):
        """Gusto can return payrolls in a dict wrapper."""
        mock_response = _mock_success_response({
            "payrolls": [
                {
                    "id": "pr_2",
                    "check_date": "2026-02-05",
                    "totals": {"net_pay": "48000", "total_tax": "13000"},
                    "employee_count": 16,
                },
            ],
        })

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(return_value=mock_response)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_read_payrolls(
                payload={"company_id": "company_123"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert len(result.data["payrolls"]) == 1

    @pytest.mark.asyncio
    async def test_with_date_filter(self, suite_id, office_id, correlation_id):
        """Date filter params are forwarded as query params."""
        mock_response = _mock_success_response({"payrolls": []})
        captured_request = {}

        async def capture_request(req):
            captured_request["path"] = req.path
            captured_request["query_params"] = req.query_params
            return mock_response

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(side_effect=capture_request)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            await execute_gusto_read_payrolls(
                payload={
                    "company_id": "co_123",
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-01",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert captured_request["query_params"]["start_date"] == "2026-01-01"
        assert captured_request["query_params"]["end_date"] == "2026-02-01"

    @pytest.mark.asyncio
    async def test_missing_company_id(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_read_payrolls(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "company_id" in result.error


class TestGustoPayrollRun:
    """Test gusto.payroll.run executor (RED tier)."""

    @pytest.mark.asyncio
    async def test_success(self, suite_id, office_id, correlation_id):
        mock_response = _mock_success_response({
            "payroll_id": "pr_submit_1",
            "status": "submitted",
            "totals": {"net_pay": "50000.00"},
            "employee_count": 20,
        })

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(return_value=mock_response)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={
                    "company_id": "company_123",
                    "payroll_id": "pr_submit_1",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["payroll_id"] == "pr_submit_1"
        assert result.data["status"] == "submitted"
        assert result.data["employee_count"] == 20
        assert result.receipt_data["risk_tier"] == "red"

    @pytest.mark.asyncio
    async def test_binding_fields_in_receipt(self, suite_id, office_id, correlation_id):
        """RED tier: binding fields for post-hoc verification."""
        mock_response = _mock_success_response({"status": "submitted"})

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(return_value=mock_response)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={
                    "company_id": "co_bind",
                    "payroll_id": "pr_bind",
                },
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        binding = result.receipt_data["binding_fields"]
        assert binding["company_id"] == "co_bind"
        assert binding["payroll_id"] == "pr_bind"

    @pytest.mark.asyncio
    async def test_missing_company_id(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={"payroll_id": "pr_1"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "company_id" in result.error

    @pytest.mark.asyncio
    async def test_missing_payroll_id(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={"company_id": "co_1"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "payroll_id" in result.error

    @pytest.mark.asyncio
    async def test_missing_both_params(self, suite_id, office_id, correlation_id):
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert "company_id" in result.error
        assert "payroll_id" in result.error

    @pytest.mark.asyncio
    async def test_red_tier_always_in_receipt(self, suite_id, office_id, correlation_id):
        """payroll.run must always be RED tier in receipt."""
        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.receipt_data["risk_tier"] == "red"

    @pytest.mark.asyncio
    async def test_api_failure_emits_receipt(self, suite_id, office_id, correlation_id):
        """API failure still emits receipt (Law #2)."""
        mock_response = _mock_error_response(
            {"error": "payroll already submitted"},
            status_code=409,
            error_code=InternalErrorCode.DOMAIN_CONFLICT,
        )

        with patch("aspire_orchestrator.providers.gusto_client._get_client") as mock_gc:
            client = MagicMock(spec=GustoClient)
            client._request = AsyncMock(return_value=mock_response)
            client.make_receipt_data = GustoClient.make_receipt_data.__get__(client)
            client.provider_id = "gusto"
            mock_gc.return_value = client

            result = await execute_gusto_payroll_run(
                payload={"company_id": "co_1", "payroll_id": "pr_1"},
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["outcome"] == "failed"
        assert "binding_fields" in result.receipt_data


# =============================================================================
# Tool Executor Registry Wiring Tests
# =============================================================================


class TestToolExecutorRegistryWiring:
    """Verify Wave 5 tools are registered in the tool executor."""

    def test_plaid_tools_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("plaid.accounts.get")
        assert is_live_tool("plaid.transactions.get")
        # M10/S3-L1: plaid.transfer.create REMOVED — money movement discontinued
        assert not is_live_tool("plaid.transfer.create")

    def test_gusto_tools_registered(self):
        from aspire_orchestrator.services.tool_executor import is_live_tool
        assert is_live_tool("gusto.read_company")
        assert is_live_tool("gusto.read_payrolls")
        assert is_live_tool("gusto.payroll.run")

    def test_all_six_tools_in_live_list(self):
        from aspire_orchestrator.services.tool_executor import get_live_tools
        live = get_live_tools()
        # M10/S3-L1: plaid.transfer.create REMOVED — money movement discontinued
        wave5_tools = [
            "plaid.accounts.get", "plaid.transactions.get",
            "gusto.read_company", "gusto.read_payrolls", "gusto.payroll.run",
        ]
        for tool in wave5_tools:
            assert tool in live, f"{tool} not found in live executors"
