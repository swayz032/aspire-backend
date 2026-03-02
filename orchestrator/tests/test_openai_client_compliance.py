"""Compliance tests for shared OpenAI adapter behavior."""

from __future__ import annotations

from aspire_orchestrator.services import openai_client as oc


def test_profile_resolution_uses_available_model(monkeypatch) -> None:
    oc._MODEL_PROBE_CACHE.clear()
    oc._MODEL_PROBE_CACHE.update({
        "gpt-5.2": False,
        "gpt-5": True,
        "gpt-5-mini": True,
    })
    resolved, fallback_used = oc._resolve_model_for_profile("primary_reasoner", "gpt-5.2")
    assert resolved == "gpt-5"
    assert fallback_used is True


def test_model_probe_status_exposes_health_cache() -> None:
    oc._MODEL_PROBE_CACHE.clear()
    oc._PROFILE_PROBE_CACHE.clear()
    oc._MODEL_PROBE_CACHE.update({"gpt-5-mini": True})
    oc._PROFILE_PROBE_CACHE.update({"cheap_classifier": "gpt-5-mini"})

    status = oc.get_model_probe_status()
    assert status["healthy"] is True
    assert status["models"]["gpt-5-mini"] is True
    assert status["profiles"]["cheap_classifier"] == "gpt-5-mini"


def test_reason_code_mapping_timeout() -> None:
    err = TimeoutError("upstream timeout")
    assert oc._reason_code_for_error(err) == "UPSTREAM_TIMEOUT"

