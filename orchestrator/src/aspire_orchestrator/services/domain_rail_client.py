"""Domain Rail S2S Client — HMAC-authenticated HTTP calls (Law #7).

Per Phase 0C architecture:
  - Domain Rail runs on Railway at domain-rail.railway.internal (private)
  - S2S auth uses HMAC-SHA256 signatures with timestamp/nonce/body-hash
  - Signature format: HMAC(secret, f"{ts}.{nonce}.{METHOD}.{pathAndQuery}.{sha256(body)}")
  - All calls propagate correlation_id for tracing (Gate 2)
  - Fail-closed if S2S secret not configured (Law #3)

Endpoints mapped from domain-rail/src/routes/domains.ts:
  GET  /v1/domains/check?domain=<name>       — GREEN: availability check
  GET  /v1/domains/verify?domain=<name>       — GREEN: ownership verification
  POST /v1/domains/dns                        — YELLOW: create DNS record
  POST /v1/domains/purchase                   — RED: domain purchase
  DELETE /v1/domains/:domain                  — RED: domain deletion
  POST /v1/domains/mail/accounts              — YELLOW: create mail account
  GET  /v1/domains/mail/accounts?domain=<name> — GREEN: list mail accounts
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from aspire_orchestrator.config.settings import settings

logger = logging.getLogger(__name__)

# Timeout for Domain Rail HTTP calls (15s — matches provider config)
DOMAIN_RAIL_TIMEOUT_SECONDS = 15

# S2S auth header constants
HEADER_TIMESTAMP = "x-aspire-timestamp"
HEADER_NONCE = "x-aspire-nonce"
HEADER_SIGNATURE = "x-aspire-signature"
HEADER_CORRELATION_ID = "x-correlation-id"
HEADER_SUITE_ID = "x-suite-id"
HEADER_OFFICE_ID = "x-office-id"


@dataclass(frozen=True)
class DomainRailResponse:
    """Response from Domain Rail."""

    status_code: int
    body: dict[str, Any]
    success: bool
    error: str | None = None


class DomainRailClientError(Exception):
    """Domain Rail client-level error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _get_s2s_secret() -> str:
    """Get S2S HMAC secret. Fail closed if not configured (Law #3)."""
    import os

    secret = settings.s2s_hmac_secret
    if not secret:
        secret = os.environ.get("ASPIRE_S2S_HMAC_SECRET", "")
    if not secret:
        raise DomainRailClientError(
            "S2S_SECRET_MISSING",
            "S2S HMAC secret not configured. "
            "Fail-closed per Law #3: cannot call Domain Rail without authentication.",
        )
    return secret


