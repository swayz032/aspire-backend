"""E2E Provider Status Tests -- Connection Verification for All Providers.

Tests the status/connection endpoints for each financial/integration
provider registered in the Aspire Desktop server.

Endpoints tested:
- GET /api/plaid/status           -- Plaid connection status
- GET /api/quickbooks/status      -- QuickBooks connection status
- GET /api/gusto/status           -- Gusto connection status
- GET /api/stripe-connect/status  -- Stripe Connect connection status

Each endpoint returns a connection status object with at minimum a
``connected`` boolean field.  These tests do NOT require active
provider credentials -- they verify the endpoint responds correctly
regardless of configuration state.

Law compliance:
- Law #9: status endpoints must not expose secrets or credentials
"""

from __future__ import annotations

import pytest
import requests

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.needs_desktop,
]


# =============================================================================
# Shared helpers
# =============================================================================

PROVIDER_ENDPOINTS = [
    ("plaid", "/api/plaid/status"),
    ("quickbooks", "/api/quickbooks/status"),
    ("gusto", "/api/gusto/status"),
    ("stripe_connect", "/api/stripe-connect/status"),
]


# =============================================================================
# Parametrized tests across all providers
# =============================================================================


class TestProviderStatus:
    """Provider status endpoints return connection information."""

    @pytest.mark.parametrize(
        "provider,endpoint",
        PROVIDER_ENDPOINTS,
        ids=[p[0] for p in PROVIDER_ENDPOINTS],
    )
    def test_provider_status_returns_200(
        self,
        http: requests.Session,
        desktop_url: str,
        provider: str,
        endpoint: str,
    ) -> None:
        """Each provider status endpoint returns 200."""
        resp = http.get(f"{desktop_url}{endpoint}", timeout=5)
        assert resp.status_code == 200, (
            f"{provider} status returned {resp.status_code}, expected 200"
        )

    @pytest.mark.parametrize(
        "provider,endpoint",
        PROVIDER_ENDPOINTS,
        ids=[p[0] for p in PROVIDER_ENDPOINTS],
    )
    def test_provider_status_has_connected_field(
        self,
        http: requests.Session,
        desktop_url: str,
        provider: str,
        endpoint: str,
    ) -> None:
        """Each provider status response contains a ``connected`` boolean."""
        resp = http.get(f"{desktop_url}{endpoint}", timeout=5)
        data = resp.json()
        assert "connected" in data, (
            f"{provider} status response missing 'connected' field: {data}"
        )
        assert isinstance(data["connected"], bool), (
            f"{provider} 'connected' should be bool, got {type(data['connected']).__name__}"
        )

    @pytest.mark.parametrize(
        "provider,endpoint",
        PROVIDER_ENDPOINTS,
        ids=[p[0] for p in PROVIDER_ENDPOINTS],
    )
    def test_provider_status_no_secrets(
        self,
        http: requests.Session,
        desktop_url: str,
        provider: str,
        endpoint: str,
    ) -> None:
        """Provider status must not expose secrets or tokens (Law #9)."""
        resp = http.get(f"{desktop_url}{endpoint}", timeout=5)
        text = resp.text.lower()

        secret_indicators = [
            "sk_test_",
            "sk_live_",
            "access_token",
            "secret_key",
            "api_key",
            "bearer ",
            "password",
            "refresh_token",
        ]
        for indicator in secret_indicators:
            assert indicator not in text, (
                f"{provider} status may be leaking secrets: found '{indicator}'"
            )


# =============================================================================
# Individual provider tests with provider-specific fields
# =============================================================================


class TestPlaidStatus:
    """GET /api/plaid/status -- Plaid-specific verification."""

    def test_plaid_status_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Plaid status includes connection count."""
        resp = http.get(f"{desktop_url}/api/plaid/status", timeout=5)
        data = resp.json()
        assert "connected" in data
        # Plaid returns connections count
        assert "connections" in data, (
            "Plaid status should include 'connections' count"
        )


class TestQuickBooksStatus:
    """GET /api/quickbooks/status -- QuickBooks-specific verification."""

    def test_quickbooks_status_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """QuickBooks status includes realmId."""
        resp = http.get(f"{desktop_url}/api/quickbooks/status", timeout=5)
        data = resp.json()
        assert "connected" in data
        assert "realmId" in data, (
            "QuickBooks status should include 'realmId' field"
        )


class TestGustoStatus:
    """GET /api/gusto/status -- Gusto-specific verification."""

    def test_gusto_status_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Gusto status includes connected boolean."""
        resp = http.get(f"{desktop_url}/api/gusto/status", timeout=5)
        data = resp.json()
        assert "connected" in data
        # When not connected, may include detail
        if not data["connected"]:
            assert "detail" in data, (
                "Gusto disconnected status should include 'detail' field"
            )


class TestStripeConnectStatus:
    """GET /api/stripe-connect/status -- Stripe Connect-specific verification."""

    def test_stripe_connect_status_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Stripe Connect status includes accountId."""
        resp = http.get(f"{desktop_url}/api/stripe-connect/status", timeout=5)
        data = resp.json()
        assert "connected" in data
        assert "accountId" in data, (
            "Stripe Connect status should include 'accountId' field"
        )
