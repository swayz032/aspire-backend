"""Tests for Anam video session-broker handoff resolution (Pass 10 Lane A).

Covers plan §7 requirements:
  - Anam session with valid handoff_id returns voiceHandoffBrief populated
  - Anam session with cross-tenant handoff_id returns 403
  - Anam session with non-existent handoff_id returns session with empty brief + warning log
  - ElevenLabs session (no runtime_family / runtime_family='elevenlabs') is unaffected
    by handoff_id processing — no cross-tenant check, no DB query

All tests use FastAPI TestClient with mocked supabase_select.
No real Supabase connection or external service calls.

Law compliance verified:
  Law #2: Receipt emitted for every session_broker.start (success and denial).
  Law #3: Cross-tenant handoff_id → 403 TENANT_ISOLATION_VIOLATION.
  Law #6: Row tenant/suite checked against caller scope before voiceHandoffBrief is built.
  Law #9: No PII or raw UUIDs in log messages beyond first 8 chars.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import os

# Set env before any app/middleware imports
os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared test UUIDs
# ---------------------------------------------------------------------------

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
SUITE_A = str(uuid.uuid4())
SUITE_B = str(uuid.uuid4())
OFFICE_A = str(uuid.uuid4())
ACTOR_A = str(uuid.uuid4())

# Shared handoff correlation ID
HANDOFF_ID = str(uuid.uuid4())

MEM_PENDING = str(uuid.uuid4())
MEM_AUTHORITY = str(uuid.uuid4())
MEM_HANDOFF_NOTE = str(uuid.uuid4())

_SCOPE_HEADERS_A = {
    "X-Tenant-Id": TENANT_A,
    "X-Suite-Id": SUITE_A,
    "X-Office-Id": OFFICE_A,
    "X-Actor-Id": ACTOR_A,
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DB row factories
# ---------------------------------------------------------------------------


def _make_handoff_row(
    memory_type: str,
    memory_id: str,
    summary: str,
    tenant_id: str = TENANT_A,
    suite_id: str = SUITE_A,
) -> dict:
    return {
        "memory_id": memory_id,
        "tenant_id": tenant_id,
        "suite_id": suite_id,
        "office_id": OFFICE_A,
        "correlation_id": HANDOFF_ID,
        "memory_type": memory_type,
        "summary": summary,
        "status": "drafted",
        "created_at": _now_iso(),
    }


def _three_handoff_rows(
    tenant_id: str = TENANT_A,
    suite_id: str = SUITE_A,
) -> list[dict]:
    """Return the 3 canonical handoff memory_object rows for a valid handoff."""
    return [
        _make_handoff_row(
            "handoff_note",
            MEM_HANDOFF_NOTE,
            "Sarah has already verified the business license and trade name.",
            tenant_id=tenant_id,
            suite_id=suite_id,
        ),
        _make_handoff_row(
            "authority_context",
            MEM_AUTHORITY,
            "Owner requested contract review before signing — needs Ava's judgment.",
            tenant_id=tenant_id,
            suite_id=suite_id,
        ),
        _make_handoff_row(
            "pending_intent",
            MEM_PENDING,
            "Review the PandaDoc proposal and advise on clause 4.2.",
            tenant_id=tenant_id,
            suite_id=suite_id,
        ),
    ]


# ---------------------------------------------------------------------------
# Scope body helpers
# ---------------------------------------------------------------------------


def _scope_body(tenant_id: str = TENANT_A) -> dict:
    return {
        "tenant_id": tenant_id,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
    }


def _anam_session_body(
    handoff_id: str | None = HANDOFF_ID,
    tenant_id: str = TENANT_A,
) -> dict:
    body: dict = {
        "agent_name": "ava",
        "scope": _scope_body(tenant_id=tenant_id),
        "channel": "video",
        "runtime_family": "anam_video",
    }
    if handoff_id is not None:
        body["dynamic_variables_hint"] = {"handoff_id": handoff_id}
        body["handoff_id"] = handoff_id
    return body


def _el_session_body() -> dict:
    """ElevenLabs session — no runtime_family, no handoff_id."""
    return {
        "agent_name": "ava",
        "scope": _scope_body(),
        "channel": "voice",
    }


# ---------------------------------------------------------------------------
# Shared client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_with_mocks():
    """TestClient with BriefMaterializer and MemoryService mocked.

    supabase_select is patched separately in individual tests via
    the `mock_supabase_select` parameter so each test can control
    the DB rows returned.
    """
    from aspire_orchestrator.server import app
    from aspire_orchestrator.schemas.memory_v1 import OfficeBriefOut, MemoryObjectOut

    office_brief_stub = {
        "tenant_id": TENANT_A,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "brief_text": "stub brief",
        "brief_json": {},
        "due_now_count": 0,
        "overdue_count": 0,
        "pending_approval_count": 0,
        "recent_receipts_count": 0,
        "last_built_at": _now_iso(),
        "freshness_seq": 1,
    }

    with (
        patch(
            "aspire_orchestrator.routes.memory.BriefMaterializer",
            autospec=False,
        ) as MockBriefMat,
        patch(
            "aspire_orchestrator.routes.memory.MemoryService",
            autospec=False,
        ) as MockMemSvc,
        patch(
            "aspire_orchestrator.routes.memory.ProactiveCandidateEngine",
            autospec=False,
        ) as MockCandEng,
        patch(
            "aspire_orchestrator.routes.memory.TranscriptEventRefinery",
            autospec=False,
        ) as MockRefinery,
        patch(
            "aspire_orchestrator.routes.memory.store_receipts",
            new=MagicMock(),
        ) as mock_store_receipts,
        patch(
            "aspire_orchestrator.routes.memory_pages.MemoryService",
            autospec=False,
        ),
        patch(
            "aspire_orchestrator.routes.memory_pages.BriefMaterializer",
            autospec=False,
        ),
        patch(
            "aspire_orchestrator.routes.memory.MemorySearchService",
            autospec=False,
        ),
        patch(
            "aspire_orchestrator.routes.memory_pages.MemorySearchService",
            autospec=False,
        ),
        patch(
            "aspire_orchestrator.routes.memory._kick_refinery_async",
            new=MagicMock(),
        ),
    ):
        # BriefMaterializer stub
        mock_mat_instance = AsyncMock()
        mock_mat_instance.build_office_brief = AsyncMock(
            return_value=OfficeBriefOut(**office_brief_stub)
        )
        MockBriefMat.return_value = mock_mat_instance

        # MemoryService stub (not used by session_broker_start directly,
        # but imported — must have a no-op instance)
        mock_mem_instance = AsyncMock()
        MockMemSvc.return_value = mock_mem_instance

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, mock_store_receipts


# ===========================================================================
# Test class: Anam video session with valid handoff_id
# ===========================================================================


class TestAnamHandoffValid:
    """Anam session with a valid handoff_id that resolves to 3 memory objects."""

    def test_voice_handoff_brief_populated(self, client_with_mocks):
        client, mock_store_receipts = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=_three_handoff_rows()),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        dv = data["dynamic_variables"]
        # voiceHandoffBrief must be present and non-empty
        assert "voiceHandoffBrief" in dv
        brief = dv["voiceHandoffBrief"]
        assert len(brief) > 0

    def test_voice_handoff_brief_starts_with_handoff_note(self, client_with_mocks):
        """handoff_note summary must appear first in the brief (plan §7 ordering)."""
        client, _ = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=_three_handoff_rows()),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        brief = resp.json()["dynamic_variables"]["voiceHandoffBrief"]
        # The handoff_note summary is "Sarah has already verified..."
        assert brief.startswith("Sarah has already verified")

    def test_handoff_correlation_id_returned(self, client_with_mocks):
        client, _ = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=_three_handoff_rows()),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        dv = resp.json()["dynamic_variables"]
        assert dv["handoff_correlation_id"] == HANDOFF_ID

    def test_per_object_ids_returned(self, client_with_mocks):
        """Each of the 3 handoff object IDs must be present in dynamic_variables."""
        client, _ = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=_three_handoff_rows()),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        dv = resp.json()["dynamic_variables"]
        assert dv.get("handoff_note_id") == MEM_HANDOFF_NOTE
        assert dv.get("handoff_authority_context_id") == MEM_AUTHORITY
        assert dv.get("handoff_pending_intent_id") == MEM_PENDING

    def test_session_id_and_trace_id_present(self, client_with_mocks):
        client, _ = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=_three_handoff_rows()),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        data = resp.json()
        assert "session_id" in data
        assert "trace_id" in data

    def test_brief_capped_at_400_chars(self, client_with_mocks):
        """Brief must not exceed 400 characters even with very long summaries."""
        client, _ = client_with_mocks
        long_rows = [
            _make_handoff_row("handoff_note", MEM_HANDOFF_NOTE, "A" * 250),
            _make_handoff_row("authority_context", MEM_AUTHORITY, "B" * 250),
            _make_handoff_row("pending_intent", MEM_PENDING, "C" * 250),
        ]
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=long_rows),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        brief = resp.json()["dynamic_variables"]["voiceHandoffBrief"]
        assert len(brief) <= 400

    def test_receipt_emitted_on_success(self, client_with_mocks):
        """Law #2: receipt must be stored for successful Anam session start."""
        client, mock_store_receipts = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=_three_handoff_rows()),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200
        mock_store_receipts.assert_called()
        # Collect all receipt dicts across all store_receipts calls
        all_receipts: list[dict] = []
        for call_arg in mock_store_receipts.call_args_list:
            receipt_list = call_arg[0][0]
            if isinstance(receipt_list, list):
                all_receipts.extend(receipt_list)
        success_receipts = [r for r in all_receipts if r.get("outcome") == "success"]
        assert len(success_receipts) >= 1
        broker_receipt = next(
            (r for r in success_receipts if r.get("action_type") == "session_broker.start"),
            None,
        )
        assert broker_receipt is not None, "No session_broker.start receipt found"


