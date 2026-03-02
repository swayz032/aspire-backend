"""Shared OpenAI client helpers (Responses-first with optional chat fallback).

Centralizes model-call behavior across orchestrator nodes/services:
  - Responses API preferred for GPT-5 family and modern OpenAI usage.
  - Optional fallback to Chat Completions for compatibility.
  - Reasoning-model role rules: developer role, no temperature.
  - Basic JSON extraction helpers for classifier-style calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import openai
from openai import AsyncOpenAI, OpenAI

logger = logging.getLogger(__name__)


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


def _candidate_models(primary_model: str) -> list[str]:
    models = [primary_model]
    for alt in _model_fallback_map().get(primary_model, []):
        if alt not in models:
            models.append(alt)
    return models


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
) -> str:
    """Generate text from a chat-style message list."""
    reasoning_model = _is_reasoning_model(model)
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
    for call_model in _candidate_models(model):
        if prefer_responses_api:
            try:
                return await _via_responses(call_model)
            except Exception as e:
                last_error = e
                if not _should_allow_chat_fallback():
                    continue
                logger.warning(
                    "Responses API failed for model=%s; trying chat fallback (%s)",
                    call_model, type(e).__name__,
                )
                try:
                    return await _via_chat(call_model)
                except Exception as chat_e:
                    last_error = chat_e
                    logger.warning(
                        "Chat fallback failed for model=%s (%s)",
                        call_model, type(chat_e).__name__,
                    )
                    continue
        else:
            try:
                return await _via_chat(call_model)
            except Exception as e:
                last_error = e
                continue

    if last_error:
        raise last_error
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
) -> str:
    """Sync version for sync call sites (respond node)."""
    reasoning_model = _is_reasoning_model(model)
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
    for call_model in _candidate_models(model):
        if prefer_responses_api:
            try:
                return _via_responses(call_model)
            except Exception as e:
                last_error = e
                if not _should_allow_chat_fallback():
                    continue
                logger.warning(
                    "Responses API failed for model=%s; trying chat fallback (%s)",
                    call_model, type(e).__name__,
                )
                try:
                    return _via_chat(call_model)
                except Exception as chat_e:
                    last_error = chat_e
                    logger.warning(
                        "Chat fallback failed for model=%s (%s)",
                        call_model, type(chat_e).__name__,
                    )
                    continue
        else:
            try:
                return _via_chat(call_model)
            except Exception as e:
                last_error = e
                continue

    if last_error:
        raise last_error
    return ""
