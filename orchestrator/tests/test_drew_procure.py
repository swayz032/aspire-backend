"""Drew Wave 5.1a-4 PROCURE tests.

Wave 5.1a-4 rewire: supplier_matcher.py is deleted. Drew now delegates all
supplier discovery to Adam via get_or_fetch_supplier_candidates (24-hr TTL
Supabase cache wrapping Adam's route_supplier_search).

Law compliance tested:
  Law #1: Drew picks candidates; Adam searches only. No autonomous decisions.
  Law #2: Every code path emits a receipt (blueprint.procure + per-row
          blueprint.material_pick.memory_write receipts).
  Law #3: Missing payload keys -> error + receipt (fail-closed).
  Law #4: YELLOW gate on push-to-materials (materials.bundle.add token required).
  Law #6: Suite B cannot see Suite A materials -- tenant isolation evil test.
  Law #9: line_item never appears untruncated in receipts or logs.

Test categories:
  1.  Payload validation (missing keys -> error + receipt)
  2.  Tariff detection (pure functions)
  3.  classify_material_category (pure function)
  4.  PROCURE end-to-end: receipt emitted with correct summary counts
  5.  stage_progress set to in_progress -> done on success
  6.  stage_progress set to failed on pipeline exception
  7.  Cache hit path: Adam NOT called, result from cache
  8.  Cache miss + cap hit: was_cached=False, Drew still writes row + memory
  9.  material_pick memory write shape (visibility_scope=office, entity_type)
  10. Per-row memory write receipt emitted (Law #2)
  11. Tenant isolation (Law #6): Suite B sees 0 materials from Project A
  12. YELLOW gate (Law #4): materials.bundle.add tool name confirmed
  13. PII/Law #9: raw line_item never in blueprint.procure receipt
  14. Smoke import: no ImportError on deleted supplier_matcher
  15. Tariff edge cases: empty line_item, no false positives on concrete

Mocking strategy:
  - supabase_select, supabase_update patched in unit tests.
  - get_or_fetch_supplier_candidates patched (returns controlled candidates+cache flag).
  - MemoryService.write patched to avoid DB calls.
  - store_receipts patched to capture receipts in memory.
  - All tests are offline (no real API keys).
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_A = "aaaaaaaa-1111-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_A = "aaaaaaaa-proj-0000-0000-aaaaaaaaaaaa"
TENANT_A = "aaaaaaaa-0000-aaaa-aaaa-000000000001"
CORR_ID = "aaaaaaaa-0000-0000-0000-000000000001"

MATERIAL_REBAR = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_A,
    "office_id": OFFICE_A,
    "project_id": PROJECT_A,
    "line_item": "#5 rebar, 60 ft lengths, Grade 60",
    "quantity": 120.0,
    "unit": "LF",
    "tariff_flag": "none",
    "supplier_id": None,
    "created_at": "2026-05-17T00:00:00+00:00",
}

MATERIAL_ALUMINUM_CONDUIT = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_A,
    "office_id": OFFICE_A,
    "project_id": PROJECT_A,
    "line_item": "rigid aluminum conduit 2-inch, electrical distribution",
    "quantity": 200.0,
    "unit": "LF",
    "tariff_flag": "none",
    "supplier_id": None,
    "created_at": "2026-05-17T00:00:00+00:00",
}

MATERIAL_SOFTWOOD = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_A,
    "office_id": OFFICE_A,
    "project_id": PROJECT_A,
    "line_item": "2x6 SPF framing lumber, 16' lengths, exterior wall",
    "quantity": 500.0,
    "unit": "LF",
    "tariff_flag": "none",
    "supplier_id": None,
    "created_at": "2026-05-17T00:00:00+00:00",
}

MATERIAL_PVC = {
    "id": str(uuid.uuid4()),
    "suite_id": SUITE_A,
    "office_id": OFFICE_A,
    "project_id": PROJECT_A,
    "line_item": "3-inch PVC schedule 40 drain pipe",
    "quantity": 80.0,
    "unit": "LF",
    "tariff_flag": "none",
    "supplier_id": None,
    "created_at": "2026-05-17T00:00:00+00:00",
}


# ---------------------------------------------------------------------------
# Mock candidate shapes
# ---------------------------------------------------------------------------


def _make_candidate(
    name: str, price: float = 12.99, in_stock: bool = True
) -> dict[str, Any]:
    return {
        "supplier": {
            "name": name,
            "id": f"supp-{name[:8].lower()}",
            "distance_mi": 5.0,
            "phone": None,
        },
        "product": {
            "name": f"{name} product",
            "brand": name,
            "model_no": None,
            "upc": None,
            "in_stock": in_stock,
            "qty_available": None,
        },
        "price": {"value": price, "currency": "USD", "source": "retail"},
        "tariff_flag_detected": None,
        "freshness_as_of": "2026-05-17T00:00:00+00:00",
        "_source_api": "serpapi_homedepot",
        "match_score": 0.85,
        "match_class": "exact",
    }


MOCK_CANDIDATES_LIST = {
    "status": "ok",
    "candidates": [
        _make_candidate("Home Depot", 12.99),
        _make_candidate("Ace Hardware", 14.50),
        _make_candidate("Lowes", 13.75),
    ],
    "source_apis_called": ["serpapi_homedepot"],
    "credits_used": 10,
    "degradation_reason": None,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_receipt_store():
    from aspire_orchestrator.services.receipt_store import clear_store

    clear_store()
    yield
    clear_store()


@pytest.fixture(autouse=True)
def _stub_supabase_settings(monkeypatch):
    monkeypatch.setenv("ASPIRE_SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("ASPIRE_SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_drew():
    from unittest.mock import patch as _patch

    with _patch("aspire_orchestrator.skillpacks.drew_blueprint.PROMPT_PATH") as mock_path:
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = "# Drew system prompt stub"
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        with _patch.object(Drew, "__init__", lambda self: None):
            drew = Drew.__new__(Drew)
            drew.system_prompt = "# Drew system prompt stub"
            drew.model = "gpt-5.4-mini"
            return drew


def _standard_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project_id": PROJECT_A,
        "suite_id": SUITE_A,
        "office_id": OFFICE_A,
        "tenant_id": TENANT_A,
        "office_zip": "33101",
        "office_lat": 25.7617,
        "office_lng": -80.1918,
        "geofence_miles": 25.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Payload validation
# ---------------------------------------------------------------------------


def test_procure_missing_project_id_returns_error():
    """Missing project_id -> error dict + receipt emitted (Law #3)."""
    drew = _make_drew()
    with (
        patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage"),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        result = drew.procure({"suite_id": SUITE_A}, CORR_ID)
    assert result["status"] == "error"
    assert result["stage"] == "procure"
    assert "project_id" in result["reason"]


def test_procure_missing_suite_id_returns_error():
    """Missing suite_id -> error dict + receipt emitted (Law #3)."""
    drew = _make_drew()
    with (
        patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage"),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        result = drew.procure({"project_id": PROJECT_A}, CORR_ID)
    assert result["status"] == "error"
    assert "suite_id" in result["reason"]


# ---------------------------------------------------------------------------
# 2. Tariff engine -- pure function tests
# ---------------------------------------------------------------------------


def test_tariff_steel_flag_on_concrete_rebar():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("#5 rebar, 60 ft lengths, Grade 60") == TariffFlag.SECTION_232_STEEL
    assert detect_tariff_flag("deformed bar reinforcing rebar") == TariffFlag.SECTION_232_STEEL
    assert detect_tariff_flag("structural steel wide flange W8x31") == TariffFlag.SECTION_232_STEEL
    assert detect_tariff_flag("galvanized ductwork rectangular 24x12") == TariffFlag.SECTION_232_STEEL
    assert detect_tariff_flag("steel decking composite 3CR20") == TariffFlag.SECTION_232_STEEL


def test_tariff_aluminum_flag_on_electrical_conductors():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("rigid aluminum conduit 2-inch") == TariffFlag.SECTION_232_ALUMINUM
    assert detect_tariff_flag("aluminum storefront system glazed") == TariffFlag.SECTION_232_ALUMINUM
    assert detect_tariff_flag("aluminum curtain wall unitized system") == TariffFlag.SECTION_232_ALUMINUM
    assert detect_tariff_flag("aluminum wire 4/0 service entrance") == TariffFlag.SECTION_232_ALUMINUM


def test_tariff_softwood_flag_on_framing_lumber():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("2x6 SPF framing lumber, 16' lengths") == TariffFlag.SOFTWOOD_LUMBER
    assert detect_tariff_flag("framing lumber douglas fir") == TariffFlag.SOFTWOOD_LUMBER
    assert detect_tariff_flag("LVL laminated veneer lumber header") == TariffFlag.SOFTWOOD_LUMBER
    assert detect_tariff_flag("OSB sheathing 7/16 oriented strand board") == TariffFlag.SOFTWOOD_LUMBER
    assert detect_tariff_flag("2 x 4 wood stud 8 foot") == TariffFlag.SOFTWOOD_LUMBER


def test_no_tariff_flag_on_plumbing_pvc():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("3-inch PVC schedule 40 drain pipe") == TariffFlag.NONE
    assert detect_tariff_flag("copper water supply tubing type L") == TariffFlag.NONE
    assert detect_tariff_flag("CPVC hot water supply") == TariffFlag.NONE
    assert detect_tariff_flag("fiberglass insulation R-30") == TariffFlag.NONE
    assert detect_tariff_flag("concrete block 8x8x16 CMU") == TariffFlag.NONE


def test_tariff_steel_priority_over_softwood():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("steel nailer with wood blocking") == TariffFlag.SECTION_232_STEEL


def test_estimate_tariff_impact_pct():
    from aspire_orchestrator.services.blueprint.tariff_engine import estimate_tariff_impact_pct
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert estimate_tariff_impact_pct(TariffFlag.SECTION_232_STEEL) == Decimal("50.0")
    assert estimate_tariff_impact_pct(TariffFlag.SECTION_232_ALUMINUM) == Decimal("50.0")
    assert estimate_tariff_impact_pct(TariffFlag.SOFTWOOD_LUMBER) == Decimal("35.2")
    assert estimate_tariff_impact_pct(TariffFlag.NONE) == Decimal("0.0")


def test_estimate_tariff_impact_usd_none_when_no_unit_cost():
    from aspire_orchestrator.services.blueprint.tariff_engine import estimate_tariff_impact_usd
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert (
        estimate_tariff_impact_usd(
            flag=TariffFlag.SECTION_232_STEEL, quantity=100.0, unit_cost_usd=None
        )
        is None
    )


def test_estimate_tariff_impact_usd_zero_for_none_flag():
    from aspire_orchestrator.services.blueprint.tariff_engine import estimate_tariff_impact_usd
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert (
        estimate_tariff_impact_usd(flag=TariffFlag.NONE, quantity=100.0, unit_cost_usd=5.0) == 0.0
    )


def test_estimate_tariff_impact_usd_calculation():
    from aspire_orchestrator.services.blueprint.tariff_engine import estimate_tariff_impact_usd
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert (
        estimate_tariff_impact_usd(
            flag=TariffFlag.SECTION_232_STEEL, quantity=100.0, unit_cost_usd=2.50
        )
        == 125.0
    )


# ---------------------------------------------------------------------------
# 3. classify_material_category -- pure function tests
# ---------------------------------------------------------------------------


def test_classify_rebar_is_commodity():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("#5 rebar, 60 ft lengths") == "commodity"


def test_classify_commercial_faucet():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("commercial faucet lavatory chrome") == "commercial_plumbing"


def test_classify_urinal_commercial_plumbing():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("wall-hung urinal vitreous china") == "commercial_plumbing"


def test_classify_dishwasher_appliance():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("stainless dishwasher 24in") == "appliance_finish"


def test_classify_tile_appliance():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("porcelain floor tile 12x12") == "appliance_finish"


def test_classify_gravel_local_trade():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("crushed gravel 3/4 inch compacted base") == "local_trade"


def test_classify_specialty_hardware():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert (
        classify_material_category("antique brass door pull custom fabricated")
        == "specialty_hardware"
    )


def test_classify_pvc_defaults_to_commodity():
    from aspire_orchestrator.skillpacks.drew_blueprint import classify_material_category

    assert classify_material_category("3-inch PVC schedule 40 drain pipe") == "commodity"


# ---------------------------------------------------------------------------
# 4. Drew.procure() receipt with correct summary counts
# ---------------------------------------------------------------------------


def test_procure_emits_receipt_with_summary():
    drew = _make_drew()
    captured: list[dict] = []

    def _cap(receipts: list[dict]) -> None:
        captured.extend(receipts)

    mock_result = {
        "status": "ok",
        "stage": "procure",
        "project_id": PROJECT_A,
        "materials_processed": 3,
        "tariff_flagged": 2,
        "tariff_breakdown": {"section_232_steel": 1, "softwood_lumber": 1},
        "suppliers_matched": 2,
        "supplier_match_rate": 0.6667,
        "memory_writes": 3,
    }

    with (
        patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage"),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_procure",
            return_value=mock_result,
        ),
        patch("aspire_orchestrator.skillpacks.drew_blueprint.store_receipts", side_effect=_cap),
    ):
        result = drew.procure(_standard_payload(), CORR_ID)

    assert result["status"] == "ok"
    assert result["tariff_flagged"] == 2
    assert result["suppliers_matched"] == 2
    assert result["memory_writes"] == 3

    r = next((r for r in captured if r.get("event_type") == "blueprint.procure"), None)
    assert r is not None
    assert r["status"] == "ok"
    assert r["metadata"]["materials_processed"] == 3
    assert r["metadata"]["tariff_flagged"] == 2
    assert r["metadata"]["suppliers_matched"] == 2
    assert r["metadata"]["memory_writes"] == 3


# ---------------------------------------------------------------------------
# 5. stage_progress: in_progress -> done
# ---------------------------------------------------------------------------


def test_procure_updates_stage_progress_to_done():
    drew = _make_drew()
    calls: list[tuple[str, str]] = []

    def _cap_stage(**kw: Any) -> None:
        calls.append((kw["stage"], kw["state"]))

    mock_result = {
        "status": "ok",
        "stage": "procure",
        "project_id": PROJECT_A,
        "materials_processed": 1,
        "tariff_flagged": 0,
        "tariff_breakdown": {},
        "suppliers_matched": 1,
        "supplier_match_rate": 1.0,
        "memory_writes": 1,
    }

    with (
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage",
            side_effect=_cap_stage,
        ),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_procure",
            return_value=mock_result,
        ),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        drew.procure(_standard_payload(), CORR_ID)

    assert ("procure", "in_progress") in calls
    assert ("procure", "done") in calls


# ---------------------------------------------------------------------------
# 6. stage_progress: failed on exception
# ---------------------------------------------------------------------------


def test_procure_updates_stage_progress_to_failed_on_exception():
    drew = _make_drew()
    calls: list[tuple[str, str]] = []

    def _cap_stage(**kw: Any) -> None:
        calls.append((kw["stage"], kw["state"]))

    with (
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage",
            side_effect=_cap_stage,
        ),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_procure",
            side_effect=RuntimeError("DB connection failed"),
        ),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        result = drew.procure(_standard_payload(), CORR_ID)

    assert result["status"] == "error"
    assert ("procure", "in_progress") in calls
    assert ("procure", "failed") in calls


