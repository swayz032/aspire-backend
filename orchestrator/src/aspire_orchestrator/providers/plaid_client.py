"""Plaid Provider Client — Account and transaction data for Finn (Money Desk) skill pack.

Provider: Plaid (https://plaid.com)
Auth: Body-based — Plaid uses client_id + secret in the JSON request body (NOT headers).
      This is different from most providers which use header-based auth.
Base URL: https://production.plaid.com (prod) / https://sandbox.plaid.com (dev)
Risk tier: GREEN (accounts.get, transactions.get), RED (transfer.create)
Idempotency: Limited — Plaid uses idempotency_key in request body for transfers
Timeout: 30s (Plaid can be slow, especially for transaction aggregation)

Tools:
  - plaid.accounts.get: Get linked bank accounts (GREEN, Finn reads)
  - plaid.transactions.get: Get transaction history (GREEN, Finn reads)
  - plaid.transfer.create: Create ACH transfer (RED, Ava executes after authority approval)

IMPORTANT: Plaid auth is IN THE BODY, not in headers. _authenticate_headers() returns
empty dict; credentials are injected into the request body instead.

Per-suite access_tokens are obtained via Plaid Link and stored in finance_connections.
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


class PlaidClient(BaseProviderClient):
    """Plaid API client with body-based authentication.

    Unlike most providers, Plaid requires client_id and secret in the JSON
    request body, not in HTTP headers. _authenticate_headers() returns an empty
    dict, and credentials are injected by each executor function.
    """

    provider_id = "plaid"
    base_url = "https://production.plaid.com"
    timeout_seconds = 30.0
    max_retries = 1  # Plaid errors are rarely transient
    idempotency_support = False  # Handled per-endpoint in body

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Plaid does NOT use header-based auth — return empty dict.

        Auth credentials (client_id + secret) are injected into request body
        by each executor function. This override prevents the base class from
        failing on missing auth headers.
        """
        # Validate that credentials are configured (fail-closed, Law #3)
        if not settings.plaid_client_id or not settings.plaid_secret:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Plaid credentials not configured (ASPIRE_PLAID_CLIENT_ID, ASPIRE_PLAID_SECRET)",
                provider_id=self.provider_id,
            )
        return {}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map Plaid-specific error responses to internal error codes."""
        error_type = body.get("error_type", "")
        error_code = body.get("error_code", "")

        if error_type == "INVALID_INPUT":
            return InternalErrorCode.INPUT_INVALID_FORMAT
        if error_type == "INVALID_REQUEST":
            if "ACCESS_TOKEN" in error_code.upper() or error_code == "INVALID_ACCESS_TOKEN":
                return InternalErrorCode.AUTH_EXPIRED_TOKEN
            return InternalErrorCode.INPUT_INVALID_FORMAT
        if error_type == "ITEM_ERROR":
            if error_code == "ITEM_LOGIN_REQUIRED":
                return InternalErrorCode.AUTH_EXPIRED_TOKEN
            return InternalErrorCode.DOMAIN_FORBIDDEN
        if error_type == "RATE_LIMIT_EXCEEDED":
            return InternalErrorCode.RATE_LIMITED
        if error_type == "API_ERROR":
            return InternalErrorCode.SERVER_INTERNAL_ERROR
        if error_type == "INSTITUTION_ERROR":
            return InternalErrorCode.SERVER_UNAVAILABLE

        return super()._parse_error(status_code, body)

    def _inject_auth(self, body: dict[str, Any] | None) -> dict[str, Any]:
        """Inject Plaid client_id and secret into request body."""
        result = dict(body) if body else {}
        result["client_id"] = settings.plaid_client_id
        result["secret"] = settings.plaid_secret
        return result


# Singleton client instance (lazy init)
_client: PlaidClient | None = None


def _get_client() -> PlaidClient:
    global _client
    if _client is None:
        _client = PlaidClient()
    return _client


# =============================================================================
# Tool Executors — wired into tool_executor.py registry
# =============================================================================


async def execute_plaid_accounts_get(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute plaid.accounts.get — get linked bank accounts.

    Required payload:
      - access_token: str — per-suite Plaid access token from Link

    GREEN tier: Finn reads account data for context.
    Auth: client_id + secret injected into body (NOT headers).
    """
    client = _get_client()

    access_token = payload.get("access_token", "")
    if not access_token:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="plaid.accounts.get",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.accounts.get",
            error="Missing required parameter: access_token",
            receipt_data=receipt,
        )

    body = client._inject_auth({"access_token": access_token})

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/accounts/get",
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
        tool_id="plaid.accounts.get",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        data = response.body
        accounts = data.get("accounts", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="plaid.accounts.get",
            data={
                "accounts": [
                    {
                        "account_id": a.get("account_id", ""),
                        "name": a.get("name", ""),
                        "type": a.get("type", ""),
                        "balance": a.get("balances", {}).get("current"),
                    }
                    for a in accounts
                ],
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.accounts.get",
            error=response.error_message or f"Plaid API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_plaid_transactions_get(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute plaid.transactions.get — get transaction history.

    Required payload:
      - access_token: str — per-suite Plaid access token from Link
      - start_date: str — YYYY-MM-DD format
      - end_date: str — YYYY-MM-DD format

    GREEN tier: Finn reads transaction data for reconciliation.
    Auth: client_id + secret injected into body (NOT headers).
    """
    client = _get_client()

    access_token = payload.get("access_token", "")
    start_date = payload.get("start_date", "")
    end_date = payload.get("end_date", "")

    missing = []
    if not access_token:
        missing.append("access_token")
    if not start_date:
        missing.append("start_date")
    if not end_date:
        missing.append("end_date")

    if missing:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="plaid.transactions.get",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.transactions.get",
            error=f"Missing required parameters: {', '.join(missing)}",
            receipt_data=receipt,
        )

    body = client._inject_auth({
        "access_token": access_token,
        "start_date": start_date,
        "end_date": end_date,
    })

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/transactions/get",
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
        tool_id="plaid.transactions.get",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        data = response.body
        transactions = data.get("transactions", [])
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="plaid.transactions.get",
            data={
                "transactions": [
                    {
                        "id": t.get("transaction_id", ""),
                        "name": t.get("name", ""),
                        "amount": t.get("amount"),
                        "date": t.get("date", ""),
                        "category": t.get("category", []),
                    }
                    for t in transactions
                ],
                "total": data.get("total_transactions", len(transactions)),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.transactions.get",
            error=response.error_message or f"Plaid API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_plaid_transfer_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "red",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute plaid.transfer.create — create ACH transfer.

    Required payload:
      - access_token: str — per-suite Plaid access token
      - account_id: str — Plaid bank account ID
      - amount: str — decimal amount (e.g., "100.50")
      - description: str — transfer description
      - idempotency_key: str — required for RED tier

    RED tier: Requires spend approval binding + video presence.
    Auth: client_id + secret injected into body (NOT headers).

    Binding fields: [account_id, amount]
    """
    client = _get_client()

    access_token = payload.get("access_token", "")
    account_id = payload.get("account_id", "")
    amount = payload.get("amount")
    description = payload.get("description", "")
    idempotency_key = payload.get("idempotency_key", "")

    missing = []
    if not access_token:
        missing.append("access_token")
    if not account_id:
        missing.append("account_id")
    if amount is None:
        missing.append("amount")
    if not description:
        missing.append("description")
    if not idempotency_key:
        missing.append("idempotency_key")

    if missing:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="plaid.transfer.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.transfer.create",
            error=f"Missing required parameters: {', '.join(missing)}",
            receipt_data=receipt,
        )

    # Validate amount is positive
    try:
        amount_float = float(amount)
    except (TypeError, ValueError):
        amount_float = -1.0

    if amount_float <= 0:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="plaid.transfer.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_INVALID_FORMAT",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.transfer.create",
            error=f"amount must be a positive number, got: {amount}",
            receipt_data=receipt,
        )

    body = client._inject_auth({
        "access_token": access_token,
        "account_id": account_id,
        "type": "debit",
        "network": "ach",
        "amount": str(amount),
        "description": description,
    })

    if idempotency_key:
        body["idempotency_key"] = idempotency_key

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/transfer/create",
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
        tool_id="plaid.transfer.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    # Add binding fields for post-hoc verification
    receipt["binding_fields"] = {
        "account_id": account_id,
        "amount": str(amount),
    }

    if response.success:
        transfer = response.body.get("transfer", response.body)
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="plaid.transfer.create",
            data={
                "transfer_id": transfer.get("id", transfer.get("transfer_id", "")),
                "status": transfer.get("status", ""),
                "amount": transfer.get("amount", ""),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="plaid.transfer.create",
            error=response.error_message or f"Plaid API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
