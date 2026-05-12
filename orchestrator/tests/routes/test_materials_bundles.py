"""Integration + contract + evil tests for /v1/materials/bundles/* — Pass D.

Test IDs:
  INT-01: add → list → contains item with correct quantity
  INT-02: add same product twice → quantity increments to 2
  INT-03: remove → list does not contain
  INT-04: update-quantity → reflected in list
  INT-05: clear → list empty
  INT-06: push-to-estimate → estimate_drafts row created, items marked pushed
  INT-07: push-to-estimate emits YELLOW tier receipt
  NEG-01: bundle for project A invisible to project B (cross-project isolation)
  NEG-02: cross-tenant RLS simulation — suite B cannot see suite A bundles
  NEG-03: invalid project_id (empty) → 400
  NEG-04: push-to-estimate on empty bundle → 400 BUNDLE_EMPTY
  NEG-05: remove with wrong suite_id doesn't delete (tenant isolation)
  NEG-06: update-quantity with negative qty → 400
  EVIL-01: no capability token → 401
  EVIL-02: expired capability token → 401
  EVIL-03: wrong suite in token → 401
  EVIL-04: oversized project_id → 400
  EVIL-05: list without scope headers → 401
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only-32bytes!")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.materials_bundles import router as bundles_router

_app = FastAPI()
_app.include_router(bundles_router)
_client = TestClient(_app, raise_server_exceptions=False)

SUITE_ID = "11111111-0000-0000-0000-000000000011"
OFFICE_ID = "22222222-0000-0000-0000-000000000022"
TENANT_ID = "99999999-0000-0000-0000-000000000099"
SUITE_B_ID = "bbbbbbbb-0000-0000-0000-000000000011"

_SCOPE_HEADERS = {
    "X-Tenant-Id": TENANT_ID,
    "X-Suite-Id": SUITE_ID,
    "X-Office-Id": OFFICE_ID,
}

PROJECT_ID = "123-main-st-austin-tx"
PROJECT_B_ID = "456-elm-st-dallas-tx"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _mint(scope: str = "materials:bundles.write", suite_id: str = SUITE_ID) -> str:
    from aspire_orchestrator.services.token_service import mint_token
    tok = mint_token(
        suite_id=suite_id,
        office_id=OFFICE_ID,
        tool="materials_bundles",
        scopes=[scope],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=45,
    )
    return json.dumps(tok)


def _mint_read() -> str:
    return _mint(scope="materials:bundles.read")


def _mint_push() -> str:
    return _mint(scope="materials:bundles.push")


def _expired_token_json() -> str:
    from aspire_orchestrator.services.token_service import mint_token
    import time
    tok = mint_token(
        suite_id=SUITE_ID,
        office_id=OFFICE_ID,
        tool="materials_bundles",
        scopes=["materials:bundles.write"],
        correlation_id=str(uuid.uuid4()),
        ttl_seconds=1,
    )
    # Backdate expires_at to force expiry (re-sign isn't possible without key so we
    # monkey-patch validate_token instead — see EVIL-02 test)
    return json.dumps(tok)


# ---------------------------------------------------------------------------
# Product fixture
# ---------------------------------------------------------------------------

def _product(product_id: str = "hd-305832") -> dict[str, Any]:
    return {
        "id": product_id,
        "title": "Behr Marquee 5-gal Matte",
        "brand": "Behr",
        "price": 218.0,
        "unit": "pail",
        "sku": "305832",
        "imageUrl": "https://example.com/img.jpg",
        "store": {"id": "hd-0507", "name": "Home Depot Austin N"},
        "fetchedAt": "2026-05-12T00:00:00+00:00",
        "fetched_at": "2026-05-12T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Supabase mock factory
# ---------------------------------------------------------------------------

class _FakeDB:
    """In-memory Supabase stand-in for bundle tests.

    All methods are SYNCHRONOUS — they return values directly (not coroutines).
    The async patches in _db_patches() use AsyncMock with side_effect pointing
    to these methods; AsyncMock will return a coroutine that resolves to the
    synchronous return value.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._drafts: list[dict[str, Any]] = []

    def reset(self) -> None:
        self._rows.clear()
        self._drafts.clear()

    def select(self, table: str, filters: str, *, order_by: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        if table == "material_bundles":
            rows = list(self._rows)
            for part in filters.split("&"):
                if "=eq." in part:
                    field, val = part.split("=eq.", 1)
                    if val == "true":
                        rows = [r for r in rows if r.get(field) is True]
                    elif val == "false":
                        rows = [r for r in rows if r.get(field) is False]
                    else:
                        rows = [r for r in rows if str(r.get(field, "")) == val]
            if limit:
                rows = rows[:limit]
            return rows
        if table == "estimate_drafts":
            return list(self._drafts)
        return []

    def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        row = {"id": data.get("id") or str(uuid.uuid4()), **data}
        if table == "material_bundles":
            self._rows.append(row)
        elif table == "estimate_drafts":
            self._drafts.append(row)
        return row

    def update(self, table: str, filters: str, data: dict[str, Any]) -> dict[str, Any]:
        if table == "material_bundles":
            for r in self._rows:
                if self._matches_all(r, filters):
                    r.update(data)
        return data

    def delete(self, table: str, filters: str) -> None:
        if table == "material_bundles":
            self._rows = [
                r for r in self._rows
                if not self._matches_all(r, filters)
            ]

    def _matches_all(self, row: dict[str, Any], filters: str) -> bool:
        for part in filters.split("&"):
            if "=eq." in part:
                field, val = part.split("=eq.", 1)
                if val == "true":
                    if row.get(field) is not True:
                        return False
                elif val == "false":
                    if row.get(field) is not False:
                        return False
                else:
                    if str(row.get(field, "")) != val:
                        return False
        return True


_db = _FakeDB()


def _db_patches():
    """Context manager providing all supabase_client patches.

    All Supabase functions are async — we use AsyncMock with side_effect so
    the route's `await` calls are handled correctly.
    """
    return [
        patch(
            "aspire_orchestrator.routes.materials_bundles.supabase_select",
            new_callable=AsyncMock,
            side_effect=lambda table, filters, **kw: _db.select(table, filters, **kw),
        ),
        patch(
            "aspire_orchestrator.routes.materials_bundles.supabase_insert",
            new_callable=AsyncMock,
            side_effect=lambda table, data: _db.insert(table, data),
        ),
        patch(
            "aspire_orchestrator.routes.materials_bundles.supabase_update",
            new_callable=AsyncMock,
            side_effect=lambda table, filters, data: _db.update(table, filters, data),
        ),
        patch(
            "aspire_orchestrator.routes.materials_bundles.supabase_delete",
            new_callable=AsyncMock,
            side_effect=lambda table, filters: _db.delete(table, filters),
        ),
        patch(
            "aspire_orchestrator.routes.materials_bundles.receipt_store.store_receipts",
            side_effect=lambda receipts: None,
        ),
    ]


import contextlib


@contextlib.contextmanager
def _patched():
    _db.reset()
    patches = _db_patches()
    started = [p.start() for p in patches]
    try:
        yield _db
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# INT-01: add → list → contains item
# ---------------------------------------------------------------------------


def test_int_01_add_then_list():
    with _patched():
        token = _mint()
        resp = _client.post(
            "/v1/materials/bundles/add",
            json={
                "project_id": PROJECT_ID,
                "product": _product(),
                "quantity": 1,
                "capability_token": token,
            },
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert len(body["items"]) == 1
        assert body["items"][0]["product"]["id"] == "hd-305832"
        assert body["items"][0]["quantity"] == 1.0
        assert body["bundle_subtotal"] == 218.0
        assert "receipt_id" in body


# ---------------------------------------------------------------------------
# INT-02: add same product twice → quantity increments to 2
# ---------------------------------------------------------------------------


def test_int_02_add_same_product_twice():
    with _patched():
        token = _mint()
        for _ in range(2):
            resp = _client.post(
                "/v1/materials/bundles/add",
                json={
                    "project_id": PROJECT_ID,
                    "product": _product(),
                    "quantity": 1,
                    "capability_token": token,
                },
                headers=_SCOPE_HEADERS,
            )
            assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1  # still 1 row
        assert body["items"][0]["quantity"] == 2.0
        assert body["bundle_subtotal"] == pytest.approx(436.0)


# ---------------------------------------------------------------------------
# INT-03: remove → list does not contain
# ---------------------------------------------------------------------------


def test_int_03_remove():
    with _patched():
        write_token = _mint()
        add_resp = _client.post(
            "/v1/materials/bundles/add",
            json={
                "project_id": PROJECT_ID,
                "product": _product(),
                "quantity": 1,
                "capability_token": write_token,
            },
            headers=_SCOPE_HEADERS,
        )
        item_id = add_resp.json()["items"][0]["id"]

        rem_resp = _client.post(
            "/v1/materials/bundles/remove",
            json={
                "project_id": PROJECT_ID,
                "bundle_item_id": item_id,
                "capability_token": write_token,
            },
            headers=_SCOPE_HEADERS,
        )
        assert rem_resp.status_code == 200
        assert rem_resp.json()["items"] == []


# ---------------------------------------------------------------------------
# INT-04: update-quantity → reflected in list
# ---------------------------------------------------------------------------


def test_int_04_update_quantity():
    with _patched():
        write_token = _mint()
        add_resp = _client.post(
            "/v1/materials/bundles/add",
            json={
                "project_id": PROJECT_ID,
                "product": _product(),
                "quantity": 1,
                "capability_token": write_token,
            },
            headers=_SCOPE_HEADERS,
        )
        item_id = add_resp.json()["items"][0]["id"]

        upd_resp = _client.post(
            "/v1/materials/bundles/update-quantity",
            json={
                "project_id": PROJECT_ID,
                "bundle_item_id": item_id,
                "quantity": 5,
                "capability_token": write_token,
            },
            headers=_SCOPE_HEADERS,
        )
        assert upd_resp.status_code == 200
        body = upd_resp.json()
        assert body["items"][0]["quantity"] == 5.0
        assert body["bundle_subtotal"] == pytest.approx(1090.0)


# ---------------------------------------------------------------------------
# INT-05: clear → list empty
# ---------------------------------------------------------------------------


def test_int_05_clear():
    with _patched():
        write_token = _mint()
        _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": PROJECT_ID, "product": _product(), "quantity": 2,
                  "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        clear_resp = _client.post(
            "/v1/materials/bundles/clear",
            json={"project_id": PROJECT_ID, "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["items"] == []
        assert clear_resp.json()["bundle_subtotal"] == 0.0


# ---------------------------------------------------------------------------
# INT-06: push-to-estimate → estimate_drafts row created, items marked pushed
# ---------------------------------------------------------------------------


def test_int_06_push_to_estimate():
    with _patched() as db:
        write_token = _mint()
        push_token = _mint_push()
        _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": PROJECT_ID, "product": _product("hd-001"), "quantity": 2,
                  "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        push_resp = _client.post(
            "/v1/materials/bundles/push-to-estimate",
            json={"project_id": PROJECT_ID, "capability_token": push_token},
            headers=_SCOPE_HEADERS,
        )
        assert push_resp.status_code == 200
        body = push_resp.json()
        assert body["success"] is True
        draft_id = body["estimate_draft_id"]
        assert draft_id

        # estimate_drafts row written
        assert len(db._drafts) == 1
        draft = db._drafts[0]
        assert draft["id"] == draft_id
        assert draft["source"] == "materials_bundle"
        assert draft["suite_id"] == SUITE_ID

        # Items marked pushed
        pushed = [r for r in db._rows if r.get("pushed_to_estimate") is True]
        assert len(pushed) == 1
        assert pushed[0].get("estimate_draft_id") == draft_id


# ---------------------------------------------------------------------------
# INT-07: push-to-estimate emits YELLOW tier receipt
# ---------------------------------------------------------------------------


def test_int_07_push_emits_yellow_receipt():
    captured_receipts: list[dict[str, Any]] = []

    def _capture(receipts: list) -> None:
        captured_receipts.extend(receipts)

    with _patched() as db:
        with patch(
            "aspire_orchestrator.routes.materials_bundles.receipt_store.store_receipts",
            side_effect=_capture,
        ):
            write_token = _mint()
            push_token = _mint_push()
            _client.post(
                "/v1/materials/bundles/add",
                json={"project_id": PROJECT_ID, "product": _product(), "quantity": 1,
                      "capability_token": write_token},
                headers=_SCOPE_HEADERS,
            )
            push_resp = _client.post(
                "/v1/materials/bundles/push-to-estimate",
                json={"project_id": PROJECT_ID, "capability_token": push_token},
                headers=_SCOPE_HEADERS,
            )
            assert push_resp.status_code == 200

    push_receipts = [r for r in captured_receipts
                     if r.get("action_type") == "materials.bundle.push_to_estimate"]
    assert len(push_receipts) >= 1
    r = push_receipts[-1]
    assert r["risk_tier"] == "yellow"
    assert r["outcome"] == "success"
    assert r["reason_code"] == "EXECUTED"


# ---------------------------------------------------------------------------
# NEG-01: cross-project isolation
# ---------------------------------------------------------------------------


def test_neg_01_cross_project_isolation():
    with _patched():
        write_token = _mint()
        # Add to project A
        _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": PROJECT_ID, "product": _product("hd-A"), "quantity": 1,
                  "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        # List for project B — must be empty
        read_token = _mint_read()
        resp = _client.get(
            "/v1/materials/bundles",
            params={"project_id": PROJECT_B_ID, "capability_token": read_token},
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# NEG-02: cross-tenant RLS simulation
# ---------------------------------------------------------------------------


def test_neg_02_cross_tenant_rls():
    """Suite B's token must not leak Suite A's bundles.

    The route enforces suite_id isolation via the supabase_select filter
    (suite_id=eq.<suite_id>). This test verifies the filter is applied.
    """
    with _patched() as db:
        write_token_a = _mint(suite_id=SUITE_ID)
        # Inject Suite A data directly into the DB (synchronous insert)
        db.insert("material_bundles", {
            "id": str(uuid.uuid4()),
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "product_payload": _product("hd-A"),
            "store_id": "hd-0507",
            "category_hint": "",
            "quantity": 1.0,
            "unit_price": 218.0,
            "fetched_at": "2026-05-12T00:00:00+00:00",
            "pushed_to_estimate": False,
            "created_at": "2026-05-12T00:00:00+00:00",
        })
        # Suite B token + Suite B headers should see zero rows
        from aspire_orchestrator.services.token_service import mint_token
        tok_b = mint_token(
            suite_id=SUITE_B_ID,
            office_id=OFFICE_ID,
            tool="materials_bundles",
            scopes=["materials:bundles.read"],
            correlation_id=str(uuid.uuid4()),
            ttl_seconds=45,
        )
        headers_b = {
            "X-Tenant-Id": TENANT_ID,
            "X-Suite-Id": SUITE_B_ID,
            "X-Office-Id": OFFICE_ID,
        }
        resp = _client.get(
            "/v1/materials/bundles",
            params={"project_id": PROJECT_ID, "capability_token": json.dumps(tok_b)},
            headers=headers_b,
        )
        assert resp.status_code == 200
        # Suite B sees only its own rows → 0 (the row belongs to Suite A)
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# NEG-03: invalid project_id (empty) → 400
# ---------------------------------------------------------------------------


def test_neg_03_empty_project_id():
    with _patched():
        write_token = _mint()
        resp = _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": "", "product": _product(), "quantity": 1,
                  "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# NEG-04: push-to-estimate on empty bundle → 400 BUNDLE_EMPTY
# ---------------------------------------------------------------------------


def test_neg_04_push_empty_bundle():
    with _patched():
        push_token = _mint_push()
        resp = _client.post(
            "/v1/materials/bundles/push-to-estimate",
            json={"project_id": PROJECT_ID, "capability_token": push_token},
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "BUNDLE_EMPTY"


# ---------------------------------------------------------------------------
# NEG-05: remove with wrong suite_id doesn't delete (tenant isolation)
# ---------------------------------------------------------------------------


def test_neg_05_remove_wrong_suite_isolation():
    with _patched() as db:
        item_id = str(uuid.uuid4())
        db.insert("material_bundles", {
            "id": item_id,
            "project_id": PROJECT_ID,
            "suite_id": SUITE_ID,
            "office_id": OFFICE_ID,
            "product_payload": _product(),
            "store_id": "",
            "quantity": 1.0,
            "unit_price": 218.0,
            "fetched_at": "2026-05-12T00:00:00+00:00",
            "pushed_to_estimate": False,
            "created_at": "2026-05-12T00:00:00+00:00",
        })
        # Suite B tries to remove Suite A's item
        from aspire_orchestrator.services.token_service import mint_token
        tok_b = mint_token(
            suite_id=SUITE_B_ID,
            office_id=OFFICE_ID,
            tool="materials_bundles",
            scopes=["materials:bundles.write"],
            correlation_id=str(uuid.uuid4()),
            ttl_seconds=45,
        )
        headers_b = {"X-Tenant-Id": TENANT_ID, "X-Suite-Id": SUITE_B_ID, "X-Office-Id": OFFICE_ID}
        resp = _client.post(
            "/v1/materials/bundles/remove",
            json={"project_id": PROJECT_ID, "bundle_item_id": item_id,
                  "capability_token": json.dumps(tok_b)},
            headers=headers_b,
        )
        assert resp.status_code == 200  # No error — just no-op delete
        # Suite A's row must still exist in the DB
        assert len(db._rows) == 1
        assert db._rows[0]["id"] == item_id


# ---------------------------------------------------------------------------
# NEG-06: update-quantity with negative qty → 400
# ---------------------------------------------------------------------------


def test_neg_06_negative_quantity():
    with _patched():
        write_token = _mint()
        resp = _client.post(
            "/v1/materials/bundles/update-quantity",
            json={"project_id": PROJECT_ID, "bundle_item_id": str(uuid.uuid4()),
                  "quantity": -1, "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# EVIL-01: no capability token → 401
# ---------------------------------------------------------------------------


def test_evil_01_no_token():
    with _patched():
        resp = _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": PROJECT_ID, "product": _product(), "quantity": 1},
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "MISSING_CAPABILITY_TOKEN"


# ---------------------------------------------------------------------------
# EVIL-02: expired capability token → 401
# ---------------------------------------------------------------------------


def test_evil_02_expired_token():
    with _patched():
        from aspire_orchestrator.services import token_service
        from aspire_orchestrator.services.token_service import TokenValidationError, TokenValidationResult

        expired_result = TokenValidationResult(
            valid=False,
            error=TokenValidationError.TOKEN_EXPIRED,
            error_message="Token expired",
        )
        with patch("aspire_orchestrator.routes.materials_bundles.validate_token", return_value=expired_result):
            # Still need a parseable token (validation is mocked)
            fake_token = json.dumps({
                "token_id": str(uuid.uuid4()),
                "suite_id": SUITE_ID,
                "office_id": OFFICE_ID,
                "tool": "materials_bundles",
                "scopes": ["materials:bundles.write"],
                "issued_at": "2026-05-01T00:00:00+00:00",
                "expires_at": "2026-05-01T00:00:01+00:00",
                "signature": "fake",
                "correlation_id": str(uuid.uuid4()),
            })
            resp = _client.post(
                "/v1/materials/bundles/add",
                json={"project_id": PROJECT_ID, "product": _product(), "quantity": 1,
                      "capability_token": fake_token},
                headers=_SCOPE_HEADERS,
            )
            assert resp.status_code == 401
            assert "EXPIRED" in resp.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# EVIL-03: wrong suite in token → 401
# ---------------------------------------------------------------------------


def test_evil_03_wrong_suite_token():
    with _patched():
        # Mint with Suite B token but present Suite A headers
        from aspire_orchestrator.services.token_service import mint_token
        tok_b = mint_token(
            suite_id=SUITE_B_ID,
            office_id=OFFICE_ID,
            tool="materials_bundles",
            scopes=["materials:bundles.write"],
            correlation_id=str(uuid.uuid4()),
            ttl_seconds=45,
        )
        resp = _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": PROJECT_ID, "product": _product(), "quantity": 1,
                  "capability_token": json.dumps(tok_b)},
            headers=_SCOPE_HEADERS,  # Suite A headers, Suite B token
        )
        assert resp.status_code == 401
        assert "MISMATCH" in resp.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# EVIL-04: oversized project_id → 400
# ---------------------------------------------------------------------------


def test_evil_04_oversized_project_id():
    with _patched():
        write_token = _mint()
        resp = _client.post(
            "/v1/materials/bundles/add",
            json={"project_id": "x" * 501, "product": _product(), "quantity": 1,
                  "capability_token": write_token},
            headers=_SCOPE_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# EVIL-05: list without scope headers → 401
# ---------------------------------------------------------------------------


def test_evil_05_list_no_scope_headers():
    with _patched():
        read_token = _mint_read()
        resp = _client.get(
            "/v1/materials/bundles",
            params={"project_id": PROJECT_ID, "capability_token": read_token},
            # No scope headers
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# INT: receipt generation — every add produces a receipt
# ---------------------------------------------------------------------------


def test_receipt_emitted_on_add():
    captured: list[dict[str, Any]] = []

    def _capture(receipts: list) -> None:
        captured.extend(receipts)

    with _patched():
        with patch(
            "aspire_orchestrator.routes.materials_bundles.receipt_store.store_receipts",
            side_effect=_capture,
        ):
            write_token = _mint()
            _client.post(
                "/v1/materials/bundles/add",
                json={"project_id": PROJECT_ID, "product": _product(), "quantity": 1,
                      "capability_token": write_token},
                headers=_SCOPE_HEADERS,
            )

    assert len(captured) >= 1
    r = captured[-1]
    assert r["action_type"] == "materials.bundle.add"
    assert r["risk_tier"] == "green"
    assert r["outcome"] == "success"
    assert r["suite_id"] == SUITE_ID
    # No PII in receipt
    assert "product_payload" not in str(r)
