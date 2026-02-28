"""Shared fixtures for E2E verification tests.

These tests hit real HTTP endpoints (Desktop server on :5000 and Gateway on :3100,
Orchestrator on :8000).  Every fixture degrades gracefully when the target
server is not reachable so the test run can still collect / skip cleanly.

Convention: all E2E tests carry the ``@pytest.mark.e2e`` marker so they
can be selected (or excluded) with ``pytest -m e2e``.
"""

from __future__ import annotations

import os
import socket
from typing import Generator

import pytest
import requests

# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers to avoid PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "e2e: End-to-end integration test")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if *host:port* accepts TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# URL fixtures
# ---------------------------------------------------------------------------

DESKTOP_DEFAULT = "http://localhost:3100"
GATEWAY_DEFAULT = "http://localhost:3100"
ORCHESTRATOR_DEFAULT = "http://localhost:8000"
DOMAIN_RAIL_DEFAULT = "https://domain-rail-production.up.railway.app"


@pytest.fixture(scope="session")
def desktop_url() -> str:
    """Base URL for the Aspire Desktop server."""
    return os.environ.get("DESKTOP_URL", DESKTOP_DEFAULT)


@pytest.fixture(scope="session")
def gateway_url() -> str:
    """Base URL for the Aspire Gateway (TypeScript/Express on 3100)."""
    return os.environ.get("GATEWAY_URL", GATEWAY_DEFAULT)


@pytest.fixture(scope="session")
def orchestrator_url() -> str:
    """Base URL for the Python FastAPI orchestrator."""
    return os.environ.get("ORCHESTRATOR_URL", ORCHESTRATOR_DEFAULT)


@pytest.fixture(scope="session")
def domain_rail_url() -> str:
    """Base URL for the Domain Rail production service."""
    return os.environ.get("DOMAIN_RAIL_URL", DOMAIN_RAIL_DEFAULT)


# ---------------------------------------------------------------------------
# Tenant / auth fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def suite_id() -> str:
    """Test suite ID (tenant A).  Can be overridden via env var."""
    return os.environ.get("E2E_SUITE_ID", "00000000-0000-0000-0000-000000000001")


@pytest.fixture(scope="session")
def office_id() -> str:
    """Test office ID.  Can be overridden via env var."""
    return os.environ.get("E2E_OFFICE_ID", "00000000-0000-0000-0000-000000000011")


@pytest.fixture(scope="session")
def auth_headers(suite_id: str, office_id: str) -> dict[str, str]:
    """Headers required for authenticated requests in dev-auth mode."""
    return {
        "X-Suite-Id": suite_id,
        "X-Office-Id": office_id,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Reachability-based skip helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def desktop_reachable(desktop_url: str) -> bool:
    """True when the Desktop server responds to /api/health."""
    try:
        r = requests.get(f"{desktop_url}/api/health", timeout=3)
        return r.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


@pytest.fixture(scope="session")
def gateway_reachable(gateway_url: str) -> bool:
    """True when the Gateway server responds to /healthz."""
    try:
        r = requests.get(f"{gateway_url}/healthz", timeout=3)
        return r.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


@pytest.fixture(scope="session")
def orchestrator_reachable(orchestrator_url: str) -> bool:
    """True when the Orchestrator responds to /healthz."""
    try:
        r = requests.get(f"{orchestrator_url}/healthz", timeout=3)
        return r.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


@pytest.fixture(scope="session")
def domain_rail_reachable(domain_rail_url: str) -> bool:
    """True when Domain Rail responds to /health."""
    try:
        r = requests.get(f"{domain_rail_url}/health", timeout=5)
        return r.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


@pytest.fixture(autouse=True)
def _skip_if_no_desktop(request: pytest.FixtureRequest, desktop_reachable: bool) -> None:
    """Auto-skip tests that need the Desktop server when it is not reachable.

    Tests that do NOT need the Desktop server should use
    ``@pytest.mark.usefixtures()`` override or a separate marker.
    """
    marker = request.node.get_closest_marker("needs_desktop")
    if marker is not None and not desktop_reachable:
        pytest.skip("Desktop server not reachable")


@pytest.fixture(autouse=True)
def _skip_if_no_orchestrator(request: pytest.FixtureRequest, orchestrator_reachable: bool) -> None:
    """Auto-skip tests that need the Orchestrator when it is not reachable."""
    marker = request.node.get_closest_marker("needs_orchestrator")
    if marker is not None and not orchestrator_reachable:
        pytest.skip("Orchestrator not reachable")


@pytest.fixture(autouse=True)
def _skip_if_no_domain_rail(request: pytest.FixtureRequest, domain_rail_reachable: bool) -> None:
    """Auto-skip tests that need Domain Rail when it is not reachable."""
    marker = request.node.get_closest_marker("needs_domain_rail")
    if marker is not None and not domain_rail_reachable:
        pytest.skip("Domain Rail not reachable")


# ---------------------------------------------------------------------------
# HTTP session fixture (connection pooling)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def http() -> Generator[requests.Session, None, None]:
    """Shared requests.Session for connection pooling across tests."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    yield session
    session.close()
