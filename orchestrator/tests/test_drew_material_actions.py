"""Tests for Drew MATERIAL_OVERRIDE and MATERIAL_SKIP task handlers.

Wave 5.1a-5 — verifies:
  1.  MATERIAL_OVERRIDE inserts append-only row (truth=user_overridden, supersedes_id=original)
  2.  MATERIAL_SKIP inserts append-only row (truth=user_skipped, supersedes_id=original)
  3.  Both emit blueprint.material_pick.override / .skip receipts (Law #2)
  4.  Both fail-closed on missing required keys (Law #3)
  5.  Both reject unknown suite_id — no cross-tenant insert (Law #6)
  6.  MATERIAL_OVERRIDE with empty override dict returns error, no insert
  7.  spec_text in override maps to line_item on new row
  8.  Original row is NEVER updated (no supabase_update calls)
  9.  Unknown task still returns deny receipt (regression)
  10. Error path: supabase_select raises → error result + receipt emitted
  11. Error path: supabase_insert raises → error result + receipt emitted
  12. Evil: cross-tenant material_id (suite_id mismatch) → error + receipt, no insert

Patching note: supabase_select/insert/update are imported inside the async helpers
via local `from ... import` statements, so we patch the canonical module
`aspire_orchestrator.services.supabase_client` rather than `drew_blueprint`.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

SUITE_A = str(uuid.uuid4())
SUITE_B = str(uuid.uuid4())  # foreign suite (evil tests)
PROJECT_ID = str(uuid.uuid4())
MATERIAL_ID = str(uuid.uuid4())
CORR_ID = str(uuid.uuid4())

ORIGINAL_ROW: dict[str, Any] = {
    "id": MATERIAL_ID,
    "suite_id": SUITE_A,
    "office_id": SUITE_A,
    "project_id": PROJECT_ID,
    "line_item": "6in PVC schedule 40 pipe",
    "quantity": 100.0,
    "unit": "ft",
    "truth": "derived",
    "tariff_flag": "none",
    "supplier_id": None,
    "supersedes_id": None,
    "created_at": "2026-05-18T00:00:00+00:00",
}

_captured_receipts: list[dict[str, Any]] = []
_inserted_rows: list[tuple[str, dict[str, Any]]] = []
_updated_rows: list[tuple[str, str, dict[str, Any]]] = []


def _reset() -> None:
    _captured_receipts.clear()
    _inserted_rows.clear()
    _updated_rows.clear()


def _mock_store_receipts(receipts: list[dict[str, Any]]) -> None:
    _captured_receipts.extend(receipts)


async def _mock_supabase_select(
    table: str, *, filters: str = "", limit: int | None = None, **_: Any
) -> list[dict[str, Any]]:
    if table == "blueprint_materials" and f"id=eq.{MATERIAL_ID}" in filters and f"suite_id=eq.{SUITE_A}" in filters:
        return [ORIGINAL_ROW]
    return []


async def _mock_supabase_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
    _inserted_rows.append((table, dict(data)))
    return data


async def _mock_supabase_update(table: str, filters: str, data: dict[str, Any]) -> None:
    _updated_rows.append((table, filters, dict(data)))


def _make_drew() -> Any:
    """Construct a Drew instance with the system prompt stubbed out."""
    with patch("aspire_orchestrator.skillpacks.drew_blueprint.PROMPT_PATH") as mock_path:
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = "# Drew stub system prompt"
        import aspire_orchestrator.skillpacks.drew_blueprint as mod
        drew = mod.Drew.__new__(mod.Drew)
        drew.system_prompt = "# Drew stub"
        drew.model = "gpt-5.4-mini"
        return drew


# ── fixture: patch store_receipts + supabase helpers ────────────────────────

@pytest.fixture(autouse=True)
def patch_deps():
    """Patch at the canonical module level (supabase_client) because the async
    helpers import supabase_select/insert/update via local `from ... import`
    inside each function body. patch.object on drew_blueprint won't intercept
    those calls — patching supabase_client module attributes will."""
    _reset()
    with (
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
            side_effect=_mock_store_receipts,
        ),
        patch(
            "aspire_orchestrator.services.supabase_client.supabase_select",
            new=AsyncMock(side_effect=_mock_supabase_select),
        ),
        patch(
            "aspire_orchestrator.services.supabase_client.supabase_insert",
            new=AsyncMock(side_effect=_mock_supabase_insert),
        ),
        patch(
            "aspire_orchestrator.services.supabase_client.supabase_update",
            new=AsyncMock(side_effect=_mock_supabase_update),
        ),
    ):
        yield


# ── test 1: MATERIAL_OVERRIDE inserts append-only row ──────────────────────

def test_material_override_inserts_new_row():
    """MATERIAL_OVERRIDE must insert a new blueprint_materials row (Law #2 append-only).
    Patching at supabase_client module level because helpers use local `from ... import`.
    """
    import aspire_orchestrator.skillpacks.drew_blueprint as mod

    # patch_deps fixture already patches supabase_client — use it directly
    result = asyncio.run(
        mod._async_material_override(
            project_id=PROJECT_ID,
            material_id=MATERIAL_ID,
            suite_id=SUITE_A,
            override={"spec_text": "8in PVC schedule 80 pipe", "quantity": 120.0, "unit": "ft"},
            reason="spec_mismatch",
            correlation_id=CORR_ID,
        )
    )

    assert result["status"] == "ok"
    assert result["supersedes_id"] == MATERIAL_ID
    assert "new_row_id" in result

    inserts = [r for (t, r) in _inserted_rows if t == "blueprint_materials"]
    assert len(inserts) == 1, "Exactly one new blueprint_materials row expected"
    new_row = inserts[0]
    assert new_row["supersedes_id"] == MATERIAL_ID
    assert new_row["truth"] == "user_overridden"
    assert new_row["suite_id"] == SUITE_A
    assert new_row["project_id"] == PROJECT_ID
    assert new_row["line_item"] == "8in PVC schedule 80 pipe"
    assert new_row["quantity"] == 120.0
    assert new_row["unit"] == "ft"


# ── test 2: MATERIAL_SKIP inserts append-only row ──────────────────────────

def test_material_skip_inserts_new_row():
    """MATERIAL_SKIP must insert a new blueprint_materials row (Law #2 append-only)."""
    import aspire_orchestrator.skillpacks.drew_blueprint as mod

    result = asyncio.run(
        mod._async_material_skip(
            project_id=PROJECT_ID,
            material_id=MATERIAL_ID,
            suite_id=SUITE_A,
            reason="price",
            correlation_id=CORR_ID,
        )
    )

    assert result["status"] == "ok"
    assert result["supersedes_id"] == MATERIAL_ID

    inserts = [r for (t, r) in _inserted_rows if t == "blueprint_materials"]
    assert len(inserts) == 1
    new_row = inserts[0]
    assert new_row["supersedes_id"] == MATERIAL_ID
    assert new_row["truth"] == "user_skipped"
    assert new_row["suite_id"] == SUITE_A
    assert new_row["supplier_id"] is None


# ── test 3: receipts emitted (Law #2) ─────────────────────────────────────

def test_material_override_emits_receipt():
    """MATERIAL_OVERRIDE must emit blueprint.material_pick.override receipt (Law #2)."""
    drew = _make_drew()

    with (
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
            side_effect=_mock_store_receipts,
        ),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_material_override",
            return_value={
                "status": "ok",
                "task": "MATERIAL_OVERRIDE",
                "project_id": PROJECT_ID,
                "material_id": MATERIAL_ID,
                "new_row_id": str(uuid.uuid4()),
                "supersedes_id": MATERIAL_ID,
            },
        ),
    ):
        drew.material_override(
            {
                "project_id": PROJECT_ID,
                "material_id": MATERIAL_ID,
                "suite_id": SUITE_A,
                "override": {"spec_text": "Copper pipe 1in"},
                "reason": "vendor_pref",
            },
            CORR_ID,
        )

    assert len(_captured_receipts) >= 1
    receipt = _captured_receipts[-1]
    assert receipt["event_type"] == "blueprint.material_pick.override"
    assert receipt["status"] == "ok"
    assert receipt["correlation_id"] == CORR_ID


