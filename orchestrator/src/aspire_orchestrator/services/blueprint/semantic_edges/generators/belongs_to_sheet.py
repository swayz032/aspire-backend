"""Wave 20.14.5L L-4 — BelongsToSheetGenerator.

Deterministic generator. For every composed_symbol + legend in the
project, emits a `belongs_to_sheet` edge pointing at its parent sheet.

Why this is first:
  - Existing rows already carry sheet_id; this is provenance materialization
    not inference, so confidence is intentionally 0.999.
  - Validates the L-3 framework end-to-end on real production data.
  - Provides the ledger entry every downstream Phase 4/5 walker expects
    when traversing "what sheet does this entity live on?"

Primitives are NOT covered by this generator on first cut — 1.28M rows
would saturate `max_edges_emitted` and offer no semantic info beyond
the primitive.sheet_id column itself. A separate PrimitiveBelongsToSheetGenerator
can land later with batched per-sheet runs if the edge ledger needs
primitive-level coverage.
"""
from __future__ import annotations

from typing import ClassVar, Iterable, Optional

from ..generator import Candidate, EdgeGenerator, GeneratorContext
from ..locality import LocalityCaps
from ..taxonomy import EdgeType, EntityType, LocalityScope
from .data_access import EntitiesQueryClient, EntityRecord


class BelongsToSheetGenerator(EdgeGenerator):
    """Emits belongs_to_sheet edges for composed_symbols + legends.

    Construction:
        gen = BelongsToSheetGenerator(query_client)

    The query_client must implement EntitiesQueryClient. Pass
    InMemoryQueryClient for tests, SupabaseQueryClient for prod.
    """

    name: ClassVar[str] = "belongs_to_sheet"
    version: ClassVar[str] = "1.0"
    edge_types: ClassVar[tuple[EdgeType, ...]] = (EdgeType.BELONGS_TO_SHEET,)
    extraction_method: ClassVar[str] = "belongs_to_sheet_v1"

    # belongs_to_sheet is fully deterministic. Caps are mostly
    # generous; the real safety is in stage 5 + DB CHECK.
    caps: ClassVar[LocalityCaps] = LocalityCaps(
        max_radius_px=1.0,           # not spatial; nominal
        max_neighbors=1,
        max_candidates=50_000,
        max_edges_emitted=50_000,
        max_expensive_ops=50_000,
    )

    def __init__(self, query_client: EntitiesQueryClient):
        self._client = query_client

    def stage0_emit_candidates(
        self, ctx: GeneratorContext
    ) -> Iterable[Candidate]:
        """One candidate per (composed_symbol|legend) that has a sheet_id."""
        composed = self._client.list_composed_symbols(
            project_id=ctx.project_id, suite_id=ctx.suite_id
        )
        legends = self._client.list_legends(
            project_id=ctx.project_id, suite_id=ctx.suite_id
        )

        for entity in (*composed, *legends):
            if not entity.sheet_id:
                continue
            yield _entity_to_candidate(entity)


def _entity_to_candidate(entity: EntityRecord) -> Candidate:
    page_index = entity.page_index if entity.page_index is not None else 0
    bbox = entity.bbox if entity.bbox else [0.0, 0.0, 0.0, 0.0]
    return Candidate(
        edge_type=EdgeType.BELONGS_TO_SHEET,
        project_id=entity.project_id,
        sheet_id=entity.sheet_id,
        src_entity_id=entity.entity_id,
        src_entity_type=entity.entity_type,           # composed_symbol|legend
        dst_entity_id=entity.sheet_id,                # type: ignore[arg-type]
        dst_entity_type=EntityType.SHEET.value,
        src_entity_version=entity.version,
        dst_entity_version=1,
        locality_scope=LocalityScope.SAME_SHEET.value,
        confidence=0.999,                              # deterministic from row data
        evidence={"page_index": int(page_index)},
        provenance={
            "sheet_id": entity.sheet_id,
            "bbox": bbox,
            "source_table": _table_for(entity.entity_type),
        },
        neighborhood_size=1,
    )


def _table_for(entity_type: str) -> str:
    return {
        "composed_symbol": "blueprint_composed_symbols",
        "legend": "blueprint_project_legends",
    }.get(entity_type, "unknown")
