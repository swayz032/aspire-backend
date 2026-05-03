from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.middleware.correlation import CorrelationIdMiddleware
from aspire_orchestrator.middleware.sentry_middleware import (
    SentryRequestContextMiddleware,
    _before_send,
    _resolve_dsn,
)


def test_resolve_dsn_prefers_primary_env(monkeypatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://primary.example")
    monkeypatch.setenv("SENTRY_BACKEND_DSN", "https://fallback.example")

    assert _resolve_dsn() == "https://primary.example"


def test_resolve_dsn_falls_back_to_backend_env(monkeypatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setenv("SENTRY_BACKEND_DSN", "https://fallback.example")

    assert _resolve_dsn() == "https://fallback.example"


def test_before_send_scrubs_request_context_pii() -> None:
    event = {
        "request": {
            "headers": {
                "authorization": "Bearer secret-token",
                "x-suite-id": "suite-123",
            },
            "query_string": "email=tony@example.com",
        },
        "extra": {"api_key": "sk-test-abcdef123456"},
        "contexts": {"aspire_request": {"actor_email": "tony@example.com"}},
    }

    scrubbed = _before_send(event, {})

    assert scrubbed is not None
    assert scrubbed["request"]["headers"]["authorization"] == "[Filtered]"
    assert "***@***.***" in scrubbed["request"]["query_string"]
    assert scrubbed["extra"]["api_key"] == "[Filtered]"
    assert scrubbed["contexts"]["aspire_request"]["actor_email"] == "[Filtered]"


def test_request_context_middleware_tags_sentry_scope(monkeypatch) -> None:
    import aspire_orchestrator.middleware.sentry_middleware as sentry_middleware

    class FakeScope:
        def __init__(self) -> None:
            self.tags: dict[str, str] = {}
            self.contexts: dict[str, object] = {}
            self.user: dict[str, str] = {}

        def set_tag(self, key: str, value: str) -> None:
            self.tags[key] = value

        def set_context(self, key: str, value: object) -> None:
            self.contexts[key] = value

        def set_user(self, value: dict[str, str]) -> None:
            self.user = value

    scope = FakeScope()
    breadcrumbs: list[dict[str, object]] = []
    fake_sentry = types.ModuleType("sentry_sdk")
    fake_sentry.get_current_scope = lambda: scope
    fake_sentry.add_breadcrumb = lambda **kwargs: breadcrumbs.append(kwargs)

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sentry)
    monkeypatch.setattr(sentry_middleware, "_initialized", True)

    app = FastAPI()
    app.add_middleware(SentryRequestContextMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/v1/demo")
    async def demo():
        return {"ok": True}

    client = TestClient(app)
    response = client.get(
        "/v1/demo",
        headers={
            "X-Correlation-Id": "corr-test-1",
            "X-Suite-Id": "suite-123",
            "X-Office-Id": "office-456",
            "X-Actor-Id": "actor-789",
        },
    )

    assert response.status_code == 200
    assert scope.tags["surface"] == "backend"
    assert scope.tags["route_family"] == "v1"
    assert scope.tags["correlation_id"] == "corr-test-1"
    assert scope.tags["suite_id"] == "suite-123"
    assert scope.tags["office_id"] == "office-456"
    assert scope.user == {"id": "actor-789"}
    assert breadcrumbs
