"""Lightweight smoke test for L-7 audit persistor + governance. No scipy needed.
Bypasses 20-min pytest discovery on Windows — imports only audit + audit_persistor.
Run: `python scripts/win_debug/audit_persistor_smoke.py`"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> int:
    from aspire_orchestrator.services.blueprint.semantic_edges.audit import (
        AuditVerdict, OracleVerifier, audit_edges,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.audit_persistor import (
        ALLOWED_WRITER_AUTHORITIES,
        InMemoryAuditReceiptPersistor,
        UnauthorizedAuditWrite,
        WriterAuthority,
        persist_audit_report,
        report_to_db_row,
    )
    from aspire_orchestrator.services.blueprint.semantic_edges.taxonomy import (
        EdgeProposal, EdgeType, EntityType, LocalityScope,
    )

    UID_SUITE = "00000000-0000-0000-0000-000000000001"
    UID_PROJECT = "00000000-0000-0000-0000-000000000002"

    def make_edge(src="p-1", dst="o-1"):
        return EdgeProposal(
            edge_type=EdgeType.ANNOTATED_BY,
            suite_id=UID_SUITE, project_id=UID_PROJECT,
            src_entity_id=src, src_entity_type=EntityType.PRIMITIVE,
            dst_entity_id=dst, dst_entity_type=EntityType.OCR_SPAN,
            locality_scope=LocalityScope.SAME_SHEET,
            spatial_distance=10.0, confidence=0.85,
            evidence={"ocr_text": "A-101", "distance_px": 10,
                      "alignment": "horizontal", "zone": "title_block"},
            provenance={
                "extractor": "test", "wave": "20.14.5L", "pipeline_version": "1.0",
                "sheet_id": "sheet-1", "generator_window": "test",
                "bbox": [0, 0, 10, 10],
            },
            extraction_method="test_v1",
        )

    def make_report(n=3):
        edges = [make_edge(f"p-{i}", f"o-{i}") for i in range(n)]
        truth = {(f"p-{i}", f"o-{i}", EdgeType.ANNOTATED_BY):
                 AuditVerdict.TRUE_POSITIVE for i in range(n)}
        return audit_edges(
            edges, verifier=OracleVerifier(truth),
            generator_name="annotated_by", generator_version="1.0",
            project_id=UID_PROJECT, sample_size=n,
        )

    results: dict[str, str] = {}

    # 1) Enum coverage
    try:
        assert {a.value for a in WriterAuthority} == {
            "drew_pipeline", "manual_cli", "runtime_reconciler", "l6_smoke_test"
        }
        assert ALLOWED_WRITER_AUTHORITIES == frozenset(WriterAuthority)
        results["enum_matches_db_check"] = "PASS"
    except Exception as e:
        results["enum_matches_db_check"] = f"FAIL: {e}"

    # 2) Row projection has all DB columns
    try:
        report = make_report()
        row = report_to_db_row(
            report, suite_id=UID_SUITE,
            writer_authority=WriterAuthority.DREW_PIPELINE,
        )
        required = {
            "audit_id", "suite_id", "office_id", "project_id",
            "generator_name", "generator_version", "verifier_name",
            "verifier_version", "sample_size", "total_edges", "sample_seed",
            "threshold", "passes_threshold", "failing_types",
            "verdict_counts", "per_type_precision", "audited_edges",
            "writer_authority", "created_by",
        }
        assert required <= set(row.keys()), f"missing: {required - set(row.keys())}"
        results["row_projection_columns"] = "PASS"
    except Exception as e:
        results["row_projection_columns"] = f"FAIL: {e}"
        traceback.print_exc()

    # 3) Compact audited_edges projection
    try:
        report = make_report()
        row = report_to_db_row(report, suite_id=UID_SUITE,
                               writer_authority=WriterAuthority.DREW_PIPELINE)
        assert isinstance(row["audited_edges"], list)
        sample = row["audited_edges"][0]
        assert set(sample.keys()) == {
            "edge_type", "src", "dst", "replay_hash",
            "confidence", "verdict", "rationale",
        }
        results["compact_audited_edges"] = "PASS"
    except Exception as e:
        results["compact_audited_edges"] = f"FAIL: {e}"

    # 4) Bad authority rejected (fail-closed)
    try:
        report = make_report()
        try:
            report_to_db_row(report, suite_id=UID_SUITE,
                             writer_authority="rogue")
            results["bad_authority_rejected"] = "FAIL: no exception raised"
        except UnauthorizedAuditWrite:
            results["bad_authority_rejected"] = "PASS"
    except Exception as e:
        results["bad_authority_rejected"] = f"FAIL: {e}"

    # 5) persist_audit_report wrapper rejects bad authority
    try:
        report = make_report()
        persistor = InMemoryAuditReceiptPersistor()
        try:
            persist_audit_report(report, persistor, suite_id=UID_SUITE,
                                 writer_authority="rogue")
            results["wrapper_rejects_bad_authority"] = "FAIL"
        except UnauthorizedAuditWrite:
            assert persistor.rows == []
            results["wrapper_rejects_bad_authority"] = "PASS"
    except Exception as e:
        results["wrapper_rejects_bad_authority"] = f"FAIL: {e}"

    # 6) All 4 authorities persist successfully
    try:
        report = make_report()
        persistor = InMemoryAuditReceiptPersistor()
        for authority in WriterAuthority:
            persist_audit_report(report, persistor, suite_id=UID_SUITE,
                                 writer_authority=authority)
        # All 4 attempts hit same audit_id → idempotency keeps it at 1
        assert len(persistor.rows) == 1
        # But the row's last writer_authority is whichever was written first
        results["all_authorities_can_persist"] = "PASS"
    except Exception as e:
        results["all_authorities_can_persist"] = f"FAIL: {e}"

    # 7) Idempotency
    try:
        persistor = InMemoryAuditReceiptPersistor()
        report = make_report()
        persistor.persist(report, suite_id=UID_SUITE,
                          writer_authority=WriterAuthority.DREW_PIPELINE)
        persistor.persist(report, suite_id=UID_SUITE,
                          writer_authority=WriterAuthority.DREW_PIPELINE)
        assert len(persistor.rows) == 1
        results["idempotent_persist"] = "PASS"
    except Exception as e:
        results["idempotent_persist"] = f"FAIL: {e}"

    # 8) Rejection logged for forensics
    try:
        persistor = InMemoryAuditReceiptPersistor()
        report = make_report()
        try:
            persistor.persist(report, suite_id=UID_SUITE,
                              writer_authority="evil")
        except UnauthorizedAuditWrite:
            pass
        assert len(persistor.rejected) == 1
        results["rejection_logged"] = "PASS"
    except Exception as e:
        results["rejection_logged"] = f"FAIL: {e}"

    # 9) Different reports → different audit_ids
    try:
        persistor = InMemoryAuditReceiptPersistor()
        r1 = make_report(n=2)
        r2 = make_report(n=4)
        assert r1.audit_id != r2.audit_id
        persistor.persist(r1, suite_id=UID_SUITE,
                          writer_authority=WriterAuthority.DREW_PIPELINE)
        persistor.persist(r2, suite_id=UID_SUITE,
                          writer_authority=WriterAuthority.DREW_PIPELINE)
        assert len(persistor.rows) == 2
        results["distinct_reports_distinct_ids"] = "PASS"
    except Exception as e:
        results["distinct_reports_distinct_ids"] = f"FAIL: {e}"

    # Report
    print("=== L-7 audit persistor smoke ===")
    n_pass = sum(1 for v in results.values() if v == "PASS")
    for name, v in results.items():
        print(f"  [{v[:4]}] {name}")
    print(f"\n{n_pass}/{len(results)} pass")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
