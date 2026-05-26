"""Wave 20.14.5L L-4 — MemberOfFamilyGenerator.

Deterministic generator. Materializes the constituent_motif_ids array on
blueprint_composed_symbols into discrete edges:

  src = primitive (each motif id), dst = composed_symbol

This is the backbone of the future material takeoff — once Drew knows
which primitives compose which family, downstream Phase 5 binding can
resolve those families against the project legend (e.g. all 64
QR pendant sprinkler instances).

Locality scope: SAME_SHEET (composed symbols + their constituents live
on the same sheet by Wave 20.13 PCE construction rules).
"""
from __future__ import annotations

from typing import ClassVar, Iterable

from ..generator import Candidate, EdgeGenerator, GeneratorContext
from ..locality import LocalityCaps
from ..taxonomy import EdgeType, EntityType, LocalityScope
from .data_access import CompositionRecord, EntitiesQueryClient


class MemberOfFamilyGenerator(EdgeGenerator):
    """Emits member_of_family edges from composed_symbols.constituent_motif_ids.

    One edge per (constituent_primitive, composed_symbol) pair.
    """

    name: ClassVar[str] = "member_of_family"
    version: ClassVar[str] = "1.0"
    edge_types: ClassVar[tuple[EdgeType, ...]] = (EdgeType.MEMBER_OF_FAMILY,)
    extraction_method: ClassVar[str] = "member_of_family_v1"

    caps: ClassVar[LocalityCaps] = LocalityCaps(
        max_radius_px=1.0,
        max_neighbors=1,
        max_candidates=50_000,
        max_edges_emitted=50_000,
        max_expensive_ops=50_000,         # framework absolute ceiling
    )

    def __init__(self, query_client: EntitiesQueryClient):
        self._client = query_client

    def stage0_emit_candidates(
        self, ctx: GeneratorContext
    ) -> Iterable[Candidate]:
        compositions = self._client.list_compositions(
            project_id=ctx.project_id, suite_id=ctx.suite_id
        )
        for comp in compositions:
            yield from _composition_to_candidates(comp)


def _composition_to_candidates(comp: CompositionRecord) -> Iterable[Candidate]:
    n_constituents = len(comp.constituent_ids)
    if n_constituents == 0:
        return
    for primitive_id in comp.constituent_ids:
        yield Candidate(
            edge_type=EdgeType.MEMBER_OF_FAMILY,
            project_id=comp.project_id,
            sheet_id=comp.sheet_id,
            src_entity_id=primitive_id,
            src_entity_type=EntityType.PRIMITIVE.value,
            dst_entity_id=comp.composed_symbol_id,
            dst_entity_type=EntityType.COMPOSED_SYMBOL.value,
            locality_scope=LocalityScope.SAME_SHEET.value,
            confidence=0.999,                       # deterministic from array
            evidence={
                "family_id": comp.composed_symbol_id,
                "similarity": 1.0,
            },
            provenance={
                "sheet_id": comp.sheet_id or "",
                "bbox": [0.0, 0.0, 0.0, 0.0],       # not bbox-bound
                "source_table": "blueprint_composed_symbols",
                "source_column": "constituent_motif_ids",
                "composition_size": n_constituents,
            },
            neighborhood_size=n_constituents,
        )
