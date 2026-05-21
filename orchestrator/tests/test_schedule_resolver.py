"""Tests for Wave 19.1b ScheduleResolver — provider-wired re-OCR.

Asserts:
  * Skip path when Azure credentials are absent (Law #3 fail-closed).
  * Skip path when no schedule-ish sheets are present.
  * PNG comes from ``candidate.data_provider`` when provider is set —
    legacy ``sheet["image_bytes"]`` is NOT consulted in that case.
  * Legacy ``sheet["image_bytes"]`` fallback fires when no provider is
    attached (pre-19.1 back-compat path).
  * Per-cascade Azure call cap honored at 8 even when 20 schedule sheets
    are eligible.
  * ResolverResult.evidence carries ``azure_calls_made`` and
    ``png_source`` so the cascade receipt can surface them.
  * Tabular fallback method (``table_structure_parse``) is independent
    of the provider and still works.

No real Azure or Supabase calls — all transport patched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aspire_orchestrator.services.blueprint.resolvers.base import (
    MissingInputCandidate,
    MissingInputCategory,
)
from aspire_orchestrator.services.blueprint.resolvers.schedule_resolver import (
    ScheduleResolver,
    _AZURE_SHEETS_PER_CASCADE_CAP,
)


_SUITE = "94b89098-c4bf-4419-a154-e18d9d53f993"
_PROJECT = "5b0adf45-f3d3-45f6-9ac7-50eeba1b03c2"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _schedule_sheet(sheet_id: str, *, image_bytes: bytes | None = None, ocr: str = "") -> dict[str, Any]:
    """Construct a blueprint_sheets-shaped dict that matches schedule regex."""
    row: dict[str, Any] = {
        "id": sheet_id,
        # The regex matches on title OR ocr_text[:200]. We use ocr_text so we
        # don't have to also rebuild a real sheet_title.
        "ocr_text": "MECHANICAL EQUIPMENT SCHEDULE\nRTU-1  Daikin  5T\n" + ocr,
        "sheet_title": None,
    }
    if image_bytes is not None:
        row["image_bytes"] = image_bytes
    return row


def _candidate(sheets: list[dict[str, Any]], provider: Any | None = None) -> MissingInputCandidate:
    return MissingInputCandidate(
        missing_input_id="aaaaaaaa-0000-0000-0000-000000000001",
        project_id=_PROJECT,
        suite_id=_SUITE,
        office_id=None,
        description="Mechanical equipment schedules are partially legible",
        suggested_resolution=None,
        category=MissingInputCategory.SCHEDULE,
        sheets=sheets,
        symbols_by_sheet={},
        ocr_by_sheet={},
        data_provider=provider,
    )


@dataclass
class _FakeAzureResponse:
    """Shape-compatible stub for ProviderResponse — only the fields the resolver reads."""
    status_code: int = 200
    body: dict[str, Any] | None = None
    success: bool = True
    error_code: Any = None


def _azure_tables_body(*, row_counts: list[int]) -> dict[str, Any]:
    """Build a fake Azure ``analyzeResult`` containing one table per row_count."""
    tables: list[dict[str, Any]] = []
    for rc in row_counts:
        tables.append({
            "rowCount": rc,
            "columnCount": 4,
            "cells": [{"rowIndex": i, "columnIndex": j} for i in range(rc) for j in range(4)],
        })
    return {"analyzeResult": {"tables": tables}}


# A fake AzureDocIntelClient with a working ``analyze_layout`` method.
# We attach this via patching ``get_azure_doc_intel_client``.
def _fake_client(response: _FakeAzureResponse) -> Any:
    client = MagicMock()
    client.analyze_layout = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# Credential / sheet-discovery skips
# ---------------------------------------------------------------------------


class TestAzureSkipPaths:
    @pytest.mark.asyncio
    async def test_skips_when_azure_creds_missing(self) -> None:
        resolver = ScheduleResolver()
        cand = _candidate(sheets=[_schedule_sheet("s1", image_bytes=b"PNG")])

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "",
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        assert result.resolved is False
        assert result.confidence == 0.0
        assert result.evidence["skip_reason"] == "azure_creds_missing"
        assert result.evidence["azure_calls_made"] == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_schedule_sheets(self) -> None:
        resolver = ScheduleResolver()
        # OCR must contain none of the regex anchors
        # (schedule|legend|equipment list|fixture schedule). Earlier draft
        # of this fixture used "no schedule keywords here at all" — which
        # accidentally MATCHED the regex via "\bschedule\b", letting the
        # sheet pass the category filter and fall through to the "no PNG"
        # skip. Caught by the 66/67 pytest run.
        cand = _candidate(sheets=[{
            "id": "s1",
            "ocr_text": "FLOOR PLAN — architectural layout only, room dimensions, wall heights.",
            "sheet_title": None,
        }])

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        assert result.resolved is False
        assert result.evidence["skip_reason"] == "no_schedule_sheets_matched"
        assert result.evidence["schedule_sheets_found"] == 0
        assert result.evidence["azure_calls_made"] == 0


# ---------------------------------------------------------------------------
# Provider-wired PNG fetch (the Wave 19.1b behavior change)
# ---------------------------------------------------------------------------


class TestProviderPngWired:
    @pytest.mark.asyncio
    async def test_png_pulled_from_provider_when_present(self) -> None:
        """When candidate.data_provider is set, resolver fetches PNG from provider."""
        resolver = ScheduleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(return_value=b"\x89PNG_FROM_STORAGE")

        cand = _candidate(
            sheets=[_schedule_sheet("sheet-001")],  # no legacy image_bytes
            provider=provider,
        )

        fake = _fake_client(_FakeAzureResponse(body=_azure_tables_body(row_counts=[5])))

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ), patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake,
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        provider.get_sheet_png.assert_awaited_once_with("sheet-001")
        # Azure analyze_layout received the PNG from the provider, not legacy.
        kwargs = fake.analyze_layout.await_args.kwargs
        assert kwargs["pdf_bytes"] == b"\x89PNG_FROM_STORAGE"
        assert kwargs["suite_id"] == _SUITE
        assert result.evidence["png_source"] == "provider"
        assert result.evidence["azure_calls_made"] == 1
        # 5 rows ≥ 3 → confidence 0.90, resolves.
        assert result.confidence == pytest.approx(0.90)
        assert result.resolved is True

    @pytest.mark.asyncio
    async def test_provider_failure_falls_back_to_legacy_field(self) -> None:
        """If provider.get_sheet_png raises, we still try legacy image_bytes."""
        resolver = ScheduleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(side_effect=RuntimeError("storage offline"))

        cand = _candidate(
            sheets=[_schedule_sheet("sheet-001", image_bytes=b"LEGACY_PNG")],
            provider=provider,
        )

        fake = _fake_client(_FakeAzureResponse(body=_azure_tables_body(row_counts=[4])))

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ), patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake,
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        kwargs = fake.analyze_layout.await_args.kwargs
        assert kwargs["pdf_bytes"] == b"LEGACY_PNG"
        assert result.evidence["png_source"] == "legacy"

    @pytest.mark.asyncio
    async def test_legacy_fallback_when_no_provider(self) -> None:
        """Pre-19.1 fixtures (data_provider=None) still get legacy field path."""
        resolver = ScheduleResolver()
        cand = _candidate(
            sheets=[_schedule_sheet("sheet-001", image_bytes=b"LEGACY_BYTES")],
            provider=None,
        )

        fake = _fake_client(_FakeAzureResponse(body=_azure_tables_body(row_counts=[3])))

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ), patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake,
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        assert result.evidence["png_source"] == "legacy"
        assert result.evidence["azure_calls_made"] == 1
        assert result.resolved is True

    @pytest.mark.asyncio
    async def test_no_png_available_yields_skip(self) -> None:
        """No provider PNG, no legacy field — skip with no_png_bytes_available."""
        resolver = ScheduleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(return_value=None)

        cand = _candidate(
            sheets=[_schedule_sheet("sheet-001")],  # no legacy image_bytes
            provider=provider,
        )

        fake = _fake_client(_FakeAzureResponse(body=_azure_tables_body(row_counts=[3])))

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ), patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake,
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        assert result.resolved is False
        assert result.evidence["skip_reason"] == "no_png_bytes_available"
        assert result.evidence["azure_calls_made"] == 0
        fake.analyze_layout.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cap honored
# ---------------------------------------------------------------------------


class TestCascadeCallCap:
    @pytest.mark.asyncio
    async def test_cap_honored_at_8_even_with_20_eligible_sheets(self) -> None:
        resolver = ScheduleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(return_value=b"PNG")

        cand = _candidate(
            sheets=[_schedule_sheet(f"sheet-{i:03d}") for i in range(20)],
            provider=provider,
        )

        fake = _fake_client(_FakeAzureResponse(body=_azure_tables_body(row_counts=[3])))

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ), patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake,
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        assert fake.analyze_layout.await_count == _AZURE_SHEETS_PER_CASCADE_CAP == 8
        assert provider.get_sheet_png.await_count == 8  # cap also bounds PNG fetches
        assert result.evidence["azure_calls_made"] == 8
        assert result.evidence["azure_calls_capped_at"] == 8
        assert result.evidence["schedule_sheets_found"] == 20

    @pytest.mark.asyncio
    async def test_azure_failure_still_counts_against_cap(self) -> None:
        """A non-success Azure response counts as a billed call attempt."""
        resolver = ScheduleResolver()
        provider = MagicMock()
        provider.get_sheet_png = AsyncMock(return_value=b"PNG")

        cand = _candidate(
            sheets=[_schedule_sheet(f"sheet-{i:03d}") for i in range(3)],
            provider=provider,
        )

        fake = _fake_client(_FakeAzureResponse(
            status_code=429, body={}, success=False,
        ))

        with patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_endpoint",
            "https://example.azure.com",
        ), patch(
            "aspire_orchestrator.config.settings.settings.azure_doc_intel_key",
            "key",
        ), patch(
            "aspire_orchestrator.providers.azure_doc_intel_client.get_azure_doc_intel_client",
            return_value=fake,
        ):
            result = await resolver._method_azure_premium_ocr(cand)

        assert result.resolved is False
        assert result.evidence["azure_calls_made"] == 3
        assert result.evidence["skip_reason"] == "no_table_rows_extracted"


# ---------------------------------------------------------------------------
# Table-structure fallback (provider-independent)
# ---------------------------------------------------------------------------


class TestTableStructureParse:
    @pytest.mark.asyncio
    async def test_parses_pipe_separated_rows(self) -> None:
        resolver = ScheduleResolver()
        ocr = (
            "ELECTRICAL PANEL SCHEDULE\n"
            "C-1  |  20A  |  Lighting Floor 1\n"
            "C-2  |  20A  |  Receptacles Floor 1\n"
            "C-3  |  30A  |  HVAC RTU-1\n"
            "C-4  |  30A  |  HVAC RTU-2\n"
            "C-5  |  20A  |  Emergency Lighting\n"
        )
        sheets = [{"id": "s1", "ocr_text": ocr, "sheet_title": None}]
        cand = _candidate(sheets=sheets, provider=None)

        result = await resolver._method_table_structure_parse(cand)

        # Below threshold (0.80 < 0.85) — surfaces as evidence only.
        assert result.resolved is False
        assert result.confidence in (0.80, 0.60)
        assert result.evidence["schedule_sheets_found"] >= 1


# ---------------------------------------------------------------------------
# Receipt allow-list integration (cascade lifts azure_calls_made onto receipt)
# ---------------------------------------------------------------------------


class TestReceiptAllowList:
    def test_safe_evidence_keeps_azure_counters(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _safe_evidence_for_receipt,
        )
        ev = {
            "azure_calls_made": 5,
            "azure_calls_capped_at": 8,
            "png_source": "provider",
            "ocr_tier": "base",
            "sheets_processed": 3,
            "total_rows_extracted": 17,
            # These must be stripped — not on the allow-list.
            "raw_payload": {"sensitive": "data"},
            "long_text": "x" * 1000,
        }
        safe = _safe_evidence_for_receipt(ev)
        assert safe["azure_calls_made"] == 5
        assert safe["azure_calls_capped_at"] == 8
        assert safe["png_source"] == "provider"
        assert safe["sheets_processed"] == 3
        assert "raw_payload" not in safe
        assert "long_text" not in safe

    def test_safe_evidence_truncates_long_strings(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _safe_evidence_for_receipt,
        )
        # Even allow-listed string keys are length-capped at 120.
        ev = {"skip_reason": "x" * 500}
        safe = _safe_evidence_for_receipt(ev)
        assert len(safe["skip_reason"]) <= 120

    def test_safe_evidence_drops_unknown_keys(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _safe_evidence_for_receipt,
        )
        safe = _safe_evidence_for_receipt({"never_heard_of_this_key": 42})
        assert safe == {}

    def test_safe_evidence_handles_non_dict_input(self) -> None:
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            _safe_evidence_for_receipt,
        )
        assert _safe_evidence_for_receipt(None) == {}  # type: ignore[arg-type]
        assert _safe_evidence_for_receipt("string") == {}  # type: ignore[arg-type]
