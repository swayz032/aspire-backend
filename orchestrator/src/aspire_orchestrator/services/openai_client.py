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
import threading
import time
from typing import Any

from openai import AsyncOpenAI, OpenAI

from aspire_orchestrator.config.settings import resolve_openai_api_key, settings
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

# --- Client Singleton Cache ---
_async_client_cache: dict[tuple[str, str], AsyncOpenAI] = {}
_sync_client_cache: dict[tuple[str, str], OpenAI] = {}
_client_cache_lock = threading.Lock()


def _get_or_create_async_client(
    api_key: str,
    base_url: str,
    timeout: float = 30.0,
) -> AsyncOpenAI:
    """Return cached AsyncOpenAI client or create one.

    5f: Cache key includes timeout so callers with different timeouts
    get distinct clients instead of sharing one with the wrong timeout.
    """
    cache_key = (api_key, base_url, timeout)
    if cache_key not in _async_client_cache:
        with _client_cache_lock:
            if cache_key not in _async_client_cache:
                _async_client_cache[cache_key] = AsyncOpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=timeout,
                )
    return _async_client_cache[cache_key]


def _get_or_create_sync_client(
    api_key: str,
    base_url: str,
    timeout: float = 30.0,
) -> OpenAI:
    """Return cached sync OpenAI client or create one.

    5f: Cache key includes timeout for correct client isolation.
    """
    cache_key = (api_key, base_url, timeout)
    if cache_key not in _sync_client_cache:
        with _client_cache_lock:
            if cache_key not in _sync_client_cache:
                _sync_client_cache[cache_key] = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=timeout,
                )
    return _sync_client_cache[cache_key]


def clear_client_cache() -> None:
    """Clear all cached clients. For testing and shutdown."""
    with _client_cache_lock:
        _async_client_cache.clear()
        _sync_client_cache.clear()


