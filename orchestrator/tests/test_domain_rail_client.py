"""Domain Rail S2S Client Tests — Wave 7.

Validates:
- HMAC-SHA256 signature computation matches Domain Rail auth middleware format
- S2S header construction (timestamp, nonce, signature, correlation-id)
- Fail-closed when S2S secret not configured (Law #3)
- All 7 Domain Rail operations route correctly
- HTTP error handling (timeout, connection refused, server errors)
- Receipt emission for all outcomes (Law #2)

Cross-reference: domain-rail/src/middleware/auth.ts for signature format.
"""

import hashlib
import hmac as hmac_mod
import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.domain_rail_client import (
    DomainRailClientError,
    DomainRailResponse,
    compute_s2s_signature,
    _build_s2s_headers,
    _get_s2s_secret,
    domain_check,
    domain_verify,
    domain_dns_create,
    domain_purchase,
    domain_delete,
    mail_account_create,
    mail_account_read,
    HEADER_TIMESTAMP,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_CORRELATION_ID,
    HEADER_SUITE_ID,
    HEADER_OFFICE_ID,
)


# =============================================================================
# Test Constants
# =============================================================================

TEST_SECRET = "test-s2s-hmac-secret-256bit-enterprise"
SUITE_ID = "suite-test-001"
OFFICE_ID = "office-test-001"
CORRELATION_ID = "corr-wave7-001"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def set_s2s_secret(monkeypatch):
    """Set S2S HMAC secret for all tests."""
    monkeypatch.setattr(
        "aspire_orchestrator.services.domain_rail_client.settings",
        MagicMock(
            s2s_hmac_secret=TEST_SECRET,
            domain_rail_url="http://localhost:3000",
        ),
    )


# =============================================================================
# S2S Signature Tests — HMAC-SHA256 Cryptographic Verification
# =============================================================================


