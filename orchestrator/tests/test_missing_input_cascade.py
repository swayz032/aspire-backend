"""Tests for Wave 19c missing_input_cascade orchestrator.

Pure unit tests — no Supabase, no LLM. All resolvers mocked.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.blueprint.missing_input_cascade import (
    ResolutionOutcome,
    categorize_description,
    build_candidate,
    resolve_or_emit,
)
from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCategory,
    MissingInputCandidate,
    ResolverResult,
    RESOLVED_CONFIDENCE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_candidate(
    category: MissingInputCategory = MissingInputCategory.DISCIPLINE,
    description: str = "Sheet 9 discipline cannot be confidently identified",
) -> MissingInputCandidate:
    return MissingInputCandidate(
        missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
        project_id="5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2",
        suite_id="94b89098-c4bf-4419-a154-e18d9d53f993",
        office_id=None,
        description=description,
        suggested_resolution=None,
        category=category,
        sheets=[],
        symbols_by_sheet={},
        ocr_by_sheet={},
    )


# ---------------------------------------------------------------------------
# Category routing tests
# ---------------------------------------------------------------------------

class TestCategorizeDescription:
    def test_discipline_keywords(self) -> None:
        assert categorize_description("Sheet 9 discipline cannot be identified") == MissingInputCategory.DISCIPLINE
        assert categorize_description("discipline ambiguity on sheets 20, 23") == MissingInputCategory.DISCIPLINE

    def test_scale_keywords(self) -> None:
        assert categorize_description("Most sheets show no calibrated scale") == MissingInputCategory.SCALE
        assert categorize_description("Sheet 19 scale calibration is not confirmed") == MissingInputCategory.SCALE

    def test_addenda_keywords(self) -> None:
        assert categorize_description("Addendum hierarchy is incomplete") == MissingInputCategory.ADDENDA
        assert categorize_description("revision hierarchy is not fully established") == MissingInputCategory.ADDENDA

    def test_dimension_keywords(self) -> None:
        assert categorize_description("room-by-room architectural dimensions are not provided") == MissingInputCategory.DIMENSION

    def test_detail_keywords(self) -> None:
        assert categorize_description("Door and partition detail references are incomplete") == MissingInputCategory.DETAIL

    def test_schedule_keywords(self) -> None:
        assert categorize_description("Electrical panel schedules are partially visible") == MissingInputCategory.SCHEDULE
        assert categorize_description("Mechanical equipment schedules are partially legible") == MissingInputCategory.SCHEDULE

    def test_unknown_returns_unknown(self) -> None:
        assert categorize_description("some random unclassified text xyz") == MissingInputCategory.UNKNOWN

    def test_fire_protection_not_matched_to_wrong_category(self) -> None:
        desc = "Fire protection plan: exact sprinkler head count"
        # No schedule keyword — should not hit SCHEDULE for "protection plan"
        result = categorize_description(desc)
        assert result != MissingInputCategory.SCHEDULE


# ---------------------------------------------------------------------------
# resolve_or_emit tests
# ---------------------------------------------------------------------------

class TestResolveOrEmit:
    """Tests for the cascade orchestrator entry point."""

    @pytest.mark.asyncio
    async def test_resolves_when_resolver_returns_confident_result(self) -> None:
        """If resolver returns a result with confidence >= 0.85, outcome.resolved=True."""
        candidate = _make_candidate()

        mock_result = ResolverResult(
            resolved=True,
            value="E",
            confidence=0.98,
            method="verifier_signals",
            evidence={"verdict": "promote"},
            category=MissingInputCategory.DISCIPLINE,
        )

        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=[mock_result])

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: mock_resolver},
        ):
            with patch(
                "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
            ):
                outcome = await resolve_or_emit(candidate, correlation_id="test-corr-id")

        assert isinstance(outcome, ResolutionOutcome)
        assert outcome.resolved is True
        assert outcome.best_result is not None
        assert outcome.best_result.confidence >= RESOLVED_CONFIDENCE_THRESHOLD
        assert outcome.missing_input_id == candidate.missing_input_id

    @pytest.mark.asyncio
    async def test_not_resolved_when_all_below_threshold(self) -> None:
        """If all resolver results < 0.85, outcome.resolved=False."""
        candidate = _make_candidate()

        mock_results = [
            ResolverResult(
                resolved=False,
                value=None,
                confidence=0.60,
                method="verifier_signals",
                evidence={},
                category=MissingInputCategory.DISCIPLINE,
            ),
            ResolverResult(
                resolved=False,
                value=None,
                confidence=0.55,
                method="vision_title_block",
                evidence={"skip_reason": "no_thumbnail_available"},
                category=MissingInputCategory.DISCIPLINE,
            ),
        ]

        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=mock_results)

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: mock_resolver},
        ):
            with patch(
                "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
            ):
                outcome = await resolve_or_emit(candidate, correlation_id="test-corr-id")

        assert outcome.resolved is False
        assert outcome.best_result is None

    @pytest.mark.asyncio
    async def test_unknown_category_returns_skipped_outcome(self) -> None:
        """Category with no registered resolver → resolved=False, no exception."""
        candidate = _make_candidate(category=MissingInputCategory.UNKNOWN)

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={},
        ):
            with patch(
                "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
            ):
                outcome = await resolve_or_emit(candidate, correlation_id="test-corr-id")

        assert outcome.resolved is False

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_resolution(self) -> None:
        """Receipt is emitted regardless of whether candidate is resolved."""
        candidate = _make_candidate()

        mock_result = ResolverResult(
            resolved=True,
            value="E",
            confidence=0.90,
            method="verifier_signals",
            evidence={},
            category=MissingInputCategory.DISCIPLINE,
        )
        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=[mock_result])

        receipt_calls = []

        def _capture_receipt(*a, **kw):
            receipt_calls.append((a, kw))

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: mock_resolver},
        ):
            with patch(
                "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt",
                side_effect=_capture_receipt,
            ):
                await resolve_or_emit(candidate, correlation_id="test-corr-id")

        assert len(receipt_calls) == 1

    @pytest.mark.asyncio
    async def test_receipt_emitted_on_failure(self) -> None:
        """Receipt is emitted even when resolver raises (not_resolved path)."""
        candidate = _make_candidate()

        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(side_effect=RuntimeError("resolver exploded"))

        receipt_calls = []

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: mock_resolver},
        ):
            with patch(
                "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt",
                side_effect=lambda *a, **kw: receipt_calls.append(1),
            ):
                outcome = await resolve_or_emit(candidate, correlation_id="x")

        assert outcome.resolved is False
        assert len(receipt_calls) == 1

    @pytest.mark.asyncio
    async def test_cascade_timeout_returns_unresolved(self) -> None:
        """90s cascade timeout → outcome.resolved=False, no exception propagated."""
        candidate = _make_candidate()

        async def _hang_forever(c):
            await asyncio.sleep(999)
            return []

        mock_resolver = MagicMock()
        mock_resolver.resolve = _hang_forever

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: mock_resolver},
        ):
            with patch(
                "aspire_orchestrator.services.blueprint.missing_input_cascade._CASCADE_TIMEOUT_S",
                0.05,
            ):
                with patch(
                    "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
                ):
                    outcome = await resolve_or_emit(candidate, correlation_id="x")

        assert outcome.resolved is False

    @pytest.mark.asyncio
    async def test_no_pii_in_receipt_metadata(self) -> None:
        """Receipt metadata must not contain raw description text."""
        candidate = _make_candidate(
            description="SSN: 123-45-6789 in sheet 9 discipline is unknown"
        )

        mock_result = ResolverResult(
            resolved=False,
            value=None,
            confidence=0.0,
            method="verifier_signals",
            evidence={},
            category=MissingInputCategory.DISCIPLINE,
        )
        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=[mock_result])

        captured_receipts = []

        def _capture(receipts):
            captured_receipts.extend(receipts)

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: mock_resolver},
        ):
            # store_receipts is imported inside _emit_receipt; patch the receipt_store module
            with patch(
                "aspire_orchestrator.services.receipt_store.store_receipts",
                side_effect=_capture,
            ):
                await resolve_or_emit(candidate, correlation_id="x")

        for receipt in captured_receipts:
            import json
            receipt_str = json.dumps(receipt)
            # Raw SSN must not appear in receipt
            assert "123-45-6789" not in receipt_str


# ---------------------------------------------------------------------------
# build_candidate tests
# ---------------------------------------------------------------------------

class TestBuildCandidate:
    def test_categorizes_discipline_description(self) -> None:
        row = {
            "id": "mi-001",
            "project_id": "proj-001",
            "suite_id": "suite-001",
            "office_id": None,
            "description": "Sheet 9 discipline cannot be confidently identified",
            "suggested_resolution": None,
        }
        candidate = build_candidate(row, sheets=[], symbols_by_sheet={}, ocr_by_sheet={})
        assert candidate.category == MissingInputCategory.DISCIPLINE
        assert candidate.missing_input_id == "mi-001"

    def test_categorizes_scale_description(self) -> None:
        row = {
            "id": "mi-002",
            "project_id": "p",
            "suite_id": "s",
            "office_id": None,
            "description": "Most sheets show no calibrated scale",
            "suggested_resolution": None,
        }
        candidate = build_candidate(row, sheets=[], symbols_by_sheet={}, ocr_by_sheet={})
        assert candidate.category == MissingInputCategory.SCALE

    def test_sheets_context_passed_through(self) -> None:
        row = {
            "id": "mi-003",
            "project_id": "p",
            "suite_id": "s",
            "office_id": None,
            "description": "discipline issue",
            "suggested_resolution": None,
        }
        sheets = [{"id": "s1", "discipline": "A"}]
        candidate = build_candidate(row, sheets=sheets, symbols_by_sheet={}, ocr_by_sheet={})
        assert candidate.sheets == sheets


# ---------------------------------------------------------------------------
# Wave 19.1c (Commit 2.6) — Receipt persistence end-to-end through the real
# receipt_store mapper. Regresses the bug where cascade wrote ``metadata``
# but the mapper harvested ``redacted_outputs``, producing 24+ empty
# ``result {}`` columns on Gulfstream cascade receipts.
# ---------------------------------------------------------------------------


class TestReceiptPersistenceEndToEnd:
    """Prove cascade evidence actually lands in receipts.result, not just receipts.metadata."""

    @pytest.mark.asyncio
    async def test_cascade_result_field_persists_through_mapper(self) -> None:
        """End-to-end: cascade emits receipt → real mapper packs result → assertions."""
        from aspire_orchestrator.services.receipt_store import _map_receipt_to_row

        candidate = _make_candidate(category=MissingInputCategory.SCHEDULE)

        # Stub resolver that returns evidence with allow-listed keys
        # (azure_calls_made, png_source) — exactly the data Bug 1 was hiding.
        mock_result = ResolverResult(
            resolved=True,
            value=[{"row_count": 7}],
            confidence=0.90,
            method="azure_premium_ocr",
            evidence={
                "azure_calls_made": 3,
                "png_source": "provider",
                "sheets_processed": 3,
                "ocr_tier": "base",
                "total_rows_extracted": 21,
                # This key is NOT on the allow-list and must be stripped.
                "raw_payload_bytes": b"\x00" * 4096,
            },
            category=MissingInputCategory.SCHEDULE,
        )
        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(return_value=[mock_result])

        # Capture what the cascade hands to store_receipts (the raw receipt dict).
        captured_receipts: list[dict[str, Any]] = []

        def _capture(receipts: list[dict[str, Any]]) -> None:
            captured_receipts.extend(receipts)

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.SCHEDULE: mock_resolver},
        ), patch(
            "aspire_orchestrator.services.receipt_store.store_receipts",
            side_effect=_capture,
        ):
            await resolve_or_emit(candidate, correlation_id="test-corr-id")

        # The cascade emitted exactly one receipt dict.
        assert len(captured_receipts) == 1
        raw_receipt = captured_receipts[0]

        # Both keys are present on the receipt dict.
        assert "redacted_outputs" in raw_receipt, \
            "cascade must write to redacted_outputs so the mapper picks it up"
        assert "metadata" in raw_receipt, \
            "cascade also writes metadata for in-process back-compat consumers"

        # ---- THE KEY ASSERTION ----
        # Push the receipt through the real mapper and confirm the result
        # column actually carries the cascade payload. This is exactly what
        # was failing in production: empty ``result {}`` because metadata
        # was not on the harvest list.
        #
        # Mapper contract (verified at receipt_store.py:178-183):
        #   result_data["redacted_outputs"] = receipt["redacted_outputs"]
        # The mapper preserves the field name as a top-level key inside
        # result_data — it does NOT unwrap. So the cascade payload is
        # nested under ``result["redacted_outputs"]``, not promoted to
        # the top level of ``result``.
        row = _map_receipt_to_row(raw_receipt)
        result_jsonb = row["result"]
        assert result_jsonb != {}, (
            "REGRESSION: receipts.result is empty after mapping. This is the "
            "exact Wave 19 production bug. The cascade must write to a key "
            "the mapper harvests (currently: redacted_outputs)."
        )
        assert "redacted_outputs" in result_jsonb, (
            "Mapper failed to harvest redacted_outputs — cascade fix did not "
            "take effect, or the mapper convention has changed."
        )
        cascade_payload = result_jsonb["redacted_outputs"]
        assert cascade_payload["category"] == "schedule"
        assert cascade_payload["resolved"] is True
        assert cascade_payload["best_method"] == "azure_premium_ocr"
        assert cascade_payload["best_confidence"] == 0.9

        # method_summary carries the allow-listed evidence — including
        # azure_calls_made and png_source, the two fields we were flying
        # blind without.
        ms = cascade_payload["method_summary"]
        assert "azure_premium_ocr" in ms
        ev = ms["azure_premium_ocr"]["evidence"]
        assert ev["azure_calls_made"] == 3
        assert ev["png_source"] == "provider"
        assert ev["sheets_processed"] == 3
        assert ev["ocr_tier"] == "base"
        assert ev["total_rows_extracted"] == 21
        # Disallowed key was stripped by the allow-list.
        assert "raw_payload_bytes" not in ev

    def test_cascade_receipt_dict_includes_both_metadata_and_redacted_outputs(self) -> None:
        """In-process back-compat: receipt dict carries both keys with identical payloads."""
        # This test only exercises the dict shape; doesn't run a cascade.
        # The cascade source must write both keys with the same payload so
        # any consumer reading the in-process dict (instead of the
        # persisted row) still sees data.
        import inspect
        from aspire_orchestrator.services.blueprint import missing_input_cascade as mod
        src = inspect.getsource(mod._emit_receipt)
        assert '"redacted_outputs": cascade_result' in src
        assert '"metadata": cascade_result' in src
