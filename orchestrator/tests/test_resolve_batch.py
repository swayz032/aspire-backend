"""Tests for Wave 19.1c (Commit 2.5) — ``resolve_batch`` and provider auto-inject.

The Wave 19.1b Gulfstream backfill silently degraded every resolver to
no-PNG mode because the standalone backfill script invoked
``resolve_or_emit`` directly with candidates whose ``data_provider`` was
None. Commit 2.5 introduces:

  1. ``resolve_batch`` — the preferred batch entry point. Owns
     ``SheetDataProvider`` construction so callers cannot forget it. Shares
     ONE provider across every candidate in the batch.
  2. Auto-inject inside ``resolve_or_emit`` — a defense-in-depth fallback
     for callers that still call the per-candidate entry point directly
     with no provider. Logs a warning and builds a per-call provider so
     resolvers see real data instead of silently degrading.

These tests prove both layers actually do what they claim.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.blueprint.missing_input_cascade import (
    resolve_batch,
    resolve_or_emit,
    BatchOutcome,
)
from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
    ResolverResult,
)


_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2"
_CORR = "test-corr-batch"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row(idx: int, *, description: str = "Mechanical equipment schedules") -> dict[str, Any]:
    return {
        "id": f"aaaaaaaa-0000-0000-0000-{idx:012d}",
        "project_id": _PROJECT,
        "suite_id": _SUITE,
        "office_id": None,
        "description": description,
        "suggested_resolution": None,
    }


# A stub resolver that returns a single confident ResolverResult. We attach it
# via _get_registry patch so we don't need to actually run any resolver.
def _confident_result(category: MissingInputCategory) -> ResolverResult:
    return ResolverResult(
        resolved=True,
        value="X",
        confidence=0.95,
        method="stub",
        evidence={"azure_calls_made": 0},
        category=category,
    )


def _make_stub_resolver(category: MissingInputCategory) -> Any:
    res = MagicMock()
    res.resolve = AsyncMock(return_value=[_confident_result(category)])
    return res


# ---------------------------------------------------------------------------
# resolve_batch — construction enforcement
# ---------------------------------------------------------------------------


class TestResolveBatchConstruction:
    @pytest.mark.asyncio
    async def test_constructs_exactly_one_provider_for_the_batch(self) -> None:
        """The core breakthrough lock: ONE provider per batch, not one per candidate."""
        rows = [_row(i) for i in range(5)]
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider"
        ) as ProviderMock, patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={
                MissingInputCategory.SCHEDULE: _make_stub_resolver(
                    MissingInputCategory.SCHEDULE
                ),
            },
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            ProviderMock.return_value.cache_stats.return_value = {
                "png_cache_entries": 3,
                "png_cache_hits_with_bytes": 3,
                "yolo_cache_entries": 0,
                "yolo_detections_total": 0,
            }
            outcome = await resolve_batch(
                rows,
                suite_id=_SUITE,
                project_id=_PROJECT,
                correlation_id=_CORR,
            )

        # PROOF: SheetDataProvider was constructed exactly once.
        assert ProviderMock.call_count == 1, (
            f"expected ONE provider per batch, got {ProviderMock.call_count}"
        )
        # Construction args carry the batch's scoping.
        ctor_kwargs = ProviderMock.call_args.kwargs
        assert ctor_kwargs["suite_id"] == _SUITE
        assert ctor_kwargs["project_id"] == _PROJECT
        assert ctor_kwargs["correlation_id"] == _CORR

        # BatchOutcome shape sanity.
        assert isinstance(outcome, BatchOutcome)
        assert outcome.candidates_count == 5
        assert outcome.resolved_count == 5
        assert outcome.provider_cache_stats["png_cache_entries"] == 3

    @pytest.mark.asyncio
    async def test_provider_attached_to_every_candidate(self) -> None:
        """The single provider instance must reach every candidate's data_provider field."""
        rows = [_row(i) for i in range(3)]
        seen_providers: list[Any] = []

        async def _capturing_resolve(c: MissingInputCandidate) -> list[ResolverResult]:
            seen_providers.append(c.data_provider)
            return [_confident_result(c.category)]

        stub = MagicMock()
        stub.resolve = _capturing_resolve

        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.SCHEDULE: stub},
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            await resolve_batch(
                rows,
                suite_id=_SUITE, project_id=_PROJECT, correlation_id=_CORR,
            )

        assert len(seen_providers) == 3
        # All three candidates see the SAME provider instance — proves
        # caches are actually shared across the batch.
        assert seen_providers[0] is seen_providers[1] is seen_providers[2]
        assert seen_providers[0] is not None

    @pytest.mark.asyncio
    async def test_drops_cross_tenant_rows_fail_closed(self) -> None:
        """A row whose suite_id disagrees with the batch must be excluded (Law #6)."""
        rows = [
            _row(0),
            {**_row(1), "suite_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"},  # mismatch
            _row(2),
        ]
        with patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={
                MissingInputCategory.SCHEDULE: _make_stub_resolver(
                    MissingInputCategory.SCHEDULE
                ),
            },
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            outcome = await resolve_batch(
                rows,
                suite_id=_SUITE, project_id=_PROJECT, correlation_id=_CORR,
            )
        # The two matching rows survive; the cross-tenant one is dropped.
        assert outcome.candidates_count == 2
        assert outcome.resolved_count == 2

    @pytest.mark.asyncio
    async def test_provider_construction_failure_falls_back_gracefully(self) -> None:
        """If SheetDataProvider raises, resolvers degrade — cascade does not crash."""
        rows = [_row(0)]
        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider",
            side_effect=RuntimeError("provider boom"),
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={
                MissingInputCategory.SCHEDULE: _make_stub_resolver(
                    MissingInputCategory.SCHEDULE
                ),
            },
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            outcome = await resolve_batch(
                rows,
                suite_id=_SUITE, project_id=_PROJECT, correlation_id=_CORR,
            )
        # Stub resolver still resolves — provider absence doesn't crash the batch.
        assert outcome.candidates_count == 1
        assert outcome.resolved_count == 1
        # Cache stats empty because provider didn't construct.
        assert outcome.provider_cache_stats == {}

    @pytest.mark.asyncio
    async def test_empty_rows_short_circuits(self) -> None:
        outcome = await resolve_batch(
            [], suite_id=_SUITE, project_id=_PROJECT, correlation_id=_CORR,
        )
        assert outcome.candidates_count == 0
        assert outcome.resolved_count == 0

    def test_requires_suite_id(self) -> None:
        import asyncio
        with pytest.raises(ValueError, match="suite_id"):
            asyncio.run(resolve_batch(
                [_row(0)], suite_id="", project_id=_PROJECT, correlation_id=_CORR,
            ))

    def test_requires_project_id(self) -> None:
        import asyncio
        with pytest.raises(ValueError, match="project_id"):
            asyncio.run(resolve_batch(
                [_row(0)], suite_id=_SUITE, project_id="", correlation_id=_CORR,
            ))


