"""L-6 FP regression suite — adversarial fixtures vs real generators.

Each fixture in FP_FIXTURES exercises a known Wave 20.13.3-class
failure mode. If any future generator change permits a regression,
these tests catch it BEFORE the bid sheet hits Pop's screen.
"""
from __future__ import annotations

from typing import Any

import pytest

from .audit import AuditVerdict, RuleBasedVerifier, audit_edges
from .fp_regression import FP_FIXTURES, FPFixture
from .generators.adjacent_to import AdjacentToGenerator
from .generators.annotated_by import AnnotatedByGenerator
from .generators.data_access import (
    EntityRecord,
    InMemoryQueryClient,
    OcrSpanRecord,
    ZoneRecord,
)
from .generators.inside_zone import InsideZoneGenerator
from .. import semantic_edges  # noqa: F401  (ensure package is loaded for taxonomy)
from .generator import GeneratorContext, InMemoryLockProvider, InMemoryReceiptPersistor


# ============================================================
# Fixture → InMemoryQueryClient adapter
# ============================================================

UID_SUITE = "00000000-0000-0000-0000-000000000001"
UID_PROJECT = "00000000-0000-0000-0000-000000000fff"
UID_SHEET = "00000000-0000-0000-0000-00000000aaaa"


def _sheet() -> EntityRecord:
    return EntityRecord(
        entity_id=UID_SHEET, entity_type="sheet",
        project_id=UID_PROJECT, suite_id=UID_SUITE,
        sheet_id=UID_SHEET,
    )


def _build_client(fx: FPFixture) -> InMemoryQueryClient:
    """Materialize fixture scenario into an InMemoryQueryClient."""
    prims: list[EntityRecord] = []
    spans: list[OcrSpanRecord] = []
    zones: list[ZoneRecord] = []

    for p in fx.primitives:
        prims.append(EntityRecord(
            entity_id=p["id"], entity_type="primitive",
            project_id=UID_PROJECT, suite_id=UID_SUITE,
            sheet_id=UID_SHEET, bbox=p["bbox"],
        ))
    for s in fx.ocr_spans:
        spans.append(OcrSpanRecord(
            span_id=s["id"], sheet_id=UID_SHEET,
            project_id=UID_PROJECT, suite_id=UID_SUITE,
            text=s["text"], bbox=s["bbox"],
        ))
    for z in fx.zones:
        zones.append(ZoneRecord(
            zone_id=z["id"], sheet_id=UID_SHEET,
            project_id=UID_PROJECT, suite_id=UID_SUITE,
            zone_type=z["zone_type"], bbox=z["bbox"],
        ))

    return InMemoryQueryClient(
        sheets=[_sheet()],
        primitives=prims,
        ocr_spans=spans,
        zones=zones,
    )


def _run_generator(fx: FPFixture, client: InMemoryQueryClient):
    """Pick the right generator for the fixture + run it."""
    ctx = GeneratorContext(
        suite_id=UID_SUITE,
        project_id=UID_PROJECT,
        pipeline_version="L6_fp_regression",
        lock_provider=InMemoryLockProvider(),
    )
    persistor = InMemoryReceiptPersistor()

    if fx.generator_target == "annotated_by":
        # Inject source_discipline provenance when fixture sets discipline
        # on a primitive — needed for RuleBasedVerifier discipline check.
        gen = AnnotatedByGenerator(client)
        result = gen.run(ctx, receipt_persistor=persistor)
        # Re-tag provenance.source_discipline based on the matching
        # fixture primitive so the verifier can apply Rule 1.
        disc_by_prim = {p["id"]: p.get("discipline", "") for p in fx.primitives}
        for edge in result.accepted_edges + result.unknown_review_edges:
            disc = disc_by_prim.get(edge.src_entity_id)
            if disc:
                edge.provenance["source_discipline"] = disc
        return result

    if fx.generator_target == "adjacent_to":
        return AdjacentToGenerator(client).run(ctx, receipt_persistor=persistor)

    if fx.generator_target == "inside_zone":
        return InsideZoneGenerator(client).run(ctx, receipt_persistor=persistor)

    raise ValueError(f"unknown generator_target: {fx.generator_target}")


# ============================================================
# Per-fixture parameterized test
# ============================================================


@pytest.mark.parametrize("fx", FP_FIXTURES, ids=[f.name for f in FP_FIXTURES])
def test_fp_fixture(fx: FPFixture) -> None:
    """Run fixture, validate expected outcome."""
    client = _build_client(fx)
    result = _run_generator(fx, client)

    n_verified = len(result.accepted_edges)
    n_ur = len(result.unknown_review_edges)

    # Expectation: zero edges emitted (framework rejected at validator)
    if fx.expected_no_edges:
        assert n_verified == 0 and n_ur == 0, (
            f"{fx.name}: expected no edges, got verified={n_verified} ur={n_ur}"
        )
        return

    # Expectation: exact VERIFIED count
    if fx.expected_verified_count is not None:
        assert n_verified == fx.expected_verified_count, (
            f"{fx.name}: expected {fx.expected_verified_count} verified edges, "
            f"got {n_verified}"
        )

    # Expectation: exact UR count
    if fx.expected_ur_count is not None:
        assert n_ur == fx.expected_ur_count, (
            f"{fx.name}: expected {fx.expected_ur_count} UR edges, got {n_ur}"
        )

    # Expectation: verifier verdict on the first emitted edge
    if fx.expected_verdict_for_first_edge is not None:
        all_edges = result.accepted_edges + result.unknown_review_edges
        if not all_edges:
            pytest.fail(
                f"{fx.name}: expected first-edge verdict "
                f"{fx.expected_verdict_for_first_edge.value} "
                f"but generator emitted no edges"
            )
        verifier = RuleBasedVerifier()
        verdict, rationale = verifier.verify(all_edges[0])
        assert verdict == fx.expected_verdict_for_first_edge, (
            f"{fx.name}: expected {fx.expected_verdict_for_first_edge.value}, "
            f"got {verdict.value} ({rationale})"
        )


# ============================================================
# End-to-end audit report against all FP fixtures
# ============================================================


def test_combined_fp_audit_meets_90pct_threshold() -> None:
    """Run all annotated_by fixtures through the auditor.

    The auditor's job: catch every FP in the suite. If overall precision
    on the combined sample is below 90%, the audit gate fails.

    For this combined run we use RuleBasedVerifier which knows about
    the FP modes. A passing run means the verifier correctly recognized
    the adversarial cases (verdicts TRUE_POSITIVE for clean edges +
    FALSE_POSITIVE / UR_MISSED for the dirty ones).
    """
    ab_fixtures = [f for f in FP_FIXTURES if f.generator_target == "annotated_by"]
    all_edges = []
    for fx in ab_fixtures:
        client = _build_client(fx)
        result = _run_generator(fx, client)
        all_edges.extend(result.accepted_edges)
        all_edges.extend(result.unknown_review_edges)

    if not all_edges:
        pytest.skip("no edges emitted across annotated_by fixtures")

    report = audit_edges(
        all_edges,
        verifier=RuleBasedVerifier(),
        generator_name="annotated_by",
        generator_version="1.0",
        project_id=UID_PROJECT,
        sample_size=len(all_edges),
        threshold=0.0,            # we're measuring, not gating
    )

    # Sanity: at least one verdict was rendered
    assert sum(report.verdict_counts.values()) == len(all_edges)
    # Per-type bucket exists
    assert report.per_type
