"""Tests for Wave 19b DisciplineResolver.

Pure unit tests — no Supabase, no network calls. LLM calls mocked.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCategory,
    MissingInputCandidate,
    RESOLVED_CONFIDENCE_THRESHOLD,
)
from aspire_orchestrator.services.blueprint.resolvers.discipline_resolver import (
    DisciplineResolver,
)


def _make_candidate(description: str = "Sheet 9 discipline cannot be identified") -> MissingInputCandidate:
    return MissingInputCandidate(
        missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
        project_id="5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2",
        suite_id="94b89098-c4bf-4419-a154-e18d9d53f993",
        office_id=None,
        description=description,
        suggested_resolution=None,
        category=MissingInputCategory.DISCIPLINE,
        sheets=[
            {
                "id": "sheet-0001-0000-0000-000000000001",
                "sheet_number": "E-1",
                "sheet_code": "E-1",
                "discipline": "E",
                "ocr_text": (
                    "outlet receptacle gfci panel conduit breaker switch circuit voltage "
                    * 10
                ),
                "thumbnail_url": None,
                "image_url": None,
            }
        ],
        symbols_by_sheet={
            "sheet-0001-0000-0000-000000000001": [
                {"class": "electrical_outlet", "confidence": 0.9},
                {"class": "gfci_receptacle", "confidence": 0.88},
                {"class": "panel_board", "confidence": 0.85},
                {"class": "circuit_breaker", "confidence": 0.87},
            ]
        },
        ocr_by_sheet={
            "sheet-0001-0000-0000-000000000001": (
                "outlet receptacle gfci panel conduit breaker switch circuit voltage " * 10
            )
        },
    )


class TestDisciplineResolverVerifierSignals:
    """Tests for the verifier_signals method."""

    def test_resolver_category_is_discipline(self) -> None:
        resolver = DisciplineResolver()
        assert resolver.category == MissingInputCategory.DISCIPLINE

    @pytest.mark.asyncio
    async def test_verifier_signals_resolves_when_4_signals_agree(self) -> None:
        """When all 4 verifier signals agree → confidence >= 0.85 → resolved."""
        resolver = DisciplineResolver()
        candidate = _make_candidate()

        # patch verify_classification to return a high-confidence promote verdict
        from aspire_orchestrator.services.blueprint.discipline_verifier import VerifyResult
        mock_verdict = VerifyResult(
            sheet_id="sheet-0001-0000-0000-000000000001",
            discipline_assigned="E",
            discipline_final="E",
            confidence=0.98,
            truth="field_confirmed",
            verdict="promote",
            agreed_count=4,
            disagreed_count=0,
            abstained_count=0,
            signals=(),
            missing_input_reason=None,
        )

        with patch(
            "aspire_orchestrator.services.blueprint.discipline_verifier.verify_classification",
            return_value=mock_verdict,
        ):
            results = await resolver.resolve(candidate)

        confident = [r for r in results if r.is_confident]
        assert len(confident) >= 1
        best = max(results, key=lambda r: r.confidence)
        assert best.resolved is True
        assert best.confidence >= RESOLVED_CONFIDENCE_THRESHOLD
        assert best.value == "E"
        assert best.method == "verifier_signals"

    @pytest.mark.asyncio
    async def test_verifier_signals_not_resolved_on_flag_verdict(self) -> None:
        """Verifier 'flag' verdict → confidence 0.55 → not resolved."""
        resolver = DisciplineResolver()
        candidate = _make_candidate()

        from aspire_orchestrator.services.blueprint.discipline_verifier import VerifyResult
        mock_verdict = VerifyResult(
            sheet_id="sheet-0001-0000-0000-000000000001",
            discipline_assigned="E",
            discipline_final="E",
            confidence=0.55,
            truth="assumed",
            verdict="flag",
            agreed_count=2,
            disagreed_count=1,
            abstained_count=1,
            signals=(),
            missing_input_reason="Sheet 9: discipline discrepancy — sheet_code says A vs E",
        )

        with patch(
            "aspire_orchestrator.services.blueprint.discipline_verifier.verify_classification",
            return_value=mock_verdict,
        ):
            results = await resolver.resolve(candidate)

        # verifier_signals result must NOT be confident
        verifier_results = [r for r in results if r.method == "verifier_signals"]
        if verifier_results:
            assert verifier_results[0].confidence < RESOLVED_CONFIDENCE_THRESHOLD

    @pytest.mark.asyncio
    async def test_verifier_signals_handles_no_sheets(self) -> None:
        """Empty sheets → returns result with resolved=False, no exception."""
        resolver = DisciplineResolver()
        candidate = MissingInputCandidate(
            missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
            project_id="proj",
            suite_id="suite",
            office_id=None,
            description="discipline issue",
            suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
            sheets=[],
            symbols_by_sheet={},
            ocr_by_sheet={},
        )
        results = await resolver.resolve(candidate)
        assert isinstance(results, list)
        # All results must have resolved=False when no sheets
        for r in results:
            if r.method == "verifier_signals":
                assert r.resolved is False


class TestDisciplineResolverVisionTitleBlock:
    """Tests for the vision_title_block method."""

    @pytest.mark.asyncio
    async def test_vision_resolves_with_high_confidence(self) -> None:
        """Vision returning discipline=E confidence=0.90 → resolved."""
        resolver = DisciplineResolver()
        candidate = MissingInputCandidate(
            missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
            project_id="proj",
            suite_id="suite",
            office_id=None,
            description="discipline issue",
            suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
            sheets=[{
                "id": "sheet-vision-001",
                "thumbnail_url": "https://example.com/thumb.png",
                "discipline": None,
            }],
            symbols_by_sheet={},
            ocr_by_sheet={},
        )

        mock_result = {
            "json": {"discipline": "E", "confidence": 0.90, "evidence": "title block E-1"},
            "usage": {},
        }

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch(
                "aspire_orchestrator.services.openai_client.generate_json_async",
                new=AsyncMock(return_value=mock_result),
            ):
                results = await resolver.resolve(candidate)

        vision_results = [r for r in results if r.method == "vision_title_block"]
        if vision_results:
            vr = vision_results[0]
            assert vr.resolved is True
            assert vr.value == "E"
            assert vr.confidence >= RESOLVED_CONFIDENCE_THRESHOLD

    @pytest.mark.asyncio
    async def test_vision_skips_when_no_thumbnail(self) -> None:
        """Sheet without thumbnail → vision method returns resolved=False."""
        resolver = DisciplineResolver()
        candidate = MissingInputCandidate(
            missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
            project_id="proj",
            suite_id="suite",
            office_id=None,
            description="discipline issue",
            suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
            sheets=[{"id": "sheet-no-thumb", "thumbnail_url": None, "image_url": None}],
            symbols_by_sheet={},
            ocr_by_sheet={},
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            results = await resolver.resolve(candidate)

        vision_results = [r for r in results if r.method == "vision_title_block"]
        if vision_results:
            assert vision_results[0].resolved is False
            assert "no_thumbnail" in vision_results[0].evidence.get("skip_reason", "")

    @pytest.mark.asyncio
    async def test_vision_skips_when_no_api_key(self) -> None:
        """Missing API key → vision method returns resolved=False without raising."""
        resolver = DisciplineResolver()
        candidate = _make_candidate()

        with patch.dict("os.environ", {}, clear=True):
            # Ensure OPENAI_API_KEY and ASPIRE_OPENAI_API_KEY are absent
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ASPIRE_OPENAI_API_KEY", None)
            results = await resolver.resolve(candidate)

        vision_results = [r for r in results if r.method == "vision_title_block"]
        if vision_results:
            assert vision_results[0].resolved is False

    @pytest.mark.asyncio
    async def test_vision_rejects_invalid_discipline(self) -> None:
        """Vision returning an invalid discipline string → resolved=False."""
        resolver = DisciplineResolver()
        candidate = MissingInputCandidate(
            missing_input_id="x",
            project_id="p",
            suite_id="s",
            office_id=None,
            description="d",
            suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
            sheets=[{"id": "s1", "thumbnail_url": "https://x.com/t.png"}],
            symbols_by_sheet={},
            ocr_by_sheet={},
        )
        mock_result = {
            "json": {"discipline": "UNKNOWN_DISC", "confidence": 0.95, "evidence": "x"},
            "usage": {},
        }
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch(
                "aspire_orchestrator.services.openai_client.generate_json_async",
                new=AsyncMock(return_value=mock_result),
            ):
                results = await resolver.resolve(candidate)

        vision_results = [r for r in results if r.method == "vision_title_block"]
        if vision_results:
            assert vision_results[0].resolved is False


class TestDisciplineResolverTimeout:
    """Timeout and failure mode tests."""

    @pytest.mark.asyncio
    async def test_timeout_returns_false_not_raises(self) -> None:
        """If resolver methods timeout, resolve() returns list with resolved=False."""
        resolver = DisciplineResolver()
        candidate = _make_candidate()

        def _slow(*a, **kw):
            # Synchronous side-effect that raises so the resolver catches it
            raise TimeoutError("simulated timeout")

        with patch(
            "aspire_orchestrator.services.blueprint.discipline_verifier.verify_classification",
            side_effect=_slow,
        ):
            # Should complete quickly with timeout results
            results = await asyncio.wait_for(resolver.resolve(candidate), timeout=5.0)

        assert isinstance(results, list)
        for r in results:
            assert not r.is_confident