# ---------------------------------------------------------------------------
# resolve_or_emit — defense-in-depth provider auto-inject
# ---------------------------------------------------------------------------


class TestAutoInjectProvider:
    @pytest.mark.asyncio
    async def test_auto_injects_when_candidate_has_no_provider(self) -> None:
        """A naked candidate with real suite/project gets a per-call provider auto-built."""
        # Bare candidate (no data_provider attached) — simulates a future
        # caller that uses resolve_or_emit directly.
        candidate = MissingInputCandidate(
            missing_input_id="mi-x",
            project_id=_PROJECT,
            suite_id=_SUITE,
            office_id=None,
            description="Mechanical equipment schedules are partially legible",
            suggested_resolution=None,
            category=MissingInputCategory.SCHEDULE,
        )
        assert candidate.data_provider is None

        seen_provider: list[Any] = []

        async def _capturing_resolve(c: MissingInputCandidate) -> list[ResolverResult]:
            seen_provider.append(c.data_provider)
            return [_confident_result(c.category)]

        stub = MagicMock()
        stub.resolve = _capturing_resolve

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider"
        ) as ProviderMock, patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.SCHEDULE: stub},
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            await resolve_or_emit(candidate, correlation_id=_CORR)

        # PROOF: a provider was auto-constructed for this single candidate.
        assert ProviderMock.call_count == 1
        ctor_kwargs = ProviderMock.call_args.kwargs
        assert ctor_kwargs["suite_id"] == _SUITE
        assert ctor_kwargs["project_id"] == _PROJECT
        # Resolver saw a non-None provider (the auto-injected one).
        assert seen_provider == [ProviderMock.return_value]

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_provider(self) -> None:
        """If candidate already has a provider, auto-inject must NOT replace it."""
        custom_provider = MagicMock(name="caller-provided-provider")
        candidate = MissingInputCandidate(
            missing_input_id="mi-y",
            project_id=_PROJECT, suite_id=_SUITE, office_id=None,
            description="discipline ambiguity", suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
            data_provider=custom_provider,
        )

        seen: list[Any] = []

        async def _capturing_resolve(c: MissingInputCandidate) -> list[ResolverResult]:
            seen.append(c.data_provider)
            return [_confident_result(c.category)]

        stub = MagicMock()
        stub.resolve = _capturing_resolve

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider"
        ) as ProviderMock, patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: stub},
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            await resolve_or_emit(candidate, correlation_id=_CORR)

        # The caller's provider survived; no auto-inject happened.
        assert ProviderMock.call_count == 0
        assert seen == [custom_provider]

    @pytest.mark.asyncio
    async def test_skips_auto_inject_when_suite_or_project_missing(self) -> None:
        """Unit-test fixtures with empty suite_id/project_id (no real scoping) — no auto-inject.

        Without real scoping data we cannot build a Storage-path-safe provider
        (Law #6 — suite_id-prefixed paths), so auto-inject must skip.
        """
        candidate = MissingInputCandidate(
            missing_input_id="mi-z",
            project_id="",  # blank
            suite_id="",
            office_id=None,
            description="discipline", suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
        )
        stub = _make_stub_resolver(MissingInputCategory.DISCIPLINE)

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider"
        ) as ProviderMock, patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: stub},
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            await resolve_or_emit(candidate, correlation_id=_CORR)

        assert ProviderMock.call_count == 0, "auto-inject must not fire without real scoping"

    @pytest.mark.asyncio
    async def test_auto_inject_failure_does_not_crash_cascade(self) -> None:
        """If SheetDataProvider raises during auto-inject, the cascade still returns."""
        candidate = MissingInputCandidate(
            missing_input_id="mi-q",
            project_id=_PROJECT, suite_id=_SUITE, office_id=None,
            description="discipline", suggested_resolution=None,
            category=MissingInputCategory.DISCIPLINE,
        )
        stub = _make_stub_resolver(MissingInputCategory.DISCIPLINE)

        with patch(
            "aspire_orchestrator.services.blueprint.resolvers.sheet_data_provider.SheetDataProvider",
            side_effect=RuntimeError("boom"),
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._get_registry",
            return_value={MissingInputCategory.DISCIPLINE: stub},
        ), patch(
            "aspire_orchestrator.services.blueprint.missing_input_cascade._emit_receipt"
        ):
            outcome = await resolve_or_emit(candidate, correlation_id=_CORR)

        # Cascade still completed — stub resolver still ran.
        assert outcome.resolved is True