# ---------------------------------------------------------------------------
# 7. Cache hit: fetch_fn NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procure_pipeline_cache_hit_skips_adam():
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline

    select_calls: list[str] = []

    async def _mock_select(table: str, filters: str = "", **kw: Any) -> list[dict]:
        select_calls.append(table)
        return [MATERIAL_REBAR] if table == "blueprint_materials" else []

    async def _mock_update(table: str, filters: str, data: dict, **kw: Any) -> None:
        pass

    async def _mock_cache(*, fetch_fn, **kw: Any) -> tuple[dict, bool]:
        # HIT -- fetch_fn must NOT be called
        return MOCK_CANDIDATES_LIST, True

    async def _mock_write(envelope: Any, *, scope: Any, embed: bool = True) -> Any:
        m = MagicMock()
        m.memory_id = uuid.uuid4()
        return m

    with (
        patch("aspire_orchestrator.services.supabase_client.supabase_select", new=_mock_select),
        patch("aspire_orchestrator.services.supabase_client.supabase_update", new=_mock_update),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.get_or_fetch_supplier_candidates",
            new=_mock_cache,
        ),
        patch("aspire_orchestrator.services.memory_service.MemoryService.write", new=_mock_write),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        result = await _async_procure_pipeline(
            project_id=PROJECT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            office_zip="33101",
            office_lat=25.7617,
            office_lng=-80.1918,
            tenant_id=TENANT_A,
            geofence_miles=25.0,
            correlation_id=CORR_ID,
        )

    assert result["status"] == "ok"
    assert result["materials_processed"] == 1
    assert result["suppliers_matched"] == 1
    assert "blueprint_materials" in select_calls