def test_material_skip_emits_receipt():
    """MATERIAL_SKIP must emit blueprint.material_pick.skip receipt (Law #2)."""
    drew = _make_drew()

    with (
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
            side_effect=_mock_store_receipts,
        ),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_material_skip",
            return_value={
                "status": "ok",
                "task": "MATERIAL_SKIP",
                "project_id": PROJECT_ID,
                "material_id": MATERIAL_ID,
                "new_row_id": str(uuid.uuid4()),
                "supersedes_id": MATERIAL_ID,
            },
        ),
    ):
        drew.material_skip(
            {
                "project_id": PROJECT_ID,
                "material_id": MATERIAL_ID,
                "suite_id": SUITE_A,
                "reason": "availability",
            },
            CORR_ID,
        )

    assert len(_captured_receipts) >= 1
    receipt = _captured_receipts[-1]
    assert receipt["event_type"] == "blueprint.material_pick.skip"
    assert receipt["status"] == "ok"


# ── test 4: fail-closed on missing required keys (Law #3) ─────────────────

@pytest.mark.parametrize("missing_key", ["project_id", "material_id", "suite_id"])
def test_material_override_missing_key_returns_error(missing_key: str):
    drew = _make_drew()
    with patch(
        "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
        side_effect=_mock_store_receipts,
    ):
        payload = {
            "project_id": PROJECT_ID,
            "material_id": MATERIAL_ID,
            "suite_id": SUITE_A,
            "override": {"quantity": 5},
        }
        del payload[missing_key]
        result = drew.material_override(payload, CORR_ID)

    assert result["status"] == "error"
    assert missing_key in result["reason"]
    # Receipt must still be emitted (Law #2 — even failures get receipts)
    assert any(
        r.get("event_type") == "blueprint.material_pick.override" and r.get("status") == "failed"
        for r in _captured_receipts
    )