def compute_s2s_signature(
    *,
    secret: str,
    timestamp: str,
    nonce: str,
    method: str,
    path_and_query: str,
    body: bytes,
) -> str:
    """Compute S2S HMAC-SHA256 signature matching Domain Rail auth middleware.

    Format: HMAC-SHA256(secret, f"{timestamp}.{nonce}.{METHOD}.{pathAndQuery}.{sha256(body)}")
    """
    body_hash = hashlib.sha256(body).hexdigest()
    base = f"{timestamp}.{nonce}.{method.upper()}.{path_and_query}.{body_hash}"
    return hmac_mod.new(
        secret.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_s2s_headers(
    *,
    method: str,
    path_and_query: str,
    body: bytes,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> dict[str, str]:
    """Build S2S authentication headers for a Domain Rail request."""
    secret = _get_s2s_secret()
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    nonce = uuid.uuid4().hex

    signature = compute_s2s_signature(
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
        method=method,
        path_and_query=path_and_query,
        body=body,
    )

    return {
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: signature,
        HEADER_CORRELATION_ID: correlation_id,
        HEADER_SUITE_ID: suite_id,
        HEADER_OFFICE_ID: office_id,
        "Content-Type": "application/json",
    }


async def _call_domain_rail(
    *,
    method: str,
    path: str,
    query_params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Make an authenticated HTTP call to Domain Rail.

    Handles S2S HMAC signing, timeouts, and structured error responses.
    """
    base_url = settings.domain_rail_url.rstrip("/")
    body_bytes = json.dumps(body or {}).encode("utf-8") if body else b""

    # Build path with query string for signature computation
    path_and_query = path
    if query_params:
        qs = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
        path_and_query = f"{path}?{qs}"

    url = f"{base_url}{path_and_query}"

    headers = _build_s2s_headers(
        method=method,
        path_and_query=path_and_query,
        body=body_bytes if body else b"",
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )

    logger.info(
        "Domain Rail request: %s %s (suite=%s, corr=%s)",
        method, path,
        suite_id[:8] if len(suite_id) > 8 else suite_id,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    try:
        async with httpx.AsyncClient(timeout=DOMAIN_RAIL_TIMEOUT_SECONDS) as client:
            if method.upper() == "GET":
                response = await client.get(url, headers=headers)
            elif method.upper() == "POST":
                response = await client.post(
                    url,
                    headers=headers,
                    content=body_bytes,
                )
            elif method.upper() == "DELETE":
                response = await client.delete(url, headers=headers)
            else:
                raise DomainRailClientError(
                    "UNSUPPORTED_METHOD",
                    f"HTTP method {method} not supported",
                )

        try:
            response_body = response.json()
        except Exception:
            response_body = {"raw": response.text[:500]}

        success = 200 <= response.status_code < 300

        logger.info(
            "Domain Rail response: %s %s → %d (success=%s)",
            method, path, response.status_code, success,
        )

        return DomainRailResponse(
            status_code=response.status_code,
            body=response_body,
            success=success,
            error=response_body.get("error") if not success else None,
        )

    except httpx.TimeoutException:
        logger.error("Domain Rail timeout: %s %s", method, path)
        return DomainRailResponse(
            status_code=504,
            body={"error": "DOMAIN_RAIL_TIMEOUT"},
            success=False,
            error="DOMAIN_RAIL_TIMEOUT",
        )
    except httpx.ConnectError:
        logger.error("Domain Rail connection refused: %s %s", method, path)
        return DomainRailResponse(
            status_code=503,
            body={"error": "DOMAIN_RAIL_UNAVAILABLE"},
            success=False,
            error="DOMAIN_RAIL_UNAVAILABLE",
        )
    except DomainRailClientError:
        raise
    except Exception as e:
        logger.error("Domain Rail unexpected error: %s %s — %s", method, path, e)
        return DomainRailResponse(
            status_code=500,
            body={"error": "DOMAIN_RAIL_ERROR", "message": str(e)},
            success=False,
            error="DOMAIN_RAIL_ERROR",
        )


# =============================================================================
# Domain Rail Operations — mapped from domain-rail/src/routes/domains.ts
# =============================================================================


async def domain_check(
    *,
    domain: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Check domain availability (GREEN tier)."""
    return await _call_domain_rail(
        method="GET",
        path="/v1/domains/check",
        query_params={"domain": domain},
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )


async def domain_verify(
    *,
    domain: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Verify domain ownership (GREEN tier)."""
    return await _call_domain_rail(
        method="GET",
        path="/v1/domains/verify",
        query_params={"domain": domain},
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )


async def domain_dns_create(
    *,
    domain: str,
    record_type: str,
    value: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Create DNS record (YELLOW tier)."""
    return await _call_domain_rail(
        method="POST",
        path="/v1/domains/dns",
        body={
            "domain": domain,
            "record_type": record_type,
            "value": value,
        },
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )


async def domain_purchase(
    *,
    domain_name: str,
    years: int,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Purchase domain (RED tier)."""
    return await _call_domain_rail(
        method="POST",
        path="/v1/domains/purchase",
        body={
            "domain_name": domain_name,
            "years": years,
        },
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )


async def domain_delete(
    *,
    domain: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Delete domain (RED tier)."""
    return await _call_domain_rail(
        method="DELETE",
        path=f"/v1/domains/{domain}",
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )


async def mail_account_create(
    *,
    domain: str,
    email_address: str,
    display_name: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """Create mail account (YELLOW tier)."""
    return await _call_domain_rail(
        method="POST",
        path="/v1/domains/mail/accounts",
        body={
            "domain": domain,
            "email_address": email_address,
            "display_name": display_name,
        },
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )


async def mail_account_read(
    *,
    domain: str,
    correlation_id: str,
    suite_id: str,
    office_id: str,
) -> DomainRailResponse:
    """List mail accounts for domain (GREEN tier)."""
    return await _call_domain_rail(
        method="GET",
        path="/v1/domains/mail/accounts",
        query_params={"domain": domain},
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
    )
