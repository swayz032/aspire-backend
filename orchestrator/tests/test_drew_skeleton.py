"""Drew skeleton tests — imports + dispatch + fail-closed on unknown task.

Updated in Wave 5 to reflect that all stages are now real implementations:
  Wave 2A: INGEST + CLASSIFY (real)
  Wave 3:  SEE (real on wave-4 branch; stub on dev/blueprint-engine base)
  Wave 4:  REASON (real on wave-4 branch; stub on dev/blueprint-engine base)
  Wave 5:  PROCURE (real — this wave)

All real stages return {"status": "error", "stage": <name>} when required
payload keys are missing (Law #3 fail-closed). The old "stub" status is retired.
"""
from __future__ import annotations


def test_drew_imports() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    assert drew.actor == "drew"


def test_drew_run_agentic_loop_validates_payload() -> None:
    """Real INGEST with empty payload returns error (missing pdf_bytes), not stub."""
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("INGEST", {}, "test-correlation-id")
    # Wave 2A: INGEST is real — missing pdf_bytes returns error, not stub
    assert result["status"] == "error"
    assert result["stage"] == "ingest"
    assert "pdf_bytes" in result["reason"]


def test_drew_unknown_task_denies() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("BOGUS", {}, "test-correlation-id")
    assert result["status"] == "deny"


def test_drew_all_stages_fail_closed_on_empty_payload() -> None:
    """All real stages fail-closed (return error) when required payload keys are missing.

    Wave 5 update: PROCURE stub replaced with real implementation.
    All stages now validate payload keys before executing (Law #3).
    """
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()

    # INGEST — requires pdf_bytes, suite_id, office_id
    result = drew.run_agentic_loop("INGEST", {}, "test-correlation-id")
    assert result["status"] == "error"
    assert result["stage"] == "ingest"

    # CLASSIFY — requires project_id, suite_id
    result = drew.run_agentic_loop("CLASSIFY", {}, "test-correlation-id")
    assert result["status"] == "error"
    assert result["stage"] == "classify"

    # PROCURE — requires project_id, suite_id (Wave 5 real implementation)
    result = drew.run_agentic_loop("PROCURE", {}, "test-correlation-id")
    assert result["status"] == "error"
    assert result["stage"] == "procure"


def test_drew_dispatcher_routes_all_five_stages() -> None:
    """Dispatcher recognizes all 5 stage names (does not return deny for valid tasks)."""
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    for task in ("INGEST", "CLASSIFY", "SEE", "REASON", "PROCURE"):
        result = drew.run_agentic_loop(task, {}, "test-correlation-id")
        # Should not return "deny" — only unknown tasks get "deny"
        assert result.get("status") != "deny", f"Task {task} returned deny unexpectedly"
        # All should return the correct stage name
        assert "stage" in result