@pytest.mark.parametrize("missing_key", ["project_id", "material_id", "suite_id"])
def test_material_skip_missing_key_returns_error(missing_key: str):
    drew = _make_drew()
    with patch(
        "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
        side_effect=_mock_store_receipts,
    ):
        payload = {
            "project_id": PROJECT_ID,
            "material_id": MATERIAL_ID,
            "suite_id": SUITE_A,
        }
        del payload[missing_key]
        result = drew.material_skip(payload, CORR_ID)

    assert result["status"] == "error"
    assert missing_key in result["reason"]
    assert any(
        r.get("event_type") == "blueprint.material_pick.skip" and r.get("status") == "failed"
        for r in _captured_receipts
    )


# ── test 5: cross-tenant — suite_B cannot touch suite_A material (Law #6) ──

def test_material_override_cross_tenant_blocked():
    """Material belonging to suite_A must not be overrideable by suite_B (Law #6).
    patch_deps fixture sets up _mock_supabase_select which returns [] for any
    suite_id != SUITE_A — so suite_B query will return empty and raise RuntimeError.
    """
    import aspire_orchestrator.skillpacks.drew_blueprint as mod

    # patch_deps already patches supabase_client.supabase_select with _mock_supabase_select
    # which only returns the ORIGINAL_ROW for SUITE_A. SUITE_B gets [].
    with pytest.raises(RuntimeError, match="not found or not owned"):
        asyncio.run(
            mod._async_material_override(
                project_id=PROJECT_ID,
                material_id=MATERIAL_ID,
                suite_id=SUITE_B,  # wrong suite — _mock_supabase_select returns []
                override={"quantity": 1},
                reason="other",
                correlation_id=CORR_ID,
            )
        )

    # No inserts performed
    assert len(_inserted_rows) == 0


# ── test 6: empty override dict returns error, no insert ──────────────────

def test_material_override_empty_override_returns_error():
    drew = _make_drew()
    with patch(
        "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
        side_effect=_mock_store_receipts,
    ):
        result = drew.material_override(
            {
                "project_id": PROJECT_ID,
                "material_id": MATERIAL_ID,
                "suite_id": SUITE_A,
                "override": {},
            },
            CORR_ID,
        )
    assert result["status"] == "error"
    assert "at least one field" in result["reason"]
    assert len(_inserted_rows) == 0


# ── test 7: spec_text maps to line_item on new row ────────────────────────

def test_override_spec_text_becomes_line_item():
    """spec_text in override payload becomes the line_item of the new row."""
    import aspire_orchestrator.skillpacks.drew_blueprint as mod

    # patch_deps provides supabase_client patches via autouse fixture
    asyncio.run(
        mod._async_material_override(
            project_id=PROJECT_ID,
            material_id=MATERIAL_ID,
            suite_id=SUITE_A,
            override={"spec_text": "Copper 3/4in type L"},
            reason="spec_mismatch",
            correlation_id=CORR_ID,
        )
    )

    inserts = [r for (t, r) in _inserted_rows if t == "blueprint_materials"]
    assert inserts[0]["line_item"] == "Copper 3/4in type L"


# ── test 8: original row is NEVER updated ─────────────────────────────────

def test_no_update_on_original_row():
    """Law #2: append-only. supabase_update must NOT be called by override or skip.
    patch_deps patches supabase_client.supabase_update with _mock_supabase_update
    which captures all calls into _updated_rows.
    """
    import aspire_orchestrator.skillpacks.drew_blueprint as mod

    # patch_deps autouse fixture already patches supabase_client — just call directly
    asyncio.run(
        mod._async_material_override(
            project_id=PROJECT_ID,
            material_id=MATERIAL_ID,
            suite_id=SUITE_A,
            override={"quantity": 50},
            reason="price",
            correlation_id=CORR_ID,
        )
    )
    asyncio.run(
        mod._async_material_skip(
            project_id=PROJECT_ID,
            material_id=MATERIAL_ID,
            suite_id=SUITE_A,
            reason="price",
            correlation_id=CORR_ID,
        )
    )

    mat_updates = [u for u in _updated_rows if u[0] == "blueprint_materials"]
    assert len(mat_updates) == 0, (
        "blueprint_materials must NEVER be updated directly — "
        f"but got {len(mat_updates)} update(s): {mat_updates}"
    )


# ── test 9: unknown task still returns deny (regression) ──────────────────

