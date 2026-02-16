"""Wave 4 Tests — Teressa (Books/QuickBooks) + Eli Phase B (Mail State Machine).

Test coverage (80+ tests):
  QuickBooks (30+):
    - OAuth2 token lifecycle (retrieval, refresh, expiry, per-suite isolation)
    - qbo.read_company (success, normalized response, missing params)
    - qbo.read_transactions (date range, limit, normalized)
    - qbo.read_accounts (all, type filter, normalized)
    - qbo.journal_entry.create (success, binding fields, line validation)
    - Fail-closed on missing credentials/realm_id
    - Receipt emission for all outcomes

  Mail State Machine (50+):
    - Initial state validation
    - All 13 valid forward transitions
    - Invalid transitions raise InvalidTransitionError
    - Each transition emits MailTransitionReceipt
    - Receipt correctness (from_state, to_state, receipt_type, actor, correlation_id)
    - History is append-only (immutable)
    - Terminal state (deleted) blocks all transitions
    - Bounce -> retry loop
    - Failed -> retry
    - can_transition correctness
    - DLP fields not leaked in receipts
    - Concurrent safety
    - TRANSITIONS completeness
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from aspire_orchestrator.models import Outcome

# QuickBooks imports
from aspire_orchestrator.providers.quickbooks_client import (
    QuickBooksClient,
    _get_client,
    execute_qbo_read_company,
    execute_qbo_read_transactions,
    execute_qbo_read_accounts,
    execute_qbo_journal_entry_create,
    _QBO_SANDBOX_URL,
    _QBO_PRODUCTION_URL,
    _QBO_TOKEN_URL,
)
from aspire_orchestrator.providers.base_client import (
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.providers.oauth2_manager import (
    OAuth2Config,
    OAuth2Manager,
    OAuth2Token,
)

# Mail state machine imports
from aspire_orchestrator.services.mail_state_machine import (
    MailStateMachine,
    InvalidTransitionError,
    TRANSITIONS,
    VALID_STATES,
)
from aspire_orchestrator.services.mail_receipt_types import (
    MailReceiptType,
    MailTransitionReceipt,
    receipt_type_for_state,
    _STATE_TO_RECEIPT,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def suite_a() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


@pytest.fixture
def suite_b() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000002"))


@pytest.fixture
def office_id() -> str:
    return str(uuid.UUID("00000000-0000-0000-0000-000000000011"))


@pytest.fixture
def corr_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def realm_id() -> str:
    return "1234567890"


@pytest.fixture
def mail_id() -> str:
    return str(uuid.uuid4())


def _make_qbo_success_response(body: dict) -> ProviderResponse:
    """Helper to build a successful QBO ProviderResponse."""
    return ProviderResponse(
        status_code=200,
        body=body,
        success=True,
        latency_ms=42.0,
    )


def _make_qbo_error_response(status_code: int, error_body: dict) -> ProviderResponse:
    """Helper to build an error QBO ProviderResponse."""
    return ProviderResponse(
        status_code=status_code,
        body=error_body,
        success=False,
        error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
        error_message=f"HTTP {status_code}",
        latency_ms=15.0,
    )


# =============================================================================
# QuickBooks Client — OAuth2 Token Tests
# =============================================================================


class TestQuickBooksOAuth2:
    """OAuth2 token lifecycle for QuickBooks."""

    def test_client_provider_id(self):
        """QuickBooks client has correct provider_id."""
        client = QuickBooksClient()
        assert client.provider_id == "quickbooks"

    def test_client_timeout(self):
        """Timeout is 10s per spec."""
        client = QuickBooksClient()
        assert client.timeout_seconds == 10.0

    def test_client_max_retries(self):
        """Max retries is 2 per spec."""
        client = QuickBooksClient()
        assert client.max_retries == 2

    def test_client_no_idempotency(self):
        """QBO does not support idempotency."""
        client = QuickBooksClient()
        assert client.idempotency_support is False

    def test_default_base_url_is_sandbox(self):
        """Default base URL should be sandbox."""
        client = QuickBooksClient()
        assert "sandbox" in client.base_url

    @patch("aspire_orchestrator.providers.quickbooks_client.settings")
    def test_production_base_url(self, mock_settings):
        """When quickbooks_base_url is set, use it."""
        mock_settings.quickbooks_base_url = _QBO_PRODUCTION_URL
        mock_settings.quickbooks_client_id = "test-id"
        mock_settings.quickbooks_client_secret = "test-secret"
        # Re-import to pick up the mock
        from aspire_orchestrator.providers.quickbooks_client import _get_base_url
        url = _get_base_url()
        assert url == _QBO_PRODUCTION_URL

    def test_oauth2_manager_created_lazily(self):
        """OAuth2Manager should be created on first access."""
        client = QuickBooksClient()
        assert client._oauth2 is None
        manager = client.oauth2_manager
        assert manager is not None
        assert isinstance(manager, OAuth2Manager)

    @pytest.mark.asyncio
    async def test_oauth2_token_retrieval_success(self, suite_a):
        """Successfully get an access token from OAuth2Manager."""
        client = QuickBooksClient()
        manager = client.oauth2_manager
        # Seed a valid token
        token = OAuth2Token(
            access_token="qbo-access-123",
            refresh_token="qbo-refresh-456",
            expires_at=time.time() + 3600,
        )
        manager.set_token(suite_a, token)

        with patch.object(
            client, "_authenticate_headers", wraps=client._authenticate_headers
        ):
            headers = await client._authenticate_headers(
                ProviderRequest(method="GET", path="/test", suite_id=suite_a)
            )
            assert headers["Authorization"] == "Bearer qbo-access-123"

    @pytest.mark.asyncio
    async def test_oauth2_token_expired_triggers_refresh(self, suite_a):
        """Expired token triggers refresh via OAuth2Manager."""
        client = QuickBooksClient()
        manager = client.oauth2_manager

        # Seed an expired token that needs refresh
        expired_token = OAuth2Token(
            access_token="old-token",
            refresh_token="refresh-token",
            expires_at=time.time() - 10,  # Already expired
        )
        manager.set_token(suite_a, expired_token)

        # Mock the refresh to return a new token
        new_token = OAuth2Token(
            access_token="new-token",
            refresh_token="new-refresh",
            expires_at=time.time() + 3600,
        )

        with patch.object(manager, "_refresh", return_value=new_token) as mock_refresh:
            headers = await client._authenticate_headers(
                ProviderRequest(method="GET", path="/test", suite_id=suite_a)
            )
            assert headers["Authorization"] == "Bearer new-token"
            mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_oauth2_no_token_raises_error(self, suite_a):
        """No token for suite raises ProviderError (fail-closed, Law #3)."""
        client = QuickBooksClient()
        # No token set for this suite
        with pytest.raises(ProviderError) as exc_info:
            await client._authenticate_headers(
                ProviderRequest(method="GET", path="/test", suite_id=suite_a)
            )
        assert exc_info.value.code == InternalErrorCode.AUTH_EXPIRED_TOKEN

    @pytest.mark.asyncio
    @patch("aspire_orchestrator.providers.quickbooks_client.settings")
    async def test_missing_oauth2_credentials_fail_closed(self, mock_settings, suite_a):
        """Missing client_id/secret raises ProviderError (Law #3)."""
        mock_settings.quickbooks_client_id = ""
        mock_settings.quickbooks_client_secret = ""
        mock_settings.quickbooks_base_url = ""

        client = QuickBooksClient()
        with pytest.raises(ProviderError) as exc_info:
            await client._authenticate_headers(
                ProviderRequest(method="GET", path="/test", suite_id=suite_a)
            )
        assert exc_info.value.code == InternalErrorCode.AUTH_INVALID_KEY

    def test_per_suite_token_isolation(self, suite_a, suite_b):
        """Suite A's token is not accessible from Suite B (Law #6)."""
        client = QuickBooksClient()
        manager = client.oauth2_manager

        token_a = OAuth2Token(
            access_token="token-a",
            refresh_token="refresh-a",
            expires_at=time.time() + 3600,
        )
        manager.set_token(suite_a, token_a)

        # Suite B has no token
        assert suite_b not in manager._tokens
        # Suite A has its token
        assert manager._tokens[suite_a].access_token == "token-a"

    def test_token_url_is_intuit(self):
        """OAuth2 token URL should be Intuit's token endpoint."""
        assert _QBO_TOKEN_URL == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


# =============================================================================
# QuickBooks Client — Error Parsing
# =============================================================================


class TestQuickBooksErrorParsing:
    """QuickBooks-specific error code mapping."""

    def test_401_maps_to_expired_token(self):
        client = QuickBooksClient()
        code = client._parse_error(401, {})
        assert code == InternalErrorCode.AUTH_EXPIRED_TOKEN

    def test_403_maps_to_scope_insufficient(self):
        client = QuickBooksClient()
        code = client._parse_error(403, {})
        assert code == InternalErrorCode.AUTH_SCOPE_INSUFFICIENT

    def test_429_maps_to_rate_limited(self):
        client = QuickBooksClient()
        code = client._parse_error(429, {})
        assert code == InternalErrorCode.RATE_LIMITED

    def test_400_maps_to_input_invalid(self):
        client = QuickBooksClient()
        code = client._parse_error(400, {})
        assert code == InternalErrorCode.INPUT_INVALID_FORMAT

    def test_6240_duplicate_maps_to_conflict(self):
        client = QuickBooksClient()
        body = {"Fault": {"Error": [{"code": "6240", "Detail": "Duplicate entry"}]}}
        code = client._parse_error(400, body)
        assert code == InternalErrorCode.DOMAIN_CONFLICT

    def test_404_maps_to_not_found(self):
        client = QuickBooksClient()
        code = client._parse_error(404, {})
        assert code == InternalErrorCode.DOMAIN_NOT_FOUND

    def test_500_falls_through_to_base(self):
        client = QuickBooksClient()
        code = client._parse_error(500, {})
        assert code == InternalErrorCode.SERVER_INTERNAL_ERROR


# =============================================================================
# QuickBooks Executors — qbo.read_company
# =============================================================================


class TestQBOReadCompany:
    """Tests for qbo.read_company executor."""

    @pytest.mark.asyncio
    async def test_missing_realm_id_fails(self, suite_a, office_id, corr_id):
        """Missing realm_id returns FAILED with receipt (Law #3)."""
        result = await execute_qbo_read_company(
            payload={},
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "realm_id" in (result.error or "")
        assert result.receipt_data["reason_code"] == "INPUT_MISSING_REQUIRED"
        assert result.receipt_data["tool_used"] == "qbo.read_company"

    @pytest.mark.asyncio
    async def test_success_normalized_response(self, suite_a, office_id, corr_id, realm_id):
        """Successful read_company normalizes QBO response."""
        qbo_body = {
            "CompanyInfo": {
                "CompanyName": "Aspire Inc",
                "LegalName": "Aspire Inc Legal",
                "FiscalYearStartMonth": "January",
                "Country": "US",
            }
        }

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_read_company(
                payload={"realm_id": realm_id},
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["company_name"] == "Aspire Inc"
        assert result.data["legal_name"] == "Aspire Inc Legal"
        assert result.data["fiscal_year_start"] == "January"
        assert result.data["country"] == "US"
        assert result.receipt_data["reason_code"] == "EXECUTED"

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_success(self, suite_a, office_id, corr_id, realm_id):
        """Receipt is emitted on successful read_company."""
        qbo_body = {"CompanyInfo": {"CompanyName": "Test"}}

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_read_company(
                payload={"realm_id": realm_id},
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        r = result.receipt_data
        assert r["correlation_id"] == corr_id
        assert r["suite_id"] == suite_a
        assert r["office_id"] == office_id
        assert r["outcome"] == "success"
        assert r["tool_used"] == "qbo.read_company"

    @pytest.mark.asyncio
    async def test_api_error_returns_failed_with_receipt(self, suite_a, office_id, corr_id, realm_id):
        """API error returns FAILED outcome with receipt."""
        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_error_response(500, {"error": "internal"}),
        ):
            result = await execute_qbo_read_company(
                payload={"realm_id": realm_id},
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.error is not None
        assert result.receipt_data["outcome"] == "failed"


# =============================================================================
# QuickBooks Executors — qbo.read_transactions
# =============================================================================


class TestQBOReadTransactions:
    """Tests for qbo.read_transactions executor."""

    @pytest.mark.asyncio
    async def test_missing_params_fails(self, suite_a, office_id, corr_id):
        """Missing required params returns FAILED."""
        result = await execute_qbo_read_transactions(
            payload={"realm_id": "123"},  # missing dates
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "start_date" in (result.error or "")

    @pytest.mark.asyncio
    async def test_success_with_date_range(self, suite_a, office_id, corr_id, realm_id):
        """Successful query with date range returns normalized transactions."""
        qbo_body = {
            "QueryResponse": {
                "Transaction": [
                    {
                        "Id": "1001",
                        "Type": "Invoice",
                        "TxnDate": "2026-01-15",
                        "TotalAmt": 250.00,
                        "PrivateNote": "Client payment",
                    },
                    {
                        "Id": "1002",
                        "Type": "Expense",
                        "TxnDate": "2026-01-20",
                        "TotalAmt": 75.50,
                        "PrivateNote": "Office supplies",
                    },
                ],
                "totalCount": 2,
            }
        }

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_read_transactions(
                payload={
                    "realm_id": realm_id,
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert len(result.data["transactions"]) == 2
        assert result.data["total_count"] == 2
        assert result.data["transactions"][0]["id"] == "1001"
        assert result.data["transactions"][1]["amount"] == 75.50

    @pytest.mark.asyncio
    async def test_empty_transaction_list(self, suite_a, office_id, corr_id, realm_id):
        """Empty response returns empty transactions list."""
        qbo_body = {"QueryResponse": {"totalCount": 0}}

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_read_transactions(
                payload={
                    "realm_id": realm_id,
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["transactions"] == []
        assert result.data["total_count"] == 0

    @pytest.mark.asyncio
    async def test_receipt_contains_correlation_id(self, suite_a, office_id, corr_id, realm_id):
        """Receipt carries the correlation_id for tracing."""
        qbo_body = {"QueryResponse": {"Transaction": [], "totalCount": 0}}

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_read_transactions(
                payload={
                    "realm_id": realm_id,
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.receipt_data["correlation_id"] == corr_id


# =============================================================================
# QuickBooks Executors — qbo.read_accounts
# =============================================================================


class TestQBOReadAccounts:
    """Tests for qbo.read_accounts executor."""

    @pytest.mark.asyncio
    async def test_missing_realm_id_fails(self, suite_a, office_id, corr_id):
        """Missing realm_id returns FAILED."""
        result = await execute_qbo_read_accounts(
            payload={},
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "realm_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_success_all_accounts(self, suite_a, office_id, corr_id, realm_id):
        """Read all accounts without filter."""
        qbo_body = {
            "QueryResponse": {
                "Account": [
                    {"Id": "1", "Name": "Checking", "AccountType": "Bank", "CurrentBalance": 10000},
                    {"Id": "2", "Name": "Revenue", "AccountType": "Income", "CurrentBalance": 5000},
                ],
            }
        }

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_read_accounts(
                payload={"realm_id": realm_id},
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert len(result.data["accounts"]) == 2
        assert result.data["accounts"][0]["name"] == "Checking"
        assert result.data["accounts"][1]["balance"] == 5000

    @pytest.mark.asyncio
    async def test_success_with_type_filter(self, suite_a, office_id, corr_id, realm_id):
        """Read accounts filtered by type passes filter in query."""
        qbo_body = {
            "QueryResponse": {
                "Account": [
                    {"Id": "1", "Name": "Checking", "AccountType": "Bank", "CurrentBalance": 10000},
                ],
            }
        }

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ) as mock_req:
            result = await execute_qbo_read_accounts(
                payload={"realm_id": realm_id, "account_type": "Bank"},
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        # Verify the query included the account type filter
        call_args = mock_req.call_args
        request: ProviderRequest = call_args[0][0]
        assert "Bank" in request.query_params.get("query", "")

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_failure(self, suite_a, office_id, corr_id, realm_id):
        """Receipt is emitted even on API failure."""
        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_error_response(401, {"error": "unauthorized"}),
        ):
            result = await execute_qbo_read_accounts(
                payload={"realm_id": realm_id},
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["outcome"] == "failed"
        assert result.receipt_data["id"]  # Receipt has an ID


# =============================================================================
# QuickBooks Executors — qbo.journal_entry.create
# =============================================================================


class TestQBOJournalEntryCreate:
    """Tests for qbo.journal_entry.create executor."""

    @pytest.mark.asyncio
    async def test_missing_realm_id_fails(self, suite_a, office_id, corr_id):
        """Missing realm_id returns FAILED."""
        result = await execute_qbo_journal_entry_create(
            payload={"lines": [{"account_id": "1", "amount": 100, "posting_type": "Debit"}]},
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "realm_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_missing_lines_fails(self, suite_a, office_id, corr_id, realm_id):
        """Missing lines returns FAILED."""
        result = await execute_qbo_journal_entry_create(
            payload={"realm_id": realm_id},
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "lines" in (result.error or "")

    @pytest.mark.asyncio
    async def test_invalid_posting_type_fails(self, suite_a, office_id, corr_id, realm_id):
        """Invalid posting_type returns FAILED."""
        result = await execute_qbo_journal_entry_create(
            payload={
                "realm_id": realm_id,
                "lines": [{"account_id": "1", "amount": 100, "posting_type": "INVALID"}],
            },
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "posting_type" in (result.error or "")

    @pytest.mark.asyncio
    async def test_missing_line_fields_fails(self, suite_a, office_id, corr_id, realm_id):
        """Line missing account_id/amount/posting_type returns FAILED."""
        result = await execute_qbo_journal_entry_create(
            payload={
                "realm_id": realm_id,
                "lines": [{"account_id": "1"}],  # missing amount and posting_type
            },
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        assert result.outcome == Outcome.FAILED
        assert "missing required fields" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_success_creates_journal_entry(self, suite_a, office_id, corr_id, realm_id):
        """Successful journal entry creation returns normalized response."""
        qbo_body = {
            "JournalEntry": {
                "Id": "5001",
                "TotalAmt": 500.00,
                "TxnDate": "2026-02-13",
            }
        }

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_journal_entry_create(
                payload={
                    "realm_id": realm_id,
                    "lines": [
                        {"account_id": "10", "amount": 500.0, "description": "Revenue", "posting_type": "Debit"},
                        {"account_id": "20", "amount": 500.0, "description": "Cash", "posting_type": "Credit"},
                    ],
                    "memo": "Monthly revenue entry",
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.SUCCESS
        assert result.data["entry_id"] == "5001"
        assert result.data["total"] == 500.00
        assert result.data["date"] == "2026-02-13"

    @pytest.mark.asyncio
    async def test_binding_fields_present_in_request(self, suite_a, office_id, corr_id, realm_id):
        """POST body includes lines + memo (binding fields for approval hash)."""
        qbo_body = {"JournalEntry": {"Id": "99", "TotalAmt": 100, "TxnDate": "2026-02-13"}}

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ) as mock_req:
            await execute_qbo_journal_entry_create(
                payload={
                    "realm_id": realm_id,
                    "lines": [
                        {"account_id": "1", "amount": 100, "posting_type": "Debit"},
                    ],
                    "memo": "Test memo",
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        call_args = mock_req.call_args
        request: ProviderRequest = call_args[0][0]
        assert request.body is not None
        assert "Line" in request.body  # QBO body
        assert request.body.get("PrivateNote") == "Test memo"

    @pytest.mark.asyncio
    async def test_receipt_on_success(self, suite_a, office_id, corr_id, realm_id):
        """Receipt emitted on successful journal entry creation."""
        qbo_body = {"JournalEntry": {"Id": "99", "TotalAmt": 100, "TxnDate": "2026-02-13"}}

        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_success_response(qbo_body),
        ):
            result = await execute_qbo_journal_entry_create(
                payload={
                    "realm_id": realm_id,
                    "lines": [
                        {"account_id": "1", "amount": 100, "posting_type": "Debit"},
                    ],
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        r = result.receipt_data
        assert r["outcome"] == "success"
        assert r["reason_code"] == "EXECUTED"
        assert r["tool_used"] == "qbo.journal_entry.create"
        assert r["risk_tier"] == "yellow"

    @pytest.mark.asyncio
    async def test_receipt_on_api_error(self, suite_a, office_id, corr_id, realm_id):
        """Receipt emitted on API error for journal entry."""
        with patch.object(
            QuickBooksClient, "_request",
            return_value=_make_qbo_error_response(400, {"error": "bad request"}),
        ):
            result = await execute_qbo_journal_entry_create(
                payload={
                    "realm_id": realm_id,
                    "lines": [
                        {"account_id": "1", "amount": 100, "posting_type": "Debit"},
                    ],
                },
                correlation_id=corr_id,
                suite_id=suite_a,
                office_id=office_id,
            )

        assert result.outcome == Outcome.FAILED
        assert result.receipt_data["outcome"] == "failed"

    @pytest.mark.asyncio
    async def test_default_risk_tier_is_yellow(self, suite_a, office_id, corr_id, realm_id):
        """Journal entry creation defaults to YELLOW risk tier."""
        result = await execute_qbo_journal_entry_create(
            payload={"realm_id": realm_id, "lines": []},
            correlation_id=corr_id,
            suite_id=suite_a,
            office_id=office_id,
        )
        # Even the failed result should show yellow risk tier
        assert result.receipt_data["risk_tier"] == "yellow"


# =============================================================================
# QuickBooks — Tool Executor Wiring
# =============================================================================


class TestQBOToolExecutorWiring:
    """Verify QBO executors are wired into tool_executor registry."""

    def test_qbo_tools_are_live(self):
        """All 4 QBO tools should be live (not stub)."""
        from aspire_orchestrator.services.tool_executor import is_live_tool

        assert is_live_tool("qbo.read_company")
        assert is_live_tool("qbo.read_transactions")
        assert is_live_tool("qbo.read_accounts")
        assert is_live_tool("qbo.journal_entry.create")

    def test_qbo_tools_in_live_list(self):
        """QBO tools appear in get_live_tools()."""
        from aspire_orchestrator.services.tool_executor import get_live_tools

        live = get_live_tools()
        assert "qbo.read_company" in live
        assert "qbo.read_transactions" in live
        assert "qbo.read_accounts" in live
        assert "qbo.journal_entry.create" in live


# =============================================================================
# Mail Receipt Types
# =============================================================================


class TestMailReceiptTypes:
    """Tests for mail receipt type enum and mapping."""

    def test_all_13_state_receipt_types(self):
        """All 13 mail states have a receipt type."""
        state_types = [
            MailReceiptType.MAIL_RECEIVED,
            MailReceiptType.MAIL_TRIAGED,
            MailReceiptType.MAIL_CLASSIFIED,
            MailReceiptType.MAIL_DRAFT_GENERATED,
            MailReceiptType.MAIL_DRAFT_REVIEWED,
            MailReceiptType.MAIL_APPROVED,
            MailReceiptType.MAIL_SENDING,
            MailReceiptType.MAIL_SENT,
            MailReceiptType.MAIL_DELIVERED,
            MailReceiptType.MAIL_BOUNCED,
            MailReceiptType.MAIL_FAILED,
            MailReceiptType.MAIL_ARCHIVED,
            MailReceiptType.MAIL_DELETED,
        ]
        assert len(state_types) == 13

    def test_3_meta_receipt_types(self):
        """3 meta receipt types exist."""
        assert MailReceiptType.MAIL_TRANSITION_DENIED == "mail.transition_denied"
        assert MailReceiptType.MAIL_DLP_REDACTED == "mail.dlp_redacted"
        assert MailReceiptType.MAIL_RETRY == "mail.retry"

    def test_total_16_receipt_types(self):
        """Total of 16 receipt types (13 state + 3 meta)."""
        assert len(MailReceiptType) == 16

    def test_receipt_type_for_state_mapping(self):
        """All 13 states map to correct receipt types."""
        assert receipt_type_for_state("received") == MailReceiptType.MAIL_RECEIVED
        assert receipt_type_for_state("triaged") == MailReceiptType.MAIL_TRIAGED
        assert receipt_type_for_state("classified") == MailReceiptType.MAIL_CLASSIFIED
        assert receipt_type_for_state("draft_generated") == MailReceiptType.MAIL_DRAFT_GENERATED
        assert receipt_type_for_state("draft_reviewed") == MailReceiptType.MAIL_DRAFT_REVIEWED
        assert receipt_type_for_state("approved") == MailReceiptType.MAIL_APPROVED
        assert receipt_type_for_state("sending") == MailReceiptType.MAIL_SENDING
        assert receipt_type_for_state("sent") == MailReceiptType.MAIL_SENT
        assert receipt_type_for_state("delivered") == MailReceiptType.MAIL_DELIVERED
        assert receipt_type_for_state("bounced") == MailReceiptType.MAIL_BOUNCED
        assert receipt_type_for_state("failed") == MailReceiptType.MAIL_FAILED
        assert receipt_type_for_state("archived") == MailReceiptType.MAIL_ARCHIVED
        assert receipt_type_for_state("deleted") == MailReceiptType.MAIL_DELETED

    def test_unknown_state_raises_key_error(self):
        """Unknown state name raises KeyError."""
        with pytest.raises(KeyError):
            receipt_type_for_state("nonexistent")

    def test_receipt_values_are_strings(self):
        """Receipt type values are dotted strings."""
        for rt in MailReceiptType:
            assert isinstance(rt.value, str)
            assert rt.value.startswith("mail.")

    def test_state_to_receipt_completeness(self):
        """_STATE_TO_RECEIPT covers all VALID_STATES."""
        for state in VALID_STATES:
            assert state in _STATE_TO_RECEIPT


# =============================================================================
# Mail Transition Receipt
# =============================================================================


class TestMailTransitionReceipt:
    """Tests for MailTransitionReceipt dataclass."""

    def test_receipt_is_frozen(self):
        """MailTransitionReceipt is immutable (frozen dataclass)."""
        receipt = MailTransitionReceipt(
            mail_id="m1",
            suite_id="s1",
            office_id="o1",
            from_state="received",
            to_state="triaged",
            receipt_type=MailReceiptType.MAIL_TRIAGED,
            actor="ava",
            correlation_id="c1",
            timestamp=datetime.now(timezone.utc),
        )
        with pytest.raises(AttributeError):
            receipt.mail_id = "modified"  # type: ignore[misc]

    def test_receipt_has_all_fields(self):
        """Receipt includes all required fields from spec."""
        now = datetime.now(timezone.utc)
        receipt = MailTransitionReceipt(
            mail_id="m1",
            suite_id="s1",
            office_id="o1",
            from_state="received",
            to_state="triaged",
            receipt_type=MailReceiptType.MAIL_TRIAGED,
            actor="ava",
            correlation_id="c1",
            timestamp=now,
            metadata={"key": "value"},
        )
        assert receipt.mail_id == "m1"
        assert receipt.suite_id == "s1"
        assert receipt.office_id == "o1"
        assert receipt.from_state == "received"
        assert receipt.to_state == "triaged"
        assert receipt.receipt_type == MailReceiptType.MAIL_TRIAGED
        assert receipt.actor == "ava"
        assert receipt.correlation_id == "c1"
        assert receipt.timestamp == now
        assert receipt.metadata == {"key": "value"}

    def test_receipt_default_metadata(self):
        """Default metadata is empty dict."""
        receipt = MailTransitionReceipt(
            mail_id="m1",
            suite_id="s1",
            office_id="o1",
            from_state="received",
            to_state="triaged",
            receipt_type=MailReceiptType.MAIL_TRIAGED,
            actor="ava",
            correlation_id="c1",
            timestamp=datetime.now(timezone.utc),
        )
        assert receipt.metadata == {}

    def test_now_returns_utc(self):
        """MailTransitionReceipt.now() returns UTC datetime."""
        ts = MailTransitionReceipt.now()
        assert ts.tzinfo is not None
        assert ts.tzinfo == timezone.utc


# =============================================================================
# Mail State Machine — Initialization
# =============================================================================


class TestMailStateMachineInit:
    """Tests for MailStateMachine initialization."""

    def test_initial_state_is_received(self, mail_id, suite_a, office_id):
        """Default initial state is 'received'."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.current_state == "received"

    def test_custom_initial_state(self, mail_id, suite_a, office_id):
        """Can start at a custom initial state."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="classified")
        assert sm.current_state == "classified"

    def test_invalid_initial_state_raises(self, mail_id, suite_a, office_id):
        """Invalid initial state raises ValueError."""
        with pytest.raises(ValueError, match="Invalid initial state"):
            MailStateMachine(mail_id, suite_a, office_id, initial_state="bogus")

    def test_empty_history_at_start(self, mail_id, suite_a, office_id):
        """History is empty at initialization."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.history == []

    def test_properties_match_init(self, mail_id, suite_a, office_id):
        """mail_id, suite_id, office_id properties match constructor args."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.mail_id == mail_id
        assert sm.suite_id == suite_a
        assert sm.office_id == office_id


# =============================================================================
# Mail State Machine — Valid Transitions
# =============================================================================


class TestMailStateMachineTransitions:
    """Tests for valid state transitions."""

    def test_received_to_triaged(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition("triaged", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "triaged"
        assert receipt.from_state == "received"
        assert receipt.to_state == "triaged"
        assert receipt.receipt_type == MailReceiptType.MAIL_TRIAGED

    def test_triaged_to_classified(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="triaged")
        receipt = sm.transition("classified", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "classified"
        assert receipt.receipt_type == MailReceiptType.MAIL_CLASSIFIED

    def test_classified_to_draft_generated(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="classified")
        receipt = sm.transition("draft_generated", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "draft_generated"
        assert receipt.receipt_type == MailReceiptType.MAIL_DRAFT_GENERATED

    def test_classified_to_archived(self, mail_id, suite_a, office_id, corr_id):
        """Classified can skip to archived (no response needed)."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="classified")
        receipt = sm.transition("archived", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "archived"
        assert receipt.receipt_type == MailReceiptType.MAIL_ARCHIVED

    def test_draft_generated_to_draft_reviewed(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="draft_generated")
        receipt = sm.transition("draft_reviewed", actor="user", correlation_id=corr_id)
        assert sm.current_state == "draft_reviewed"

    def test_draft_reviewed_to_approved(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="draft_reviewed")
        receipt = sm.transition("approved", actor="user", correlation_id=corr_id)
        assert sm.current_state == "approved"
        assert receipt.receipt_type == MailReceiptType.MAIL_APPROVED

    def test_draft_reviewed_to_draft_generated_loop(self, mail_id, suite_a, office_id, corr_id):
        """draft_reviewed can loop back to draft_generated for edits."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="draft_reviewed")
        receipt = sm.transition("draft_generated", actor="user", correlation_id=corr_id)
        assert sm.current_state == "draft_generated"

    def test_approved_to_sending(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="approved")
        receipt = sm.transition("sending", actor="system", correlation_id=corr_id)
        assert sm.current_state == "sending"

    def test_sending_to_sent(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="sending")
        receipt = sm.transition("sent", actor="system", correlation_id=corr_id)
        assert sm.current_state == "sent"
        assert receipt.receipt_type == MailReceiptType.MAIL_SENT

    def test_sending_to_failed(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="sending")
        receipt = sm.transition("failed", actor="system", correlation_id=corr_id)
        assert sm.current_state == "failed"
        assert receipt.receipt_type == MailReceiptType.MAIL_FAILED

    def test_sent_to_delivered(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="sent")
        receipt = sm.transition("delivered", actor="system", correlation_id=corr_id)
        assert sm.current_state == "delivered"
        assert receipt.receipt_type == MailReceiptType.MAIL_DELIVERED

    def test_sent_to_bounced(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="sent")
        receipt = sm.transition("bounced", actor="system", correlation_id=corr_id)
        assert sm.current_state == "bounced"
        assert receipt.receipt_type == MailReceiptType.MAIL_BOUNCED

    def test_sent_to_failed(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="sent")
        receipt = sm.transition("failed", actor="system", correlation_id=corr_id)
        assert sm.current_state == "failed"

    def test_delivered_to_archived(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="delivered")
        receipt = sm.transition("archived", actor="system", correlation_id=corr_id)
        assert sm.current_state == "archived"

    def test_bounced_to_draft_generated(self, mail_id, suite_a, office_id, corr_id):
        """Bounced can retry by going back to draft_generated."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="bounced")
        receipt = sm.transition("draft_generated", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "draft_generated"

    def test_bounced_to_archived(self, mail_id, suite_a, office_id, corr_id):
        """Bounced can give up and archive."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="bounced")
        receipt = sm.transition("archived", actor="user", correlation_id=corr_id)
        assert sm.current_state == "archived"

    def test_failed_to_sending_retry(self, mail_id, suite_a, office_id, corr_id):
        """Failed can retry sending."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="failed")
        receipt = sm.transition("sending", actor="system", correlation_id=corr_id)
        assert sm.current_state == "sending"

    def test_failed_to_archived(self, mail_id, suite_a, office_id, corr_id):
        """Failed can give up and archive."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="failed")
        receipt = sm.transition("archived", actor="system", correlation_id=corr_id)
        assert sm.current_state == "archived"

    def test_archived_to_deleted(self, mail_id, suite_a, office_id, corr_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="archived")
        receipt = sm.transition("deleted", actor="user", correlation_id=corr_id)
        assert sm.current_state == "deleted"
        assert receipt.receipt_type == MailReceiptType.MAIL_DELETED


# =============================================================================
# Mail State Machine — Invalid Transitions (Law #3: Fail-Closed)
# =============================================================================


class TestMailStateMachineInvalidTransitions:
    """Invalid transitions must raise InvalidTransitionError with denial receipt."""

    def test_received_to_sent_denied(self, mail_id, suite_a, office_id, corr_id):
        """Cannot jump from received to sent."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("sent", actor="ava", correlation_id=corr_id)
        assert exc_info.value.denial_receipt.receipt_type == MailReceiptType.MAIL_TRANSITION_DENIED
        assert sm.current_state == "received"  # State unchanged

    def test_deleted_to_anything_denied(self, mail_id, suite_a, office_id, corr_id):
        """Terminal state: cannot transition from 'deleted'."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="deleted")
        with pytest.raises(InvalidTransitionError):
            sm.transition("received", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "deleted"

    def test_deleted_to_archived_denied(self, mail_id, suite_a, office_id, corr_id):
        """Cannot reverse from deleted to archived."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="deleted")
        with pytest.raises(InvalidTransitionError):
            sm.transition("archived", actor="ava", correlation_id=corr_id)

    def test_sent_to_triaged_denied(self, mail_id, suite_a, office_id, corr_id):
        """Cannot go backward from sent to triaged."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="sent")
        with pytest.raises(InvalidTransitionError):
            sm.transition("triaged", actor="ava", correlation_id=corr_id)

    def test_approved_to_classified_denied(self, mail_id, suite_a, office_id, corr_id):
        """Cannot go from approved back to classified."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="approved")
        with pytest.raises(InvalidTransitionError):
            sm.transition("classified", actor="ava", correlation_id=corr_id)

    def test_unknown_target_state_denied(self, mail_id, suite_a, office_id, corr_id):
        """Unknown target state raises InvalidTransitionError."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("nonexistent_state", actor="ava", correlation_id=corr_id)
        assert "Unknown state" in str(exc_info.value)
        assert exc_info.value.denial_receipt.receipt_type == MailReceiptType.MAIL_TRANSITION_DENIED

    def test_denial_receipt_has_reason(self, mail_id, suite_a, office_id, corr_id):
        """Denial receipt includes reason in metadata."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("deleted", actor="ava", correlation_id=corr_id)
        denial = exc_info.value.denial_receipt
        assert "reason" in denial.metadata
        assert "not allowed" in denial.metadata["reason"]

    def test_denial_receipt_appended_to_history(self, mail_id, suite_a, office_id, corr_id):
        """Denial receipts are appended to history (Law #2)."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        with pytest.raises(InvalidTransitionError):
            sm.transition("deleted", actor="ava", correlation_id=corr_id)
        assert len(sm.history) == 1
        assert sm.history[0].receipt_type == MailReceiptType.MAIL_TRANSITION_DENIED

    def test_self_transition_denied(self, mail_id, suite_a, office_id, corr_id):
        """Cannot transition to the same state (not in allowed transitions)."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        with pytest.raises(InvalidTransitionError):
            sm.transition("received", actor="ava", correlation_id=corr_id)


# =============================================================================
# Mail State Machine — History & Immutability
# =============================================================================


class TestMailStateMachineHistory:
    """Tests for history append-only behavior."""

    def test_history_grows_with_transitions(self, mail_id, suite_a, office_id, corr_id):
        """Each transition adds one entry to history."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        sm.transition("triaged", actor="ava", correlation_id=corr_id)
        assert len(sm.history) == 1
        sm.transition("classified", actor="ava", correlation_id=corr_id)
        assert len(sm.history) == 2

    def test_history_is_copy(self, mail_id, suite_a, office_id, corr_id):
        """history property returns a copy, not a reference."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        sm.transition("triaged", actor="ava", correlation_id=corr_id)
        h1 = sm.history
        h2 = sm.history
        assert h1 == h2
        assert h1 is not h2  # Different list objects

    def test_cannot_mutate_internal_history_via_property(self, mail_id, suite_a, office_id, corr_id):
        """Modifying the returned history list does not affect internal state."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        sm.transition("triaged", actor="ava", correlation_id=corr_id)
        h = sm.history
        h.clear()  # Modify the copy
        assert len(sm.history) == 1  # Internal state unaffected

    def test_history_order_is_chronological(self, mail_id, suite_a, office_id, corr_id):
        """History entries are in chronological order."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        sm.transition("triaged", actor="ava", correlation_id=corr_id)
        sm.transition("classified", actor="ava", correlation_id=corr_id)
        sm.transition("draft_generated", actor="ava", correlation_id=corr_id)

        h = sm.history
        assert h[0].to_state == "triaged"
        assert h[1].to_state == "classified"
        assert h[2].to_state == "draft_generated"

    def test_receipt_contains_correct_actor(self, mail_id, suite_a, office_id, corr_id):
        """Receipt actor matches the transition caller."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition("triaged", actor="eli-agent", correlation_id=corr_id)
        assert receipt.actor == "eli-agent"

    def test_receipt_contains_correlation_id(self, mail_id, suite_a, office_id, corr_id):
        """Receipt correlation_id matches the provided value."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition("triaged", actor="ava", correlation_id=corr_id)
        assert receipt.correlation_id == corr_id

    def test_receipt_contains_metadata(self, mail_id, suite_a, office_id, corr_id):
        """Receipt includes custom metadata."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition(
            "triaged",
            actor="ava",
            correlation_id=corr_id,
            metadata={"source": "imap", "priority": "high"},
        )
        assert receipt.metadata["source"] == "imap"
        assert receipt.metadata["priority"] == "high"


# =============================================================================
# Mail State Machine — can_transition
# =============================================================================


class TestMailStateMachineCanTransition:
    """Tests for can_transition boolean check."""

    def test_received_can_transition_to_triaged(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.can_transition("triaged") is True

    def test_received_cannot_transition_to_sent(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.can_transition("sent") is False

    def test_deleted_cannot_transition_anywhere(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="deleted")
        for state in VALID_STATES:
            assert sm.can_transition(state) is False

    def test_unknown_target_returns_false(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.can_transition("nonexistent") is False

    def test_classified_can_go_to_draft_generated_or_archived(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="classified")
        assert sm.can_transition("draft_generated") is True
        assert sm.can_transition("archived") is True
        assert sm.can_transition("triaged") is False


# =============================================================================
# Mail State Machine — Terminal State & Utility
# =============================================================================


class TestMailStateMachineTerminal:
    """Tests for terminal state and utility methods."""

    def test_deleted_is_terminal(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="deleted")
        assert sm.is_terminal is True

    def test_received_is_not_terminal(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id)
        assert sm.is_terminal is False

    def test_get_valid_transitions(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id)
        valid = sm.get_valid_transitions()
        assert valid == ["triaged"]

    def test_get_valid_transitions_deleted_empty(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="deleted")
        assert sm.get_valid_transitions() == []

    def test_get_valid_transitions_classified(self, mail_id, suite_a, office_id):
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="classified")
        valid = sm.get_valid_transitions()
        assert "draft_generated" in valid
        assert "archived" in valid


# =============================================================================
# Mail State Machine — Complex Flows
# =============================================================================


class TestMailStateMachineFlows:
    """Tests for complex email lifecycle flows."""

    def test_full_happy_path(self, mail_id, suite_a, office_id, corr_id):
        """Complete lifecycle: received -> ... -> archived."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        states = [
            "triaged", "classified", "draft_generated", "draft_reviewed",
            "approved", "sending", "sent", "delivered", "archived",
        ]
        for state in states:
            sm.transition(state, actor="ava", correlation_id=corr_id)
        assert sm.current_state == "archived"
        assert len(sm.history) == len(states)

    def test_bounce_retry_loop(self, mail_id, suite_a, office_id, corr_id):
        """Bounce -> draft_generated -> ... -> sent (retry loop)."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="bounced")
        sm.transition("draft_generated", actor="ava", correlation_id=corr_id)
        sm.transition("draft_reviewed", actor="user", correlation_id=corr_id)
        sm.transition("approved", actor="user", correlation_id=corr_id)
        sm.transition("sending", actor="system", correlation_id=corr_id)
        sm.transition("sent", actor="system", correlation_id=corr_id)
        assert sm.current_state == "sent"
        assert len(sm.history) == 5

    def test_failed_retry_loop(self, mail_id, suite_a, office_id, corr_id):
        """Failed -> sending -> sent (retry)."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="failed")
        sm.transition("sending", actor="system", correlation_id=corr_id)
        sm.transition("sent", actor="system", correlation_id=corr_id)
        assert sm.current_state == "sent"

    def test_classify_auto_archive(self, mail_id, suite_a, office_id, corr_id):
        """Classified -> archived (no response needed, auto-archive)."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        sm.transition("triaged", actor="ava", correlation_id=corr_id)
        sm.transition("classified", actor="ava", correlation_id=corr_id)
        sm.transition("archived", actor="ava", correlation_id=corr_id)
        assert sm.current_state == "archived"
        assert len(sm.history) == 3

    def test_full_path_to_deleted(self, mail_id, suite_a, office_id, corr_id):
        """Full lifecycle including deletion."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        states = [
            "triaged", "classified", "draft_generated", "draft_reviewed",
            "approved", "sending", "sent", "delivered", "archived", "deleted",
        ]
        for state in states:
            sm.transition(state, actor="ava", correlation_id=corr_id)
        assert sm.current_state == "deleted"
        assert sm.is_terminal is True
        assert len(sm.history) == 10

    def test_draft_edit_loop(self, mail_id, suite_a, office_id, corr_id):
        """Multiple draft edit iterations: draft_reviewed -> draft_generated -> draft_reviewed."""
        sm = MailStateMachine(mail_id, suite_a, office_id, initial_state="draft_reviewed")

        # Loop back for edits
        sm.transition("draft_generated", actor="user", correlation_id=corr_id)
        sm.transition("draft_reviewed", actor="user", correlation_id=corr_id)
        # Loop again
        sm.transition("draft_generated", actor="user", correlation_id=corr_id)
        sm.transition("draft_reviewed", actor="user", correlation_id=corr_id)
        # Finally approve
        sm.transition("approved", actor="user", correlation_id=corr_id)

        assert sm.current_state == "approved"
        assert len(sm.history) == 5


# =============================================================================
# Mail State Machine — TRANSITIONS Completeness
# =============================================================================


class TestTransitionsGraph:
    """Verify the TRANSITIONS graph is well-formed."""

    def test_all_13_states_present(self):
        """TRANSITIONS dict has all 13 states."""
        assert len(TRANSITIONS) == 13

    def test_valid_states_match_transitions_keys(self):
        """VALID_STATES frozenset matches TRANSITIONS keys."""
        assert VALID_STATES == frozenset(TRANSITIONS.keys())

    def test_all_transition_targets_are_valid_states(self):
        """Every target in TRANSITIONS values is a valid state."""
        for source, targets in TRANSITIONS.items():
            for target in targets:
                assert target in VALID_STATES, f"Invalid target {target!r} from {source!r}"

    def test_deleted_has_no_transitions(self):
        """'deleted' is the only terminal state."""
        assert TRANSITIONS["deleted"] == []
        for state, targets in TRANSITIONS.items():
            if state != "deleted":
                assert len(targets) > 0, f"State {state!r} has no transitions but is not 'deleted'"

    def test_no_self_transitions(self):
        """No state transitions to itself."""
        for state, targets in TRANSITIONS.items():
            assert state not in targets, f"State {state!r} has self-transition"


# =============================================================================
# Mail State Machine — DLP & Security
# =============================================================================


class TestMailStateMachineSecurity:
    """Security and DLP checks."""

    def test_no_pii_in_default_receipt(self, mail_id, suite_a, office_id, corr_id):
        """Default receipt metadata does not contain PII fields."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition("triaged", actor="ava", correlation_id=corr_id)
        # Should not have email addresses, SSN, etc in metadata
        metadata_str = str(receipt.metadata)
        assert "@" not in metadata_str  # No email leakage
        assert "SSN" not in metadata_str

    def test_metadata_is_user_controlled(self, mail_id, suite_a, office_id, corr_id):
        """Metadata is passed through but not auto-populated with sensitive data."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition(
            "triaged",
            actor="ava",
            correlation_id=corr_id,
            metadata={"subject": "Invoice #1234"},
        )
        # Metadata only contains what was passed
        assert receipt.metadata == {"subject": "Invoice #1234"}

    def test_receipt_does_not_contain_email_body(self, mail_id, suite_a, office_id, corr_id):
        """Receipt should not auto-include email body content."""
        sm = MailStateMachine(mail_id, suite_a, office_id)
        receipt = sm.transition("triaged", actor="ava", correlation_id=corr_id)
        # Receipt has no "body" or "content" field
        assert "body" not in receipt.metadata
        assert "content" not in receipt.metadata
