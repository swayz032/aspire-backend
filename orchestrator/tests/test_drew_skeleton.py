"""Drew Wave 1A skeleton tests — imports + dispatch + fail-closed on unknown task.

Updated for Wave 3 SEE flip: SEE is no longer a stub (returns status='error' on
empty payload, not 'stub'). Only REASON and PROCURE remain stubs.
"""
from __future__ import annotations


def test_drew_imports() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    assert drew.actor == "drew"


def test_drew_run_agentic_loop_ingest_with_empty_payload_errors() -> None:
    """INGEST is now real (Wave 2A) — empty payload returns error, not stub."""
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("INGEST", {}, "test-correlation-id")
    # Real INGEST validates required keys and returns error on missing keys.
    assert result["status"] in ("error", "failed")
    assert result["stage"] == "ingest"


def test_drew_unknown_task_denies() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("BOGUS", {}, "test-correlation-id")
    assert result["status"] == "deny"


def test_drew_remaining_stub_stages() -> None:
    """PROCURE remains a stub in Wave 4.

    INGEST (Wave 2A), CLASSIFY (Wave 2A), SEE (Wave 3), and REASON (Wave 4) are
    real — they validate payloads and return status='error' or 'ok', not 'stub'.
    """
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    for task, expected_stage in [
        ("PROCURE", "procure"),
    ]:
        result = drew.run_agentic_loop(task, {}, "test-correlation-id")
        assert result["status"] == "stub"
        assert result["stage"] == expected_stage


def test_drew_reason_with_empty_payload_errors() -> None:
    """REASON is now real (Wave 4) — empty payload returns error, not stub."""
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("REASON", {}, "test-correlation-id")
    assert result["status"] == "error"
    assert result["stage"] == "reason"


def test_drew_see_with_empty_payload_errors() -> None:
    """SEE is now real (Wave 3) — empty payload returns error, not stub."""
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("SEE", {}, "test-correlation-id")
    assert result["status"] == "error"
    assert result["stage"] == "see"
