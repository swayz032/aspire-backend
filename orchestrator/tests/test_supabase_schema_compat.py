from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import aspire_orchestrator.routes.admin as admin_routes
import aspire_orchestrator.services.sre_triage as sre_triage
from aspire_orchestrator.routes.admin import clear_admin_stores
from aspire_orchestrator.server import app
from aspire_orchestrator.services.receipt_store import clear_store

_TEST_JWT_SECRET = "test-admin-jwt-secret-for-testing"


def _make_admin_token(sub: str = "admin-test") -> str:
    return pyjwt.encode({"sub": sub}, _TEST_JWT_SECRET, algorithm="HS256")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_value(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


class _FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _FakeQuery:
    def __init__(self, client: "_FakeSupabaseClient", table_name: str):
        self._client = client
        self._table_name = table_name
        self._selected = "*"
        self._count = None
        self._filters: list[tuple[str, str, object]] = []
        self._limit: int | None = None
        self._order_by: tuple[str, bool] | None = None

    def select(self, columns: str, count: str | None = None):
        self._selected = columns
        self._count = count
        self._client.selected_columns.setdefault(self._table_name, []).append(columns)
        return self

    def eq(self, column: str, value):
        self._filters.append(("eq", column, value))
        return self

    def gte(self, column: str, value):
        self._filters.append(("gte", column, value))
        return self

    def in_(self, column: str, values):
        self._filters.append(("in", column, tuple(values)))
        return self

    def ilike(self, column: str, value: str):
        self._filters.append(("ilike", column, value.lower().replace("%", "")))
        return self

    def order(self, column: str, desc: bool = False):
        self._order_by = (column, desc)
        return self

    def limit(self, value: int):
        self._limit = value
        return self

    def execute(self):
        error = self._client.errors.get(self._table_name)
        if error is not None:
            raise error

        rows = [dict(row) for row in self._client.tables.get(self._table_name, [])]
        for op, column, expected in self._filters:
            if op == "eq":
                rows = [row for row in rows if row.get(column) == expected]
            elif op == "gte":
                rhs = _coerce_value(expected)
                rows = [
                    row
                    for row in rows
                    if row.get(column) is not None and _coerce_value(row.get(column)) >= rhs
                ]
            elif op == "in":
                rows = [row for row in rows if row.get(column) in expected]
            elif op == "ilike":
                rows = [
                    row
                    for row in rows
                    if expected in str(row.get(column) or "").strip().lower()
                ]

        if self._order_by is not None:
            column, desc = self._order_by
            rows.sort(key=lambda row: _coerce_value(row.get(column) or ""), reverse=desc)

        if self._limit is not None:
            rows = rows[: self._limit]

        if self._selected != "*":
            columns = [part.strip() for part in self._selected.split(",")]
            rows = [{column: row.get(column) for column in columns} for row in rows]

        count = len(rows) if self._count == "exact" else None
        return _FakeResult(rows, count=count)


class _FakeSupabaseClient:
    def __init__(self, *, tables: dict[str, list[dict]], errors: dict[str, Exception] | None = None):
        self.tables = tables
        self.errors = errors or {}
        self.selected_columns: dict[str, list[str]] = {}

    def table(self, name: str):
        return _FakeQuery(self, name)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ASPIRE_ADMIN_JWT_SECRET", _TEST_JWT_SECRET)
    clear_admin_stores()
    clear_store()
    admin_routes._supabase_client = None
    yield TestClient(app)
    clear_admin_stores()
    clear_store()
    admin_routes._supabase_client = None
    os.environ.pop("ASPIRE_ADMIN_JWT_SECRET", None)


@pytest.fixture
def admin_headers():
    return {
        "x-admin-token": _make_admin_token(),
        "x-correlation-id": str(uuid.uuid4()),
    }


def test_sre_triage_queries_match_live_supabase_schema():
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fake = _FakeSupabaseClient(
        tables={
            "receipts": [
                {"receipt_id": "r1", "status": "FAILED", "created_at": _iso_now()},
                {"receipt_id": "r2", "status": "FAILED", "created_at": _iso_now()},
                {"receipt_id": "r3", "status": "SUCCEEDED", "created_at": _iso_now()},
            ],
            "provider_call_log": [
                {"external_provider": "stripe", "status": "failed", "started_at": _iso_now()},
                {"external_provider": "stripe", "status": "failed", "started_at": _iso_now()},
                {"external_provider": "plaid", "status": "failed", "started_at": _iso_now()},
            ],
        }
    )

    assert sre_triage._query_receipt_failures(fake, since) == 2
    assert sre_triage._query_provider_failures(fake, since) == {"stripe": 2, "plaid": 1}
    assert fake.selected_columns["receipts"][-1] == "receipt_id"
    assert fake.selected_columns["provider_call_log"][-1] == "external_provider"


def test_admin_providers_endpoint_reads_provider_call_log_schema(client, admin_headers, monkeypatch):
    fake = _FakeSupabaseClient(
        tables={
            "finance_connections": [],
            "provider_call_log": [
                {
                    "external_provider": "stripe",
                    "status": "success",
                    "started_at": "2026-03-19T10:00:00+00:00",
                    "completed_at": "2026-03-19T10:00:01+00:00",
                },
                {
                    "external_provider": "stripe",
                    "status": "failed",
                    "started_at": "2026-03-19T11:00:00+00:00",
                    "completed_at": "2026-03-19T11:00:03+00:00",
                },
            ],
            "webhook_deliveries": [],
        }
    )
    monkeypatch.setattr(admin_routes, "_get_supabase_client", lambda: fake)
    monkeypatch.setattr(admin_routes, "_build_runtime_provider_items", lambda: {})
    monkeypatch.setattr(admin_routes, "_overlay_live_provider_health", lambda items_map: None)

    response = client.get("/admin/ops/providers", headers=admin_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["warnings"] == []
    assert data["count"] == 1
    assert data["source"] == "supabase"
    assert fake.selected_columns["provider_call_log"][-1] == "external_provider,status,started_at,completed_at"

    item = data["items"][0]
    assert item["provider"] == "stripe"
    assert item["status"] == "degraded"
    assert item["latency_ms"] == 2000
    assert item["p95_latency_ms"] == 3000
    assert item["error_rate"] == 50.0


def test_admin_webhooks_fallback_reads_provider_call_log_schema(client, admin_headers, monkeypatch):
    fake = _FakeSupabaseClient(
        tables={
            "webhook_deliveries": [],
            "provider_call_log": [
                {
                    "call_id": "call-1",
                    "external_provider": "stripe",
                    "tool": "provider",
                    "operation": "webhook.dispatch",
                    "resource_type": "webhook",
                    "status": "failed",
                    "http_status": 500,
                    "started_at": "2026-03-19T12:00:00+00:00",
                    "completed_at": "2026-03-19T12:00:00.250000+00:00",
                }
            ],
        }
    )
    monkeypatch.setattr(admin_routes, "_get_supabase_client", lambda: fake)

    response = client.get("/admin/ops/webhooks", headers=admin_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["warnings"] == []
    assert data["source"] == "supabase:provider_call_log"
    assert data["count"] == 1
    assert fake.selected_columns["provider_call_log"][-1] == (
        "call_id,external_provider,tool,operation,resource_type,status,http_status,started_at,completed_at"
    )

    item = data["items"][0]
    assert item["webhook_id"] == "call-1"
    assert item["provider"] == "stripe"
    assert item["event_type"] == "provider.webhook.dispatch"
    assert item["status"] == "failed"
    assert item["latency_ms"] == 250.0


def test_admin_webhooks_missing_relation_warning_is_suppressed(client, admin_headers, monkeypatch):
    fake = _FakeSupabaseClient(
        tables={
            "provider_call_log": [
                {
                    "call_id": "call-1",
                    "external_provider": "stripe",
                    "tool": "provider",
                    "operation": "webhook.dispatch",
                    "resource_type": "webhook",
                    "status": "failed",
                    "http_status": 500,
                    "started_at": "2026-03-19T12:00:00+00:00",
                    "completed_at": "2026-03-19T12:00:00.250000+00:00",
                }
            ],
        },
        errors={
            "webhook_deliveries": Exception(
                "Could not find the table 'public.webhook_deliveries' in the schema cache"
            )
        },
    )
    monkeypatch.setattr(admin_routes, "_get_supabase_client", lambda: fake)

    response = client.get("/admin/ops/webhooks", headers=admin_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["warnings"] == []
    assert data["source"] == "supabase:provider_call_log"
    assert data["count"] == 1


def test_dashboard_metrics_reads_receipts_and_provider_call_log_schema(client, admin_headers, monkeypatch):
    now = datetime.now(timezone.utc)
    fake = _FakeSupabaseClient(
        tables={
            "receipts": [
                {"receipt_id": "r1", "status": "FAILED", "created_at": now.isoformat()},
                {"receipt_id": "r2", "status": "SUCCEEDED", "created_at": now.isoformat()},
            ],
            "incidents": [
                {"id": "inc-1", "status": "open", "severity": "high"},
            ],
            "provider_call_log": [
                {
                    "external_provider": "stripe",
                    "status": "success",
                    "started_at": now.isoformat(),
                    "completed_at": (now + timedelta(milliseconds=400)).isoformat(),
                },
                {
                    "external_provider": "stripe",
                    "status": "failed",
                    "started_at": now.isoformat(),
                    "completed_at": (now + timedelta(milliseconds=1200)).isoformat(),
                },
            ],
            "approval_requests": [
                {"approval_id": "apr-1", "status": "pending"},
            ],
        }
    )
    monkeypatch.setattr(admin_routes, "_get_supabase_client", lambda: fake)

    response = client.get("/admin/ops/dashboard/metrics", headers=admin_headers)
    assert response.status_code == 200

    metrics = response.json()["metrics"]
    assert fake.selected_columns["receipts"] == ["receipt_id", "receipt_id", "receipt_id"]
    assert fake.selected_columns["provider_call_log"][-1] == "external_provider, status, started_at, completed_at"
    assert metrics["receipts_total"] == 2
    assert metrics["receipts_24h"] == 2
    assert metrics["receipts_failed_24h"] == 1
    assert metrics["provider_calls_24h"] == 2
    assert metrics["provider_success_rate"] == 50.0
    assert metrics["provider_avg_latency_ms"] == 800.0
    assert metrics["provider_breakdown"]["stripe"]["avg_latency_ms"] == 800.0
    assert fake.selected_columns["approval_requests"][-1] == "approval_id"
    assert metrics["approvals_pending"] == 1
    assert metrics["system_status"] == "degraded"
