"""Wave 20.14.5L L-7 — Persistors + governance for audit receipts.

Two implementations of the AuditReceiptPersistor Protocol:

  - InMemoryAuditReceiptPersistor  — test/CI fixture
  - SupabaseAuditReceiptPersistor  — production writer

PLUS a lightweight Phase-3 capability gate: WriterAuthority enum. Only
identities in the ALLOWED_WRITER_AUTHORITIES set may persist an audit
report. Phase 4 / Phase 5 will replace this with real capability_tokens
FK validation per Aspire Law #5.

Design choices:
  - Persistor takes (report, *, writer_authority, suite_id) so the
    governance check happens BEFORE any DB call (fail-closed, Law #3).
  - report.audit_id is the primary key — re-persisting the same report
    is an idempotent no-op via ON CONFLICT DO NOTHING.
  - Writer authority + created_by are persisted into the row for audit-
    of-the-auditor lineage.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from .audit import AuditReport


class WriterAuthority(str, Enum):
    """Phase 3 lightweight capability gate.

    Only these identities may insert into blueprint_semantic_edge_audit_reports.
    DB CHECK constraint enforces the same set at the schema layer.

    Phase 5 will graduate this to capability_tokens FK + cryptographic
    proof of authority (Law #5). Until then this enum + DB CHECK is the
    governance fence.
    """
    DREW_PIPELINE = "drew_pipeline"           # production Drew SEE stage
    MANUAL_CLI = "manual_cli"                 # human-triggered debug audit
    RUNTIME_RECONCILER = "runtime_reconciler" # background reconciliation worker
    L6_SMOKE_TEST = "l6_smoke_test"           # CI / regression suite


ALLOWED_WRITER_AUTHORITIES: frozenset[WriterAuthority] = frozenset(WriterAuthority)


class UnauthorizedAuditWrite(Exception):
    """Raised when a caller tries to persist with an unauthorized writer.

    Fail-closed per Aspire Law #3. The exception is unrecoverable —
    callers must use a known WriterAuthority value or be denied.
    """


# ============================================================
# Row projection
# ============================================================


def report_to_db_row(
    report: AuditReport,
    *,
    suite_id: str,
    office_id: Optional[str] = None,
    project_id: Optional[str] = None,
    writer_authority: WriterAuthority,
    created_by: str = "system",
    verifier_name: str = "rule_based",
    verifier_version: str = "1.0",
    pipeline_version: str = "L7_1.0",
    sample_seed: int = 0,
    total_edges: int = 0,
    taxonomy_version: Optional[str] = None,
) -> dict[str, Any]:
    """Project an AuditReport into the row shape Supabase expects.

    The compact audited_edges projection keeps each audited row small
    (full edge bodies live in blueprint_semantic_edges). One row per
    sampled edge: {edge_type, src, dst, replay_hash, confidence,
    verdict, rationale}.
    """
    if writer_authority not in ALLOWED_WRITER_AUTHORITIES:
        raise UnauthorizedAuditWrite(
            f"writer_authority={writer_authority!r} is not in the "
            f"ALLOWED_WRITER_AUTHORITIES set"
        )

    audited_compact = [
        {
            "edge_type": e.edge_type.value,
            "src": e.src_entity_id,
            "dst": e.dst_entity_id,
            "replay_hash": e.replay_hash,
            "confidence": e.confidence,
            "verdict": e.verdict.value,
            "rationale": e.rationale,
        }
        for e in report.audited_edges
    ]

    return {
        "audit_id": report.audit_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "project_id": project_id or report.project_id,
        "generator_name": report.generator_name,
        "generator_version": report.generator_version,
        "taxonomy_version": taxonomy_version,
        "pipeline_version": pipeline_version,
        "verifier_name": verifier_name,
        "verifier_version": verifier_version,
        "sample_size": report.sample_size,
        "total_edges": total_edges,
        "sample_seed": sample_seed,
        "threshold": float(report.threshold),
        "passes_threshold": report.passes_threshold(),
        "failing_types": report.failing_types(),
        "verdict_counts": dict(report.verdict_counts),
        "per_type_precision": report.per_type_precision(),
        "audited_edges": audited_compact,
        "writer_authority": writer_authority.value,
        "created_by": created_by,
    }


# ============================================================
# Persistor protocol + impls
# ============================================================


class AuditReceiptPersistor(Protocol):
    """Pluggable persistence backend for audit reports."""

    def persist(
        self,
        report: AuditReport,
        *,
        suite_id: str,
        writer_authority: WriterAuthority,
        office_id: Optional[str] = None,
        created_by: str = "system",
        verifier_name: str = "rule_based",
        verifier_version: str = "1.0",
        pipeline_version: str = "L7_1.0",
        sample_seed: int = 0,
        total_edges: int = 0,
        taxonomy_version: Optional[str] = None,
    ) -> str:
        """Persist an audit report. Returns the audit_id."""
        ...


@dataclass
class InMemoryAuditReceiptPersistor:
    """Test-only persistor. Buffers rows so tests can assert on persistence."""

    def __post_init__(self) -> None:
        if not hasattr(self, "rows"):
            self.rows: list[dict[str, Any]] = []
        if not hasattr(self, "rejected"):
            self.rejected: list[tuple[str, str]] = []  # (audit_id, reason)

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.rejected: list[tuple[str, str]] = []

    def persist(
        self,
        report: AuditReport,
        *,
        suite_id: str,
        writer_authority: WriterAuthority,
        office_id: Optional[str] = None,
        created_by: str = "system",
        verifier_name: str = "rule_based",
        verifier_version: str = "1.0",
        pipeline_version: str = "L7_1.0",
        sample_seed: int = 0,
        total_edges: int = 0,
        taxonomy_version: Optional[str] = None,
    ) -> str:
        try:
            row = report_to_db_row(
                report,
                suite_id=suite_id,
                office_id=office_id,
                writer_authority=writer_authority,
                created_by=created_by,
                verifier_name=verifier_name,
                verifier_version=verifier_version,
                pipeline_version=pipeline_version,
                sample_seed=sample_seed,
                total_edges=total_edges,
                taxonomy_version=taxonomy_version,
            )
        except UnauthorizedAuditWrite as exc:
            self.rejected.append((report.audit_id, str(exc)))
            raise

        # Idempotency — duplicate audit_id = no-op
        if any(r["audit_id"] == row["audit_id"] for r in self.rows):
            return row["audit_id"]
        self.rows.append(row)
        return row["audit_id"]


class SupabaseAuditReceiptPersistor:
    """Production persistor — writes to blueprint_semantic_edge_audit_reports.

    Uses the existing supabase_insert helper (sync — wraps async client
    in asyncio.run when called from sync code).
    """

    def __init__(self) -> None:
        # Lazy import so unit tests can use InMemoryAuditReceiptPersistor
        # without Supabase credentials.
        from aspire_orchestrator.services.supabase_client import (  # noqa: F401
            supabase_insert_batch_ignore_conflicts,
            supabase_select,
        )
        self._insert = supabase_insert_batch_ignore_conflicts

    def _run_async(self, coro):
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError(
                "SupabaseAuditReceiptPersistor.persist called inside an event loop; "
                "use the async variant when one is added"
            )
        return asyncio.run(coro)

    def persist(
        self,
        report: AuditReport,
        *,
        suite_id: str,
        writer_authority: WriterAuthority,
        office_id: Optional[str] = None,
        created_by: str = "system",
        verifier_name: str = "rule_based",
        verifier_version: str = "1.0",
        pipeline_version: str = "L7_1.0",
        sample_seed: int = 0,
        total_edges: int = 0,
        taxonomy_version: Optional[str] = None,
    ) -> str:
        row = report_to_db_row(
            report,
            suite_id=suite_id,
            office_id=office_id,
            writer_authority=writer_authority,
            created_by=created_by,
            verifier_name=verifier_name,
            verifier_version=verifier_version,
            pipeline_version=pipeline_version,
            sample_seed=sample_seed,
            total_edges=total_edges,
            taxonomy_version=taxonomy_version,
        )
        self._run_async(
            self._insert("blueprint_semantic_edge_audit_reports", [row])
        )
        return row["audit_id"]


# ============================================================
# Convenience wrapper
# ============================================================


def persist_audit_report(
    report: AuditReport,
    persistor: AuditReceiptPersistor,
    *,
    suite_id: str,
    writer_authority: WriterAuthority,
    **kwargs: Any,
) -> str:
    """Thin wrapper that explicitly enforces the writer-authority check
    BEFORE any persistor work happens. Use this instead of calling
    persistor.persist() directly when you want defense in depth.
    """
    if writer_authority not in ALLOWED_WRITER_AUTHORITIES:
        raise UnauthorizedAuditWrite(
            f"writer_authority={writer_authority!r} not allowed"
        )
    return persistor.persist(
        report,
        suite_id=suite_id,
        writer_authority=writer_authority,
        **kwargs,
    )
