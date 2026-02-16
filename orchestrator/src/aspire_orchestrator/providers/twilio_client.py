"""Twilio Provider Client — Telephony for Sarah (Front Desk) skill pack.

Provider: Twilio REST API (https://api.twilio.com/2010-04-01)
Auth: HTTP Basic Auth (account_sid:auth_token) — base64 encoded Authorization header
Risk tier: YELLOW (call.create — external communication), GREEN (call.status — read-only)
Idempotency: Yes — Twilio supports idempotency via Idempotency-Key header

Tools:
  - twilio.call.create: Initiate an outbound phone call
  - twilio.call.status: Get status of an existing call

Per policy_matrix.yaml:
  call.create: YELLOW, binding_fields=[to, from_number, url]

IMPORTANT: Twilio uses form-encoded bodies (application/x-www-form-urlencoded),
NOT JSON. The _request method is overridden to handle this.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
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


class TwilioClient(BaseProviderClient):
    """Twilio REST API client with Basic Auth and form-encoded bodies."""

    provider_id = "twilio"
    base_url = "https://api.twilio.com/2010-04-01"
    timeout_seconds = 10.0
    max_retries = 2  # Twilio is idempotent-safe for retries
    idempotency_support = True

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        account_sid = settings.twilio_account_sid
        auth_token = settings.twilio_auth_token

        if not account_sid or not auth_token:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message=(
                    "Twilio credentials not configured "
                    "(ASPIRE_TWILIO_ACCOUNT_SID / ASPIRE_TWILIO_AUTH_TOKEN)"
                ),
                provider_id=self.provider_id,
            )

        # HTTP Basic Auth: base64(account_sid:auth_token)
        credentials = f"{account_sid}:{auth_token}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 401:
            return InternalErrorCode.AUTH_INVALID_KEY
        if status_code == 429:
            return InternalErrorCode.RATE_LIMITED
        error_code = body.get("code")
        if error_code == 20404:
            return InternalErrorCode.DOMAIN_NOT_FOUND
        if error_code == 21211:
            return InternalErrorCode.INPUT_INVALID_FORMAT  # Invalid 'To' number
        if error_code == 21214:
            return InternalErrorCode.INPUT_INVALID_FORMAT  # Invalid 'To' number
        return super()._parse_error(status_code, body)

    def _parse_response(self, raw_body: bytes) -> dict[str, Any]:
        """Parse Twilio JSON response. Twilio always returns JSON."""
        import json
        try:
            return json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, ValueError):
            return {"raw": raw_body.decode("utf-8", errors="replace")[:500]}

    async def _request(self, request: ProviderRequest) -> ProviderResponse:
        """Override to send form-encoded bodies for POST requests.

        Twilio uses application/x-www-form-urlencoded for POST bodies,
        but returns JSON responses.
        """
        import time as _time
        import uuid

        import httpx

        # Circuit breaker check (Law #3 — fail-closed)
        try:
            self._circuit.check()
        except ProviderError as e:
            return ProviderResponse(
                status_code=503,
                body={"error": e.code.value, "message": e.message},
                success=False,
                error_code=e.code,
                error_message=e.message,
            )

        # Build URL
        url = f"{self.base_url.rstrip('/')}{request.path}"
        if request.query_params:
            qs = "&".join(
                f"{k}={v}" for k, v in sorted(request.query_params.items())
            )
            url = f"{url}?{qs}"

        # Get auth headers
        try:
            auth_headers = await self._authenticate_headers(request)
        except ProviderError as e:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=401,
                body={"error": e.code.value, "message": e.message},
                success=False,
                error_code=e.code,
                error_message=e.message,
            )

        # Build headers — Twilio uses form-encoded for POST
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Correlation-Id": request.correlation_id or str(uuid.uuid4()),
        }
        headers.update(auth_headers)
        headers.update(request.extra_headers)

        # Add idempotency key if POST
        if self.idempotency_support and request.method.upper() in ("POST",):
            idem_key = self._compute_idempotency_key(request)
            headers["Idempotency-Key"] = idem_key

        # Encode body: form-encoded for POST, none for GET
        body_data: bytes | None = None
        if request.method.upper() == "POST" and request.body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body_data = urllib.parse.urlencode(request.body).encode("utf-8")
        elif request.method.upper() == "GET":
            pass  # No body for GET

        logger.info(
            "Provider request: %s %s %s (suite=%s, corr=%s)",
            self.provider_id,
            request.method,
            request.path,
            (request.suite_id[:8] if len(request.suite_id) > 8 else request.suite_id),
            (request.correlation_id[:8] if len(request.correlation_id) > 8 else request.correlation_id),
        )

        last_error: ProviderError | None = None
        attempt = 0

        while attempt <= self.max_retries:
            if attempt > 0:
                import asyncio
                wait = self._backoff_seconds(attempt)
                await asyncio.sleep(wait)

            start = _time.monotonic()

            try:
                client = await self._get_client()

                if request.method.upper() == "GET":
                    response = await client.get(url, headers=headers)
                elif request.method.upper() == "POST":
                    response = await client.post(
                        url, headers=headers, content=body_data
                    )
                else:
                    raise ProviderError(
                        code=InternalErrorCode.INPUT_INVALID_FORMAT,
                        message=f"Unsupported HTTP method: {request.method}",
                        provider_id=self.provider_id,
                    )

                latency_ms = (_time.monotonic() - start) * 1000
                parsed = self._parse_response(response.content)
                success = 200 <= response.status_code < 300

                logger.info(
                    "Provider response: %s %s %s -> %d (%.0fms, success=%s)",
                    self.provider_id,
                    request.method,
                    request.path,
                    response.status_code,
                    latency_ms,
                    success,
                )

                if success:
                    self._circuit.record_success()
                    return ProviderResponse(
                        status_code=response.status_code,
                        body=parsed,
                        success=True,
                        provider_request_id=parsed.get("sid"),
                        latency_ms=latency_ms,
                    )

                # Error response
                error_code = self._parse_error(response.status_code, parsed)

                if error_code.circuit_breaker_relevant:
                    self._circuit.record_failure()

                if error_code.retryable and attempt < self.max_retries:
                    last_error = ProviderError(
                        code=error_code,
                        message=parsed.get("message", f"HTTP {response.status_code}"),
                        provider_id=self.provider_id,
                        status_code=response.status_code,
                    )
                    attempt += 1
                    continue

                return ProviderResponse(
                    status_code=response.status_code,
                    body=parsed,
                    success=False,
                    error_code=error_code,
                    error_message=parsed.get("message", f"HTTP {response.status_code}"),
                    provider_request_id=parsed.get("sid"),
                    latency_ms=latency_ms,
                )

            except httpx.TimeoutException:
                latency_ms = (_time.monotonic() - start) * 1000
                self._circuit.record_failure()
                last_error = ProviderError(
                    code=InternalErrorCode.NETWORK_TIMEOUT,
                    message=f"Timeout after {self.timeout_seconds}s",
                    provider_id=self.provider_id,
                )
                attempt += 1
                continue

            except httpx.ConnectError:
                self._circuit.record_failure()
                last_error = ProviderError(
                    code=InternalErrorCode.NETWORK_CONNECTION_REFUSED,
                    message="Connection refused",
                    provider_id=self.provider_id,
                )
                attempt += 1
                continue

            except ProviderError:
                raise

            except Exception as e:
                self._circuit.record_failure()
                return ProviderResponse(
                    status_code=500,
                    body={"error": "PROVIDER_ERROR", "message": str(e)[:200]},
                    success=False,
                    error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                    error_message=str(e)[:200],
                )

        # Exhausted retries
        error_code = last_error.code if last_error else InternalErrorCode.SERVER_INTERNAL_ERROR
        error_msg = last_error.message if last_error else "Max retries exhausted"

        return ProviderResponse(
            status_code=last_error.status_code or 503 if last_error else 503,
            body={"error": error_code.value, "message": error_msg},
            success=False,
            error_code=error_code,
            error_message=error_msg,
        )


_client: TwilioClient | None = None


def _get_client() -> TwilioClient:
    global _client
    if _client is None:
        _client = TwilioClient()
    return _client


async def execute_twilio_call_create(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute twilio.call.create — initiate an outbound phone call.

    Required payload:
      - to: str — destination phone number (E.164 format)
      - from_number: str — Twilio phone number to call from (E.164)
      - url: str — TwiML URL for call instructions

    Optional payload:
      - status_callback: str — URL for call status webhooks
      - idempotency_key: str — explicit idempotency key
    """
    client = _get_client()
    account_sid = settings.twilio_account_sid

    to = payload.get("to", "")
    from_number = payload.get("from_number", "")
    url = payload.get("url", "")

    if not all([to, from_number, url]):
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="twilio.call.create",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="twilio.call.create",
            error="Missing required parameters: to, from_number, url",
            receipt_data=receipt,
        )

    # Build Twilio form-encoded body
    body: dict[str, Any] = {
        "To": to,
        "From": from_number,
        "Url": url,
    }

    if payload.get("status_callback"):
        body["StatusCallback"] = payload["status_callback"]

    response = await client._request(
        ProviderRequest(
            method="POST",
            path=f"/Accounts/{account_sid}/Calls.json",
            body=body,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            idempotency_key=payload.get("idempotency_key"),
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
        tool_id="twilio.call.create",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        call = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="twilio.call.create",
            data={
                "call_sid": call.get("sid", ""),
                "status": call.get("status", ""),
                "from": call.get("from", ""),
                "to": call.get("to", ""),
                "direction": call.get("direction", "outbound-api"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="twilio.call.create",
            error=response.error_message or f"Twilio API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )


async def execute_twilio_call_status(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute twilio.call.status — get status of an existing call.

    Required payload:
      - call_sid: str — Twilio Call SID
    """
    client = _get_client()
    account_sid = settings.twilio_account_sid

    call_sid = payload.get("call_sid", "")
    if not call_sid:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="twilio.call.status",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="twilio.call.status",
            error="Missing required parameter: call_sid",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="GET",
            path=f"/Accounts/{account_sid}/Calls/{call_sid}.json",
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
        tool_id="twilio.call.status",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        call = response.body
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="twilio.call.status",
            data={
                "call_sid": call.get("sid", ""),
                "status": call.get("status", ""),
                "duration": call.get("duration"),
                "start_time": call.get("start_time"),
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="twilio.call.status",
            error=response.error_message or f"Twilio API error: HTTP {response.status_code}",
            receipt_data=receipt,
        )
