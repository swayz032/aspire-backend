"""Abstract Provider Client — Base class for all external provider integrations.

Implements:
  - Structured request/response with correlation tracking
  - Timeout enforcement (configurable per provider, default 15s)
  - Circuit breaker (5 failures in 60s → open for 30s, fail-closed per Law #3)
  - Exponential backoff with jitter (max 3 retries on retryable errors)
  - Receipt data emission for all outcomes (Law #2)
  - Idempotency key forwarding (for providers that support it)
  - PII-safe logging (never logs request bodies, only metadata)

Subclass contract:
  1. Set `provider_id`, `base_url`, `timeout_seconds`
  2. Implement `_authenticate_headers()` for auth strategy
  3. Call `self._request()` for all HTTP calls
  4. Override `_parse_error()` for provider-specific error mapping

Pattern reference: domain_rail_client.py (S2S HMAC variant of this base)
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.providers.error_codes import (
    InternalErrorCode,
    ProviderErrorCategory,
    error_from_http_status,
)
from aspire_orchestrator.services.provider_call_logger import get_provider_call_logger

logger = logging.getLogger(__name__)

# Maximum request body size for logging (safety, never log full payloads)
_MAX_LOG_BODY_CHARS = 80


class CircuitState(str, Enum):
    """Circuit breaker state machine."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass(frozen=True)
class ProviderRequest:
    """Structured request to a provider."""

    method: str
    path: str
    body: dict[str, Any] | None = None
    query_params: dict[str, str] | None = None
    correlation_id: str = ""
    suite_id: str = ""
    office_id: str = ""
    idempotency_key: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResponse:
    """Structured response from a provider."""

    status_code: int
    body: dict[str, Any]
    success: bool
    error_code: InternalErrorCode | None = None
    error_message: str | None = None
    provider_request_id: str | None = None
    latency_ms: float = 0.0

    @property
    def receipt_data(self) -> dict[str, Any]:
        """Extract receipt-relevant fields."""
        return {
            "provider_status_code": self.status_code,
            "provider_request_id": self.provider_request_id,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code.value if self.error_code else None,
        }


class ProviderError(Exception):
    """Provider-level error with canonical error code."""

    def __init__(
        self,
        code: InternalErrorCode,
        message: str,
        provider_id: str = "",
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.provider_id = provider_id
        self.status_code = status_code
        super().__init__(f"[{provider_id}:{code.value}] {message}")


class CircuitBreaker:
    """Per-provider circuit breaker (Law #3: fail-closed).

    Parameters:
        failure_threshold: Number of failures before opening circuit
        recovery_timeout_s: Seconds to wait before half-open test
        window_s: Rolling window for counting failures
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
        window_s: float = 60.0,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_s
        self._window = window_s
        self._state = CircuitState.CLOSED
        self._failures: list[float] = []
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        """Record a successful call — closes circuit if half-open."""
        self._failures.clear()
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failure — may open circuit."""
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < self._window]
        self._failures.append(now)

        if len(self._failures) >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at = now

    def check(self) -> None:
        """Check if circuit allows request. Raises ProviderError if open."""
        state = self.state
        if state == CircuitState.OPEN:
            raise ProviderError(
                code=InternalErrorCode.NETWORK_CIRCUIT_OPEN,
                message=f"Circuit breaker OPEN — provider unavailable "
                f"({len(self._failures)} failures in {self._window}s window)",
            )