# 5a: Circuit breaker — trip after consecutive failures, half-open after cooldown
class _CircuitBreaker:
    """Simple circuit breaker for OpenAI calls.

    States: CLOSED (normal) → OPEN (tripped, reject fast) → HALF_OPEN (allow one probe).
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 30.0) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._state = "CLOSED"
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN" and time.monotonic() - self._last_failure_time >= self._cooldown_seconds:
                self._state = "HALF_OPEN"
            return self._state

    def allow_request(self) -> bool:
        current = self.state
        return current in ("CLOSED", "HALF_OPEN")

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = "CLOSED"

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()
            if self._consecutive_failures >= self._failure_threshold:
                self._state = "OPEN"
                logger.warning(
                    "5a: OpenAI circuit breaker OPEN after %d consecutive failures",
                    self._consecutive_failures,
                )


_openai_circuit_breaker = _CircuitBreaker(failure_threshold=5, cooldown_seconds=30.0)


class OpenAIAdapterError(RuntimeError):
    """Normalized adapter error with reason code for upstream mapping."""

    def __init__(self, reason_code: str, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.cause = cause


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3"))



# M5: Consolidated fallback map — single parser for both model and profile fallbacks
def _parse_fallback_env() -> dict[str, list[str]] | None:
    """Parse ASPIRE_MODEL_FALLBACK_MAP env var (used by both model + profile paths)."""
    raw = os.environ.get("ASPIRE_MODEL_FALLBACK_MAP", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            result: dict[str, list[str]] = {}
            for k, v in parsed.items():
                if isinstance(k, str) and isinstance(v, list):
                    normalized = [str(item).strip() for item in v if str(item).strip()]
                    if normalized:
                        result[k] = normalized
            if result:
                return result
    except Exception:
        logger.warning("Invalid ASPIRE_MODEL_FALLBACK_MAP JSON, using defaults")
    return None


def _model_fallback_map() -> dict[str, list[str]]:
    return _parse_fallback_env() or {
        "gpt-5.2": ["gpt-5", "gpt-5-mini"],
        "gpt-5": ["gpt-5-mini"],
        "gpt-5-mini": ["gpt-5"],
    }


def _profile_fallback_map() -> dict[str, list[str]]:
    return _parse_fallback_env() or dict(_DEFAULT_PROFILE_FALLBACKS)


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
    # H7: Fail-closed — unknown/unprobed models default to UNAVAILABLE (Law #3).
    # Only models explicitly probed as True at startup are considered available.
    return _MODEL_PROBE_CACHE.get(model, False)


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
    """H6: Distinguish error types for correct retry strategy.

    - Endpoint 404 (wrong URL): ENDPOINT_NOT_FOUND — don't retry
    - Model 404 (wrong model name): MODEL_UNAVAILABLE — try fallback model
    - Timeout: UPSTREAM_TIMEOUT — retry with backoff
    - Rate limit (429): RATE_LIMITED — retry with backoff
    - Other: UPSTREAM_ERROR — generic upstream failure
    """
    if error is None:
        return "MODEL_UNAVAILABLE"
    name = type(error).__name__.lower()
    msg = str(error).lower()
    if "timeout" in name or "timeout" in msg:
        return "UPSTREAM_TIMEOUT"
    if "ratelimit" in name or "429" in msg or "rate limit" in msg:
        return "RATE_LIMITED"
    if "404" in msg or "not found" in msg:
        if "model" in msg or "does not exist" in msg:
            return "MODEL_UNAVAILABLE"
        return "ENDPOINT_NOT_FOUND"
    if "auth" in msg or "401" in msg or "403" in msg:
        return "AUTH_ERROR"
    return "UPSTREAM_ERROR"


async def probe_models_startup() -> dict[str, Any]:
    """Probe configured model profiles at startup and cache availability.

    Returns structured probe result for readiness diagnostics.
    """
    api_key = resolve_openai_api_key()
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

    client = _get_or_create_async_client(
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
                max_output_tokens=32,
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
    # 5a: Circuit breaker — reject fast when OpenAI is down
    if not _openai_circuit_breaker.allow_request():
        raise OpenAIAdapterError(
            "CIRCUIT_OPEN",
            "OpenAI circuit breaker is OPEN — too many consecutive failures. "
            "Will retry automatically after cooldown.",
        )

    profile = model_profile or _profile_for_model(model)
    resolved_model, _ = _resolve_model_for_profile(profile, model)
    reasoning_model = _is_reasoning_model(resolved_model)
    normalized = _normalize_messages(messages, reasoning_model=reasoning_model)
    effective_temp = None if reasoning_model else temperature

    # --- LLM cache check (Phase 5A) ---
    from aspire_orchestrator.services.llm_cache import get_llm_cache, LLMCache
    cache = get_llm_cache()
    system_prompt = messages[0].get("content", "") if messages else ""
    user_prompt = messages[-1].get("content", "") if messages else ""
    cache_key = LLMCache.cache_key(resolved_model, system_prompt, user_prompt)
    cached = await cache.get(cache_key)
    if cached is not None:
        logger.debug("LLM cache HIT for model=%s profile=%s", resolved_model, profile)
        METRICS.record_llm_request(
            endpoint="cache_hit",
            resolved_model=resolved_model,
            outcome="ok",
        )
        return cached

    async def _via_responses(call_model: str) -> tuple[str, Any]:
        client = _get_or_create_async_client(api_key, base_url, timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": call_model,
            "input": normalized,
            "max_output_tokens": max_output_tokens,
        }
        if effective_temp is not None:
            kwargs["temperature"] = effective_temp
        response = await client.responses.create(**kwargs)
        return _extract_output_text(response), response

    # M4: Bounded task set for token tracking — prevents memory leak
    import asyncio as _asyncio
    _TOKEN_TASKS: set[_asyncio.Task[None]] = set()
    _MAX_TOKEN_TASKS = 50

    def _schedule_token_tracking(task: _asyncio.Task[None]) -> None:
        """Add task to bounded set, cleanup completed tasks."""
        _TOKEN_TASKS.discard(None)  # type: ignore[arg-type]
        # Clean up done tasks
        done = {t for t in _TOKEN_TASKS if t.done()}
        for t in done:
            _TOKEN_TASKS.discard(t)
            if t.exception():
                logger.warning("Token tracking task failed: %s", t.exception())
        # Enforce max size — drop oldest if at capacity
        if len(_TOKEN_TASKS) >= _MAX_TOKEN_TASKS:
            logger.warning("M4: Token tracking tasks at capacity (%d), skipping", _MAX_TOKEN_TASKS)
            return
        _TOKEN_TASKS.add(task)
        task.add_done_callback(_TOKEN_TASKS.discard)

    async def _track_token_usage(response: Any, call_model: str) -> None:
        """Token usage tracking (Phase 5E) — best-effort, non-blocking."""
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            from aspire_orchestrator.services.supabase_client import supabase_insert
            token_data = {
                "suite_id": None,
                "agent_id": "orchestrator",
                "model": call_model,
                "profile": profile or "unknown",
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
                "cache_hit": False,
            }
            await supabase_insert("token_usage_log", token_data)
        except Exception:
            pass  # Token tracking is best-effort

    last_error: Exception | None = None
    for call_model in _candidate_models(resolved_model):
        try:
            result, response = await _via_responses(call_model)
            _openai_circuit_breaker.record_success()  # 5a
            METRICS.record_llm_request(
                endpoint="responses",
                resolved_model=call_model,
                outcome="ok",
            )
            # Cache the response (Phase 5A)
            await cache.set(cache_key, result, profile=profile)
            # M4: Token tracking with bounded task set (prevents memory leak)
            import asyncio
            _schedule_token_tracking(asyncio.create_task(_track_token_usage(response, call_model)))
            return result
        except Exception as e:
            last_error = e
            _openai_circuit_breaker.record_failure()  # 5a
            METRICS.record_llm_request(
                endpoint="responses",
                resolved_model=call_model,
                outcome="failed",
            )
            logger.warning(
                "Responses API failed for model=%s (%s)",
                call_model, type(e).__name__,
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
        client = _get_or_create_sync_client(api_key, base_url, timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": call_model,
            "input": normalized,
            "max_output_tokens": max_output_tokens,
        }
        if effective_temp is not None:
            kwargs["temperature"] = effective_temp
        response = client.responses.create(**kwargs)
        return _extract_output_text(response)

    last_error: Exception | None = None
    for call_model in _candidate_models(resolved_model):
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
            METRICS.record_llm_request(
                endpoint="responses",
                resolved_model=call_model,
                outcome="failed",
            )
            logger.warning(
                "Responses API failed for model=%s (%s)",
                call_model, type(e).__name__,
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
    client = _get_or_create_async_client(api_key, base_url, timeout_seconds)
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
    client = _get_or_create_sync_client(api_key, base_url, timeout_seconds)
    resp = client.embeddings.create(model=model, input=input_texts)
    METRICS.record_llm_request(
        endpoint="embeddings",
        resolved_model=model,
        outcome="ok",
    )
    return [item.embedding for item in resp.data]