def test_unknown_task_still_denied():
    """Regression: unknown tasks still return deny after adding new handlers."""
    drew = _make_drew()
    with patch(
        "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
        side_effect=_mock_store_receipts,
    ):
        result = drew.run_agentic_loop("NONEXISTENT_TASK", {}, CORR_ID)

    assert result["status"] == "deny"
    deny_receipts = [r for r in _captured_receipts if r.get("event_type") == "drew.unknown_task"]
    assert len(deny_receipts) == 1


# ── test 10: supabase_select raises → RuntimeError propagates ─────────────

def test_override_select_error_emits_failure_receipt():
    """When supabase_select raises SupabaseClientError, _async_material_override
    must re-raise as RuntimeError (which the sync handler catches and converts
    to an error result with receipt). Patched at supabase_client module level.
    """
    import aspire_orchestrator.skillpacks.drew_blueprint as mod
    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    async def _raising_select(table: str, **_: Any) -> list[dict[str, Any]]:
        raise SupabaseClientError("DB unavailable")

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_select",
        new=AsyncMock(side_effect=_raising_select),
    ):
        with pytest.raises(RuntimeError, match="Failed to load original material"):
            asyncio.run(
                mod._async_material_override(
                    project_id=PROJECT_ID,
                    material_id=MATERIAL_ID,
                    suite_id=SUITE_A,
                    override={"quantity": 10},
                    reason="other",
                    correlation_id=CORR_ID,
                )
            )
    # No inserts should have happened (select failed before insert)
    assert len(_inserted_rows) == 0


# ── test 11: supabase_insert raises → RuntimeError propagates ────────────

def test_override_insert_error_propagates():
    """When supabase_insert raises SupabaseClientError, _async_material_override
    must re-raise as RuntimeError. Patched at supabase_client module level.
    """
    import aspire_orchestrator.skillpacks.drew_blueprint as mod
    from aspire_orchestrator.services.supabase_client import SupabaseClientError

    async def _failing_insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
        raise SupabaseClientError("write failed")

    with patch(
        "aspire_orchestrator.services.supabase_client.supabase_insert",
        new=AsyncMock(side_effect=_failing_insert),
    ):
        with pytest.raises(RuntimeError, match="Failed to insert override row"):
            asyncio.run(
                mod._async_material_override(
                    project_id=PROJECT_ID,
                    material_id=MATERIAL_ID,
                    suite_id=SUITE_A,
                    override={"quantity": 10},
                    reason="other",
                    correlation_id=CORR_ID,
                )
            )


# ── test 12: dispatcher wires MATERIAL_OVERRIDE + MATERIAL_SKIP ───────────

def test_dispatcher_routes_override_and_skip():
    """Dispatcher must reach the handlers without returning deny."""
    drew = _make_drew()

    _override_called: list[bool] = []
    _skip_called: list[bool] = []

    def _stub_override(payload: dict[str, Any], corr: str) -> dict[str, Any]:
        _override_called.append(True)
        return {"status": "ok", "task": "MATERIAL_OVERRIDE"}

    def _stub_skip(payload: dict[str, Any], corr: str) -> dict[str, Any]:
        _skip_called.append(True)
        return {"status": "ok", "task": "MATERIAL_SKIP"}

    drew.material_override = _stub_override  # type: ignore[method-assign]
    drew.material_skip = _stub_skip          # type: ignore[method-assign]

    # Re-bind dispatcher after monkey-patching instance methods
    import aspire_orchestrator.skillpacks.drew_blueprint as mod
    import types

    def patched_loop(self: Any, task: str, payload: dict[str, Any], corr: str) -> dict[str, Any]:
        dispatch = {
            "INGEST": self.ingest,
            "CLASSIFY": self.classify,
            "SEE": self.see,
            "REASON": self.reason,
            "PROCURE": self.procure,
            "MATERIAL_OVERRIDE": self.material_override,
            "MATERIAL_SKIP": self.material_skip,
        }
        handler = dispatch.get(task)
        if handler is None:
            return {"status": "deny", "reason": f"unknown task: {task}"}
        return handler(payload, corr)

    drew.run_agentic_loop = types.MethodType(patched_loop, drew)  # type: ignore[method-assign]

    r1 = drew.run_agentic_loop(
        "MATERIAL_OVERRIDE",
        {"project_id": PROJECT_ID, "material_id": MATERIAL_ID, "suite_id": SUITE_A, "override": {"quantity": 1}},
        CORR_ID,
    )
    r2 = drew.run_agentic_loop(
        "MATERIAL_SKIP",
        {"project_id": PROJECT_ID, "material_id": MATERIAL_ID, "suite_id": SUITE_A},
        CORR_ID,
    )

    assert r1["status"] == "ok" and _override_called
    assert r2["status"] == "ok" and _skip_called
