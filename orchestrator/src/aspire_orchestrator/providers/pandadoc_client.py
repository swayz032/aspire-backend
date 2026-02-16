"""PandaDoc Provider Client — Contracts for Clara (Legal) skill pack.

Provider: PandaDoc API (https://api.pandadoc.com/public/v1)
Auth: API key (Bearer token in Authorization header)
Risk tier: YELLOW (contract.generate), GREEN (contract.read), RED (contract.sign)
Idempotency: No — PandaDoc does not support idempotency headers
Timeout: 15s (document generation can be slow)

Tools:
  - pandadoc.contract.generate: Generate a contract from template (YELLOW)
  - pandadoc.contract.read: Read contract/document status (GREEN)
  - pandadoc.contract.sign: Send contract for e-signature (RED, video required)

Per policy_matrix.yaml:
  contract.generate: YELLOW, binding_fields=[party_names, template_id]
  contract.sign: RED, binding_fields=[contract_id, signer_name, signer_email]
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
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


class PandaDocClient(BaseProviderClient):
    """PandaDoc API client with Bearer token auth."""

    provider_id = "pandadoc"
    base_url = "https://api.pandadoc.com/public/v1"
    timeout_seconds = 15.0
    max_retries = 1
    idempotency_support = False

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.pandadoc_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="PandaDoc API key not configured (ASPIRE_PANDADOC_API_KEY)",
                provider_id=self.provider_id,
            )
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 403:
            return InternalErrorCode.AUTH_SCOPE_INSUFFICIENT
        if status_code == 404:
            return InternalErrorCode.DOMAIN_NOT_FOUND
        if status_code == 409:
            return InternalErrorCode.DOMAIN_IDEMPOTENCY_CONFLICT
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        return super()._parse_error(status_code, body)


_client: PandaDocClient | None = None


def _get_client() -> PandaDocClient:
    global _client
    if _client is None:
        _client = PandaDocClient()
    return _client


async def execute_pandadoc_contract_generate(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute pandadoc.contract.generate — generate a contract from template.

    Required payload:
      - template_id: str — PandaDoc template UUID
      - name: str — Document name

    Optional payload:
      - recipients: list[dict] — signers [{email, first_name, last_name, role}]
      - tokens: list[dict] — template tokens [{name, value}]
      - metadata: dict — additional metadata
    """
    client = _get_client()

    template_id = payload.get("template_id", "")
    name = payload.get("name", "")

    if not all([template_id, name]):
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="pandadoc.contract.generate",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="pandadoc.contract.generate",
            error="Missing required parameters: template_id, name",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "name": name,
        "template_uuid": template_id,
        "metadata": {
            "aspire_suite_id": suite_id,
            "aspire_office_id": office_id,
            "aspire_correlation_id": correlation_id,
            **(payload.get("metadata", {})),
        },
    }

    if payload.get("recipients"):
        body["recipients"] = payload["recipients"]
    if payload.get("tokens"):
        body["tokens"] = payload["tokens"]

    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/documents",
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
        tool_id="pandadoc.contract.generate",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        doc = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="pandadoc.contract.generate",
            data={
                "document_id": doc.get("id", doc.get("uuid", "")),
                "name": doc.get("name", ""),
                "status": doc.get("status", "document.uploaded"),
                "created_date": doc.get("date_created"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="pandadoc.contract.generate",
            error=response.error_message or f"PandaDoc API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_pandadoc_contract_read(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute pandadoc.contract.read — read contract/document status.

    Required payload:
      - document_id: str — PandaDoc document ID
    """
    client = _get_client()

    document_id = payload.get("document_id", "")
    if not document_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="pandadoc.contract.read",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="pandadoc.contract.read",
            error="Missing required parameter: document_id",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/documents/{document_id}",
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
        tool_id="pandadoc.contract.read",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        doc = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="pandadoc.contract.read",
            data={
                "document_id": doc.get("id", doc.get("uuid", "")),
                "name": doc.get("name", ""),
                "status": doc.get("status", ""),
                "date_created": doc.get("date_created"),
                "date_modified": doc.get("date_modified"),
                "expiration_date": doc.get("expiration_date"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="pandadoc.contract.read",
            error=response.error_message or f"PandaDoc API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_pandadoc_contract_sign(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "red",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute pandadoc.contract.sign — send for e-signature (RED tier).

    Required payload:
      - document_id: str — PandaDoc document ID
      - message: str — Message to include in signing request

    Optional payload:
      - subject: str — Email subject for signing request
      - silent: bool — if True, don't send email notification
    """
    client = _get_client()

    document_id = payload.get("document_id", "")
    message = payload.get("message", "")

    if not all([document_id, message]):
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="pandadoc.contract.sign",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="pandadoc.contract.sign",
            error="Missing required parameters: document_id, message",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "message": message,
        "silent": payload.get("silent", False),
    }
    if payload.get("subject"):
        body["subject"] = payload["subject"]

    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/documents/{document_id}/send",
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
        tool_id="pandadoc.contract.sign",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        doc = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="pandadoc.contract.sign",
            data={
                "document_id": doc.get("id", doc.get("uuid", document_id)),
                "status": doc.get("status", "document.sent"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="pandadoc.contract.sign",
            error=response.error_message or f"PandaDoc API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
