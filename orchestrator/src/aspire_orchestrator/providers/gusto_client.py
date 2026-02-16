"""Gusto Payroll Provider Client — Payroll for Milo skill pack.

Provider: Gusto (https://api.gusto.com/v1)
Auth: OAuth2 Bearer token via OAuth2Manager — per-suite tokens (tenant isolation)
      config: client_id, client_secret, token_url: https://api.gusto.com/oauth/token
Risk tier: GREEN (read_company, read_payrolls), RED (payroll.run)
Idempotency: Yes — Gusto has native idempotency support
Timeout: 15s

Tools:
  - gusto.read_company: Read company details (GREEN, Milo reads)
  - gusto.read_payrolls: Read payroll history (GREEN, Milo reads)
  - gusto.payroll.run: Submit payroll for processing (RED, Ava executes)

Per CLAUDE.md Law #4: payroll.run is RED tier — requires explicit authority + video
presence (binding actions: money, irreversible payroll processing).

Per CLAUDE.md Law #7: Gusto client is a "hand" — it executes bounded commands.
Milo (agent) proposes payroll runs via Authority Queue; Ava (orchestrator) executes here.

OAuth2 flow: Same pattern as QuickBooks (see oauth2_manager.py).
Per-suite tokens stored in finance_connections; access tokens cached in memory.
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
from aspire_orchestrator.providers.oauth2_manager import OAuth2Config, OAuth2Manager
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


def _make_oauth2_config() -> OAuth2Config:
    """Build Gusto OAuth2 configuration from settings."""
    return OAuth2Config(
        provider_id="gusto",
        client_id=settings.gusto_client_id,
        client_secret=settings.gusto_client_secret,
        token_url="https://api.gusto.com/oauth/token",
        authorize_url="https://api.gusto.com/oauth/authorize",
        scopes=["companies:read", "payrolls:read", "payrolls:run"],
        rotate_refresh_token=True,
    )


class GustoClient(BaseProviderClient):
    """Gusto Payroll API client with OAuth2 token management.

    Uses OAuth2Manager for per-suite token refresh (same pattern as QuickBooks).
    """

    provider_id = "gusto"
    base_url = "https://api.gusto.com/v1"
    timeout_seconds = 15.0
    max_retries = 2
    idempotency_support = True

    def __init__(self) -> None:
        super().__init__()
        self._oauth2: OAuth2Manager | None = None

    @property
    def oauth2(self) -> OAuth2Manager:
        """Get or create the OAuth2 manager (lazy init)."""
        if self._oauth2 is None:
            self._oauth2 = OAuth2Manager(_make_oauth2_config())
        return self._oauth2

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Get OAuth2 Bearer token for the requesting suite."""
        if not settings.gusto_client_id or not settings.gusto_client_secret:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Gusto OAuth2 credentials not configured "
                "(ASPIRE_GUSTO_CLIENT_ID, ASPIRE_GUSTO_CLIENT_SECRET)",
                provider_id=self.provider_id,
            )

        suite_id = request.suite_id
        if not suite_id:
            raise ProviderError(
                code=InternalErrorCode.AUTH_SCOPE_INSUFFICIENT,
                message="suite_id required for per-suite Gusto OAuth2 token (Law #6)",
                provider_id=self.provider_id,
            )

        try:
            token = await self.oauth2.get_token(suite_id)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(
                code=InternalErrorCode.AUTH_REFRESH_FAILED,
                message=f"Gusto OAuth2 token error: {type(e).__name__}: {e}",
                provider_id=self.provider_id,
            )

        return {"Authorization": f"Bearer {token.access_token}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map Gusto-specific error responses to internal error codes."""
        if status_code == 401:
            return InternalErrorCode.AUTH_EXPIRED_TOKEN
        if status_code == 403:
            return InternalErrorCode.DOMAIN_FORBIDDEN
        if status_code == 404:
            return InternalErrorCode.DOMAIN_NOT_FOUND
        if status_code == 409:
            return InternalErrorCode.DOMAIN_CONFLICT
        if status_code == 422:
            return InternalErrorCode.INPUT_CONSTRAINT_VIOLATED
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


# Singleton client instance (lazy init)
_client: GustoClient | None = None


def _get_client() -> GustoClient:
    global _client
    if _client is None:
        _client = GustoClient()
    return _client


# =============================================================================
# Tool Executors — wired into tool_executor.py registry
# =============================================================================


async def execute_gusto_read_company(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute gusto.read_company — get company details.

    Required payload:
      - company_id: str — Gusto company ID

    GREEN tier: Milo reads company data.
    """
    client = _get_client()

    company_id = payload.get("company_id", "")
    if not company_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="gusto.read_company",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="gusto.read_company",
            error="Missing required parameter: company_id",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/companies/{company_id}",
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
        tool_id="gusto.read_company",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        company = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="gusto.read_company",
            data={
                "company_id": company.get("id", company.get("uuid", "")),
                "name": company.get("name", ""),
                "ein": company.get("ein", ""),
                "status": company.get("company_status", company.get("status", "")),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="gusto.read_company",
            error=response.error_message or f"Gusto API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_gusto_read_payrolls(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute gusto.read_payrolls — get payroll history.

    Required payload:
      - company_id: str — Gusto company ID

    Optional payload:
      - start_date: str — YYYY-MM-DD filter
      - end_date: str — YYYY-MM-DD filter

    GREEN tier: Milo reads payroll data for context.
    """
    client = _get_client()

    company_id = payload.get("company_id", "")
    if not company_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="gusto.read_payrolls",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="gusto.read_payrolls",
            error="Missing required parameter: company_id",
            receipt_data=receipt,
        )

    query_params: dict[str, str] = {}
    if payload.get("start_date"):
        query_params["start_date"] = payload["start_date"]
    if payload.get("end_date"):
        query_params["end_date"] = payload["end_date"]

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/companies/{company_id}/payrolls",
            query_params=query_params if query_params else None,
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
        tool_id="gusto.read_payrolls",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        # Gusto returns payrolls as a list at top level
        payrolls_data = response.body
        if isinstance(payrolls_data, dict):
            payrolls = payrolls_data.get("payrolls", payrolls_data.get("data", []))
        elif isinstance(payrolls_data, list):
            payrolls = payrolls_data
        else:
            payrolls = []

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="gusto.read_payrolls",
            data={
                "payrolls": [
                    {
                        "id": p.get("payroll_id", p.get("id", "")),
                        "pay_period": p.get("pay_period", {}),
                        "check_date": p.get("check_date", ""),
                        "total_net": p.get("totals", {}).get("net_pay", ""),
                        "total_tax": p.get("totals", {}).get("total_tax", ""),
                        "employee_count": p.get("employee_count", 0),
                    }
                    for p in payrolls
                ],
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="gusto.read_payrolls",
            error=response.error_message or f"Gusto API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_gusto_payroll_run(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "red",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute gusto.payroll.run — submit payroll for processing.

    Required payload:
      - company_id: str — Gusto company ID
      - payroll_id: str — Gusto payroll ID to submit

    RED tier: Requires explicit authority + video presence.
    Payroll is irreversible — once submitted, employees get paid.

    Binding fields: [company_id, payroll_id]
    """
    client = _get_client()

    company_id = payload.get("company_id", "")
    payroll_id = payload.get("payroll_id", "")

    missing = []
    if not company_id:
        missing.append("company_id")
    if not payroll_id:
        missing.append("payroll_id")

    if missing:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="gusto.payroll.run",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="gusto.payroll.run",
            error=f"Missing required parameters: {', '.join(missing)}",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="PUT",
            path=f"/companies/{company_id}/payrolls/{payroll_id}/submit",
            body={},
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
        tool_id="gusto.payroll.run",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    # Add binding fields for post-hoc verification
    receipt["binding_fields"] = {
        "company_id": company_id,
        "payroll_id": payroll_id,
    }

    if response.success:
        payroll = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="gusto.payroll.run",
            data={
                "payroll_id": payroll.get("payroll_id", payroll.get("id", payroll_id)),
                "status": payroll.get("status", "submitted"),
                "total_net": payroll.get("totals", {}).get("net_pay", ""),
                "employee_count": payroll.get("employee_count", 0),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="gusto.payroll.run",
            error=response.error_message or f"Gusto API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
