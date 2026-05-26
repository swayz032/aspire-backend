"""L-7 unit tests — audit receipt persistor + governance.

Coverage:
  - Persistor projects AuditReport → db row with correct shape
  - Writer authority enum gate rejects unknown identities (fail-closed)
  - Persistor idempotency — duplicate audit_id is a no-op
  - all 4 ALLOWED_WRITER_AUTHORITIES persist successfully
  - row carries the universal columns Supabase RLS + CHECK constraints expect
  - audited_edges projection is compact (no full edge body)
  - persist_audit_report convenience wrapper enforces gate
"""
from __future__ import annotations

import pytest

from .audit import (
    AuditReport,
    AuditVerdict,
    AuditedEdge,
    OracleVerifier,
    PerTypeAudit,
    audit_edges,
)
from .audit_persistor import (
    ALLOWED_WRITER_AUTHORITIES,
    InMemoryAuditReceiptPersistor,
    UnauthorizedAuditWrite,
    WriterAuthority,
    persist_audit_report,
    report_to_db_row,
)
from .taxonomy import EdgeProposal, EdgeType, EntityType, LocalityScope


# ============================================================
# Test fixtures
# ============================================================

UID_SUITE = "00000000-0000-0000-0000-000000000001"
UID_PROJECT = "00000000-0000-0000-0000-000000000002"


def _proposal(*, src: str = "p-1", dst: str = "o-1",
              confidence: float = 0.85) -> EdgeProposal:
    return EdgeProposal(
        edge_type=EdgeType.ANNOTATED_BY,
        suite_id=UID_SUITE, project_id=UID_PROJECT,
        src_entity_id=src, src_entity_type=EntityType.PRIMITIVE,
        dst_entity_id=dst, dst_entity_type=EntityType.OCR_SPAN,
        locality_scope=LocalityScope.SAME_SHEET,
        spatial_distance=10.0, confidence=confidence,
        evidence={"ocr_text": "A-101", "distance_px": 10,
                  "alignment": "horizontal", "zone": "title_block"},
        provenance={
            "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
            "sheet_id": "sheet-1", "generator_window": "test", "bbox": [0, 0, 10, 10],
        },
        extraction_method="test_v1",
    )


def _build_report(n: int = 3) -> AuditReport:
    edges = [_proposal(src=f"p-{i}", dst=f"o-{i}") for i in range(n)]
    truth = {(f"p-{i}", f"o-{i}", EdgeType.ANNOTATED_BY): AuditVerdict.TRUE_POSITIVE
             for i in range(n)}
    return audit_edges(
        edges,
        verifier=OracleVerifier(truth),
        generator_name="annotated_by",
        generator_version="1.0",
        project_id=UID_PROJECT,
        sample_size=n,
    )


# ============================================================
# WriterAuthority gate (governance core)
# ============================================================


def test_writer_authority_enum_has_four_members():
    """Schema CHECK constraint mirrors this set — drift detector."""
    assert {a.value for a in WriterAuthority} == {
        "drew_pipeline", "manual_cli", "runtime_reconciler", "l6_smoke_test"
    }


def test_allowed_writer_authorities_covers_enum():
    assert ALLOWED_WRITER_AUTHORITIES == frozenset(WriterAuthority)


def test_report_to_db_row_rejects_unauthorized_writer():
    """Pass a string that isn't a WriterAuthority → fail-closed."""
    report = _build_report()
    with pytest.raises(UnauthorizedAuditWrite):
        report_to_db_row(
            report,
            suite_id=UID_SUITE,
            writer_authority="some_random_string",  # type: ignore[arg-type]
        )


def test_persist_audit_report_wrapper_rejects_unauthorized():
    """Defense in depth — wrapper checks before persistor is even called."""
    report = _build_report()
    persistor = InMemoryAuditReceiptPersistor()
    with pytest.raises(UnauthorizedAuditWrite):
        persist_audit_report(
            report, persistor,
            suite_id=UID_SUITE,
            writer_authority="rogue_actor",  # type: ignore[arg-type]
        )
    assert persistor.rows == []  # no insertion attempted


# ============================================================
# Row projection shape
# ============================================================


def test_report_to_db_row_carries_universal_columns():
    report = _build_report()
    row = report_to_db_row(
        report,
        suite_id=UID_SUITE,
        writer_authority=WriterAuthority.DREW_PIPELINE,
    )
    # Every column the DB schema expects
    expected_columns = {
        "audit_id", "suite_id", "office_id", "project_id",
        "generator_name", "generator_version", "taxonomy_version", "pipeline_version",
        "verifier_name", "verifier_version",
        "sample_size", "total_edges", "sample_seed",
        "threshold", "passes_threshold", "failing_types",
        "verdict_counts", "per_type_precision", "audited_edges",
        "writer_authority", "created_by",
    }
    assert expected_columns <= set(row.keys())


def test_row_writer_authority_matches_db_check_set():
    """DB CHECK constraint accepts only these values."""
    report = _build_report()
    for authority in WriterAuthority:
        row = report_to_db_row(
            report, suite_id=UID_SUITE, writer_authority=authority,
        )
        assert row["writer_authority"] == authority.value


