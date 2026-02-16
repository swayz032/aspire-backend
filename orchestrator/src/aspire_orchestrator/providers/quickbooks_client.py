"""QuickBooks Online Provider Client — Bookkeeping for Teressa (Books) skill pack.

Provider: QuickBooks Online REST API
Auth: OAuth2 Bearer token via OAuth2Manager (per-suite tokens, Law #6)
  - config: client_id=settings.quickbooks_client_id, client_secret=settings.quickbooks_client_secret
  - token_url: "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
  - Per-suite tokens stored in finance_connections (tenant isolation via OAuth2Manager)
Risk tiers: GREEN (read), YELLOW (write)
Idempotency: Not supported by QBO REST API
Timeout: 10s, max retries: 2

Tools:
  - qbo.read_company:         Read company info (GREEN)
  - qbo.read_transactions:    Read transactions with date range (GREEN)
  - qbo.read_accounts:        Read chart of accounts (GREEN)
  - qbo.journal_entry.create: Create journal entry (YELLOW, binding_fields=[lines, memo])

Per policy_matrix.yaml:
  qbo.read_company: GREEN, no binding fields
  qbo.read_transactions: GREEN, no binding fields
  qbo.read_accounts: GREEN, no binding fields
  qbo.journal_entry.create: YELLOW, binding_fields=[lines, memo]

QuickBooks API model:
  - Each suite has a QuickBooks "realm_id" (company ID), stored per-suite
  - API calls use /company/{realm_id}/ path prefix (Law #6 tenant scoping)
  - All queries use QuickBooks Query Language via GET with ?query= parameter
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.providers.oauth2_manager import (
    OAuth2Config,
    OAuth2Manager,
)
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)

# QuickBooks base URLs
_QBO_SANDBOX_URL = "https://sandbox-quickbooks.api.intuit.com/v3"
_QBO_PRODUCTION_URL = "https://quickbooks.api.intuit.com/v3"

# QuickBooks OAuth2 token endpoint
_QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


def _get_base_url() -> str:
    """Resolve QuickBooks base URL from settings (default: sandbox)."""
    url = getattr(settings, "quickbooks_base_url", "")
    if url:
        return url.rstrip("/")
    return _QBO_SANDBOX_URL


def _build_oauth2_config() -> OAuth2Config:
    """Build OAuth2Config for QuickBooks from settings."""
    return OAuth2Config(
        provider_id="quickbooks",
        client_id=settings.quickbooks_client_id,
        client_secret=settings.quickbooks_client_secret,
        token_url=_QBO_TOKEN_URL,
        scopes=["com.intuit.quickbooks.accounting"],
        rotate_refresh_token=True,  # QBO rotates refresh tokens
    )


class QuickBooksClient(BaseProviderClient):
    """QuickBooks Online API client with OAuth2 per-suite token management."""

    provider_id = "quickbooks"
    base_url = _QBO_SANDBOX_URL  # Set at instance init from settings
    timeout_seconds = 10.0
    max_retries = 2
    idempotency_support = False  # QBO does not support idempotency keys

    def __init__(self) -> None:
        super().__init__()
        self.base_url = _get_base_url()
        self._oauth2: OAuth2Manager | None = None

    def _get_oauth2_manager(self) -> OAuth2Manager:
        """Lazy-init OAuth2Manager (avoid constructing with empty credentials at import)."""
        if self._oauth2 is None:
            self._oauth2 = OAuth2Manager(_build_oauth2_config())
        return self._oauth2

    @property
    def oauth2_manager(self) -> OAuth2Manager:
        """Public access to the OAuth2Manager for token setup."""
        return self._get_oauth2_manager()

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Get OAuth2 Bearer token from OAuth2Manager (per-suite, Law #6)."""
        if not settings.quickbooks_client_id or not settings.quickbooks_client_secret:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="QuickBooks OAuth2 credentials not configured "
                "(ASPIRE_QUICKBOOKS_CLIENT_ID / ASPIRE_QUICKBOOKS_CLIENT_SECRET)",
                provider_id=self.provider_id,
            )

        manager = self._get_oauth2_manager()
        token = await manager.get_token(request.suite_id)
        return {"Authorization": f"Bearer {token.access_token}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map QuickBooks error responses to internal error codes."""
        # QBO wraps errors in a Fault object
        fault = body.get("Fault", {})
        errors = fault.get("Error", [])
        error_detail = errors[0].get("Detail", "") if errors else ""
        error_code = errors[0].get("code", "") if errors else ""

        if status_code == 401:
            return InternalErrorCode.AUTH_EXPIRED_TOKEN
        if status_code == 403:
            return InternalErrorCode.AUTH_SCOPE_INSUFFICIENT
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        if error_code == "6240":  # Duplicate
            return InternalErrorCode.DOMAIN_CONFLICT
        if status_code == 404 or "not found" in error_detail.lower():
            return InternalErrorCode.DOMAIN_NOT_FOUND
        if status_code == 400:
            return InternalErrorCode.INPUT_INVALID_FORMAT

        return super()._parse_error(status_code, body)


# =============================================================================
# Module-level singleton (lazy init pattern per Stripe client)
# =============================================================================

_client: QuickBooksClient | None = None


def _get_client() -> QuickBooksClient:
    global _client
    if _client is None:
        _client = QuickBooksClient()
    return _client


# =============================================================================
# GREEN Executors — Teressa reads (no approval required)
# =============================================================================


async def execute_qbo_read_company(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute qbo.read_company — read QuickBooks company info.

    Required payload:
      - realm_id: str — QuickBooks company ID (stored per-suite)

    Response: {company_name, legal_name, fiscal_year_start, country}
    """
    client = _get_client()

    realm_id = payload.get("realm_id", "")
    if not realm_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="qbo.read_company",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.read_company",
            error="Missing required parameter: realm_id",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/company/{realm_id}/companyinfo/{realm_id}",
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
        tool_id="qbo.read_company",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        info = response.body.get("CompanyInfo", response.body)
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="qbo.read_company",
            data={
                "company_name": info.get("CompanyName", ""),
                "legal_name": info.get("LegalName", ""),
                "fiscal_year_start": info.get("FiscalYearStartMonth", ""),
                "country": info.get("Country", ""),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.read_company",
            error=response.error_message or f"QuickBooks API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_qbo_read_transactions(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute qbo.read_transactions — query transactions with date range.

    Required payload:
      - realm_id: str — QuickBooks company ID
      - start_date: str — ISO date (YYYY-MM-DD)
      - end_date: str — ISO date (YYYY-MM-DD)

    Optional payload:
      - limit: int — max rows (default 100)

    Response: {transactions: [{id, type, date, amount, memo}], total_count}
    """
    client = _get_client()

    realm_id = payload.get("realm_id", "")
    start_date = payload.get("start_date", "")
    end_date = payload.get("end_date", "")

    if not realm_id or not start_date or not end_date:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="qbo.read_transactions",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.read_transactions",
            error="Missing required parameters: realm_id, start_date, end_date",
            receipt_data=receipt,
        )

    limit = payload.get("limit", 100)
    query = (
        f"SELECT * FROM Transaction WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' MAXRESULTS {limit}"
    )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/company/{realm_id}/query",
            query_params={"query": query},
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
        tool_id="qbo.read_transactions",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        qr = response.body.get("QueryResponse", {})
        raw_txns = qr.get("Transaction", qr.get("transaction", []))
        if not isinstance(raw_txns, list):
            raw_txns = []

        transactions = [
            {
                "id": t.get("Id", t.get("id", "")),
                "type": t.get("Type", t.get("type", "")),
                "date": t.get("TxnDate", t.get("date", "")),
                "amount": t.get("TotalAmt", t.get("amount", 0)),
                "memo": t.get("PrivateNote", t.get("memo", "")),
            }
            for t in raw_txns
        ]

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="qbo.read_transactions",
            data={
                "transactions": transactions,
                "total_count": qr.get("totalCount", len(transactions)),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.read_transactions",
            error=response.error_message or f"QuickBooks API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_qbo_read_accounts(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute qbo.read_accounts — read chart of accounts.

    Required payload:
      - realm_id: str — QuickBooks company ID

    Optional payload:
      - account_type: str — filter by account type (e.g., "Bank", "Expense", "Revenue")

    Response: {accounts: [{id, name, type, balance}]}
    """
    client = _get_client()

    realm_id = payload.get("realm_id", "")
    if not realm_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="qbo.read_accounts",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.read_accounts",
            error="Missing required parameter: realm_id",
            receipt_data=receipt,
        )

    account_type = payload.get("account_type", "")
    if account_type:
        query = f"SELECT * FROM Account WHERE AccountType = '{account_type}'"
    else:
        query = "SELECT * FROM Account"

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/company/{realm_id}/query",
            query_params={"query": query},
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
        tool_id="qbo.read_accounts",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        qr = response.body.get("QueryResponse", {})
        raw_accounts = qr.get("Account", [])
        if not isinstance(raw_accounts, list):
            raw_accounts = []

        accounts = [
            {
                "id": a.get("Id", ""),
                "name": a.get("Name", ""),
                "type": a.get("AccountType", ""),
                "balance": a.get("CurrentBalance", 0),
            }
            for a in raw_accounts
        ]

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="qbo.read_accounts",
            data={"accounts": accounts},
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.read_accounts",
            error=response.error_message or f"QuickBooks API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


# =============================================================================
# YELLOW Executors — Teressa writes (require user approval)
# =============================================================================


async def execute_qbo_journal_entry_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute qbo.journal_entry.create — create a journal entry in QuickBooks.

    Required payload:
      - realm_id: str — QuickBooks company ID
      - lines: list[dict] — journal entry lines, each with:
          - account_id: str
          - amount: float
          - description: str
          - posting_type: "Debit" | "Credit"

    Optional payload:
      - memo: str — journal entry memo/note

    Binding fields (for approval hash): [lines, memo]
    Response: {entry_id, total, date}
    """
    client = _get_client()

    realm_id = payload.get("realm_id", "")
    lines = payload.get("lines", [])

    if not realm_id or not lines:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="qbo.journal_entry.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.journal_entry.create",
            error="Missing required parameters: realm_id, lines",
            receipt_data=receipt,
        )

    # Validate lines have required fields
    for i, line in enumerate(lines):
        if not line.get("account_id") or line.get("amount") is None or not line.get("posting_type"):
            receipt = client.make_receipt_data(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id="qbo.journal_entry.create",
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code="INPUT_INVALID_FORMAT",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id="qbo.journal_entry.create",
                error=f"Line {i}: missing required fields (account_id, amount, posting_type)",
                receipt_data=receipt,
            )
        if line["posting_type"] not in ("Debit", "Credit"):
            receipt = client.make_receipt_data(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id="qbo.journal_entry.create",
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code="INPUT_INVALID_FORMAT",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id="qbo.journal_entry.create",
                error=f"Line {i}: posting_type must be 'Debit' or 'Credit', got '{line['posting_type']}'",
                receipt_data=receipt,
            )

    # Build QBO JournalEntry body
    qbo_lines = []
    for line in lines:
        je_line: dict[str, Any] = {
            "DetailType": "JournalEntryLineDetail",
            "Amount": abs(float(line["amount"])),
            "Description": line.get("description", ""),
            "JournalEntryLineDetail": {
                "PostingType": line["posting_type"],
                "AccountRef": {"value": line["account_id"]},
            },
        }
        qbo_lines.append(je_line)

    body: dict[str, Any] = {"Line": qbo_lines}
    memo = payload.get("memo", "")
    if memo:
        body["PrivateNote"] = memo

    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/company/{realm_id}/journalentry",
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
        tool_id="qbo.journal_entry.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        je = response.body.get("JournalEntry", response.body)
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="qbo.journal_entry.create",
            data={
                "entry_id": je.get("Id", ""),
                "total": je.get("TotalAmt", 0),
                "date": je.get("TxnDate", ""),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="qbo.journal_entry.create",
            error=response.error_message or f"QuickBooks API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
