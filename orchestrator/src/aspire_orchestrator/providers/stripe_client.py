"""Stripe Provider Client — Invoicing & Quoting for Quinn skill pack.

Provider: Stripe (https://api.stripe.com/v1)
Auth: API key (Bearer token) — per-suite connected accounts via Stripe Connect
Risk tier: GREEN (invoice.create draft, quote.create draft), YELLOW (invoice.send, quote.send), RED (payment.send)
Idempotency: Yes — Stripe-native idempotency keys (Idempotency-Key header)

Tools:
  - stripe.invoice.create: Create a draft invoice (auto-finalizes for preview URL)
  - stripe.invoice.send: Finalize and send an invoice
  - stripe.invoice.void: Void an open invoice
  - stripe.quote.create: Create a draft quote (auto-finalizes for PDF)
  - stripe.quote.send: Finalize and accept a quote
  - stripe.quote.cancel: Cancel a draft/open quote
  - stripe.quote.update: Update a draft quote
  - stripe.quote.finalize: Finalize a draft quote (standalone)
  - stripe.payout.list: List payouts (GREEN tier, read-only)
  - stripe.payout.read: Retrieve a single payout (GREEN tier, read-only)
  - stripe.transfer.create: Create a Stripe transfer (RED tier, for Finn)

Per policy_matrix.yaml:
  invoice.create: GREEN (draft creation, no money moves)
  invoice.send: YELLOW, binding_fields=[invoice_id]

Stripe Connect model:
  - Aspire is the platform account
  - Each suite has a connected account (stored in finance_connections table)
  - API calls use Stripe-Account header for per-suite scoping (Law #6)
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


def _flatten_stripe_params(
    params: dict[str, Any], prefix: str = "",
) -> list[tuple[str, str]]:
    """Flatten nested dict to Stripe form-encoded pairs.

    Stripe expects: metadata[key]=value, items[0][amount]=100, etc.
    """
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        full_key = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            items.extend(_flatten_stripe_params(value, full_key))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    items.extend(_flatten_stripe_params(item, f"{full_key}[{i}]"))
                else:
                    items.append((f"{full_key}[{i}]", str(item)))
        elif isinstance(value, bool):
            items.append((full_key, "true" if value else "false"))
        elif value is not None:
            items.append((full_key, str(value)))
    return items


class StripeClient(BaseProviderClient):
    """Stripe API client with Connect account support."""

    provider_id = "stripe"
    base_url = "https://api.stripe.com/v1"
    timeout_seconds = 10.0
    max_retries = 2  # Stripe is idempotent-safe for retries
    idempotency_support = True

    def __init__(self) -> None:
        super().__init__()
        # Per-suite Stripe connected account IDs: {suite_id: "acct_xxx"}
        self._connected_accounts: dict[str, str] = {}

    def _prepare_body(self, request: ProviderRequest) -> tuple[str, bytes | None]:
        """Stripe requires application/x-www-form-urlencoded for POST bodies."""
        if not request.body:
            return "application/x-www-form-urlencoded", None
        from urllib.parse import urlencode
        flat = _flatten_stripe_params(request.body)
        encoded = urlencode(flat)
        return "application/x-www-form-urlencoded", encoded.encode()

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        api_key = settings.stripe_api_key
        if not api_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="Stripe API key not configured (ASPIRE_STRIPE_API_KEY)",
                provider_id=self.provider_id,
            )

        headers = {"Authorization": f"Bearer {api_key}"}

        # Stripe Connect: scope to per-suite connected account (Law #6)
        connected_acct = self._connected_accounts.get(request.suite_id)
        if connected_acct:
            headers["Stripe-Account"] = connected_acct

        return headers

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        error = body.get("error", {})
        error_type = error.get("type", "")

        if error_type == "authentication_error":
            return InternalErrorCode.AUTH_INVALID_KEY
        if error_type == "rate_limit_error":
            return InternalErrorCode.RATE_LIMITED
        if error_type == "idempotency_error":
            return InternalErrorCode.DOMAIN_IDEMPOTENCY_CONFLICT
        if error_type == "card_error":
            return InternalErrorCode.DOMAIN_INSUFFICIENT_FUNDS
        if error_type == "invalid_request_error":
            code = error.get("code", "")
            if "not_found" in code or status_code == 404:
                return InternalErrorCode.DOMAIN_NOT_FOUND
            return InternalErrorCode.INPUT_INVALID_FORMAT

        return super()._parse_error(status_code, body)

    def set_connected_account(self, suite_id: str, account_id: str) -> None:
        """Register a Stripe connected account for a suite."""
        self._connected_accounts[suite_id] = account_id
        logger.info(
            "Stripe connected account set for suite=%s: %s",
            suite_id[:8] if len(suite_id) > 8 else suite_id,
            account_id[:12],
        )


_client: StripeClient | None = None


def _get_client() -> StripeClient:
    global _client
    if _client is None:
        _client = StripeClient()
    return _client


async def _resolve_stripe_customer(
    client: StripeClient,
    email: str,
    suite_id: str,
    correlation_id: str,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    business_name: str | None = None,
    address: dict[str, str] | str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> str | None:
    """Find or create a Stripe customer by email.

    Accepts full onboarding fields matching Stripe Create Customer API:
      - first_name + last_name → combined into `name` field
      - business_name → Stripe `description` (visible on invoices)
      - address → Stripe `address` object
      - phone → Stripe `phone`

    Returns customer ID (cus_xxx) or None on failure.
    """
    # S4-L2: Normalize email to lowercase to prevent duplicate customers
    email = email.strip().lower()

    # Search by email first
    search_response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/customers?email={email}&limit=1",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id="",
        )
    )
    if search_response.success:
        data = search_response.body.get("data", [])
        if data:
            return data[0].get("id")
    else:
        logger.warning(
            "Stripe customer search failed: status=%s",
            search_response.status_code,
        )

    # Not found — create with full onboarding fields
    create_body: dict[str, Any] = {"email": email}

    # Build name from first/last or fall back to legacy `name` param
    # Stripe Create Customer has: `name` (general), `individual_name` (person), `business_name` (company)
    full_name = None
    if first_name and last_name:
        full_name = f"{first_name.strip()} {last_name.strip()}"
    elif first_name:
        full_name = first_name.strip()
    elif name:
        full_name = name
    if full_name:
        create_body["name"] = full_name

    # Business name → Stripe `business_name` field (up to 150 chars)
    if business_name:
        create_body["business_name"] = business_name.strip()[:150]

    # Phone
    if phone:
        create_body["phone"] = phone.strip()

    # Address — accept dict or string
    if isinstance(address, dict):
        # Stripe expects: line1, line2, city, state, postal_code, country
        create_body["address"] = address
    elif isinstance(address, str) and address.strip():
        # Free-form text → put in line1
        create_body["address"] = {"line1": address.strip()}

    create_body["metadata"] = {"aspire_suite_id": suite_id}

    create_response = await client._request(
        ProviderRequest(
            method="POST",
            path="/customers",
            body=create_body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id="",
        )
    )
    if create_response.success:
        return create_response.body.get("id")

    # S3-M4: Raise on failure instead of returning None (fail-closed)
    error_detail = str(create_response.body)[:200]
    logger.error(
        "S3-M4: Stripe customer resolution FAILED for email=%s suite=%s status=%s",
        email[:3] + "***",
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        create_response.status_code,
    )
    raise RuntimeError(
        f"customer_lookup_failed: Could not find or create Stripe customer "
        f"(status={create_response.status_code})"
    )


async def execute_stripe_invoice_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.invoice.create — create a draft invoice.

    Required payload (one of):
      - customer_id: str — Stripe customer ID (direct)
      - customer_email: str — email to find-or-create customer

    Customer onboarding fields (for new customer creation):
      - customer_first_name: str — client's first name
      - customer_last_name: str — client's last name
      - customer_business_name: str — business name (optional)
      - customer_address: str | dict — address (optional)
      - customer_phone: str — phone number (optional)
      - customer_name: str — legacy full name fallback

    Invoice fields:
      - amount_cents: int — total amount in cents (preferred)
      - amount: int — alias for amount_cents (backward compat)
      - currency: str — 3-letter ISO (default "usd")
      - description: str — invoice description
      - line_items: list[dict] — individual line items
      - due_days: int — days until due (default 30)
      - metadata: dict — Stripe metadata (tenant-scoped)
    """
    client = _get_client()

    customer_id = payload.get("customer_id", "")
    customer_email = payload.get("customer_email", "")
    amount = payload.get("amount_cents") or payload.get("amount")

    # Resolve customer from email if no direct ID
    if not customer_id and customer_email:
        resolved = await _resolve_stripe_customer(
            client,
            email=customer_email,
            suite_id=suite_id,
            correlation_id=correlation_id,
            first_name=payload.get("customer_first_name"),
            last_name=payload.get("customer_last_name"),
            business_name=payload.get("customer_business_name"),
            address=payload.get("customer_address"),
            phone=payload.get("customer_phone"),
            name=payload.get("customer_name"),  # legacy fallback
        )
        if resolved:
            customer_id = resolved

    if not customer_id or amount is None:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.invoice.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        missing = []
        if not customer_id:
            missing.append("customer_id or customer_email")
        if amount is None:
            missing.append("amount_cents")
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.invoice.create",
            error=f"Missing required parameters: {', '.join(missing)}",
            receipt_data=receipt,
        )

    # Build Stripe invoice body
    # Note: Stripe uses form-encoded, but we send JSON and let httpx handle it
    body: dict[str, Any] = {
        "customer": customer_id,
        "collection_method": "send_invoice",
        "days_until_due": payload.get("due_days") or 30,
        "currency": payload.get("currency", "usd"),
        "metadata": {
            "aspire_suite_id": suite_id,
            "aspire_office_id": office_id,
            "aspire_correlation_id": correlation_id,
            **(payload.get("metadata", {})),
        },
    }

    if payload.get("description"):
        body["description"] = payload["description"]

    # Custom invoice number (premium format: INV-YYYYMMDD-XXXX)
    if payload.get("invoice_number"):
        body["number"] = payload["invoice_number"]

    # Handle due_days = 0 (due immediately / upon receipt)
    if payload.get("due_days") == 0:
        body["days_until_due"] = 0

    # 5e: Auto-generate idempotency key if not provided
    import uuid as _uuid_create
    create_idem_key = payload.get("idempotency_key") or f"inv_create_{correlation_id}_{_uuid_create.uuid4()}"
    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/invoices",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=create_idem_key,
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
        tool_id="stripe.invoice.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        invoice = response.body
        invoice_id = invoice.get("id", "")

        # Add line item with the amount (Stripe requires separate invoiceitem)
        if amount and invoice_id:
            item_body: dict[str, Any] = {
                "customer": customer_id,
                "invoice": invoice_id,
                "amount": int(amount),
                "currency": payload.get("currency", "usd"),
            }
            if payload.get("description"):
                item_body["description"] = payload["description"]

            # R-006: Idempotency key prevents duplicate line items on retry
            item_response = await client._request(
                ProviderRequest(
                    method="POST",
                    path="/invoiceitems",
                    body=item_body,
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    idempotency_key=f"invoiceitem_{invoice_id}_{correlation_id}",
                )
            )
            if not item_response.success:
                logger.error(
                    "Stripe invoiceitem creation failed for invoice %s: %s",
                    invoice_id, item_response.body,
                )
                return ToolExecutionResult(
                    outcome=Outcome.FAILED,
                    tool_id="stripe.invoice.create",
                    data={
                        "invoice_id": invoice_id,
                        "error": "Line item creation failed after invoice was created",
                        "status": "partial_failure",
                    },
                    receipt_data=client.make_receipt_data(
                        correlation_id=correlation_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        tool_id="stripe.invoice.create",
                        risk_tier=risk_tier,
                        outcome=Outcome.FAILED,
                        reason_code="INVOICEITEM_CREATION_FAILED",
                        capability_token_id=capability_token_id,
                        capability_token_hash=capability_token_hash,
                    ),
                    is_stub=False,
                )

        # Auto-finalize to generate hosted_invoice_url for preview
        # Stripe only populates hosted_invoice_url after finalization (status=open)
        # The user reviews the preview in Authority Queue before approving "send"
        finalize_response = await client._request(
            ProviderRequest(
                method="POST",
                path=f"/invoices/{invoice_id}/finalize",
                body={"auto_advance": False},  # Don't auto-charge
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                idempotency_key=f"finalize_{invoice_id}_{correlation_id}",
            )
        )
        if finalize_response.success:
            finalized = finalize_response.body
            hosted_url = finalized.get("hosted_invoice_url")
            final_status = finalized.get("status", "open")
        else:
            # Finalize failed — return draft anyway (non-fatal, preview just won't be available)
            logger.warning(
                "Auto-finalize failed for invoice %s: %s (preview URL unavailable)",
                invoice_id, finalize_response.status_code,
            )
            hosted_url = None
            final_status = "draft"

        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.invoice.create",
            data={
                "invoice_id": invoice_id,
                "status": final_status,
                "amount_due": int(amount) if amount else invoice.get("amount_due", 0),
                "currency": invoice.get("currency", "usd"),
                "customer": invoice.get("customer", ""),
                "hosted_invoice_url": hosted_url,
                "invoice_pdf": finalize_response.body.get("invoice_pdf") if finalize_response.success else None,
                "created": invoice.get("created"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.invoice.create",
            error=response.error_message or f"Stripe API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_stripe_invoice_send(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.invoice.send — finalize and send an invoice.

    Required payload:
      - invoice_id: str — Stripe invoice ID to finalize and send
    """
    client = _get_client()

    invoice_id = payload.get("invoice_id", "")
    if not invoice_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.invoice.send",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.invoice.send",
            error="Missing required parameter: invoice_id",
            receipt_data=receipt,
        )

    # S3-H4: Finalize with idempotency key to prevent double-finalize on retry
    import uuid as _uuid
    finalize_nonce = payload.get("idempotency_key", str(_uuid.uuid4()))
    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/invoices/{invoice_id}/finalize",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"finalize_{invoice_id}_{finalize_nonce}",
        )
    )

    if not response.success:
        # Already-finalized invoices can proceed directly to send (resend flow)
        error_body = response.body if isinstance(response.body, dict) else {}
        error_msg = error_body.get("error", {}).get("message", "") if isinstance(error_body.get("error"), dict) else str(error_body.get("error", ""))
        already_finalized = "already finalized" in error_msg.lower()

        if not already_finalized:
            receipt = client.make_receipt_data(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id="stripe.invoice.send",
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code=response.error_code.value if response.error_code else "FAILED",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
                provider_response=response,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id="stripe.invoice.send",
                error=response.error_message or "Failed to finalize invoice",
                receipt_data=receipt,
            )
        logger.info("Invoice %s already finalized — proceeding to send (resend)", invoice_id)

    # 5e: Send with idempotency key to prevent double-send on retry
    send_response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/invoices/{invoice_id}/send",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"send_{invoice_id}_{finalize_nonce}",
        )
    )

    outcome = Outcome.SUCCESS if send_response.success else Outcome.FAILED
    reason = "EXECUTED" if send_response.success else (
        send_response.error_code.value if send_response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="stripe.invoice.send",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=send_response,
    )

    if send_response.success:
        invoice = send_response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.invoice.send",
            data={
                "invoice_id": invoice.get("id", ""),
                "status": invoice.get("status", ""),
                "hosted_invoice_url": invoice.get("hosted_invoice_url"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.invoice.send",
            error=send_response.error_message or "Failed to send invoice",
            receipt_data=receipt,
        )


async def execute_stripe_invoice_void(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.invoice.void — void an open invoice.

    Required payload:
      - invoice_id: str — Stripe invoice ID to void
    """
    client = _get_client()

    invoice_id = payload.get("invoice_id", "")
    if not invoice_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.invoice.void",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.invoice.void",
            error="Missing required parameter: invoice_id",
            receipt_data=receipt,
        )

    # 5e: Auto-generate idempotency key if not provided
    import uuid as _uuid_void
    void_idem_key = payload.get("idempotency_key") or f"void_{invoice_id}_{_uuid_void.uuid4()}"
    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/invoices/{invoice_id}/void",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=void_idem_key,
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
        tool_id="stripe.invoice.void",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        invoice = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.invoice.void",
            data={
                "invoice_id": invoice.get("id", ""),
                "status": invoice.get("status", "void"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.invoice.void",
            error=response.error_message or "Failed to void invoice",
            receipt_data=receipt,
        )


async def execute_stripe_quote_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.quote.create — create a draft quote for a customer.

    Supports two resolution paths (same as invoice.create):
      1. customer_email (preferred) — resolves/creates customer via _resolve_stripe_customer
      2. customer_id (legacy) — direct Stripe customer ID

    Required payload:
      - customer_email OR customer_id
      - line_items: list[dict] — line items with price_data

    Optional payload:
      - customer_first_name, customer_last_name, customer_business_name,
        customer_address, customer_phone — onboarding fields for new customers
      - description: str — displayed on the quote PDF (max 500 chars)
      - header: str — displayed on the quote PDF (max 50 chars)
      - footer: str — displayed on the quote PDF (max 500 chars)
      - expiry_days: int — days until quote expires (default 30)
      - idempotency_key: str — explicit idempotency key

    Each line_item: {price_data: {currency, unit_amount, product_data: {name}}, quantity: int}

    Auto-finalizes after creation so quote PDF is immediately available.
    """
    client = _get_client()

    customer_email = payload.get("customer_email", "")
    customer_id = payload.get("customer_id", "")
    line_items = payload.get("line_items", [])

    if (not customer_email and not customer_id) or not line_items:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.create",
            error="Missing required parameters: customer_email (or customer_id) and line_items",
            receipt_data=receipt,
        )

    # Resolve customer via email if no customer_id provided
    if not customer_id and customer_email:
        try:
            customer_id = await _resolve_stripe_customer(
                client,
                customer_email,
                suite_id,
                correlation_id,
                first_name=payload.get("customer_first_name"),
                last_name=payload.get("customer_last_name"),
                business_name=payload.get("customer_business_name"),
                address=payload.get("customer_address"),
                phone=payload.get("customer_phone"),
                name=payload.get("customer_name"),
            )
        except RuntimeError as e:
            receipt = client.make_receipt_data(
                correlation_id=correlation_id,
                suite_id=suite_id,
                office_id=office_id,
                tool_id="stripe.quote.create",
                risk_tier=risk_tier,
                outcome=Outcome.FAILED,
                reason_code="CUSTOMER_RESOLUTION_FAILED",
                capability_token_id=capability_token_id,
                capability_token_hash=capability_token_hash,
            )
            return ToolExecutionResult(
                outcome=Outcome.FAILED,
                tool_id="stripe.quote.create",
                error=str(e),
                receipt_data=receipt,
            )

    if not customer_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="CUSTOMER_RESOLUTION_FAILED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.create",
            error="Could not resolve Stripe customer",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {
        "customer": customer_id,
        "line_items": line_items,
        "metadata": {
            "aspire_suite_id": suite_id,
            "aspire_office_id": office_id,
            "aspire_correlation_id": correlation_id,
        },
    }

    # Optional quote fields per Stripe API
    if payload.get("description"):
        body["description"] = str(payload["description"])[:500]
    if payload.get("header"):
        body["header"] = str(payload["header"])[:50]
    if payload.get("footer"):
        body["footer"] = str(payload["footer"])[:500]

    # Expiry: convert expiry_days to Unix timestamp, default 30 days
    import time as _time_quote
    expiry_days = int(payload.get("expiry_days", 30))
    body["expires_at"] = int(_time_quote.time()) + (expiry_days * 86400)
    if payload.get("expires_at"):
        body["expires_at"] = int(payload["expires_at"])

    # Auto-generate idempotency key if not provided
    import uuid as _uuid_quote
    quote_idem_key = payload.get("idempotency_key") or f"quote_create_{correlation_id}_{_uuid_quote.uuid4()}"
    response = await client._request(
        ProviderRequest(
            method="POST",
            path="/quotes",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=quote_idem_key,
        )
    )

    if not response.success:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=response.error_code.value if response.error_code else "FAILED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            provider_response=response,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.create",
            error=response.error_message or f"Stripe API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )

    quote = response.body
    quote_id = quote.get("id", "")

    # Auto-finalize to generate quote PDF (same pattern as invoice auto-finalize)
    finalize_response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/quotes/{quote_id}/finalize",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"quote_autofinalize_{quote_id}_{_uuid_quote.uuid4()}",
        )
    )

    finalized_quote = finalize_response.body if finalize_response.success else quote
    final_status = finalized_quote.get("status", quote.get("status", "draft"))

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="stripe.quote.create",
        risk_tier=risk_tier,
        outcome=Outcome.SUCCESS,
        reason_code="EXECUTED",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="stripe.quote.create",
        data={
            "quote_id": quote_id,
            "status": final_status,
            "amount_total": finalized_quote.get("amount_total", 0),
            "currency": finalized_quote.get("currency", "usd"),
            "customer_id": customer_id,
            "expires_at": finalized_quote.get("expires_at"),
        },
        receipt_data=receipt,
    )


async def execute_stripe_quote_send(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.quote.send — finalize and accept a quote.

    Two-step operation (like invoice_send):
      1. POST /quotes/{quote_id}/finalize
      2. POST /quotes/{quote_id}/accept

    Required payload:
      - quote_id: str — Stripe quote ID
    """
    client = _get_client()

    quote_id = payload.get("quote_id", "")
    if not quote_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.send",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.send",
            error="Missing required parameter: quote_id",
            receipt_data=receipt,
        )

    # 5e: Step 1: Finalize the quote with idempotency key
    import uuid as _uuid_qf
    quote_nonce = payload.get("idempotency_key", str(_uuid_qf.uuid4()))
    finalize_response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/quotes/{quote_id}/finalize",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"quote_finalize_{quote_id}_{quote_nonce}",
        )
    )

    if not finalize_response.success:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.send",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=finalize_response.error_code.value if finalize_response.error_code else "FAILED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
            provider_response=finalize_response,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.send",
            error=finalize_response.error_message or "Failed to finalize quote",
            receipt_data=receipt,
        )

    # 5e: Step 2: Accept the quote with idempotency key
    accept_response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/quotes/{quote_id}/accept",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"quote_accept_{quote_id}_{quote_nonce}",
        )
    )

    outcome = Outcome.SUCCESS if accept_response.success else Outcome.FAILED
    reason = "EXECUTED" if accept_response.success else (
        accept_response.error_code.value if accept_response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="stripe.quote.send",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=accept_response,
    )

    if accept_response.success:
        quote = accept_response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.quote.send",
            data={
                "quote_id": quote.get("id", ""),
                "status": quote.get("status", "accepted"),
                "amount_total": quote.get("amount_total", 0),
                "currency": quote.get("currency", "usd"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.send",
            error=accept_response.error_message or "Failed to accept quote",
            receipt_data=receipt,
        )


async def execute_stripe_quote_cancel(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.quote.cancel — cancel a draft or open quote.

    Per Stripe docs: POST /quotes/{quote_id}/cancel
    A quote can only be canceled if status is 'draft' or 'open'.

    Required payload:
      - quote_id: str — Stripe quote ID (qt_xxx)
    """
    client = _get_client()

    quote_id = payload.get("quote_id", "")
    if not quote_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.cancel",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.cancel",
            error="Missing required parameter: quote_id",
            receipt_data=receipt,
        )

    import uuid as _uuid_qc
    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/quotes/{quote_id}/cancel",
            body={},
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"quote_cancel_{quote_id}_{_uuid_qc.uuid4()}",
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
        tool_id="stripe.quote.cancel",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        quote = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.quote.cancel",
            data={
                "quote_id": quote.get("id", ""),
                "status": quote.get("status", "canceled"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.cancel",
            error=response.error_message or "Failed to cancel quote",
            receipt_data=receipt,
        )


async def execute_stripe_quote_update(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.quote.update — update a draft quote.

    Per Stripe docs: POST /quotes/{quote_id}
    Only draft quotes can be updated.

    Required payload:
      - quote_id: str — Stripe quote ID (qt_xxx)

    Optional payload:
      - line_items: list[dict] — updated line items
      - description: str — displayed on quote PDF (max 500 chars)
      - header: str — displayed on quote PDF (max 50 chars)
      - footer: str — displayed on quote PDF (max 500 chars)
      - expires_at: int — Unix timestamp for quote expiration
      - expiry_days: int — alternative to expires_at
    """
    client = _get_client()

    quote_id = payload.get("quote_id", "")
    if not quote_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.update",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.update",
            error="Missing required parameter: quote_id",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {}

    if payload.get("line_items"):
        body["line_items"] = payload["line_items"]
    if payload.get("description"):
        body["description"] = str(payload["description"])[:500]
    if payload.get("header"):
        body["header"] = str(payload["header"])[:50]
    if payload.get("footer"):
        body["footer"] = str(payload["footer"])[:500]
    if payload.get("expires_at"):
        body["expires_at"] = int(payload["expires_at"])
    elif payload.get("expiry_days"):
        import time as _time_qu
        body["expires_at"] = int(_time_qu.time()) + (int(payload["expiry_days"]) * 86400)

    import uuid as _uuid_qu
    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/quotes/{quote_id}",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"quote_update_{quote_id}_{_uuid_qu.uuid4()}",
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
        tool_id="stripe.quote.update",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        quote = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.quote.update",
            data={
                "quote_id": quote.get("id", ""),
                "status": quote.get("status", "draft"),
                "amount_total": quote.get("amount_total", 0),
                "currency": quote.get("currency", "usd"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.update",
            error=response.error_message or "Failed to update quote",
            receipt_data=receipt,
        )


async def execute_stripe_quote_finalize(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.quote.finalize — finalize a draft quote (standalone).

    Per Stripe docs: POST /quotes/{quote_id}/finalize
    Makes the quote 'open' — customer can then accept or it expires.
    Generates PDF. Optional expires_at override.

    Required payload:
      - quote_id: str — Stripe quote ID (qt_xxx)

    Optional payload:
      - expires_at: int — Unix timestamp to override expiration on finalize
    """
    client = _get_client()

    quote_id = payload.get("quote_id", "")
    if not quote_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.finalize",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.finalize",
            error="Missing required parameter: quote_id",
            receipt_data=receipt,
        )

    body: dict[str, Any] = {}
    if payload.get("expires_at"):
        body["expires_at"] = int(payload["expires_at"])

    import uuid as _uuid_qfin
    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/quotes/{quote_id}/finalize",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=f"quote_finalize_{quote_id}_{_uuid_qfin.uuid4()}",
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
        tool_id="stripe.quote.finalize",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        quote = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="stripe.quote.finalize",
            data={
                "quote_id": quote.get("id", ""),
                "status": quote.get("status", "open"),
                "amount_total": quote.get("amount_total", 0),
                "currency": quote.get("currency", "usd"),
                "expires_at": quote.get("expires_at"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.finalize",
            error=response.error_message or "Failed to finalize quote",
            receipt_data=receipt,
        )


async def execute_stripe_quote_pdf(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.quote.pdf — get the PDF download URL for a finalized quote.

    Per Stripe docs: GET /quotes/{quote_id}/pdf
    Returns binary PDF. We return the download URL for the client to fetch.
    Only works for finalized (open/accepted) quotes.

    Required payload:
      - quote_id: str — Stripe quote ID (qt_xxx)
    """
    client = _get_client()

    quote_id = payload.get("quote_id", "")
    if not quote_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.quote.pdf",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.quote.pdf",
            error="Missing required parameter: quote_id",
            receipt_data=receipt,
        )

    # The Stripe quote PDF endpoint returns binary — we construct the URL
    # for the client to download directly via authenticated Stripe API call.
    # The actual PDF fetch requires the Stripe API key, so we return metadata
    # indicating the PDF is available and the quote_id for downstream use.
    pdf_url = f"https://files.stripe.com/v1/quotes/{quote_id}/pdf"

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="stripe.quote.pdf",
        risk_tier=risk_tier,
        outcome=Outcome.SUCCESS,
        reason_code="EXECUTED",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="stripe.quote.pdf",
        data={
            "quote_id": quote_id,
            "pdf_url": pdf_url,
            "note": "PDF available for finalized quotes. Use Stripe API key to download.",
        },
        receipt_data=receipt,
    )


# ── Payout Tools (GREEN, read-only) ─────────────────────────────


async def execute_stripe_payout_list(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.payout.list — list payouts for the connected account.

    Per Stripe docs: GET /v1/payouts
    Returns list of payouts sorted by most recent first.

    Optional payload:
      - status: str — filter by 'pending', 'paid', 'failed', 'canceled'
      - limit: int — max results (1-100, default 10)
      - starting_after: str — pagination cursor (payout ID)
      - arrival_date_gte: int — arrival date >= (unix timestamp)
      - arrival_date_lte: int — arrival date <= (unix timestamp)
    """
    client = _get_client()

    params: list[tuple[str, str]] = []

    status = payload.get("status")
    if status:
        params.append(("status", str(status)))

    limit = payload.get("limit", 10)
    params.append(("limit", str(min(int(limit), 100))))

    starting_after = payload.get("starting_after")
    if starting_after:
        params.append(("starting_after", str(starting_after)))

    arrival_gte = payload.get("arrival_date_gte")
    if arrival_gte:
        params.append(("arrival_date[gte]", str(arrival_gte)))

    arrival_lte = payload.get("arrival_date_lte")
    if arrival_lte:
        params.append(("arrival_date[lte]", str(arrival_lte)))

    try:
        resp = await client._request(
            ProviderRequest(
                method="GET",
                url="https://api.stripe.com/v1/payouts",
                params=params,
                suite_id=suite_id,
                correlation_id=correlation_id,
                idempotency_key=None,
            )
        )
    except ProviderError as exc:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.payout.list",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=exc.code.value if exc.code else "PROVIDER_ERROR",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.payout.list",
            error=str(exc),
            receipt_data=receipt,
        )

    raw_payouts = resp.body.get("data", [])
    payouts = [
        {
            "id": p.get("id"),
            "amount": p.get("amount"),
            "currency": p.get("currency"),
            "status": p.get("status"),
            "arrival_date": p.get("arrival_date"),
            "created": p.get("created"),
            "method": p.get("method"),
            "description": p.get("description"),
            "automatic": p.get("automatic"),
        }
        for p in raw_payouts
    ]

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="stripe.payout.list",
        risk_tier=risk_tier,
        outcome=Outcome.SUCCESS,
        reason_code="EXECUTED",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="stripe.payout.list",
        data={
            "payouts": payouts,
            "count": len(payouts),
            "has_more": resp.body.get("has_more", False),
        },
        receipt_data=receipt,
    )


async def execute_stripe_payout_retrieve(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute stripe.payout.read — retrieve a single payout by ID.

    Per Stripe docs: GET /v1/payouts/{payout_id}

    Required payload:
      - payout_id: str — Stripe payout ID (po_xxx)
    """
    client = _get_client()

    payout_id = payload.get("payout_id", "")
    if not payout_id:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.payout.read",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.payout.read",
            error="Missing required parameter: payout_id",
            receipt_data=receipt,
        )

    try:
        resp = await client._request(
            ProviderRequest(
                method="GET",
                url=f"https://api.stripe.com/v1/payouts/{payout_id}",
                suite_id=suite_id,
                correlation_id=correlation_id,
                idempotency_key=None,
            )
        )
    except ProviderError as exc:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="stripe.payout.read",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code=exc.code.value if exc.code else "PROVIDER_ERROR",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="stripe.payout.read",
            error=str(exc),
            receipt_data=receipt,
        )

    body = resp.body
    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="stripe.payout.read",
        risk_tier=risk_tier,
        outcome=Outcome.SUCCESS,
        reason_code="EXECUTED",
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
    )

    return ToolExecutionResult(
        outcome=Outcome.SUCCESS,
        tool_id="stripe.payout.read",
        data={
            "payout_id": body.get("id"),
            "amount": body.get("amount"),
            "currency": body.get("currency"),
            "status": body.get("status"),
            "arrival_date": body.get("arrival_date"),
            "created": body.get("created"),
            "method": body.get("method"),
            "description": body.get("description"),
            "automatic": body.get("automatic"),
            "failure_code": body.get("failure_code"),
            "failure_message": body.get("failure_message"),
            "destination": body.get("destination"),
            "source_type": body.get("source_type"),
        },
        receipt_data=receipt,
    )