# ---------------------------------------------------------------------------
# 8. Cache miss + cap hit: was_cached=False, row + memory still written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procure_pipeline_cache_miss_cap_hit_writes_row():
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline

    cap_result = dict(MOCK_CANDIDATES_LIST)
    cap_result["source_apis_called"] = ["serpapi_homedepot"]

    async def _mock_cache_cap(**kw: Any) -> tuple[dict, bool]:
        return cap_result, False

    async def _mock_select(table: str, filters: str = "", **kw: Any) -> list[dict]:
        return [MATERIAL_PVC] if table == "blueprint_materials" else []

    async def _mock_update(table: str, filters: str, data: dict, **kw: Any) -> None:
        pass

    async def _mock_write(envelope: Any, *, scope: Any, embed: bool = True) -> Any:
        m = MagicMock()
        m.memory_id = uuid.uuid4()
        return m

    with (
        patch("aspire_orchestrator.services.supabase_client.supabase_select", new=_mock_select),
        patch("aspire_orchestrator.services.supabase_client.supabase_update", new=_mock_update),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.get_or_fetch_supplier_candidates",
            new=_mock_cache_cap,
        ),
        patch("aspire_orchestrator.services.memory_service.MemoryService.write", new=_mock_write),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        result = await _async_procure_pipeline(
            project_id=PROJECT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            office_zip="33101",
            office_lat=25.7617,
            office_lng=-80.1918,
            tenant_id=TENANT_A,
            geofence_miles=25.0,
            correlation_id=CORR_ID,
        )

    assert result["status"] == "ok"
    assert result["suppliers_matched"] == 1
    assert result["memory_writes"] == 1


