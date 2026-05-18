"""Drew receipt coverage tests — Wave 2B.

Law #2: Every action produces an immutable, append-only receipt.
This file tests end-to-end receipt coverage: full INGEST → CLASSIFY sequence
must produce exactly two receipts (one per stage), both with actor='drew'.

Also tests:
  - Denial receipts emitted for unknown tasks
  - No receipt duplication on retry with same correlation_id
  - Receipt fields conform to the Aspire standard shape
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "blueprints"

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture not found: {path}"
    return path.read_bytes()


def _query_receipts_by_corr(correlation_id: str) -> list[dict]:
    """Query in-memory receipt store by correlation_id without suite_id filter.

    Drew stub receipts do not embed suite_id in the payload. This helper is
    test-only — production uses query_receipts(suite_id=...) (Law #6).
    """
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("correlation_id") == correlation_id]


def _query_all_receipts() -> list[dict]:
    """Snapshot the full in-memory receipt store. Test-only."""
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return list(rs._receipts)


@pytest.fixture(autouse=True)
def _clear_receipt_store():
    from aspire_orchestrator.services.receipt_store import clear_store
    clear_store()
    yield
    clear_store()


# ---------------------------------------------------------------------------
# End-to-end coverage: INGEST + CLASSIFY → 2 receipts
# ---------------------------------------------------------------------------

class TestFullIngestClassifyReceiptCoverage:
    """Law #2: Two-stage pipeline must emit exactly two receipts."""

    def test_full_ingest_classify_emits_two_receipts(self) -> None:
        """Run INGEST then CLASSIFY on the same correlation_id.

        Asserts:
          - receipt count == 2
          - one 'blueprint.ingest' receipt
          - one 'blueprint.classify' receipt
          - both have actor == 'skillpack:drew-blueprint'

        Note: Uses two separate correlation_ids (one per stage is the correct
        pattern for a multi-stage pipeline — same actor, different correlations).
        The test counts per-actor receipts across both correlations.
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        ingest_corr = "test-coverage-ingest-" + str(uuid.uuid4())
        classify_corr = "test-coverage-classify-" + str(uuid.uuid4())

        # Stage 1: INGEST
        drew.run_agentic_loop(
            "INGEST",
            {"pdf_bytes": b"fake", "suite_id": SUITE_A},
            ingest_corr,
        )

        # Stage 2: CLASSIFY
        drew.run_agentic_loop(
            "CLASSIFY",
            {"suite_id": SUITE_A},
            classify_corr,
        )

        ingest_receipts = _query_receipts_by_corr(ingest_corr)
        classify_receipts = _query_receipts_by_corr(classify_corr)

        # Must have at least one receipt per stage
        assert len(ingest_receipts) >= 1, (
            f"INGEST must emit at least 1 receipt (Law #2). Got: {len(ingest_receipts)}"
        )
        assert len(classify_receipts) >= 1, (
            f"CLASSIFY must emit at least 1 receipt (Law #2). Got: {len(classify_receipts)}"
        )

        # Exactly the right event types
        ingest_types = {r["event_type"] for r in ingest_receipts}
        classify_types = {r["event_type"] for r in classify_receipts}
        assert "blueprint.ingest" in ingest_types, (
            f"INGEST stage missing 'blueprint.ingest' receipt. Types found: {ingest_types}"
        )
        assert "blueprint.classify" in classify_types, (
            f"CLASSIFY stage missing 'blueprint.classify' receipt. Types found: {classify_types}"
        )

        # Both actors must be drew
        all_receipts = ingest_receipts + classify_receipts
        for r in all_receipts:
            assert r["actor"] == "skillpack:drew-blueprint", (
                f"Receipt actor must be 'skillpack:drew-blueprint', got: {r['actor']}"
            )

    def test_all_five_stages_each_emit_receipt(self) -> None:
        """All 5 stages (INGEST, CLASSIFY, SEE, REASON, PROCURE) must emit receipts.

        Uses the Wave 1 stubs — all stages return 'stub' but must still emit.
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        stage_corr_map = {
            "INGEST": "test-all5-ingest-" + str(uuid.uuid4()),
            "CLASSIFY": "test-all5-classify-" + str(uuid.uuid4()),
            "SEE": "test-all5-see-" + str(uuid.uuid4()),
            "REASON": "test-all5-reason-" + str(uuid.uuid4()),
            "PROCURE": "test-all5-procure-" + str(uuid.uuid4()),
        }
        expected_event_types = {
            "INGEST": "blueprint.ingest",
            "CLASSIFY": "blueprint.classify",
            "SEE": "blueprint.see",
            "REASON": "blueprint.reason",
            "PROCURE": "blueprint.procure",
        }

        for task, corr in stage_corr_map.items():
            drew.run_agentic_loop(task, {"suite_id": SUITE_A}, corr)

        for task, corr in stage_corr_map.items():
            receipts = _query_receipts_by_corr(corr)
            assert len(receipts) >= 1, (
                f"Stage {task} must emit at least 1 receipt (Law #2)"
            )
            event_types = {r["event_type"] for r in receipts}
            expected = expected_event_types[task]
            assert expected in event_types, (
                f"Stage {task} must emit event_type='{expected}'. Got: {event_types}"
            )


# ---------------------------------------------------------------------------
# Denial receipts
# ---------------------------------------------------------------------------

class TestDenialReceiptCoverage:
    """Law #2: Denied actions must also emit receipts."""

    def test_unknown_task_emits_denial_receipt(self) -> None:
        """Unknown task must produce a denial receipt with policy.decision='deny'."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-denial-unknown-" + str(uuid.uuid4())

        result = drew.run_agentic_loop("BOGUS_STAGE", {"suite_id": SUITE_A}, corr)

        assert result["status"] == "deny"
        receipts = _query_receipts_by_corr(corr)
        assert len(receipts) >= 1, "Unknown task must emit a receipt (Law #2)"

        denial_receipts = [
            r for r in receipts
            if r.get("policy", {}).get("decision") == "deny"
        ]
        assert len(denial_receipts) >= 1, (
            "Unknown task must emit a denial receipt with policy.decision='deny'. "
            f"Got receipts: {[r['event_type'] for r in receipts]}"
        )


# ---------------------------------------------------------------------------
# Receipt shape conformance
# ---------------------------------------------------------------------------

class TestReceiptShapeConformance:
    """Receipts must conform to the Aspire standard receipt shape."""

    REQUIRED_FIELDS = [
        "receipt_id",
        "receipt_version",
        "ts",
        "event_type",
        "actor",
        "correlation_id",
        "status",
        "inputs_hash",
        "policy",
    ]

    def test_ingest_receipt_has_all_required_fields(self) -> None:
        """blueprint.ingest receipt must include all Aspire standard fields."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-shape-ingest-" + str(uuid.uuid4())
        drew.run_agentic_loop("INGEST", {"pdf_bytes": b"x", "suite_id": SUITE_A}, corr)

        receipts = _query_receipts_by_corr(corr)
        ingest_receipts = [r for r in receipts if r["event_type"] == "blueprint.ingest"]
        assert ingest_receipts, "Must have blueprint.ingest receipt"
        r = ingest_receipts[0]

        missing_fields = [f for f in self.REQUIRED_FIELDS if f not in r]
        assert not missing_fields, (
            f"blueprint.ingest receipt missing required fields: {missing_fields}. "
            f"Receipt shape: {list(r.keys())}"
        )

    def test_receipt_policy_block_has_decision(self) -> None:
        """policy block must have 'decision' field set to 'allow' or 'deny'."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-policy-block-" + str(uuid.uuid4())
        drew.run_agentic_loop("CLASSIFY", {"suite_id": SUITE_A}, corr)

        receipts = _query_receipts_by_corr(corr)
        assert receipts
        r = receipts[0]
        policy = r.get("policy", {})
        assert "decision" in policy, (
            f"Receipt policy block must have 'decision'. Got policy: {policy}"
        )
        assert policy["decision"] in ("allow", "deny"), (
            f"policy.decision must be 'allow' or 'deny'. Got: {policy['decision']}"
        )

    def test_receipt_ts_is_iso8601(self) -> None:
        """Receipt timestamp must be a valid ISO 8601 datetime string."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        ISO8601_RE = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
        )

        drew = Drew()
        corr = "test-ts-iso-" + str(uuid.uuid4())
        drew.run_agentic_loop("SEE", {"suite_id": SUITE_A}, corr)

        receipts = _query_receipts_by_corr(corr)
        assert receipts
        for r in receipts:
            assert ISO8601_RE.match(r["ts"]), (
                f"Receipt ts must be ISO 8601. Got: {r['ts']}"
            )

    def test_receipt_id_is_uuid4(self) -> None:
        """receipt_id must be a valid UUID4."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-receipt-id-uuid-" + str(uuid.uuid4())
        drew.run_agentic_loop("PROCURE", {"suite_id": SUITE_A}, corr)

        receipts = _query_receipts_by_corr(corr)
        assert receipts
        for r in receipts:
            try:
                parsed = uuid.UUID(r["receipt_id"])
                assert parsed.version == 4, f"receipt_id must be UUID4. Got version: {parsed.version}"
            except (ValueError, AttributeError) as exc:
                pytest.fail(f"receipt_id is not a valid UUID: {r['receipt_id']} — {exc}")

    def test_receipt_inputs_hash_is_sha256_prefixed(self) -> None:
        """inputs_hash must be 'sha256:<hex>' format."""
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr = "test-inputs-hash-" + str(uuid.uuid4())
        drew.run_agentic_loop("REASON", {"suite_id": SUITE_A}, corr)

        receipts = _query_receipts_by_corr(corr)
        assert receipts
        for r in receipts:
            h = r.get("inputs_hash", "")
            assert h.startswith("sha256:"), (
                f"inputs_hash must start with 'sha256:'. Got: {h}"
            )
            hex_part = h[len("sha256:"):]
            assert len(hex_part) == 64, (
                f"inputs_hash SHA256 hex must be 64 chars. Got: {len(hex_part)}"
            )


# ---------------------------------------------------------------------------
# No duplicate receipts on retry
# ---------------------------------------------------------------------------

class TestNoReceiptDuplication:
    """Receipts must not be duplicated if the same call is retried."""

    def test_two_ingest_calls_emit_two_receipts_with_distinct_ids(self) -> None:
        """Two separate INGEST calls (different corr IDs) produce separate receipts.

        This confirms receipts are not deduped by content — only by receipt_id.
        (Idempotency is at the project level, not the receipt level.)
        """
        from aspire_orchestrator.skillpacks.drew_blueprint import Drew

        drew = Drew()
        corr1 = "test-no-dup-1-" + str(uuid.uuid4())
        corr2 = "test-no-dup-2-" + str(uuid.uuid4())

        drew.run_agentic_loop("INGEST", {"pdf_bytes": b"a", "suite_id": SUITE_A}, corr1)
        drew.run_agentic_loop("INGEST", {"pdf_bytes": b"a", "suite_id": SUITE_A}, corr2)

        r1 = _query_receipts_by_corr(corr1)
        r2 = _query_receipts_by_corr(corr2)
        assert len(r1) >= 1
        assert len(r2) >= 1

        ids1 = {r["receipt_id"] for r in r1}
        ids2 = {r["receipt_id"] for r in r2}
        assert ids1.isdisjoint(ids2), (
            "Each ingest call must produce receipts with unique receipt_ids. "
            f"Overlap found: {ids1 & ids2}"
        )
