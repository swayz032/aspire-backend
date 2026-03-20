from __future__ import annotations

import uuid

import httpx
import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.admin import clear_admin_stores, router as admin_router
from aspire_orchestrator.services.receipt_store import clear_store
from aspire_orchestrator.services.sentry_read import reset_sentry_read_service

_TEST_JWT_SECRET = "test-sentry-admin-sync-secret-1234567890"


def _make_admin_token(sub: str = "ops-admin") -> str:
    return pyjwt.encode({"sub": sub}, _TEST_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(admin_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ASPIRE_ADMIN_JWT_SECRET", _TEST_JWT_SECRET)
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SENTRY_ORG_SLUG", raising=False)
    monkeypatch.delenv("SENTRY_PROJECT_SLUGS", raising=False)
    monkeypatch.delenv("SENTRY_API_BASE_URL", raising=False)
    clear_admin_stores()
    clear_store()
    reset_sentry_read_service()
    yield
    clear_admin_stores()
    clear_store()
    reset_sentry_read_service()


@pytest.fixture
def headers() -> dict[str, str]:
    return {
        "x-admin-token": _make_admin_token(),
        "x-correlation-id": str(uuid.uuid4()),
    }


class _SuccessAsyncClient:
    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, dict[str, str] | None, dict[str, str] | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *, headers=None, params=None):
        self.calls.append((url, headers, params))
        request = httpx.Request("GET", url, headers=headers, params=params)
        if "ava-brain-backend" in url:
            payload = [
                {
                    "id": "101",
                    "shortId": "AVA-BRAIN-101",
                    "title": "Provider timeout spike",
                    "level": "error",
                    "status": "unresolved",
                    "count": "18",
                    "userCount": "3",
                    "firstSeen": "2026-03-19T18:00:00Z",
                    "lastSeen": "2026-03-19T19:00:00Z",
                    "project": {"slug": "ava-brain-backend"},
                    "culprit": "provider_call_log",
                    "permalink": "https://sentry.io/organizations/aspire/issues/101/",
                    "isRegression": True,
                },
            ]
            return httpx.Response(200, request=request, headers={"X-Hits": "1"}, json=payload)

        payload = [
            {
                "id": "102",
                "shortId": "ADMIN-42",
                "title": "Admin render exception",
                "level": "warning",
                "status": "unresolved",
                "count": "4",
                "userCount": "1",
                "firstSeen": "2026-03-19T17:00:00Z",
                "lastSeen": "2026-03-19T18:30:00Z",
                "project": {"slug": "admin-portal-web"},
                "culprit": "SystemHealth.tsx",
                "permalink": "https://sentry.io/organizations/aspire/issues/102/",
                "isRegression": False,
            },
        ]
        return httpx.Response(200, request=request, headers={"X-Hits": "1"}, json=payload)


class _TimeoutAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *, headers=None, params=None):
        raise httpx.ReadTimeout("boom", request=httpx.Request("GET", url))


def test_sentry_summary_and_issues_map_success(client, headers, monkeypatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "token-123")
    monkeypatch.setenv("SENTRY_ORG_SLUG", "aspire")
    monkeypatch.setenv("SENTRY_PROJECT_SLUGS", "ava-brain-backend,admin-portal-web")
    monkeypatch.setattr("aspire_orchestrator.services.sentry_read.httpx.AsyncClient", _SuccessAsyncClient)

    summary_response = client.get("/admin/ops/sentry/summary", headers=headers)
    issues_response = client.get("/admin/ops/sentry/issues?limit=5", headers=headers)

    assert summary_response.status_code == 200
    assert issues_response.status_code == 200

    summary_payload = summary_response.json()
    issues_payload = issues_response.json()

    assert summary_payload["summary"]["source"] == "sentry"
    assert summary_payload["summary"]["configured"] is True
    assert summary_payload["summary"]["open_issue_count"] == 2
    assert summary_payload["summary"]["critical_count"] == 1
    assert summary_payload["summary"]["regression_count"] == 1
    assert summary_payload["summary"]["status"] == "critical"
    assert summary_payload["summary"]["issues_url"] == "https://sentry.io/organizations/aspire/issues/"

    assert issues_payload["source"] == "sentry"
    assert issues_payload["count"] == 2
    assert len(issues_payload["items"]) == 2
    assert issues_payload["items"][0]["id"] == "101"
    assert issues_payload["items"][0]["project_slug"] == "ava-brain-backend"
    assert issues_payload["items"][0]["permalink"].startswith("https://sentry.io/organizations/aspire/issues/")


@pytest.mark.parametrize(
    "path",
    ["/admin/ops/sentry/summary", "/admin/ops/sentry/issues"],
)
def test_sentry_endpoints_require_admin_auth(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHZ_DENIED"


def test_sentry_disabled_fallback(client, headers) -> None:
    summary_response = client.get("/admin/ops/sentry/summary", headers=headers)
    issues_response = client.get("/admin/ops/sentry/issues", headers=headers)

    assert summary_response.status_code == 200
    assert issues_response.status_code == 200

    summary_payload = summary_response.json()
    issues_payload = issues_response.json()

    assert summary_payload["summary"]["source"] == "disabled"
    assert summary_payload["summary"]["configured"] is False
    assert summary_payload["summary"]["status"] == "disabled"
    assert issues_payload["source"] == "disabled"
    assert issues_payload["items"] == []
    assert issues_payload["count"] == 0


def test_sentry_failure_fallback(client, headers, monkeypatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "token-123")
    monkeypatch.setenv("SENTRY_ORG_SLUG", "aspire")
    monkeypatch.setenv("SENTRY_PROJECT_SLUGS", "ava-brain-backend")
    monkeypatch.setattr("aspire_orchestrator.services.sentry_read.httpx.AsyncClient", _TimeoutAsyncClient)

    summary_response = client.get("/admin/ops/sentry/summary", headers=headers)
    issues_response = client.get("/admin/ops/sentry/issues", headers=headers)

    assert summary_response.status_code == 200
    assert issues_response.status_code == 200

    summary_payload = summary_response.json()
    issues_payload = issues_response.json()

    assert summary_payload["summary"]["source"] == "unavailable"
    assert summary_payload["summary"]["configured"] is True
    assert summary_payload["summary"]["status"] == "unavailable"
    assert issues_payload["source"] == "unavailable"
    assert issues_payload["items"] == []
    assert issues_payload["count"] == 0
