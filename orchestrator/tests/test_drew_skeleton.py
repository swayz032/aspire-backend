"""Drew Wave 1A skeleton tests — imports + dispatch + fail-closed on unknown task."""
from __future__ import annotations


def test_drew_imports() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    assert drew.actor == "drew"


def test_drew_run_agentic_loop_stub_returns() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("INGEST", {}, "test-correlation-id")
    assert result["status"] == "stub"
    assert result["stage"] == "ingest"


def test_drew_unknown_task_denies() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    result = drew.run_agentic_loop("BOGUS", {}, "test-correlation-id")
    assert result["status"] == "deny"


def test_drew_all_stages_stub() -> None:
    from aspire_orchestrator.skillpacks.drew_blueprint import Drew

    drew = Drew()
    for task, expected_stage in [
        ("INGEST", "ingest"),
        ("CLASSIFY", "classify"),
        ("SEE", "see"),
        ("REASON", "reason"),
        ("PROCURE", "procure"),
    ]:
        result = drew.run_agentic_loop(task, {}, "test-correlation-id")
        assert result["status"] == "stub"
        assert result["stage"] == expected_stage