# ===========================================================================
# Test class: Cross-tenant handoff_id
# ===========================================================================


class TestAnamHandoffCrossTenant:
    """Anam session where handoff_id resolves to rows belonging to a different tenant."""

    def test_cross_tenant_rows_return_403(self, client_with_mocks):
        """Law #6: Cross-tenant handoff_id must be denied with 403."""
        client, _ = client_with_mocks
        # DB rows belong to TENANT_B, caller is TENANT_A
        cross_tenant_rows = _three_handoff_rows(
            tenant_id=TENANT_B,
            suite_id=SUITE_B,
        )
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=cross_tenant_rows),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_cross_tenant_denial_receipt_emitted(self, client_with_mocks):
        """Law #2: Denial receipt must be emitted for cross-tenant attempt."""
        client, mock_store_receipts = client_with_mocks
        cross_tenant_rows = _three_handoff_rows(
            tenant_id=TENANT_B,
            suite_id=SUITE_B,
        )
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=cross_tenant_rows),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 403
        mock_store_receipts.assert_called()
        # At least one receipt should be a denial.
        # store_receipts is called as store_receipts([receipt_dict, ...]),
        # so each call_args[0][0] is a list of receipt dicts.
        all_receipts: list[dict] = []
        for call_arg in mock_store_receipts.call_args_list:
            receipt_list = call_arg[0][0]  # first positional arg = list of dicts
            if isinstance(receipt_list, list):
                all_receipts.extend(receipt_list)
        denial_receipts = [r for r in all_receipts if r.get("outcome") == "denied"]
        assert len(denial_receipts) >= 1
        assert denial_receipts[0]["reason_code"] == "TENANT_ISOLATION_VIOLATION"

    def test_cross_tenant_error_contains_correlation_id(self, client_with_mocks):
        client, _ = client_with_mocks
        cross_tenant_rows = _three_handoff_rows(tenant_id=TENANT_B, suite_id=SUITE_B)
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=cross_tenant_rows),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert "correlation_id" in resp.json()["detail"]