class BaseProviderClient(ABC):
    """Abstract base for all provider HTTP clients.

    Subclasses must implement:
      - `_authenticate_headers(request)` — return auth headers for the request
      - `_parse_error(status_code, body)` — map provider error to InternalErrorCode

    Optional overrides:
      - `_parse_response(raw_body)` — custom response parsing
      - `_should_retry(error)` — custom retry logic
    """

    # Subclass must set these
    provider_id: str = ""
    base_url: str = ""
    timeout_seconds: float = 15.0
    max_retries: int = 3
    idempotency_support: bool = False

    def __init__(self) -> None:
        self._circuit = CircuitBreaker()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @abstractmethod
    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Return authentication headers for a request.

        API key clients return static headers.
        OAuth2 clients may refresh tokens before returning.
        """

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        """Map provider HTTP response to internal error code.

        Override in subclass for provider-specific error parsing.
        Default uses HTTP status code mapping.
        """
        return error_from_http_status(status_code)

    def _parse_response(self, raw_body: bytes) -> dict[str, Any]:
        """Parse response body to dict. Override for non-JSON providers."""
        import json
        try:
            return json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, ValueError):
            return {"raw": raw_body.decode("utf-8", errors="replace")[:500]}

    def _prepare_body(self, request: ProviderRequest) -> tuple[str, bytes | None]:
        """Prepare request body and return (content_type, body_bytes).

        Default: JSON encoding. Override in subclasses for different formats
        (e.g., Stripe uses application/x-www-form-urlencoded).
        """
        import json
        body_bytes = json.dumps(request.body).encode() if request.body else None
        return "application/json", body_bytes

    def _compute_idempotency_key(self, request: ProviderRequest) -> str:
        """Generate idempotency key from request content.

        Format: sha256(suite_id + correlation_id + method + path + sorted_body)
        This ensures identical business requests get the same key.
        """
        if request.idempotency_key:
            return request.idempotency_key

        import json
        body_str = json.dumps(request.body, sort_keys=True) if request.body else ""
        payload = f"{request.suite_id}:{request.correlation_id}:{request.method}:{request.path}:{body_str}"
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with jitter. Returns seconds to wait."""
        import random
        base = min(2 ** attempt, 16)  # Cap at 16s
        jitter = random.uniform(0, base * 0.5)
        return base + jitter

    async def _request(self, request: ProviderRequest) -> ProviderResponse:
        """Execute an HTTP request with full governance wrapping.

        Handles: circuit breaker, auth, timeout, retry, receipt data.
        This is the ONLY method subclasses should use for HTTP calls.
        """
        # Circuit breaker check (Law #3 — fail-closed)
        try:
            self._circuit.check()
        except ProviderError as e:
            # Log circuit breaker rejection (Wave 6 fix — all failure paths logged)
            try:
                get_provider_call_logger().log_call(
                    provider=self.provider_id,
                    action=f"{request.method.upper()} {request.path}",
                    correlation_id=request.correlation_id or "",
                    suite_id=request.suite_id or "",
                    http_status=503,
                    success=False,
                    error_code=e.code.value if hasattr(e.code, 'value') else str(e.code),
                    retry_count=0,
                    latency_ms=0,
                )
            except Exception:
                pass
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

        # Encode body (subclasses can override _prepare_body for different encoding)
        content_type, body_bytes = self._prepare_body(request)

        # Build final headers
        headers = {
            "Content-Type": content_type,
            "Accept": "application/json",
            "X-Correlation-Id": request.correlation_id or str(uuid.uuid4()),
        }
        headers.update(auth_headers)
        headers.update(request.extra_headers)

        # Add idempotency key if supported
        if self.idempotency_support and request.method.upper() in ("POST", "PUT", "PATCH"):
            idem_key = self._compute_idempotency_key(request)
            headers["Idempotency-Key"] = idem_key

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
                logger.info(
                    "Provider retry: %s %s attempt=%d, backoff=%.1fs",
                    self.provider_id, request.path, attempt, wait,
                )
                await asyncio.sleep(wait)

            start = time.monotonic()

            try:
                client = await self._get_client()

                if request.method.upper() == "GET":
                    response = await client.get(url, headers=headers)
                elif request.method.upper() == "POST":
                    response = await client.post(url, headers=headers, content=body_bytes)
                elif request.method.upper() == "PUT":
                    response = await client.put(url, headers=headers, content=body_bytes)
                elif request.method.upper() == "PATCH":
                    response = await client.patch(url, headers=headers, content=body_bytes)
                elif request.method.upper() == "DELETE":
                    response = await client.delete(url, headers=headers)
                else:
                    raise ProviderError(
                        code=InternalErrorCode.INPUT_INVALID_FORMAT,
                        message=f"Unsupported HTTP method: {request.method}",
                        provider_id=self.provider_id,
                    )

                latency_ms = (time.monotonic() - start) * 1000
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
                    # Log successful provider call (Wave 2B — F3 fix)
                    try:
                        get_provider_call_logger().log_call(
                            provider=self.provider_id,
                            action=f"{request.method.upper()} {request.path}",
                            correlation_id=request.correlation_id or "",
                            suite_id=request.suite_id or "",
                            http_status=response.status_code,
                            success=True,
                            retry_count=attempt,
                            latency_ms=latency_ms,
                        )
                    except Exception:
                        pass  # Logger failure never blocks provider calls
                    return ProviderResponse(
                        status_code=response.status_code,
                        body=parsed,
                        success=True,
                        provider_request_id=response.headers.get("x-request-id"),
                        latency_ms=latency_ms,
                    )

                # Error response — map to internal code
                error_code = self._parse_error(response.status_code, parsed)

                if error_code.circuit_breaker_relevant:
                    self._circuit.record_failure()

                if error_code.retryable and attempt < self.max_retries:
                    last_error = ProviderError(
                        code=error_code,
                        message=parsed.get("error", parsed.get("message", f"HTTP {response.status_code}")),
                        provider_id=self.provider_id,
                        status_code=response.status_code,
                    )
                    attempt += 1
                    continue

                # Log non-retryable error (Wave 2B — F3 fix)
                try:
                    get_provider_call_logger().log_call(
                        provider=self.provider_id,
                        action=f"{request.method.upper()} {request.path}",
                        correlation_id=request.correlation_id or "",
                        suite_id=request.suite_id or "",
                        http_status=response.status_code,
                        success=False,
                        error_code=error_code.value if hasattr(error_code, 'value') else str(error_code),
                        retry_count=attempt,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    pass
                return ProviderResponse(
                    status_code=response.status_code,
                    body=parsed,
                    success=False,
                    error_code=error_code,
                    error_message=parsed.get("error", parsed.get("message", f"HTTP {response.status_code}")),
                    provider_request_id=response.headers.get("x-request-id"),
                    latency_ms=latency_ms,
                )

            except httpx.TimeoutException:
                latency_ms = (time.monotonic() - start) * 1000
                self._circuit.record_failure()
                logger.warning(
                    "Provider timeout: %s %s %s (%.0fms, attempt=%d)",
                    self.provider_id, request.method, request.path,
                    latency_ms, attempt,
                )
                # Log timeout (Wave 6 fix — all failure paths logged)
                try:
                    get_provider_call_logger().log_call(
                        provider=self.provider_id,
                        action=f"{request.method.upper()} {request.path}",
                        correlation_id=request.correlation_id or "",
                        suite_id=request.suite_id or "",
                        http_status=408,
                        success=False,
                        error_code="TIMEOUT",
                        retry_count=attempt,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    pass
                last_error = ProviderError(
                    code=InternalErrorCode.NETWORK_TIMEOUT,
                    message=f"Timeout after {self.timeout_seconds}s",
                    provider_id=self.provider_id,
                )
                attempt += 1
                continue

            except httpx.ConnectError:
                latency_ms = (time.monotonic() - start) * 1000
                self._circuit.record_failure()
                logger.error(
                    "Provider connection refused: %s %s %s",
                    self.provider_id, request.method, request.path,
                )
                # Log connection failure (Wave 6 fix — all failure paths logged)
                try:
                    get_provider_call_logger().log_call(
                        provider=self.provider_id,
                        action=f"{request.method.upper()} {request.path}",
                        correlation_id=request.correlation_id or "",
                        suite_id=request.suite_id or "",
                        http_status=503,
                        success=False,
                        error_code="CONNECTION_REFUSED",
                        retry_count=attempt,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    pass
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
                latency_ms = (time.monotonic() - start) * 1000
                self._circuit.record_failure()
                logger.error(
                    "Provider unexpected error: %s %s %s — %s",
                    self.provider_id, request.method, request.path, type(e).__name__,
                )
                # Log unexpected error (Wave 6 fix — all failure paths logged)
                try:
                    get_provider_call_logger().log_call(
                        provider=self.provider_id,
                        action=f"{request.method.upper()} {request.path}",
                        correlation_id=request.correlation_id or "",
                        suite_id=request.suite_id or "",
                        http_status=500,
                        success=False,
                        error_code="UNEXPECTED_ERROR",
                        retry_count=attempt,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    pass
                sanitized_msg = f"{type(e).__name__}: Provider operation failed"
                # Law #9: Don't log str(e) — may contain query params with PII (emails, tokens)
                logger.error(
                    "Provider %s unexpected error: %s",
                    self.provider_id, type(e).__name__,
                )
                return ProviderResponse(
                    status_code=500,
                    body={"error": "PROVIDER_ERROR", "message": sanitized_msg},
                    success=False,
                    error_code=InternalErrorCode.SERVER_INTERNAL_ERROR,
                    error_message=sanitized_msg,
                )

        # Exhausted retries — log final failure (Wave 6 fix)
        error_code = last_error.code if last_error else InternalErrorCode.SERVER_INTERNAL_ERROR
        error_msg = last_error.message if last_error else "Max retries exhausted"
        try:
            get_provider_call_logger().log_call(
                provider=self.provider_id,
                action=f"{request.method.upper()} {request.path}",
                correlation_id=request.correlation_id or "",
                suite_id=request.suite_id or "",
                http_status=503,
                success=False,
                error_code="RETRIES_EXHAUSTED",
                retry_count=self.max_retries,
                latency_ms=0,
            )
        except Exception:
            pass

        return ProviderResponse(
            status_code=last_error.status_code or 503 if last_error else 503,
            body={"error": error_code.value, "message": error_msg},
            success=False,
            error_code=error_code,
            error_message=error_msg,
        )

    def make_receipt_data(
        self,
        *,
        correlation_id: str,
        suite_id: str,
        office_id: str,
        tool_id: str,
        risk_tier: str,
        outcome: Outcome,
        reason_code: str,
        capability_token_id: str | None = None,
        capability_token_hash: str | None = None,
        provider_response: ProviderResponse | None = None,
    ) -> dict[str, Any]:
        """Build receipt data for a provider call (Law #2).

        Includes provider-specific metadata (latency, request ID) alongside
        standard receipt fields.
        """
        data: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "actor_type": "system",
            "actor_id": f"provider.{self.provider_id}",
            "action_type": f"execute.{tool_id}",
            "risk_tier": risk_tier,
            "tool_used": tool_id,
            "capability_token_id": capability_token_id,
            "capability_token_hash": capability_token_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome.value,
            "reason_code": reason_code,
            "receipt_type": ReceiptType.TOOL_EXECUTION.value,
            "receipt_hash": "",
        }

        if provider_response:
            data["provider_metadata"] = provider_response.receipt_data

        return data

    # ------------------------------------------------------------------
    # Preflight + Simulate (Phase 2.5 — Provider Adapter Extensions)
    # ------------------------------------------------------------------

    async def preflight(self, request: ProviderRequest) -> PreflightResult:
        """Dry-run validation — checks inputs WITHOUT making an API call.

        Default implementation validates request structure only.
        Subclasses can override for provider-specific preflight logic
        (e.g., Stripe: validate customer_id format, Gusto: check payroll period).

        Returns PreflightResult with valid=True/False and diagnostics.
        """
        issues: list[str] = []

        if not request.method:
            issues.append("method is required")
        if not request.path:
            issues.append("path is required")
        if not request.suite_id:
            issues.append("suite_id is required (Law #6: tenant isolation)")
        if not request.correlation_id:
            issues.append("correlation_id is required (Law #2: receipt tracing)")

        # Check circuit breaker — fail-closed when circuit is OPEN (Law #3)
        circuit_warning: str | None = None
        if self._circuit.state == CircuitState.OPEN:
            circuit_warning = (
                f"Circuit breaker OPEN for {self.provider_id} — "
                f"request will be rejected at execution time"
            )
            issues.append("circuit breaker OPEN")

        return PreflightResult(
            valid=len(issues) == 0,
            issues=issues,
            circuit_warning=circuit_warning,
            provider_id=self.provider_id,
        )

    async def simulate(self, request: ProviderRequest) -> SimulateResult:
        """Mock execution for testing — returns realistic fake response.

        Default implementation returns a generic success response.
        Subclasses can override for provider-specific simulation
        (e.g., Stripe: return a fake invoice object, Gusto: return mock payroll).

        NEVER makes actual API calls. Used for:
          - Integration testing without live credentials
          - UI preview of what an action would produce
          - Partner approval demos (Gusto/Plaid submission packets)
        """
        preflight_result = await self.preflight(request)
        if not preflight_result.valid:
            return SimulateResult(
                success=False,
                simulated_response={"error": "PREFLIGHT_FAILED", "issues": preflight_result.issues},
                latency_estimate_ms=0.0,
                provider_id=self.provider_id,
                receipt_data=self.make_receipt_data(
                    correlation_id=request.correlation_id or "simulate",
                    suite_id=request.suite_id or "simulate",
                    office_id=request.office_id or "simulate",
                    tool_id=f"{self.provider_id}.simulate",
                    risk_tier="green",
                    outcome=Outcome.FAILED,
                    reason_code="PREFLIGHT_FAILED",
                ),
            )

        return SimulateResult(
            success=True,
            simulated_response={
                "id": f"sim_{uuid.uuid4().hex[:12]}",
                "provider": self.provider_id,
                "action": f"{request.method} {request.path}",
                "status": "simulated",
                "note": "This is a simulated response — no API call was made",
            },
            latency_estimate_ms=self.timeout_seconds * 100,  # Rough estimate
            provider_id=self.provider_id,
            receipt_data=self.make_receipt_data(
                correlation_id=request.correlation_id or "simulate",
                suite_id=request.suite_id or "simulate",
                office_id=request.office_id or "simulate",
                tool_id=f"{self.provider_id}.simulate",
                risk_tier="green",
                outcome=Outcome.SUCCESS,
                reason_code="SIMULATED",
            ),
        )


@dataclass(frozen=True)
class PreflightResult:
    """Result of a preflight validation check."""

    valid: bool
    issues: list[str]
    provider_id: str
    circuit_warning: str | None = None

    @property
    def summary(self) -> str:
        if self.valid:
            msg = f"Preflight OK for {self.provider_id}"
            if self.circuit_warning:
                msg += f" (WARNING: {self.circuit_warning})"
            return msg
        return f"Preflight FAILED for {self.provider_id}: {', '.join(self.issues)}"


@dataclass(frozen=True)
class SimulateResult:
    """Result of a simulated (mock) provider call."""

    success: bool
    simulated_response: dict[str, Any]
    latency_estimate_ms: float
    provider_id: str
    receipt_data: dict[str, Any]