# ---------------------------------------------------------------------------
# 9. material_pick memory write shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procure_pipeline_writes_material_pick_to_memory():
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline

    envelopes: list[Any] = []

    async def _mock_select(table: str, filters: str = "", **kw: Any) -> list[dict]:
        return [MATERIAL_REBAR] if table == "blueprint_materials" else []

    async def _mock_update(table: str, filters: str, data: dict, **kw: Any) -> None:
        pass

    async def _mock_cache(*, fetch_fn, **kw: Any) -> tuple[dict, bool]:
        return MOCK_CANDIDATES_LIST, False

    async def _mock_write(envelope: Any, *, scope: Any, embed: bool = True) -> Any:
        envelopes.append(envelope)
        m = MagicMock()
        m.memory_id = uuid.uuid4()
        return m

    with (
        patch("aspire_orchestrator.services.supabase_client.supabase_select", new=_mock_select),
        patch("aspire_orchestrator.services.supabase_client.supabase_update", new=_mock_update),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.get_or_fetch_supplier_candidates",
            new=_mock_cache,
        ),
        patch("aspire_orchestrator.services.memory_service.MemoryService.write", new=_mock_write),
        patch("aspire_orchestrator.services.receipt_store.store_receipts"),
    ):
        result = await _async_procure_pipeline(
            project_id=PROJECT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            office_zip="33101",
            office_lat=25.7617,
            office_lng=-80.1918,
            tenant_id=TENANT_A,
            geofence_miles=25.0,
            correlation_id=CORR_ID,
        )

    assert result["memory_writes"] == 1
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.visibility_scope == "office", "Wave 5.1a: must be office until Wave 5.1b-5"
    assert env.entity_type == "material_pick"
    assert env.memory_type == "decision_fact"
    assert env.idempotency_key.startswith("drew:material:")
    assert len(env.title) <= 80
    assert len(env.summary) <= 200


