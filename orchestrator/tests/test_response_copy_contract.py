from __future__ import annotations

from aspire_orchestrator.services.retrieval_verifier import verify_retrieval_grounding
from pathlib import Path


def test_respond_module_no_generic_processed_phrase() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "aspire_orchestrator" / "nodes" / "respond.py"
    source = root.read_text(encoding="utf-8")
    assert "I've processed your request." not in source
    assert "I processed your request." not in source


def test_weak_grounding_uses_graceful_constrained_prompt() -> None:
    report = verify_retrieval_grounding(
        intent_type="question",
        retrieval_status="degraded",
        grounding_score=0.2,
        agent_id="ava",
        conflict_flags=[],
    )
    assert report.passed is False
    assert "not enough grounded context" not in report.fallback_text.lower()
    assert "?" in report.fallback_text