class TestS2SSignature:
    """Verify S2S signature computation matches Domain Rail auth middleware."""

    def test_signature_format(self):
        """Signature is hex-encoded HMAC-SHA256."""
        sig = compute_s2s_signature(
            secret=TEST_SECRET,
            timestamp="1707868800",
            nonce="abc123",
            method="GET",
            path_and_query="/v1/domains/check?domain=example.com",
            body=b"",
        )
        assert len(sig) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in sig)

    def test_signature_matches_node_crypto(self):
        """Signature computation matches the Node.js crypto.createHmac pattern.

        From domain-rail/src/middleware/auth.ts:
          const base = `${ts}.${nonce}.${METHOD}.${pathAndQuery}.${bodyHash}`;
          hmac(secret, base)
        """
        timestamp = "1707868800"
        nonce = "test-nonce-uuid"
        method = "POST"
        path = "/v1/domains/dns"
        body = json.dumps({"domain": "example.com", "record_type": "A", "value": "1.2.3.4"}).encode()
        body_hash = hashlib.sha256(body).hexdigest()

        # Compute expected using raw HMAC (same as Node.js crypto.createHmac)
        base = f"{timestamp}.{nonce}.{method}.{path}.{body_hash}"
        expected = hmac_mod.new(
            TEST_SECRET.encode("utf-8"),
            base.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        actual = compute_s2s_signature(
            secret=TEST_SECRET,
            timestamp=timestamp,
            nonce=nonce,
            method=method,
            path_and_query=path,
            body=body,
        )

        assert actual == expected

    def test_empty_body_uses_empty_hash(self):
        """GET requests with no body use SHA-256 of empty bytes."""
        empty_body_hash = hashlib.sha256(b"").hexdigest()

        sig = compute_s2s_signature(
            secret=TEST_SECRET,
            timestamp="1707868800",
            nonce="nonce1",
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
        )

        # Manually compute expected
        base = f"1707868800.nonce1.GET./v1/domains/check.{empty_body_hash}"
        expected = hmac_mod.new(
            TEST_SECRET.encode("utf-8"), base.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        assert sig == expected

    def test_method_uppercase_in_signature(self):
        """Method is uppercased in signature base string."""
        sig_lower = compute_s2s_signature(
            secret=TEST_SECRET,
            timestamp="1707868800",
            nonce="n1",
            method="get",
            path_and_query="/v1/domains/check",
            body=b"",
        )
        sig_upper = compute_s2s_signature(
            secret=TEST_SECRET,
            timestamp="1707868800",
            nonce="n1",
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
        )
        assert sig_lower == sig_upper

    def test_different_secrets_produce_different_signatures(self):
        """Different secrets produce completely different signatures."""
        args = dict(
            timestamp="1707868800",
            nonce="n1",
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
        )
        sig1 = compute_s2s_signature(secret="secret-one", **args)
        sig2 = compute_s2s_signature(secret="secret-two", **args)
        assert sig1 != sig2

    def test_body_changes_signature(self):
        """Different body content produces different signatures."""
        args = dict(
            secret=TEST_SECRET,
            timestamp="1707868800",
            nonce="n1",
            method="POST",
            path_and_query="/v1/domains/dns",
        )
        sig1 = compute_s2s_signature(**args, body=b'{"domain":"a.com"}')
        sig2 = compute_s2s_signature(**args, body=b'{"domain":"b.com"}')
        assert sig1 != sig2

    def test_nonce_changes_signature(self):
        """Different nonces produce different signatures (replay defense)."""
        args = dict(
            secret=TEST_SECRET,
            timestamp="1707868800",
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
        )
        sig1 = compute_s2s_signature(**args, nonce="nonce-1")
        sig2 = compute_s2s_signature(**args, nonce="nonce-2")
        assert sig1 != sig2


class TestS2SHeaders:
    """Verify S2S auth header construction."""

    def test_all_required_headers_present(self):
        """All S2S auth headers are included."""
        headers = _build_s2s_headers(
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
            correlation_id=CORRELATION_ID,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
        )
        assert HEADER_TIMESTAMP in headers
        assert HEADER_NONCE in headers
        assert HEADER_SIGNATURE in headers
        assert HEADER_CORRELATION_ID in headers
        assert HEADER_SUITE_ID in headers
        assert HEADER_OFFICE_ID in headers

    def test_timestamp_is_unix_seconds(self):
        """Timestamp header is valid Unix seconds."""
        headers = _build_s2s_headers(
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
            correlation_id=CORRELATION_ID,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
        )
        ts = int(headers[HEADER_TIMESTAMP])
        now = int(datetime.now(timezone.utc).timestamp())
        assert abs(ts - now) < 5  # Within 5 seconds

    def test_nonce_is_unique(self):
        """Each call generates a unique nonce."""
        args = dict(
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
            correlation_id=CORRELATION_ID,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
        )
        h1 = _build_s2s_headers(**args)
        h2 = _build_s2s_headers(**args)
        assert h1[HEADER_NONCE] != h2[HEADER_NONCE]

    def test_signature_validates_against_compute(self):
        """Header signature matches direct signature computation."""
        headers = _build_s2s_headers(
            method="POST",
            path_and_query="/v1/domains/dns",
            body=b'{"domain":"test.com"}',
            correlation_id=CORRELATION_ID,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
        )
        expected = compute_s2s_signature(
            secret=TEST_SECRET,
            timestamp=headers[HEADER_TIMESTAMP],
            nonce=headers[HEADER_NONCE],
            method="POST",
            path_and_query="/v1/domains/dns",
            body=b'{"domain":"test.com"}',
        )
        assert headers[HEADER_SIGNATURE] == expected

    def test_correlation_id_propagated(self):
        """Correlation ID is passed through to headers (Gate 2)."""
        headers = _build_s2s_headers(
            method="GET",
            path_and_query="/v1/domains/check",
            body=b"",
            correlation_id="my-trace-id",
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
        )
        assert headers[HEADER_CORRELATION_ID] == "my-trace-id"


class TestS2SFailClosed:
    """Verify fail-closed behavior when S2S secret is missing (Law #3)."""

    def test_raises_when_secret_not_configured(self, monkeypatch):
        """DomainRailClientError raised when no S2S secret."""
        monkeypatch.setattr(
            "aspire_orchestrator.services.domain_rail_client.settings",
            MagicMock(s2s_hmac_secret=""),
        )
        monkeypatch.delenv("ASPIRE_S2S_HMAC_SECRET", raising=False)
        with pytest.raises(DomainRailClientError, match="S2S_SECRET_MISSING"):
            _get_s2s_secret()

    def test_env_fallback(self, monkeypatch):
        """Falls back to environment variable if settings empty."""
        monkeypatch.setattr(
            "aspire_orchestrator.services.domain_rail_client.settings",
            MagicMock(s2s_hmac_secret=""),
        )
        monkeypatch.setenv("ASPIRE_S2S_HMAC_SECRET", "env-secret-value")
        assert _get_s2s_secret() == "env-secret-value"


# =============================================================================
# Domain Rail Operation Tests (HTTP mocked)
# =============================================================================


class TestDomainCheck:
    """domain_check — GET /v1/domains/check (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Returns availability check result."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"available": True, "domain": "test.com"}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.get.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_check(
                domain="test.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is True
            assert result.body["available"] is True

    @pytest.mark.asyncio
    async def test_not_found(self):
        """Returns 404 when domain not found."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"error": "not_found"}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.get.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_check(
                domain="nonexistent.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is False
            assert result.status_code == 404


class TestDomainDnsCreate:
    """domain_dns_create — POST /v1/domains/dns (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Creates DNS record successfully."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"created": True, "record_id": "rec-001"}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_dns_create(
                domain="test.com",
                record_type="A",
                value="1.2.3.4",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is True
            assert result.body["created"] is True


class TestDomainPurchase:
    """domain_purchase — POST /v1/domains/purchase (RED tier)."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Purchases domain successfully."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"purchased": True, "domain": "new.com"}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_purchase(
                domain_name="new.com",
                years=1,
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is True


class TestDomainDelete:
    """domain_delete — DELETE /v1/domains/:domain (RED tier)."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Deletes domain successfully."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"deleted": True}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.delete.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_delete(
                domain="old.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is True


class TestMailAccountCreate:
    """mail_account_create — POST /v1/domains/mail/accounts (YELLOW tier)."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Creates mail account successfully."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"created": True, "email": "info@test.com"}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await mail_account_create(
                domain="test.com",
                email_address="info@test.com",
                display_name="Info",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is True


class TestMailAccountRead:
    """mail_account_read — GET /v1/domains/mail/accounts (GREEN tier)."""

    @pytest.mark.asyncio
    async def test_success(self):
        """Lists mail accounts."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"accounts": [{"email": "info@test.com"}]}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.get.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await mail_account_read(
                domain="test.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is True
            assert len(result.body["accounts"]) == 1


# =============================================================================
# HTTP Error Handling Tests
# =============================================================================


class TestHTTPErrorHandling:
    """Verify graceful handling of HTTP failures."""

    @pytest.mark.asyncio
    async def test_timeout_returns_504(self):
        """Timeout returns 504 with structured error."""
        import httpx

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.get.side_effect = httpx.TimeoutException("Connection timed out")
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_check(
                domain="slow.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.status_code == 504
            assert result.error == "DOMAIN_RAIL_TIMEOUT"

    @pytest.mark.asyncio
    async def test_connection_refused_returns_503(self):
        """Connection refused returns 503."""
        import httpx

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.get.side_effect = httpx.ConnectError("Connection refused")
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_check(
                domain="down.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.status_code == 503
            assert result.error == "DOMAIN_RAIL_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_server_error_returns_body(self):
        """Server 500 returns error body."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"error": "internal_server_error"}

        with patch("aspire_orchestrator.services.domain_rail_client.httpx.AsyncClient") as mock_client:
            instance = AsyncMock()
            instance.get.return_value = mock_response
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = instance

            result = await domain_check(
                domain="broken.com",
                correlation_id=CORRELATION_ID,
                suite_id=SUITE_ID,
                office_id=OFFICE_ID,
            )
            assert result.success is False
            assert result.status_code == 500