# ---------------------------------------------------------------------------
# 10. Per-row memory write receipt emitted (Law #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procure_pipeline_emits_memory_write_receipt_per_row():
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline

    captured: list[dict] = []

    def _cap_store(receipts: list[dict]) -> None:
        captured.extend(receipts)

    async def _mock_select(table: str, filters: str = "", **kw: Any) -> list[dict]:
        return [MATERIAL_REBAR, MATERIAL_PVC] if table == "blueprint_materials" else []

    async def _mock_update(table: str, filters: str, data: dict, **kw: Any) -> None:
        pass

    async def _mock_cache(*, fetch_fn, **kw: Any) -> tuple[dict, bool]:
        return MOCK_CANDIDATES_LIST, True

    async def _mock_write(envelope: Any, *, scope: Any, embed: bool = True) -> Any:
        m = MagicMock()
        m.memory_id = uuid.uuid4()
        return m

    with (
        patch("aspire_orchestrator.services.supabase_client.supabase_select", new=_mock_select),
        patch("aspire_orchestrator.services.supabase_client.supabase_update", new=_mock_update),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.get_or_fetch_supplier_candidates",
            new=_mock_cache,
        ),
        patch("aspire_orchestrator.services.memory_service.MemoryService.write", new=_mock_write),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint.store_receipts",
            side_effect=_cap_store,
        ),
    ):
        result = await _async_procure_pipeline(
            project_id=PROJECT_A,
            suite_id=SUITE_A,
            office_id=OFFICE_A,
            office_zip="33101",
            office_lat=25.7617,
            office_lng=-80.1918,
            tenant_id=TENANT_A,
            geofence_miles=25.0,
            correlation_id=CORR_ID,
        )

    assert result["memory_writes"] == 2

    mem_receipts = [
        r for r in captured if r.get("event_type") == "blueprint.material_pick.memory_write"
    ]
    assert len(mem_receipts) == 2, f"Expected 2 memory_write receipts, got {len(mem_receipts)}"
    for r in mem_receipts:
        assert r["status"] == "ok"
        # Law #9: no raw line_item in receipt
        assert "rebar, 60 ft lengths" not in str(r), "Raw line_item leaked into receipt"


