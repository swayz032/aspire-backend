"""Wave 20.14.5L L-4 — Data access abstraction for edge generators.

Generators are pure consumers of an EntitiesQueryClient. Two
implementations:

  - SupabaseQueryClient  — production (async, talks to Supabase)
  - InMemoryQueryClient  — tests (sync stand-in)

This keeps generator unit tests deterministic and fast while still
allowing the production code to share the same generator class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class EntityRecord:
    """Normalized entity row consumed by generators.

    The Supabase tables have different column names + extra columns; the
    data-access layer normalizes them into this shape so generator
    hooks never touch raw rows.
    """
    entity_id: str
    entity_type: str                            # 'primitive'|'composed_symbol'|'legend'|'sheet'|'zone'
    project_id: str
    suite_id: str
    sheet_id: Optional[str] = None
    family_id: Optional[str] = None
    label: Optional[str] = None
    bbox: Optional[list[float]] = None
    version: int = 1                            # entity_version (Phase 4 will populate)
    page_index: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OcrSpanRecord:
    """OCR span surfaced from blueprint_sheet_intelligence.paragraphs[i].

    OCR spans aren't an independent table — they live as JSON inside the
    sheet_intelligence row. We synthesize a stable UUID from
    (sheet_intelligence_id, paragraph_index) so polymorphic edge refs
    have something durable to point at.
    """
    span_id: str                                # deterministic UUID5
    sheet_id: str
    project_id: str
    suite_id: str
    text: str
    bbox: list[float]                           # axis-aligned [x0,y0,x1,y1]
    role: Optional[str] = None
    confidence: float = 1.0


@dataclass
class ZoneRecord:
    """Semantic zone polygon on a sheet (title_block, legend, schedule, etc.)."""
    zone_id: str
    sheet_id: str
    project_id: str
    suite_id: str
    zone_type: str                              # 'title_block'|'legend'|'schedule'|'notes'|'plan'
    bbox: list[float]                           # axis-aligned [x0,y0,x1,y1]


@dataclass
class CompositionRecord:
    """A composed_symbol's constituent → composed relationship.

    Surfaces the constituent_motif_ids array from blueprint_composed_symbols
    in a form member_of_family can iterate over.
    """
    composed_symbol_id: str
    sheet_id: Optional[str]
    project_id: str
    suite_id: str
    constituent_ids: list[str]                  # primitive UUIDs


class EntitiesQueryClient(Protocol):
    """Abstract data access for generators."""

    def list_composed_symbols(
        self, *, project_id: str, suite_id: str
    ) -> list[EntityRecord]: ...

    def list_legends(
        self, *, project_id: str, suite_id: str
    ) -> list[EntityRecord]: ...

    def list_sheets(
        self, *, project_id: str, suite_id: str
    ) -> list[EntityRecord]: ...

    def list_compositions(
        self, *, project_id: str, suite_id: str
    ) -> list[CompositionRecord]: ...

    def list_primitives_on_sheet(
        self, *, project_id: str, suite_id: str, sheet_id: str,
        max_rows: int = 10_000,
    ) -> list[EntityRecord]: ...

    def list_ocr_spans_on_sheet(
        self, *, project_id: str, suite_id: str, sheet_id: str,
    ) -> list[OcrSpanRecord]: ...

    def list_zones_on_sheet(
        self, *, project_id: str, suite_id: str, sheet_id: str,
    ) -> list[ZoneRecord]: ...


# ============================================================
# In-memory client (tests)
# ============================================================


class InMemoryQueryClient:
    """Test-only client. Pre-populate buckets in __init__."""

    def __init__(
        self,
        *,
        composed_symbols: Optional[list[EntityRecord]] = None,
        legends: Optional[list[EntityRecord]] = None,
        sheets: Optional[list[EntityRecord]] = None,
        compositions: Optional[list[CompositionRecord]] = None,
        primitives: Optional[list[EntityRecord]] = None,
        ocr_spans: Optional[list[OcrSpanRecord]] = None,
        zones: Optional[list[ZoneRecord]] = None,
    ) -> None:
        self._composed = composed_symbols or []
        self._legends = legends or []
        self._sheets = sheets or []
        self._compositions = compositions or []
        self._primitives = primitives or []
        self._ocr_spans = ocr_spans or []
        self._zones = zones or []

    def _filter(self, rows: list, project_id: str, suite_id: str) -> list:
        return [
            r for r in rows
            if r.project_id == project_id and r.suite_id == suite_id
        ]

    def list_composed_symbols(self, *, project_id: str, suite_id: str) -> list[EntityRecord]:
        return self._filter(self._composed, project_id, suite_id)

    def list_legends(self, *, project_id: str, suite_id: str) -> list[EntityRecord]:
        return self._filter(self._legends, project_id, suite_id)

    def list_sheets(self, *, project_id: str, suite_id: str) -> list[EntityRecord]:
        return self._filter(self._sheets, project_id, suite_id)

    def list_compositions(self, *, project_id: str, suite_id: str) -> list[CompositionRecord]:
        return self._filter(self._compositions, project_id, suite_id)

    def list_primitives_on_sheet(
        self, *, project_id: str, suite_id: str, sheet_id: str, max_rows: int = 10_000,
    ) -> list[EntityRecord]:
        out = [
            p for p in self._primitives
            if p.project_id == project_id and p.suite_id == suite_id and p.sheet_id == sheet_id
        ]
        return out[:max_rows]

    def list_ocr_spans_on_sheet(
        self, *, project_id: str, suite_id: str, sheet_id: str,
    ) -> list[OcrSpanRecord]:
        return [
            s for s in self._ocr_spans
            if s.project_id == project_id and s.suite_id == suite_id and s.sheet_id == sheet_id
        ]

    def list_zones_on_sheet(
        self, *, project_id: str, suite_id: str, sheet_id: str,
    ) -> list[ZoneRecord]:
        return [
            z for z in self._zones
            if z.project_id == project_id and z.suite_id == suite_id and z.sheet_id == sheet_id
        ]


# ============================================================
# Supabase client (production)
# ============================================================


def _row_to_entity_record(
    row: dict[str, Any],
    *,
    entity_type: str,
    sheet_id_key: str = "sheet_id",
) -> EntityRecord:
    """Normalize a raw Supabase row into an EntityRecord."""
    return EntityRecord(
        entity_id=str(row["id"]),
        entity_type=entity_type,
        project_id=str(row["project_id"]),
        suite_id=str(row["suite_id"]),
        sheet_id=str(row[sheet_id_key]) if row.get(sheet_id_key) else None,
        family_id=row.get("family_id"),
        label=row.get("label") or row.get("symbol_id"),
        bbox=row.get("bbox") or row.get("representation_bbox"),
        page_index=row.get("page_index") or row.get("sheet_number"),
        raw=row,
    )


class SupabaseQueryClient:
    """Production data-access using the existing supabase_select helper.

    The async helpers from aspire_orchestrator.services.supabase_client
    are wrapped behind sync methods on this class so EdgeGenerator can
    stay synchronous. Callers that need full async behavior can use
    SupabaseQueryClientAsync (added later).
    """

    def __init__(self) -> None:
        # Imported lazily to keep generators importable in environments
        # without supabase credentials (unit tests).
        from aspire_orchestrator.services.supabase_client import (  # noqa: F401
            supabase_select,
        )
        self._supabase_select = supabase_select

    def _run_async(self, coro):
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an event loop — caller must use the async
            # variant. Reject sync access to avoid deadlocks.
            raise RuntimeError(
                "SupabaseQueryClient.list_* called from inside an event loop; "
                "use SupabaseQueryClientAsync instead"
            )
        return asyncio.run(coro)

    def list_composed_symbols(self, *, project_id: str, suite_id: str) -> list[EntityRecord]:
        rows = self._run_async(
            self._supabase_select(
                "blueprint_composed_symbols",
                {"project_id": project_id, "suite_id": suite_id},
            )
        )
        return [_row_to_entity_record(r, entity_type="composed_symbol") for r in rows]

    def list_legends(self, *, project_id: str, suite_id: str) -> list[EntityRecord]:
        rows = self._run_async(
            self._supabase_select(
                "blueprint_project_legends",
                {"project_id": project_id, "suite_id": suite_id},
            )
        )
        return [
            _row_to_entity_record(r, entity_type="legend", sheet_id_key="source_sheet_id")
            for r in rows
        ]

    def list_sheets(self, *, project_id: str, suite_id: str) -> list[EntityRecord]:
        rows = self._run_async(
            self._supabase_select(
                "blueprint_sheets",
                {"project_id": project_id, "suite_id": suite_id},
            )
        )
        return [_row_to_entity_record(r, entity_type="sheet", sheet_id_key="id") for r in rows]

    def list_compositions(
        self, *, project_id: str, suite_id: str
    ) -> list[CompositionRecord]:
        rows = self._run_async(
            self._supabase_select(
                "blueprint_composed_symbols",
                {"project_id": project_id, "suite_id": suite_id},
            )
        )
        out: list[CompositionRecord] = []
        for r in rows:
            constituent = r.get("constituent_motif_ids") or []
            out.append(
                CompositionRecord(
                    composed_symbol_id=str(r["id"]),
                    sheet_id=str(r["sheet_id"]) if r.get("sheet_id") else None,
                    project_id=str(r["project_id"]),
                    suite_id=str(r["suite_id"]),
                    constituent_ids=[str(x) for x in constituent if x],
                )
            )
        return out
