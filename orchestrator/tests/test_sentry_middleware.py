from __future__ import annotations

from aspire_orchestrator.middleware.sentry_middleware import _resolve_dsn


def test_resolve_dsn_prefers_primary_env(monkeypatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://primary.example")
    monkeypatch.setenv("SENTRY_BACKEND_DSN", "https://fallback.example")

    assert _resolve_dsn() == "https://primary.example"


def test_resolve_dsn_falls_back_to_backend_env(monkeypatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setenv("SENTRY_BACKEND_DSN", "https://fallback.example")

    assert _resolve_dsn() == "https://fallback.example"