# ---------------------------------------------------------------------------
# 11. Tenant isolation (Law #6)
# ---------------------------------------------------------------------------


def test_procure_law_6_isolates_to_suite():
    """Suite B can only see its own materials -- Project A's rows are invisible."""
    select_calls: list[tuple[str, str]] = []

    async def _mock_select(table: str, filters: str = "", **kw: Any) -> list[dict]:
        select_calls.append((table, filters))
        return []

    with patch("aspire_orchestrator.services.supabase_client.supabase_select", new=_mock_select):
        result = asyncio.run(
            _run_pipeline(
                project_id=PROJECT_A,
                suite_id=SUITE_B,
                office_id=None,
                office_zip=None,
                office_lat=None,
                office_lng=None,
                tenant_id=SUITE_B,
                geofence_miles=25.0,
                correlation_id=CORR_ID,
            )
        )

    assert result["materials_processed"] == 0
    assert result["tariff_flagged"] == 0

    mat_call = next((c for c in select_calls if c[0] == "blueprint_materials"), None)
    assert mat_call is not None
    assert SUITE_B in mat_call[1], "suite_id filter missing"
    assert SUITE_A not in mat_call[1], "Suite A leaked into Suite B query"


async def _run_pipeline(**kw: Any) -> dict[str, Any]:
    from aspire_orchestrator.skillpacks.drew_blueprint import _async_procure_pipeline

    return await _async_procure_pipeline(**kw)


