"""Shared OpenAI client helpers (Responses-first with optional chat fallback).

Centralizes model-call behavior across orchestrator nodes/services:
  - Responses API preferred for GPT-5 family and modern OpenAI usage.
  - Optional fallback to Chat Completions for compatibility.
  - Reasoning-model role rules: developer role, no temperature.
  - Runtime model probing + deterministic profile fallback support.
  - Unified telemetry for request outcomes and model fallback transitions.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai import AsyncOpenAI, OpenAI

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.services.metrics import METRICS

logger = logging.getLogger(__name__)

_MODEL_PROBE_CACHE: dict[str, bool] = {}
_PROFILE_PROBE_CACHE: dict[str, str] = {}

_DEFAULT_PROFILE_FALLBACKS: dict[str, list[str]] = {
    "primary_reasoner": ["gpt-5.2", "gpt-5", "gpt-5-mini"],
    "high_risk_guard": ["gpt-5.2", "gpt-5"],
    "fast_general": ["gpt-5", "gpt-5-mini"],
    "cheap_classifier": ["gpt-5-mini", "gpt-5"],
}


class OpenAIAdapterError(RuntimeError):
    """Normalized adapter error with reason code for upstream mapping."""

    def __init__(self, reason_code: str, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.cause = cause


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3"))


def _should_allow_chat_fallback() -> bool:
    raw = os.environ.get("ASPIRE_OPENAI_USE_CHAT_FALLBACK", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _model_fallback_map() -> dict[str, list[str]]:
    raw = os.environ.get("ASPIRE_MODEL_FALLBACK_MAP", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            normalized: dict[str, list[str]] = {}
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, list):
                        normalized[k] = [str(item) for item in v if str(item)]
            if normalized:
                return normalized
        except Exception:
            logger.warning("Invalid ASPIRE_MODEL_FALLBACK_MAP JSON, using defaults")
    return {
        "gpt-5.2": ["gpt-5", "gpt-5-mini"],
        "gpt-5": ["gpt-5-mini"],
        "gpt-5-mini": ["gpt-5"],
    }


def _profile_fallback_map() -> dict[str, list[str]]:
    raw = os.environ.get("ASPIRE_MODEL_FALLBACK_MAP", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                profile_map: dict[str, list[str]] = {}
                for profile, chain in parsed.items():
                    if isinstance(profile, str) and isinstance(chain, list):
                        normalized = [str(m).strip() for m in chain if str(m).strip()]
                        if normalized:
                            profile_map[profile] = normalized
                if profile_map:
                    return profile_map
        except Exception:
            logger.warning("Invalid ASPIRE_MODEL_FALLBACK_MAP JSON, using profile defaults")
    return dict(_DEFAULT_PROFILE_FALLBACKS)


def _candidate_models(primary_model: str) -> list[str]:
    models = [primary_model]
    for alt in _model_fallback_map().get(primary_model, []):
        if alt not in models:
            models.append(alt)
    return models


def _profile_for_model(model: str) -> str:
    if model == settings.router_model_reasoner:
        return "primary_reasoner"
    if model == settings.router_model_high_risk:
        return "high_risk_guard"
    if model == settings.router_model_general:
        return "fast_general"
    if model == settings.router_model_classifier:
        return "cheap_classifier"
    return "unmapped"


def _preferred_model_for_profile(profile: str) -> str:
    if profile == "primary_reasoner":
        return settings.router_model_reasoner
    if profile == "high_risk_guard":
        return settings.router_model_high_risk
    if profile == "fast_general":
        return settings.router_model_general
    if profile == "cheap_classifier":
        return settings.router_model_classifier
    return ""


def _is_model_available(model: str) -> bool:
    # Unknown model in cache means "untested", allow path and let runtime decide.
    return _MODEL_PROBE_CACHE.get(model, True)


def _resolve_model_for_profile(profile: str, preferred_model: str) -> tuple[str, bool]:
    chain = _profile_fallback_map().get(profile) or _candidate_models(preferred_model)
    if not chain and preferred_model:
        chain = [preferred_model]
    if preferred_model and preferred_model not in chain:
        chain.insert(0, preferred_model)

    for candidate in chain:
        if _is_model_available(candidate):
            fallback_used = candidate != preferred_model
            if fallback_used:
                METRICS.record_llm_model_fallback(
                    profile=profile,
                    from_model=preferred_model,
                    to_model=candidate,
                )
                logger.warning(
                    "Model fallback active profile=%s from=%s to=%s",
                    profile,
                    preferred_model,
                    candidate,
                )
            return candidate, fallback_used

    # Fail-safe: return preferred model and let runtime error map to MODEL_UNAVAILABLE.
    return preferred_model, False


def _normalize_messages(
    messages: list[dict[str, str]],
    *,
    reasoning_model: bool,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system" and reasoning_model:
            role = "developer"
        normalized.append({"role": role, "content": content})
    return normalized


def _extract_output_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    # Fallback parse from structured output payload if output_text is absent.
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for part in getattr(item, "content", []) or []:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def _reason_code_for_error(error: Exception | None) -> str:
    if error is None:
        return "MODEL_UNAVAILABLE"
    name = type(error).__name__.lower()
    msg = str(error).lower()
    if "timeout" in name or "timeout" in msg:
        return "UPSTREAM_TIMEOUT"
    if "model" in msg and ("not found" in msg or "does not exist" in msg or "404" in msg):
        return "MODEL_UNAVAILABLE"
    return "MODEL_UNAVAILABLE"


async def probe_models_startup() -> dict[str, Any]:
    """Probe configured model profiles at startup and cache availability.

    Returns structured probe result for readiness diagnostics.
    """
    api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "status": "no_api_key",
            "profiles": {},
            "models": {},
        }

    profile_to_primary = {
        "primary_reasoner": settings.router_model_reasoner,
        "high_risk_guard": settings.router_model_high_risk,
        "fast_general": settings.router_model_general,
        "cheap_classifier": settings.router_model_classifier,
    }

    profile_chain = _profile_fallback_map()
    models_to_probe: set[str] = set()
    for profile, primary in profile_to_primary.items():
        if primary:
            models_to_probe.add(primary)
        for alt in profile_chain.get(profile, []):
            if alt:
                models_to_probe.add(alt)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=settings.openai_base_url,
        timeout=min(float(settings.openai_timeout_seconds), 10.0),
    )

    _MODEL_PROBE_CACHE.clear()
    _PROFILE_PROBE_CACHE.clear()
    model_errors: dict[str, str] = {}

    async def _probe(model: str) -> None:
        try:
            resp = await client.responses.create(
                model=model,
                input=[{"role": "user", "content": "ping"}],
                max_output_tokens=4,
            )
            _MODEL_PROBE_CACHE[model] = bool(_extract_output_text(resp) or True)
        except Exception as e:
            _MODEL_PROBE_CACHE[model] = False
            model_errors[model] = type(e).__name__

    for model in sorted(models_to_probe):
        await _probe(model)

    profile_status: dict[str, dict[str, Any]] = {}
    for profile, primary in profile_to_primary.items():
        resolved, fallback_used = _resolve_model_for_profile(profile, primary)
        _PROFILE_PROBE_CACHE[profile] = resolved
        profile_status[profile] = {
            "primary": primary,
            "resolved": resolved,
            "fallback_used": fallback_used,
            "available": _MODEL_PROBE_CACHE.get(resolved, False),
        }

    any_available = any(_MODEL_PROBE_CACHE.values())
    return {
        "status": "ok" if any_available else "failed",
        "profiles": profile_status,
        "models": dict(_MODEL_PROBE_CACHE),
        "errors": model_errors,
    }


def get_model_probe_status() -> dict[str, Any]:
    """Return model probe cache for readiness endpoint."""
    return {
        "models": dict(_MODEL_PROBE_CACHE),
        "profiles": dict(_PROFILE_PROBE_CACHE),
        "healthy": any(_MODEL_PROBE_CACHE.values()) if _MODEL_PROBE_CACHE else False,
    }


def parse_json_text(raw_text: str) -> dict[str, Any]:
    if not raw_text.strip():
        return {}
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return {}
    return {}


async def generate_text_async(
    *,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_seconds: float = 30.0,
    max_output_tokens: int = 1024,
    temperature: float | None = None,
    prefer_responses_api: bool = True,
    model_profile: str | None = None,
) -> str:
    """Generate text from a chat-style message list."""
    profile = model_profile or _profile_for_model(model)
    resolved_model, _ = _resolve_model_for_profile(profile, model)
    reasoning_model = _is_reasoning_model(resolved_model)
    normalized = _normalize_messages(messages, reasoning_model=reasoning_model)
    effective_temp = None if reasoning_model else temperature

    async def _via_responses(call_model: str) -> str:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": call_model,
            "input": normalized,
            "max_output_tokens": max_output_tokens,
        }
        if effective_temp is not None:
            kwargs["temperature"] = effective_temp
        response = await client.responses.create(**kwargs)
        return _extract_output_text(response)

    async def _via_chat(call_model: str) -> str:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": call_model,
            "messages": normalized,
            "max_completion_tokens": max_output_tokens,
        }
        if effective_temp is not None:
            kwargs["temperature"] = effective_temp
        response = await client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip() if response.choices else ""

    last_error: Exception | None = None
    for call_model in _candidate_models(resolved_model):
        if prefer_responses_api:
            try:
                result = await _via_responses(call_model)
                METRICS.record_llm_request(
                    endpoint="responses",
                    resolved_model=call_model,
                    outcome="ok",
                )
                return result
            except Exception as e:
                last_error = e
                if not _should_allow_chat_fallback():
                    METRICS.record_llm_request(
                        endpoint="responses",
                        resolved_model=call_model,
                        outcome="failed",
                    )
                    continue
                logger.warning(
                    "Responses API failed for model=%s; trying chat fallback (%s)",
                    call_model, type(e).__name__,
                )
                try:
                    result = await _via_chat(call_model)
                    METRICS.record_llm_request(
                        endpoint="chat_fallback",
                        resolved_model=call_model,
                        outcome="ok",
                    )
                    return result
                except Exception as chat_e:
                    last_error = chat_e
                    METRICS.record_llm_request(
                        endpoint="chat_fallback",
                        resolved_model=call_model,
                        outcome="failed",
                    )
                    logger.warning(
                        "Chat fallback failed for model=%s (%s)",
                        call_model, type(chat_e).__name__,
                    )
                    continue
        else:
            try:
                result = await _via_chat(call_model)
                METRICS.record_llm_request(
                    endpoint="chat",
                    resolved_model=call_model,
                    outcome="ok",
                )
                return result
            except Exception as e:
                last_error = e
                METRICS.record_llm_request(
                    endpoint="chat",
                    resolved_model=call_model,
                    outcome="failed",
                )
                continue

    if last_error:
        raise OpenAIAdapterError(
            _reason_code_for_error(last_error),
            f"OpenAI text generation failed for profile={profile} model={resolved_model}",
            cause=last_error,
        ) from last_error
    return ""


async def generate_json_async(
    *,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_seconds: float = 30.0,
    max_output_tokens: int = 1024,
    temperature: float | None = None,
    prefer_responses_api: bool = True,
    model_profile: str | None = None,
) -> dict[str, Any]:
    """Generate JSON by prompting model for JSON and parsing output robustly."""
    text = await generate_text_async(
        model=model,
        messages=messages,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        prefer_responses_api=prefer_responses_api,
        model_profile=model_profile,
    )
    return parse_json_text(text)


def generate_text_sync(
    *,
    model: str,
    messages: list[dict[str, str]],
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_seconds: float = 30.0,
    max_output_tokens: int = 1024,
    temperature: float | None = None,
    prefer_responses_api: bool = True,
    model_profile: str | None = None,
) -> str:
    """Sync version for sync call sites (respond node)."""
    profile = model_profile or _profile_for_model(model)
    resolved_model, _ = _resolve_model_for_profile(profile, model)
    reasoning_model = _is_reasoning_model(resolved_model)
    normalized = _normalize_messages(messages, reasoning_model=reasoning_model)
    effective_temp = None if reasoning_model else temperature

    def _via_responses(call_model: str) -> str:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": call_model,
            "input": normalized,
            "max_output_tokens": max_output_tokens,
        }
        if effective_temp is not None:
            kwargs["temperature"] = effective_temp
        response = client.responses.create(**kwargs)
        return _extract_output_text(response)

    def _via_chat(call_model: str) -> str:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": call_model,
            "messages": normalized,
            "max_completion_tokens": max_output_tokens,
        }
        if effective_temp is not None:
            kwargs["temperature"] = effective_temp
        response = client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip() if response.choices else ""

    last_error: Exception | None = None
    for call_model in _candidate_models(resolved_model):
        if prefer_responses_api:
            try:
                result = _via_responses(call_model)
                METRICS.record_llm_request(
                    endpoint="responses",
                    resolved_model=call_model,
                    outcome="ok",
                )
                return result
            except Exception as e:
                last_error = e
                if not _should_allow_chat_fallback():
                    METRICS.record_llm_request(
                        endpoint="responses",
                        resolved_model=call_model,
                        outcome="failed",
                    )
                    continue
                logger.warning(
                    "Responses API failed for model=%s; trying chat fallback (%s)",
                    call_model, type(e).__name__,
                )
                try:
                    result = _via_chat(call_model)
                    METRICS.record_llm_request(
                        endpoint="chat_fallback",
                        resolved_model=call_model,
                        outcome="ok",
                    )
                    return result
                except Exception as chat_e:
                    last_error = chat_e
                    METRICS.record_llm_request(
                        endpoint="chat_fallback",
                        resolved_model=call_model,
                        outcome="failed",
                    )
                    logger.warning(
                        "Chat fallback failed for model=%s (%s)",
                        call_model, type(chat_e).__name__,
                    )
                    continue
        else:
            try:
                result = _via_chat(call_model)
                METRICS.record_llm_request(
                    endpoint="chat",
                    resolved_model=call_model,
                    outcome="ok",
                )
                return result
            except Exception as e:
                last_error = e
                METRICS.record_llm_request(
                    endpoint="chat",
                    resolved_model=call_model,
                    outcome="failed",
                )
                continue

    if last_error:
        raise OpenAIAdapterError(
            _reason_code_for_error(last_error),
            f"OpenAI text generation failed for profile={profile} model={resolved_model}",
            cause=last_error,
        ) from last_error
    return ""


async def generate_embeddings_async(
    *,
    model: str,
    input_texts: list[str],
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_seconds: float = 30.0,
) -> list[list[float]]:
    """Generate embeddings for input texts."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
    resp = await client.embeddings.create(model=model, input=input_texts)
    METRICS.record_llm_request(
        endpoint="embeddings",
        resolved_model=model,
        outcome="ok",
    )
    return [item.embedding for item in resp.data]


def generate_embeddings_sync(
    *,
    model: str,
    input_texts: list[str],
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_seconds: float = 30.0,
) -> list[list[float]]:
    """Sync embeddings generation helper."""
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
    resp = client.embeddings.create(model=model, input=input_texts)
    METRICS.record_llm_request(
        endpoint="embeddings",
        resolved_model=model,
        outcome="ok",
    )
    return [item.embedding for item in resp.data]