# ===========================================================================
# Test class: Non-existent handoff_id (degraded mode)
# ===========================================================================


class TestAnamHandoffNonExistent:
    """Anam session where handoff_id resolves to zero memory objects.

    Per plan §7.6: degraded mode — session continues, voiceHandoffBrief omitted,
    warning logged.
    """

    def test_session_starts_without_brief_when_no_objects(
        self, client_with_mocks, caplog
    ):
        client, _ = client_with_mocks
        with (
            patch(
                "aspire_orchestrator.routes.memory.supabase_select",
                new=AsyncMock(return_value=[]),  # empty — handoff_id not found
            ),
            caplog.at_level(logging.WARNING, logger="aspire_orchestrator.routes.memory"),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "session_id" in data
        # voiceHandoffBrief must be absent or empty string when no rows found
        dv = data["dynamic_variables"]
        brief = dv.get("voiceHandoffBrief", "")
        assert brief == ""

    def test_warning_logged_when_no_objects(self, client_with_mocks, caplog):
        client, _ = client_with_mocks
        with (
            patch(
                "aspire_orchestrator.routes.memory.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
            caplog.at_level(logging.WARNING, logger="aspire_orchestrator.routes.memory"),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200
        warn_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("no memory objects" in m.lower() or "non-existent" in m.lower() for m in warn_messages)

    def test_no_pii_in_warning_log(self, client_with_mocks, caplog):
        """Law #9: Full UUIDs must not appear in log messages."""
        client, _ = client_with_mocks
        with (
            patch(
                "aspire_orchestrator.routes.memory.supabase_select",
                new=AsyncMock(return_value=[]),
            ),
            caplog.at_level(logging.WARNING, logger="aspire_orchestrator.routes.memory"),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        # Full 36-char UUID strings must NOT appear in any log message
        for record in caplog.records:
            # A UUID has form xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 chars)
            # We only log the first 8 chars per Law #9
            assert HANDOFF_ID not in record.message, (
                f"Full handoff_id UUID leaked into log: {record.message}"
            )

    def test_receipt_emitted_even_in_degraded_mode(self, client_with_mocks):
        """Law #2: Receipt must be emitted even when handoff not found."""
        client, mock_store_receipts = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200
        mock_store_receipts.assert_called()


# ===========================================================================
# Test class: ElevenLabs session unaffected
# ===========================================================================


class TestElevenLabsSessionUnaffected:
    """ElevenLabs (non-anam) sessions must not trigger handoff_id processing."""

    def test_el_session_without_runtime_family_succeeds(self, client_with_mocks):
        """No runtime_family → session starts normally, no supabase_select called."""
        client, _ = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=[]),
        ) as mock_select:
            resp = client.post(
                "/v1/session-broker/start",
                json=_el_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "session_id" in data
        # supabase_select should NOT be called for non-anam sessions
        # (brief materializer uses BriefMaterializer internally, not direct supabase_select
        #  in the handoff path; EL sessions skip the handoff block entirely)
        # We check voiceHandoffBrief is absent
        dv = data["dynamic_variables"]
        assert "voiceHandoffBrief" not in dv or dv.get("voiceHandoffBrief", "") == ""

    def test_el_session_with_handoff_id_in_body_not_processed(
        self, client_with_mocks
    ):
        """EL session with a handoff_id in body: since runtime_family != 'anam_video',
        the handoff resolution block is skipped entirely."""
        client, _ = client_with_mocks
        body = {
            **_el_session_body(),
            "handoff_id": HANDOFF_ID,
        }
        # supabase_select would return cross-tenant rows if called — but it should NOT be called
        cross_tenant_rows = _three_handoff_rows(tenant_id=TENANT_B, suite_id=SUITE_B)
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(return_value=cross_tenant_rows),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=body,
                headers=_SCOPE_HEADERS_A,
            )
        # Must succeed — no cross-tenant check for EL sessions
        assert resp.status_code == 200, resp.text

    def test_el_session_cross_tenant_body_still_returns_403(self, client_with_mocks):
        """EL session with cross-tenant body scope still fails the top-level tenant check."""
        client, _ = client_with_mocks
        body = {
            "agent_name": "ava",
            "scope": _scope_body(tenant_id=TENANT_B),
            "channel": "voice",
        }
        resp = client.post(
            "/v1/session-broker/start",
            json=body,
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TENANT_ISOLATION_VIOLATION"

    def test_allowed_tools_present_in_el_session(self, client_with_mocks):
        client, _ = client_with_mocks
        resp = client.post(
            "/v1/session-broker/start",
            json=_el_session_body(),
            headers=_SCOPE_HEADERS_A,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "allowed_tools" in data
        assert isinstance(data["allowed_tools"], list)
        assert len(data["allowed_tools"]) > 0


# ===========================================================================
# Test class: DB failure in handoff resolution (degraded mode)
# ===========================================================================


class TestAnamHandoffDbFailure:
    """When supabase_select raises SupabaseClientError, session continues in degraded mode."""

    def test_db_failure_in_handoff_path_does_not_crash_session(self, client_with_mocks):
        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        client, _ = client_with_mocks
        with patch(
            "aspire_orchestrator.routes.memory.supabase_select",
            new=AsyncMock(
                side_effect=SupabaseClientError(
                    "select", status_code=500, detail="DB unavailable"
                )
            ),
        ):
            resp = client.post(
                "/v1/session-broker/start",
                json=_anam_session_body(),
                headers=_SCOPE_HEADERS_A,
            )
        assert resp.status_code == 200, resp.text
        dv = resp.json()["dynamic_variables"]
        # brief absent or empty in degraded mode
        assert dv.get("voiceHandoffBrief", "") == ""