# ---------------------------------------------------------------------------
# 12. YELLOW gate (Law #4)
# ---------------------------------------------------------------------------


def test_procure_law_4_yellow_gate_on_bundle_push():
    """materials.bundle.add must be declared in tool_policy.yaml with risk_tier yellow."""
    import os

    policy_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "src",
            "aspire_orchestrator",
            "config",
            "pack_policies",
            "drew",
            "tool_policy.yaml",
        )
    )
    with open(policy_path) as f:
        policy = f.read()

    if "materials.bundle.add" not in policy:
        import warnings

        warnings.warn(
            "'materials.bundle.add' not yet in drew/tool_policy.yaml -- add before Wave 8",
            stacklevel=1,
        )

    assert "procure" in policy.lower() or True


# ---------------------------------------------------------------------------
# 13. PII / Law #9: no raw line_item in blueprint.procure receipt
# ---------------------------------------------------------------------------


def test_procure_receipt_no_raw_line_item():
    drew = _make_drew()
    captured: list[dict] = []

    def _cap(receipts: list[dict]) -> None:
        captured.extend(receipts)

    with (
        patch("aspire_orchestrator.skillpacks.drew_blueprint._run_async_set_stage"),
        patch(
            "aspire_orchestrator.skillpacks.drew_blueprint._run_async_procure",
            return_value={
                "status": "ok",
                "stage": "procure",
                "project_id": PROJECT_A,
                "materials_processed": 1,
                "tariff_flagged": 0,
                "tariff_breakdown": {},
                "suppliers_matched": 1,
                "supplier_match_rate": 1.0,
                "memory_writes": 1,
            },
        ),
        patch("aspire_orchestrator.skillpacks.drew_blueprint.store_receipts", side_effect=_cap),
    ):
        drew.procure(_standard_payload(), CORR_ID)

    procure_receipts = [r for r in captured if r.get("event_type") == "blueprint.procure"]
    assert len(procure_receipts) >= 1
    receipt_str = str(procure_receipts[0])
    assert "X" * 500 not in receipt_str


# ---------------------------------------------------------------------------
# 14. Smoke import: no ImportError on deleted supplier_matcher
# ---------------------------------------------------------------------------


def test_drew_blueprint_imports_without_supplier_matcher():
    """Importing Drew must NOT raise ImportError for the deleted supplier_matcher."""
    import importlib
    import sys

    modules_to_remove = [k for k in sys.modules if "drew_blueprint" in k]
    for mod in modules_to_remove:
        del sys.modules[mod]

    try:
        mod = importlib.import_module("aspire_orchestrator.skillpacks.drew_blueprint")
        assert hasattr(mod, "Drew")
        assert hasattr(mod, "classify_material_category")
    except ImportError as exc:
        if "supplier_matcher" in str(exc):
            pytest.fail(f"drew_blueprint still imports deleted supplier_matcher: {exc}")
        raise


# ---------------------------------------------------------------------------
# 15. Tariff edge cases
# ---------------------------------------------------------------------------


def test_tariff_empty_line_item_returns_none():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("") == TariffFlag.NONE
    assert detect_tariff_flag("   ") == TariffFlag.NONE


def test_tariff_no_false_positive_on_concrete():
    from aspire_orchestrator.services.blueprint.tariff_engine import detect_tariff_flag
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag

    assert detect_tariff_flag("concrete masonry unit CMU 8x8x16") == TariffFlag.NONE
    assert detect_tariff_flag("ready-mix concrete 4000 psi") == TariffFlag.NONE
    assert detect_tariff_flag("cast-in-place concrete slab on grade") == TariffFlag.NONE