def test_row_passes_threshold_reflects_report():
    report = _build_report()
    row = report_to_db_row(
        report, suite_id=UID_SUITE, writer_authority=WriterAuthority.DREW_PIPELINE,
    )
    assert row["passes_threshold"] == report.passes_threshold()


def test_row_audited_edges_is_compact_projection():
    """Audited edges shouldn't contain full provenance — just the projection."""
    report = _build_report()
    row = report_to_db_row(
        report, suite_id=UID_SUITE, writer_authority=WriterAuthority.DREW_PIPELINE,
    )
    audited = row["audited_edges"]
    assert isinstance(audited, list)
    assert len(audited) == report.sample_size
    sample_keys = set(audited[0].keys())
    # Compact projection: only these keys
    assert sample_keys == {
        "edge_type", "src", "dst", "replay_hash",
        "confidence", "verdict", "rationale",
    }


def test_row_verdict_counts_serializes_to_plain_dict():
    """Counter -> dict so jsonb insert works."""
    report = _build_report()
    row = report_to_db_row(
        report, suite_id=UID_SUITE, writer_authority=WriterAuthority.DREW_PIPELINE,
    )
    assert isinstance(row["verdict_counts"], dict)


def test_row_failing_types_is_list_of_strings():
    """text[] column — must be list of strings not list of EdgeType enum values."""
    report = _build_report()
    row = report_to_db_row(
        report, suite_id=UID_SUITE, writer_authority=WriterAuthority.DREW_PIPELINE,
    )
    assert isinstance(row["failing_types"], list)
    for v in row["failing_types"]:
        assert isinstance(v, str)


# ============================================================
# InMemoryAuditReceiptPersistor behavior
# ============================================================


def test_inmemory_persistor_stores_one_row():
    persistor = InMemoryAuditReceiptPersistor()
    report = _build_report()
    audit_id = persistor.persist(
        report,
        suite_id=UID_SUITE,
        writer_authority=WriterAuthority.DREW_PIPELINE,
    )
    assert audit_id == report.audit_id
    assert len(persistor.rows) == 1


def test_inmemory_persistor_is_idempotent():
    """Re-persisting the same audit_id is a no-op."""
    persistor = InMemoryAuditReceiptPersistor()
    report = _build_report()
    persistor.persist(report, suite_id=UID_SUITE,
                      writer_authority=WriterAuthority.DREW_PIPELINE)
    persistor.persist(report, suite_id=UID_SUITE,
                      writer_authority=WriterAuthority.DREW_PIPELINE)
    assert len(persistor.rows) == 1


def test_inmemory_persistor_records_rejection():
    """Bad authority bubbles up + persistor logs the rejection for forensics."""
    persistor = InMemoryAuditReceiptPersistor()
    report = _build_report()
    with pytest.raises(UnauthorizedAuditWrite):
        persistor.persist(
            report,
            suite_id=UID_SUITE,
            writer_authority="not_an_authority",  # type: ignore[arg-type]
        )
    assert len(persistor.rejected) == 1
    audit_id, reason = persistor.rejected[0]
    assert audit_id == report.audit_id
    assert "writer_authority" in reason


def test_inmemory_persistor_multiple_reports_different_audit_ids():
    persistor = InMemoryAuditReceiptPersistor()
    r1 = _build_report(n=2)
    r2 = _build_report(n=4)
    assert r1.audit_id != r2.audit_id
    persistor.persist(r1, suite_id=UID_SUITE,
                      writer_authority=WriterAuthority.DREW_PIPELINE)
    persistor.persist(r2, suite_id=UID_SUITE,
                      writer_authority=WriterAuthority.DREW_PIPELINE)
    assert len(persistor.rows) == 2


# ============================================================
# All 4 ALLOWED_WRITER_AUTHORITIES persist successfully
# ============================================================


@pytest.mark.parametrize("authority", list(WriterAuthority))
def test_all_authorities_can_persist(authority: WriterAuthority):
    persistor = InMemoryAuditReceiptPersistor()
    report = _build_report()
    audit_id = persist_audit_report(
        report, persistor,
        suite_id=UID_SUITE,
        writer_authority=authority,
    )
    assert audit_id == report.audit_id
    assert persistor.rows[0]["writer_authority"] == authority.value


# ============================================================
# created_by + verifier identity propagation
# ============================================================


def test_persistor_records_verifier_identity():
    persistor = InMemoryAuditReceiptPersistor()
    report = _build_report()
    persistor.persist(
        report,
        suite_id=UID_SUITE,
        writer_authority=WriterAuthority.MANUAL_CLI,
        created_by="tonio_local",
        verifier_name="rule_based",
        verifier_version="1.0",
    )
    row = persistor.rows[0]
    assert row["created_by"] == "tonio_local"
    assert row["verifier_name"] == "rule_based"
    assert row["verifier_version"] == "1.0"
