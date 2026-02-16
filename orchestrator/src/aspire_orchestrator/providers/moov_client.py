"""Moov Financial Provider Client — Payment transfers for Finn (Money Desk) skill pack.

Provider: Moov Financial (https://api.moov.io)
Auth: API key (Bearer token) — Moov uses OAuth2 client credentials;
      for Phase 2 simplicity, API key in Authorization header.
Risk tier: GREEN (account.read, transfer.status), RED (transfer.create)
Idempotency: Yes — Moov supports X-Idempotency-Key header
Timeout: 15s (financial operations need extra headroom)

Tools:
  - moov.account.read: Read account details (GREEN, Finn reads)
  - moov.transfer.create: Create money transfer (RED, Ava executes after authority approval)
  - moov.transfer.status: Check transfer status (GREEN, Finn reads)

Per CLAUDE.md Law #4: transfer.create is RED tier — requires explicit authority + strong
confirmation UX (binding actions: money). This is NOT autonomous.

Per CLAUDE.md Law #7: Moov client is a "hand" — it executes bounded commands.
Finn (agent) proposes transfers via Authority Queue; Ava (orchestrator) executes here.
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


class MoovClient(BaseProviderClient):
    """Moov Financial API client for payment transfers."""

    provider_id = "moov"
    base_url = "https://api.moov.io"
    timeout_seconds = 15.0
    max_retries = 2
    idempotency_support = True

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.moov_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Moov API key not configured (ASPIRE_MOOV_API_KEY)",
                provider_id=self.provider_id,
            )
        return {"Authorization": f"Bearer {api_key}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map Moov-specific error responses to internal error codes."""
        error = body.get("error", "")
        if isinstance(error, dict):
            error = error.get("message", "")

        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.DOMAIN_FORBIDDEN
        if status_code == 404:
            return InternalErrorCode.DOMAIN_NOT_FOUND
        if status_code == 409:
            return InternalErrorCode.DOMAIN_IDEMPOTENCY_CONFLICT
        if status_code == 422:
            if "insufficient" in str(error).lower():
                return InternalErrorCode.DOMAIN_INSUFFICIENT_FUNDS
            return InternalErrorCode.INPUT_CONSTRAINT_VIOLATED
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


# Singleton client instance (lazy init)
_client: MoovClient | None = None


def _get_client() -> MoovClient:
    global _client
    if _client is None:
        _client = MoovClient()
    return _client


# =============================================================================
# Tool Executors — wired into tool_executor.py registry
# =============================================================================


async def execute_moov_account_read(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute moov.account.read — read account details.

    Required payload:
      - account_id: str — Moov account ID

    GREEN tier: Finn reads account data for context; no approval needed.
    """
    client = _get_client()

    account_id = payload.get("account_id", "")
    if not account_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="moov.account.read",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.account.read",
            error="Missing required parameter: account_id",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/accounts/{account_id}",
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
        tool_id="moov.account.read",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        account = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="moov.account.read",
            data={
                "account_id": account.get("accountID", account.get("account_id", "")),
                "display_name": account.get("displayName", account.get("display_name", "")),
                "status": account.get("status", ""),
                "capabilities": account.get("capabilities", []),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.account.read",
            error=response.error_message or f"Moov API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_moov_transfer_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "red",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute moov.transfer.create — create a money transfer.

    Required payload:
      - source_account_id: str
      - destination_account_id: str
      - amount_cents: int (must be > 0)
      - currency: str (3-letter ISO)
      - description: str
      - idempotency_key: str (required for RED tier financial operations)

    RED tier: Requires spend approval binding. Only Ava (orchestrator) calls this
    after user approves in the Authority Queue with video presence.

    Binding fields: [source_account_id, destination_account_id, amount_cents, currency]
    Post-hoc verification uses these to ensure approved params match executed params.
    """
    client = _get_client()

    source = payload.get("source_account_id", "")
    destination = payload.get("destination_account_id", "")
    amount_cents = payload.get("amount_cents")
    currency = payload.get("currency", "")
    description = payload.get("description", "")
    idempotency_key = payload.get("idempotency_key")

    # Validate required fields
    missing = []
    if not source:
        missing.append("source_account_id")
    if not destination:
        missing.append("destination_account_id")
    if amount_cents is None:
        missing.append("amount_cents")
    if not currency:
        missing.append("currency")
    if not description:
        missing.append("description")

    if missing:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="moov.transfer.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.transfer.create",
            error=f"Missing required parameters: {', '.join(missing)}",
            receipt_data=receipt,
        )

    # Validate amount is positive integer (evil test: negative/zero amounts)
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="moov.transfer.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_INVALID_FORMAT",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.transfer.create",
            error=f"amount_cents must be a positive integer, got: {amount_cents}",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "source": {"accountID": source},
        "destination": {"accountID": destination},
        "amount": {
            "value": amount_cents,
            "currency": currency.upper(),
        },
        "description": description,
    }

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/transfers",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=idempotency_key,
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
        tool_id="moov.transfer.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    # Add binding fields to receipt for post-hoc verification
    receipt["binding_fields"] = {
        "source_account_id": source,
        "destination_account_id": destination,
        "amount_cents": amount_cents,
        "currency": currency.upper(),
    }

    if response.success:
        transfer = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="moov.transfer.create",
            data={
                "transfer_id": transfer.get("transferID", transfer.get("transfer_id", "")),
                "status": transfer.get("status", ""),
                "amount": transfer.get("amount", {}),
                "created_at": transfer.get("createdOn", transfer.get("created_at", "")),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.transfer.create",
            error=response.error_message or f"Moov API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_moov_transfer_status(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute moov.transfer.status — check transfer status.

    Required payload:
      - transfer_id: str — Moov transfer ID

    GREEN tier: Read-only status check.
    """
    client = _get_client()

    transfer_id = payload.get("transfer_id", "")
    if not transfer_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="moov.transfer.status",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.transfer.status",
            error="Missing required parameter: transfer_id",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/transfers/{transfer_id}",
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
        tool_id="moov.transfer.status",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        transfer = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="moov.transfer.status",
            data={
                "transfer_id": transfer.get("transferID", transfer.get("transfer_id", "")),
                "status": transfer.get("status", ""),
                "amount": transfer.get("amount", {}),
                "source": transfer.get("source", {}),
                "destination": transfer.get("destination", {}),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="moov.transfer.status",
            error=response.error_message or f"Moov API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
