"""Tests for Wave 19.2 / Commit 4 vision methods on addenda / detail / general.

Pure unit — no real OpenAI calls. ``call_vision_with_budget`` is patched
at each resolver's import site so the test fixture controls the
"OpenAI" response shape.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
)
from aspire_orchestrator.services.blueprint.resolvers.vision_helper import (
    VisionCallResult,
)
from aspire_orchestrator.services.blueprint.vision_budget import VisionBudget


_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2"


def _candidate(
    *,
    category: MissingInputCategory,
    description: str,
    sheets: list[dict[str, Any]],
    provider: Any | None = None,
    budget: Any | None = None,
) -> MissingInputCandidate:
    return MissingInputCandidate(
        missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
        project_id=_PROJECT,
        suite_id=_SUITE,
        office_id=None,
        description=description,
        suggested_resolution=None,
        category=category,
        sheets=sheets,
        symbols_by_sheet={},
        ocr_by_sheet={},
        data_provider=provider,
        vision_budget=budget,
    )


def _provider_with_png(png: bytes = b"\x89PNG_FAKE") -> Any:
    p = MagicMock()
    p.get_sheet_png = AsyncMock(return_value=png)
    p.get_yolo_detections = AsyncMock(return_value=[])
    return p


# ===========================================================================
# AddendaResolver._method_vision_addendum_cover
# ===========================================================================


class TestAddendaVisionMethod:
    @pytest.mark.asyncio
    async def test_resolves_at_0_85_with_5_plus_supersession_entries(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.addenda_resolver import (
            AddendaResolver,
        )
        resolver = AddendaResolver()
        budget = VisionBudget(cap_usd=1.00)
        cand = _candidate(
            category=MissingInputCategory.ADDENDA,
            description="Addendum hierarchy is incomplete",
            sheets=[
                {"id": f"s-{i:03d}", "sheet_code": f"A-{100 + i}",
                 "sheet_title": "ADDENDUM 1 — COVER" if i == 0 else None}
                for i in range(8)
            ],
            provider=_provider_with_png(),
            budget=budget,
        )

        fake_result = VisionCallResult(
            ok=True,
            json_payload={
                "supersessions": [
                    {"sheet_code": f"A-{100 + i}", "addendum_number": 1,
                     "superseded_by": f"A-{100 + i}-R2"}
                    for i in range(6)
                ],
                "confidence": 0.92,
                "notes": "Full revision log extracted from cover sheet.",
            },
            input_tokens=1200, output_tokens=450, model="gpt-5.4-mini",
            skip_reason="",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await resolver._method_vision_addendum_cover(cand)

        assert result.method == "vision_addendum_cover"
        assert result.resolved is True
        assert result.confidence == pytest.approx(0.85)
        ev = result.evidence
        assert ev["supersession_entries_found"] == 6
        assert ev["vision_confidence"] == pytest.approx(0.92)
        assert ev["vision_model_used"] == "gpt-5.4-mini"

    @pytest.mark.asyncio
    async def test_skips_when_model_returns_uncertain(self) -> None:
        """No-false-positive guarantee: < 3 entries OR model conf < 0.75 → skip."""
        from aspire_orchestrator.services.blueprint.resolvers.addenda_resolver import (
            AddendaResolver,
        )
        resolver = AddendaResolver()
        cand = _candidate(
            category=MissingInputCategory.ADDENDA,
            description="Addendum hierarchy is incomplete",
            sheets=[{"id": "s-001", "sheet_code": "A-101", "sheet_title": "COVER"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        # Model says "uncertain" — empty supersessions + low confidence.
        fake_result = VisionCallResult(
            ok=True,
            json_payload={"supersessions": [], "confidence": 0.0, "notes": "uncertain"},
            input_tokens=800, output_tokens=50, model="gpt-5.4-mini",
            skip_reason="",
        )
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await resolver._method_vision_addendum_cover(cand)

        assert result.resolved is False
        assert result.confidence == 0.0
        assert result.evidence["skip_reason"] == "vision_could_not_extract_supersession"
        assert result.evidence["supersession_entries_found"] == 0

    @pytest.mark.asyncio
    async def test_skips_when_budget_exhausted(self) -> None:
        """If the budget reports cap hit, the vision helper returns ok=False."""
        from aspire_orchestrator.services.blueprint.resolvers.addenda_resolver import (
            AddendaResolver,
        )
        resolver = AddendaResolver()
        cand = _candidate(
            category=MissingInputCategory.ADDENDA,
            description="Addendum hierarchy is incomplete",
            sheets=[{"id": "s-001", "sheet_code": "A-101", "sheet_title": "COVER"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(return_value=VisionCallResult(
                ok=False, json_payload={},
                input_tokens=0, output_tokens=0, model="gpt-5.4-mini",
                skip_reason="vision_budget_exhausted",
            )),
        ):
            result = await resolver._method_vision_addendum_cover(cand)

        assert result.resolved is False
        assert result.evidence["skip_reason"] == "vision_budget_exhausted"


# ===========================================================================
# DetailResolver._method_vision_callout_detail
# ===========================================================================


class TestDetailVisionMethod:
    @pytest.mark.asyncio
    async def test_resolves_with_2_plus_callouts_having_concrete_facts(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.detail_resolver import (
            DetailResolver,
        )
        resolver = DetailResolver()
        cand = _candidate(
            category=MissingInputCategory.DETAIL,
            description=(
                "Door and partition details are referenced — see 1/A4 and 3/A4. "
                "Verify keyed detail references."
            ),
            sheets=[
                {"id": "s-A4-id", "sheet_code": "A4", "sheet_title": "DETAILS"},
                {"id": "s-A5-id", "sheet_code": "A5", "sheet_title": "DETAILS"},
            ],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        # Two concrete answers, both with confidence >= 0.75 and either
        # dimensions OR materials populated.
        responses = [
            VisionCallResult(
                ok=True,
                json_payload={
                    "detail_label": "INTERIOR PARTITION TYPE 1",
                    "dimensions": {"thickness": "5 7/8\""},
                    "materials_called_out": ["5/8\" GWB", "3-5/8\" METAL STUD"],
                    "confidence": 0.88,
                    "notes": "Type 1 wall on A4 detail 1",
                },
                input_tokens=1100, output_tokens=300, model="gpt-5.4-mini",
                skip_reason="",
            ),
            VisionCallResult(
                ok=True,
                json_payload={
                    "detail_label": "DOOR HEAD DETAIL",
                    "dimensions": {"head_height": "7'-0\""},
                    "materials_called_out": ["HM FRAME"],
                    "confidence": 0.82,
                    "notes": "Door head A4-3",
                },
                input_tokens=1050, output_tokens=280, model="gpt-5.4-mini",
                skip_reason="",
            ),
        ]

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(side_effect=responses),
        ):
            result = await resolver._method_vision_callout_detail(cand)

        assert result.method == "vision_callout_detail"
        assert result.resolved is True
        assert result.confidence == pytest.approx(0.85)
        assert result.evidence["callouts_vision_resolved"] == 2

    @pytest.mark.asyncio
    async def test_skips_when_callouts_return_not_found(self) -> None:
        """No-false-positive: model "not found" / vague responses leave row open."""
        from aspire_orchestrator.services.blueprint.resolvers.detail_resolver import (
            DetailResolver,
        )
        resolver = DetailResolver()
        cand = _candidate(
            category=MissingInputCategory.DETAIL,
            description="see 1/A4 and 3/A4",
            sheets=[{"id": "s-A4-id", "sheet_code": "A4", "sheet_title": "DETAILS"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        not_found = VisionCallResult(
            ok=True,
            json_payload={
                "detail_label": "not found",
                "dimensions": {},
                "materials_called_out": [],
                "confidence": 0.0,
                "notes": "Detail not visible on this sheet.",
            },
            input_tokens=900, output_tokens=60, model="gpt-5.4-mini",
            skip_reason="",
        )
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(return_value=not_found),
        ):
            result = await resolver._method_vision_callout_detail(cand)

        assert result.resolved is False
        assert result.evidence["callouts_vision_resolved"] == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_callouts_in_description(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.detail_resolver import (
            DetailResolver,
        )
        resolver = DetailResolver()
        cand = _candidate(
            category=MissingInputCategory.DETAIL,
            description="Generic detail-ish text with no callout markers at all",
            sheets=[{"id": "s-A4-id", "sheet_code": "A4", "sheet_title": "DETAILS"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        # No vision call should happen — assert via mock not awaited.
        mock_vision = AsyncMock()
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=mock_vision,
        ):
            result = await resolver._method_vision_callout_detail(cand)

        mock_vision.assert_not_awaited()
        assert result.resolved is False
        assert result.evidence["skip_reason"] == "no_callouts_in_description_or_ocr"

    @pytest.mark.asyncio
    async def test_callout_cap_bounds_cost_at_3_calls(self) -> None:
        """Even with 10 callouts in description, only 3 vision calls fire."""
        from aspire_orchestrator.services.blueprint.resolvers.detail_resolver import (
            DetailResolver,
        )
        resolver = DetailResolver()
        # 10 unique callouts
        description = " ".join([f"see {i}/A4" for i in range(1, 11)])
        cand = _candidate(
            category=MissingInputCategory.DETAIL,
            description=description,
            sheets=[{"id": "s-A4-id", "sheet_code": "A4", "sheet_title": "DETAILS"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )
        # Return "not found" so loop completes naturally without consensus.
        not_found = VisionCallResult(
            ok=True,
            json_payload={"detail_label": "not found", "dimensions": {},
                          "materials_called_out": [], "confidence": 0.0, "notes": ""},
            input_tokens=500, output_tokens=30, model="gpt-5.4-mini",
            skip_reason="",
        )
        mock_vision = AsyncMock(return_value=not_found)
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=mock_vision,
        ):
            await resolver._method_vision_callout_detail(cand)
        assert mock_vision.await_count == 3


# ===========================================================================
# GeneralVisionResolver._method_vision_unknown_fallback
# ===========================================================================


class TestGeneralVisionResolver:
    @pytest.mark.asyncio
    async def test_resolves_with_concrete_facts(self) -> None:
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        resolver = GeneralVisionResolver()
        cand = _candidate(
            category=MissingInputCategory.GENERAL_VISION,
            description=(
                "Fire protection plan: exact sprinkler head count and reroute "
                "extent are not fully legible. Sheet FP-101."
            ),
            sheets=[
                {"id": "s-A101-id", "sheet_code": "A-101", "sheet_title": "PLAN"},
                {"id": "s-FP101-id", "sheet_code": "FP-101", "sheet_title": "FIRE PROTECTION"},
            ],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        fake_result = VisionCallResult(
            ok=True,
            json_payload={
                "answer": "47 sprinkler heads total, branch line reroutes shown via Zone B",
                "extracted_facts": [
                    {"fact_type": "count", "value": "47 sprinkler heads"},
                    {"fact_type": "location", "value": "Zone B reroute"},
                ],
                "confidence": 0.88,
                "notes": "Head count visible on FP-101 schedule.",
            },
            input_tokens=1500, output_tokens=400, model="gpt-5.4-mini",
            skip_reason="",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(return_value=fake_result),
        ):
            results = await resolver.resolve(cand)

        assert len(results) == 1
        r = results[0]
        assert r.method == "vision_unknown_fallback"
        assert r.resolved is True
        assert r.confidence == pytest.approx(0.85)
        assert r.evidence["extracted_facts_count"] == 2

    @pytest.mark.asyncio
    async def test_skips_when_zero_facts_extracted(self) -> None:
        """Hallucination guard: high model confidence + zero facts → skip."""
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        resolver = GeneralVisionResolver()
        cand = _candidate(
            category=MissingInputCategory.GENERAL_VISION,
            description="sprinkler head count",
            sheets=[{"id": "s-1", "sheet_code": "FP-101", "sheet_title": "FP"}],
            provider=_provider_with_png(),
            budget=VisionBudget(cap_usd=1.00),
        )

        # Model returns HIGH confidence but ZERO facts — must still skip.
        hallucination = VisionCallResult(
            ok=True,
            json_payload={
                "answer": "",  # empty answer is the canonical "did not find"
                "extracted_facts": [],
                "confidence": 0.95,  # high — but no facts
                "notes": "no concrete data on this sheet",
            },
            input_tokens=800, output_tokens=50, model="gpt-5.4-mini",
            skip_reason="",
        )
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.vision_helper."
            "call_vision_with_budget",
            new=AsyncMock(return_value=hallucination),
        ):
            results = await resolver.resolve(cand)

        r = results[0]
        assert r.resolved is False
        assert r.evidence["skip_reason"] == "vision_could_not_extract_facts"
        assert r.evidence["extracted_facts_count"] == 0


# ===========================================================================
# Cascade routing integration
# ===========================================================================


class TestGeneralVisionRouting:
    def test_fire_protection_routes_to_general_vision(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            categorize_description,
        )
        desc = (
            "Fire protection plan: exact sprinkler head count and reroute "
            "extent are not fully legible in the provided OCR excerpt."
        )
        assert categorize_description(desc) == MissingInputCategory.GENERAL_VISION

    def test_fire_protection_schedule_still_routes_to_schedule(self) -> None:
        """The route list anchors schedule BEFORE general_vision so a
        fire-protection SCHEDULE still gets the structured Azure path."""
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            categorize_description,
        )
        desc = "Fire protection equipment schedule is partially legible."
        assert categorize_description(desc) == MissingInputCategory.SCHEDULE

    def test_life_safety_routes_to_general_vision(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            categorize_description,
        )
        assert categorize_description(
            "Life safety device placement is unclear"
        ) == MissingInputCategory.GENERAL_VISION

    def test_general_vision_resolver_registered(self) -> None:
        """Registry must include the new resolver for the GENERAL_VISION category."""
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _build_resolver_registry,
        )
        from aspire_orchestrator.services.blueprint.resolvers.general_vision_resolver import (
            GeneralVisionResolver,
        )
        registry = _build_resolver_registry()
        assert MissingInputCategory.GENERAL_VISION in registry
        assert isinstance(registry[MissingInputCategory.GENERAL_VISION], GeneralVisionResolver)


# ===========================================================================
# resolve_batch constructs ONE VisionBudget per batch
# ===========================================================================


class TestVisionBudgetIntegration:
    @pytest.mark.asyncio
    async def test_resolve_batch_constructs_one_vision_budget(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            resolve_batch,
        )
        from aspire_orchestrator.services.blueprint.resolvers.base import (
            ResolverResult,
        )

        rows = [
            {
                "id": f"aaaaaaaa-0000-0000-0000-{i:012d}",
                "project_id": _PROJECT, "suite_id": _SUITE,
                "office_id": None,
                "description": "Mechanical equipment schedules",
                "suggested_resolution": None,
            }
            for i in range(3)
        ]

        # Stub resolver so we don't run real vision calls. The coroutine
        # captures the vision_budget attached to each candidate so we can
        # assert exactly ONE budget instance was constructed for the batch.
        seen_budgets: list[Any] = []

        async def _capture(c: MissingInputCandidate) -> list[ResolverResult]:
            seen_budgets.append(c.vision_budget)
            return [ResolverResult(
                resolved=True, value="x", confidence=0.95, method="stub",
                evidence={}, category=MissingInputCategory.SCHEDULE,
            )]

        stub = MagicMock()
        stub.resolve = _capture

        with patch(
            "aspire_orchestrator.services.blueprint.vision_budget.VisionBudget"
        ) as BudgetMock, patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.SCHEDULE: stub},
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            BudgetMock.return_value.receipt_evidence.return_value = {
                "vision_tokens_used": 0,
                "vision_cost_usd_estimate": 0.0,
                "vision_calls_made": 0,
                "vision_calls_aborted_by_budget": 0,
            }
            outcome = await resolve_batch(
                rows, suite_id=_SUITE, project_id=_PROJECT,
                correlation_id="x",
            )

        # PROOF: exactly one VisionBudget per batch.
        assert BudgetMock.call_count == 1
        # Every candidate saw the SAME budget instance.
        assert all(b is BudgetMock.return_value for b in seen_budgets)
        # BatchOutcome carries the budget stats.
        assert "vision_tokens_used" in outcome.vision_budget_stats
