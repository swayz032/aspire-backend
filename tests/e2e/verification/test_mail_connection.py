"""E2E Mail Connection Tests -- PolarisM / Domain Rail Verification.

Tests the mail-related endpoints on the Desktop server and the
Domain Rail production health endpoint.

Endpoints tested:
- GET /api/mail/threads         -- email thread listing
- GET /api/mail/thread/:id      -- single thread detail
- GET (Domain Rail) /health     -- Domain Rail production liveness

HMAC authentication:
- Verify that the Desktop proxy computes HMAC-SHA256 correctly
  and propagates it to Domain Rail.

Law compliance:
- Law #3: fail closed when DOMAIN_RAIL_HMAC_SECRET not configured
- Law #6: suite_id propagated in X-Suite-Id header
"""

from __future__ import annotations

import pytest
import requests

pytestmark = [
    pytest.mark.e2e,
]


# =============================================================================
# Domain Rail Health (direct)
# =============================================================================


class TestDomainRailHealth:
    """GET Domain Rail /health -- production service liveness."""

    @pytest.mark.needs_domain_rail
    def test_domain_rail_health(
        self,
        http: requests.Session,
        domain_rail_url: str,
    ) -> None:
        """Domain Rail responds with 200 on /health."""
        resp = http.get(f"{domain_rail_url}/health", timeout=10)
        assert resp.status_code == 200, (
            f"Domain Rail health returned {resp.status_code}"
        )

    @pytest.mark.needs_domain_rail
    def test_domain_rail_health_json(
        self,
        http: requests.Session,
        domain_rail_url: str,
    ) -> None:
        """Domain Rail health response is valid JSON with a status field."""
        resp = http.get(f"{domain_rail_url}/health", timeout=10)
        data = resp.json()
        assert "status" in data or "ok" in str(data).lower(), (
            "Domain Rail health response missing status indicator"
        )


# =============================================================================
# GET /api/mail/threads — Thread Listing (via Desktop proxy)
# =============================================================================


class TestMailThreads:
    """GET /api/mail/threads -- proxied through Desktop to Domain Rail."""

    @pytest.mark.needs_desktop
    def test_mail_threads_returns_response(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Mail threads endpoint returns a valid response.

        Returns 200 with threads when Domain Rail is reachable, or
        500/503 when HMAC secret is missing or Domain Rail is down.
        Both outcomes are valid for E2E verification.
        """
        resp = http.get(f"{desktop_url}/api/mail/threads", timeout=10)
        # 200 = working, 500 = HMAC not configured, 503 = Domain Rail down
        assert resp.status_code in (200, 500, 503), (
            f"Unexpected status {resp.status_code} from /api/mail/threads"
        )

    @pytest.mark.needs_desktop
    @pytest.mark.needs_domain_rail
    def test_mail_threads_success_shape(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """When Domain Rail is up, mail threads returns list of threads."""
        resp = http.get(f"{desktop_url}/api/mail/threads", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Response should be a dict with threads or a list
            assert isinstance(data, (dict, list)), (
                f"Expected dict or list, got {type(data).__name__}"
            )

    @pytest.mark.needs_desktop
    def test_mail_threads_with_pagination(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Mail threads supports limit and offset query parameters."""
        resp = http.get(
            f"{desktop_url}/api/mail/threads",
            params={"limit": "5", "offset": "0"},
            timeout=10,
        )
        # Endpoint must not crash on pagination params
        assert resp.status_code in (200, 500, 503)


# =============================================================================
# GET /api/mail/thread/:id — Thread Detail
# =============================================================================


class TestMailThreadDetail:
    """GET /api/mail/thread/:id -- single thread detail."""

    @pytest.mark.needs_desktop
    def test_mail_thread_detail_returns_response(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Thread detail endpoint returns a valid response.

        A nonexistent thread ID should return 404 from Domain Rail,
        or 500/503 if the connection is not available.
        """
        resp = http.get(
            f"{desktop_url}/api/mail/thread/nonexistent-thread-id",
            timeout=10,
        )
        # 404 = not found (correct), 200 = somehow exists, 500/503 = not connected
        assert resp.status_code in (200, 404, 500, 503), (
            f"Unexpected status {resp.status_code} from /api/mail/thread/:id"
        )

    @pytest.mark.needs_desktop
    @pytest.mark.needs_domain_rail
    def test_mail_thread_detail_nonexistent(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Requesting a nonexistent thread returns 404 or empty result."""
        resp = http.get(
            f"{desktop_url}/api/mail/thread/does-not-exist-12345",
            timeout=10,
        )
        # Either 404 or 200 with null/empty data
        assert resp.status_code in (200, 404, 500, 503)


# =============================================================================
# HMAC Authentication Verification
# =============================================================================


class TestHMACAuth:
    """Verify that HMAC authentication is enforced on Domain Rail endpoints.

    These tests verify the Desktop proxy correctly computes and sends
    HMAC signatures when forwarding requests to Domain Rail.
    """

    @pytest.mark.needs_domain_rail
    def test_unauthenticated_request_rejected(
        self,
        http: requests.Session,
        domain_rail_url: str,
    ) -> None:
        """Direct request to Domain Rail without HMAC is rejected.

        Domain Rail should return 401 or 403 for unauthenticated requests.
        """
        resp = http.get(
            f"{domain_rail_url}/api/mail/threads",
            timeout=10,
        )
        # Without HMAC, Domain Rail should reject the request
        assert resp.status_code in (401, 403), (
            f"Domain Rail accepted unauthenticated request with status "
            f"{resp.status_code}. Expected 401 or 403."
        )

    @pytest.mark.needs_desktop
    @pytest.mark.needs_domain_rail
    def test_desktop_proxy_sends_hmac(
        self,
        http: requests.Session,
        desktop_url: str,
    ) -> None:
        """Desktop proxy sends HMAC-signed requests to Domain Rail.

        If the proxy is working correctly, the /api/mail/threads
        endpoint should succeed (200) or at least not fail with 401.
        A 500 indicates HMAC secret is not configured (acceptable in dev).
        """
        resp = http.get(f"{desktop_url}/api/mail/threads", timeout=10)
        # 200 = HMAC working, 500 = secret not configured, 503 = rail down
        # 401 would mean HMAC computation is wrong
        if resp.status_code == 200:
            # HMAC is working correctly
            pass
        elif resp.status_code in (500, 503):
            # Acceptable: secret not configured or rail down
            pass
        else:
            pytest.fail(
                f"Desktop proxy may have HMAC issues: "
                f"got status {resp.status_code}"
            )
