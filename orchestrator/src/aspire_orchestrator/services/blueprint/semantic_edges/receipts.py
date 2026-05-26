"""Wave 20.14.5L L-3 — Generation receipt builder + persistence.

Builds the row inserted into blueprint_semantic_edge_generation_receipts.
The Supabase write itself is delegated to an injected client so this
module remains testable without a live DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from .metrics import GeneratorHealth, RunMetrics


@dataclass(frozen=True)
class GenerationReceipt:
    """Immutable receipt of a single generator invocation.

    Persisted to public.blueprint_semantic_edge_generation_receipts.
    Maps 1:1 to that table's schema; extra fields are forbidden so
    drift between table + dataclass is caught at insert time.
    """
    suite_id: str
    project_id: str
    generator_name: str
    generator_version: str
    taxonomy_version: str
    pipeline_version: str
    extraction_method: str
    runtime_ms: float
    edges_emitted: int
    candidates_considered: int
    unknown_review_count: int
    locality_violations: int
    health: GeneratorHealth
    input_evidence_hash: str
    output_edge_hash: str

    # Optional / nullable
    office_id: Optional[str] = None
    sheet_id: Optional[str] = None
    edges_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_locality: dict[str, int] = field(default_factory=dict)
    cheap_filter_kept: Optional[int] = None
    medium_filter_kept: Optional[int] = None
    expensive_filter_kept: Optional[int] = None
    avg_neighborhood_size: Optional[float] = None
    max_neighborhood_size: Optional[int] = None
    unknown_review_reasons: dict[str, int] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    aborted: bool = False
    abort_reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_db_row(self) -> dict[str, Any]:
        """Convert to the dict shape Supabase insert expects.

        Columns match supabase/migrations/20260525000001 exactly.
        `health` + `aborted` + `abort_reason` are NOT in the receipts
        table (they live in run state) — included here as metadata only.
        """
        row: dict[str, Any] = {
            "suite_id": self.suite_id,
            "office_id": self.office_id,
            "project_id": self.project_id,
            "sheet_id": self.sheet_id,
            "generator_name": self.generator_name,
            "extraction_method": self.extraction_method,
            "pipeline_version": self.pipeline_version,
            "edges_emitted": self.edges_emitted,
            "edges_by_type": self.edges_by_type,
            "edges_by_locality": self.edges_by_locality,
            "candidates_considered": self.candidates_considered,
            "cheap_filter_kept": self.cheap_filter_kept,
            "medium_filter_kept": self.medium_filter_kept,
            "expensive_filter_kept": self.expensive_filter_kept,
            "avg_neighborhood_size": self.avg_neighborhood_size,
            "max_neighborhood_size": self.max_neighborhood_size,
            "locality_violations": self.locality_violations,
            "unknown_review_count": self.unknown_review_count,
            "unknown_review_reasons": self.unknown_review_reasons,
            "runtime_ms": self.runtime_ms,
            "cost_usd": self.cost_usd,
            "input_evidence_hash": self.input_evidence_hash,
            "output_edge_hash": self.output_edge_hash,
        }
        return row


class ReceiptPersistor(Protocol):
    """Pluggable persistence backend.

    Implementations:
      - SupabaseReceiptPersistor — production
      - InMemoryReceiptPersistor — unit tests
    """

    def persist(self, receipt: GenerationReceipt) -> None: ...


class InMemoryReceiptPersistor:
    """Test-only persistor. Buffers receipts in a list."""

    def __init__(self) -> None:
        self.receipts: list[GenerationReceipt] = []

    def persist(self, receipt: GenerationReceipt) -> None:
        self.receipts.append(receipt)


def receipt_from_metrics(
    *,
    metrics: RunMetrics,
    suite_id: str,
    project_id: str,
    extraction_method: str,
    pipeline_version: str,
    office_id: Optional[str] = None,
    sheet_id: Optional[str] = None,
) -> GenerationReceipt:
    """Construct a GenerationReceipt from a populated RunMetrics + identity."""
    return GenerationReceipt(
        suite_id=suite_id,
        office_id=office_id,
        project_id=project_id,
        sheet_id=sheet_id,
        generator_name=metrics.generator_name,
        generator_version=metrics.generator_version,
        taxonomy_version=metrics.taxonomy_version,
        pipeline_version=pipeline_version,
        extraction_method=extraction_method,
        runtime_ms=metrics.runtime_ms_total,
        edges_emitted=metrics.edges_emitted,
        candidates_considered=metrics.candidates_considered,
        unknown_review_count=metrics.unknown_review_count,
        locality_violations=metrics.locality_violations,
        health=metrics.health,
        input_evidence_hash=metrics.input_evidence_hash,
        output_edge_hash=metrics.output_edge_hash,
        edges_by_type=dict(metrics.edges_by_type),
        edges_by_locality=dict(metrics.edges_by_locality),
        cheap_filter_kept=metrics.stages.get(
            "stage1_cheap_filter").candidates_kept if "stage1_cheap_filter" in metrics.stages else None,
        medium_filter_kept=metrics.stages.get(
            "stage3_ocr_affinity").candidates_kept if "stage3_ocr_affinity" in metrics.stages else None,
        expensive_filter_kept=metrics.stages.get(
            "stage4_topology").candidates_kept if "stage4_topology" in metrics.stages else None,
        avg_neighborhood_size=metrics.avg_neighborhood_size,
        max_neighborhood_size=metrics.max_neighborhood_size,
        unknown_review_reasons=dict(metrics.unknown_review_reasons),
        cost_usd=metrics.cost_usd or None,
        aborted=metrics.aborted,
        abort_reason=metrics.abort_reason,
    )
