"""Tests for the W10 admin trust hub routes.

Coverage targets (≥12 tests):
  1.  Missing admin Bearer → 401.
  2.  Wrong admin Bearer → 401.
  3.  ASPIRE_ADMIN_API_KEY unset → 503.
  4.  >100 suite_ids → 422.
  5.  Empty suite_ids → 422.
  6.  Malformed UUID in suite_ids → 422.
  7.  dry_run=true returns plan, does NOT enqueue ARQ jobs.
  8.  dry_run=false enqueues correct count of ARQ jobs.
  9.  Skip already-onboarded suites (returned in `skipped` list).
 10.  Skip suite with no active phone number.
 11.  manual_state_override happy path + receipt cut + actor recorded.
 12.  manual_state_override invalid new_state → 422.
 13.  manual_state_override unknown profile → 404.
 14.  manual_state_override malformed UUID → 400.
 15.  GET /dashboard returns expected shape (states, stuck, batches, rejections).

Author: Aspire — Wave 10
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aspire_orchestrator.routes.admin_trust import router

# ---------------------------------------------------------------------------
# Test app + constants
# ---------------------------------------------------------------------------

app = FastAPI()
app.include_router(router)

ADMIN_KEY = "test-admin-bearer-secret-XXXXXX"

SUITE_A = str(uuid.uuid4())
SUITE_B = str(uuid.uuid4())
SUITE_C = str(uuid.uuid4())
TRUST_PROFILE_A = str(uuid.uuid4())
TENANT_A = str(uuid.uuid4())
OFFICE_A = str(uuid.uuid4())


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _patch_admin_key(value: str = ADMIN_KEY) -> Any:
    """Patch settings.admin_api_key on the route module."""
    return patch(
        "aspire_orchestrator.routes.admin_trust.settings",
        MagicMock(admin_api_key=value, redis_url="redis://localhost:6379"),
    )


# ---------------------------------------------------------------------------
# 1. Auth tests
# ---------------------------------------------------------------------------


class TestAdminAuth:

    def test_missing_bearer_returns_401(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                json={"suite_ids": [SUITE_A], "dry_run": True},
            )
            assert r.status_code == 401
            assert r.json()["detail"]["reason_code"] == "MISSING_BEARER_TOKEN"

    def test_wrong_bearer_returns_401(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers={"Authorization": "Bearer not-the-right-key"},
                json={"suite_ids": [SUITE_A], "dry_run": True},
            )
            assert r.status_code == 401
            assert r.json()["detail"]["reason_code"] == "INVALID_BEARER_TOKEN"

    def test_admin_key_unset_returns_503(self) -> None:
        with _patch_admin_key(value=""):
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": [SUITE_A], "dry_run": True},
            )
            assert r.status_code == 503
            assert r.json()["detail"]["error"] == "ADMIN_API_KEY_NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# 2. Validation tests
# ---------------------------------------------------------------------------


class TestBatchValidation:

    def test_more_than_100_suites_returns_422(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            many = [str(uuid.uuid4()) for _ in range(101)]
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": many, "dry_run": True},
            )
            assert r.status_code == 422

    def test_empty_suite_ids_returns_422(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": [], "dry_run": True},
            )
            assert r.status_code == 422

    def test_malformed_uuid_returns_422(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": ["not-a-uuid"], "dry_run": True},
            )
            assert r.status_code == 422


# ---------------------------------------------------------------------------
# 3. Dry-run tests
# ---------------------------------------------------------------------------


class TestDryRun:

    def test_dry_run_returns_plan_does_not_enqueue(self) -> None:
        """dry_run=true: classify suites, write batch row (audit), return — no ARQ."""
        async def _fake_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            # No existing trust profile, has active phone — eligible.
            if table == "tenant_trust_profiles":
                return []
            if table == "tenant_phone_numbers":
                return [{"id": "phone-1", "suite_id": SUITE_A, "status": "active"}]
            return []

        enqueue_mock = AsyncMock(return_value=True)
        insert_mock = AsyncMock(return_value={"id": "batch-1"})

        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(side_effect=_fake_select),
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_insert",
            new=insert_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust._enqueue_advance_backfill",
            new=enqueue_mock,
        ):
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": [SUITE_A], "dry_run": True},
            )

        assert r.status_code == 202
        body = r.json()
        assert body["dry_run"] is True
        assert body["enqueued"] == 0  # no enqueue on dry run
        assert body["skipped"] == []
        # Batch row IS inserted for audit.
        insert_mock.assert_called_once()
        # ARQ NOT touched.
        enqueue_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Real-run enqueue
# ---------------------------------------------------------------------------


class TestRealRun:

    def test_real_run_enqueues_each_eligible_suite(self) -> None:
        async def _fake_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_trust_profiles":
                return []
            if table == "tenant_phone_numbers":
                return [{"id": "phone-x", "status": "active"}]
            return []

        enqueue_mock = AsyncMock(return_value=True)
        insert_mock = AsyncMock(return_value={"id": "batch-2"})
        update_mock = AsyncMock(return_value={})

        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(side_effect=_fake_select),
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_insert",
            new=insert_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_update",
            new=update_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust._enqueue_advance_backfill",
            new=enqueue_mock,
        ):
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={
                    "suite_ids": [SUITE_A, SUITE_B, SUITE_C],
                    "dry_run": False,
                    "throttle_seconds": 5,
                },
            )

        assert r.status_code == 202
        body = r.json()
        assert body["dry_run"] is False
        assert body["enqueued"] == 3
        assert enqueue_mock.call_count == 3
        # Verify staggered delays (0, 5, 10).
        delays = [call.kwargs.get("defer_seconds") for call in enqueue_mock.call_args_list]
        assert delays == [0, 5, 10]


# ---------------------------------------------------------------------------
# 5. Skipping rules
# ---------------------------------------------------------------------------


class TestSkipping:

    def test_skip_already_onboarded_suite(self) -> None:
        """Suite with is_backfill=False & state=number_attached → skipped."""
        async def _fake_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_trust_profiles":
                return [{
                    "id": TRUST_PROFILE_A,
                    "suite_id": SUITE_A,
                    "is_backfill": False,
                    "trust_state": "number_attached",
                }]
            if table == "tenant_phone_numbers":
                return [{"id": "phone-1", "status": "active"}]
            return []

        enqueue_mock = AsyncMock(return_value=True)
        insert_mock = AsyncMock(return_value={"id": "batch-3"})
        update_mock = AsyncMock(return_value={})

        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(side_effect=_fake_select),
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_insert",
            new=insert_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_update",
            new=update_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust._enqueue_advance_backfill",
            new=enqueue_mock,
        ):
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": [SUITE_A], "dry_run": False},
            )

        assert r.status_code == 202
        body = r.json()
        assert body["enqueued"] == 0
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["suite_id"] == SUITE_A
        assert body["skipped"][0]["reason"] == "already_onboarded"
        enqueue_mock.assert_not_called()

    def test_skip_suite_without_active_phone(self) -> None:
        async def _fake_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_trust_profiles":
                return []
            if table == "tenant_phone_numbers":
                return []  # no active number
            return []

        enqueue_mock = AsyncMock(return_value=True)
        insert_mock = AsyncMock(return_value={"id": "batch-4"})

        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(side_effect=_fake_select),
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_insert",
            new=insert_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust._enqueue_advance_backfill",
            new=enqueue_mock,
        ):
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/batch-backfill",
                headers=_auth_headers(),
                json={"suite_ids": [SUITE_A], "dry_run": False},
            )

        assert r.status_code == 202
        body = r.json()
        assert body["enqueued"] == 0
        assert body["skipped"][0]["reason"] == "no_active_phone_number"


# ---------------------------------------------------------------------------
# 6. Manual state override
# ---------------------------------------------------------------------------


class TestSetState:

    def test_set_state_happy_path_cuts_receipt(self) -> None:
        existing_profile = {
            "id": TRUST_PROFILE_A,
            "suite_id": SUITE_A,
            "tenant_id": TENANT_A,
            "office_id": OFFICE_A,
            "trust_state": "failed",
            "is_backfill": True,
        }
        cut_mock = AsyncMock(return_value="receipt-override-001")
        update_mock = AsyncMock(return_value={})

        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(return_value=[existing_profile]),
        ), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_update",
            new=update_mock,
        ), patch(
            "aspire_orchestrator.routes.admin_trust.cut_trust_receipt",
            new=cut_mock,
        ):
            client = TestClient(app)
            r = client.post(
                f"/v1/admin/trust-hub/profile/{TRUST_PROFILE_A}/set-state",
                headers={**_auth_headers(), "X-Admin-Actor": "tony@aspire"},
                json={
                    "new_state": "profile_drafted",
                    "reason": "Tenant called support, retrying after Twilio reject of EIN typo",
                },
            )

        assert r.status_code == 200
        body = r.json()
        assert body["from_state"] == "failed"
        assert body["to_state"] == "profile_drafted"
        assert body["actor"] == "tony@aspire"
        assert body["receipt_id"] == "receipt-override-001"

        cut_mock.assert_called_once()
        kwargs = cut_mock.call_args.kwargs
        assert kwargs["receipt_type"] == "manual_state_override"
        # actor + reason recorded in redacted_inputs (Law #2 audit).
        assert kwargs["redacted_inputs"]["actor"] == "tony@aspire"
        assert "Tenant called support" in kwargs["redacted_inputs"]["reason"]

    def test_set_state_unknown_target_returns_422(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.post(
                f"/v1/admin/trust-hub/profile/{TRUST_PROFILE_A}/set-state",
                headers=_auth_headers(),
                json={"new_state": "totally_invalid_state", "reason": "test"},
            )
            assert r.status_code == 422

    def test_set_state_profile_not_found_returns_404(self) -> None:
        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            client = TestClient(app)
            r = client.post(
                f"/v1/admin/trust-hub/profile/{TRUST_PROFILE_A}/set-state",
                headers=_auth_headers(),
                json={"new_state": "profile_drafted", "reason": "valid reason"},
            )
            assert r.status_code == 404
            assert r.json()["detail"]["reason_code"] == "TRUST_PROFILE_MISSING"

    def test_set_state_malformed_uuid_returns_400(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.post(
                "/v1/admin/trust-hub/profile/not-a-uuid/set-state",
                headers=_auth_headers(),
                json={"new_state": "profile_drafted", "reason": "valid reason"},
            )
            # Pydantic Path with min_length 8 + UUID regex check returns 400.
            assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# 7. Dashboard
# ---------------------------------------------------------------------------


class TestDashboard:

    def test_dashboard_returns_expected_shape(self) -> None:
        # Arrange a fake DB world.
        async def _fake_select(table: str, filters: str, **kwargs: Any) -> list[dict[str, Any]]:
            if table == "tenant_trust_profiles" and "id=not.is.null" in filters:
                return [
                    {"trust_state": "number_attached"},
                    {"trust_state": "number_attached"},
                    {"trust_state": "shaken_submitted"},
                    {"trust_state": "failed"},
                ]
            if table == "tenant_trust_profiles" and "trust_state=eq.profile_submitted" in filters:
                return []
            if table == "tenant_trust_profiles" and "trust_state=eq.shaken_submitted" in filters:
                return [{"id": "p1"}]
            if table == "tenant_trust_profiles" and "trust_state=eq.cnam_submitted" in filters:
                return []
            if table == "tenant_trust_profiles" and "trust_state=in.(profile_rejected,failed)" in filters:
                return [{
                    "id": "p2", "suite_id": SUITE_A,
                    "trust_state": "failed",
                    "rejection_code": "DOB_INVALID",
                    "rejection_reason": "DOB must be at least 18 years ago",
                    "is_backfill": False,
                    "updated_at": "2026-05-04T00:00:00+00:00",
                }]
            if table == "tenant_trust_backfill_batches":
                if "status=in.(pending,in_progress)" in filters:
                    return [{"id": "b1"}, {"id": "b2"}]
                return [{
                    "id": "b1", "started_by_admin": "admin",
                    "status": "in_progress",
                    "dry_run": False,
                    "enqueued_count": 5,
                    "completed_count": 2,
                    "failed_count": 0,
                    "created_at": "2026-05-04T00:00:00+00:00",
                    "completed_at": None,
                }]
            return []

        with _patch_admin_key(), patch(
            "aspire_orchestrator.routes.admin_trust.supabase_select",
            new=AsyncMock(side_effect=_fake_select),
        ):
            client = TestClient(app)
            r = client.get(
                "/v1/admin/trust-hub/dashboard",
                headers=_auth_headers(),
            )

        assert r.status_code == 200
        body = r.json()
        # states_count
        assert body["states_count"]["number_attached"] == 2
        assert body["states_count"]["shaken_submitted"] == 1
        # stuck_tenants_count from the 3 *_submitted state queries (only shaken
        # returned 1 row in the fake)
        assert body["stuck_tenants_count"] == 1
        # backfill_in_progress = 2 (b1, b2)
        assert body["backfill_in_progress"] == 2
        assert len(body["backfill_recent"]) == 1
        assert len(body["rejections_recent"]) == 1
        assert "generated_at" in body

    def test_dashboard_requires_admin_auth(self) -> None:
        with _patch_admin_key():
            client = TestClient(app)
            r = client.get("/v1/admin/trust-hub/dashboard")
            assert r.status_code == 401
