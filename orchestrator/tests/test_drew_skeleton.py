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


def test_drew_procure_with_empty_payload_errors() -> None:
    """PROCURE is now real (Wave 5) — empty payload returns error, not stub.

    All 5 stages (INGEST/CLASSIFY/SEE/REASON/PROCURE) are real implementations.
    Pipeline is complete; no remaining stubs.
    """
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("PROCURE", {}, "test-correlation-id")
    assert result["status"] == "error"
    assert result["stage"] == "procure"


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
