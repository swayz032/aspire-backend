"""Shared vision-call helper for Wave 19.2 cascade resolvers.

The three new methods (`_method_vision_addendum_cover`,
`_method_vision_callout_detail`, `GeneralVisionResolver._method_vision`)
all do the same plumbing:

  1. Check the VisionBudget — abort cleanly if ceiling hit
  2. Get a sheet PNG via the SheetDataProvider (data URL conversion)
  3. POST to OpenAI Responses API with structured-output instructions
  4. Parse JSON response — reject vague/empty answers
  5. Record token usage against the budget

This module centralises that plumbing so each new method only declares its
own system prompt, JSON schema, and structured-result interpretation.

Law compliance
--------------
* Law #1 — Helper returns the parsed JSON + usage stats; the caller decides
  what to do with it.
* Law #2 — Token usage is surfaced to the caller's evidence dict; no
  receipts are emitted here.
* Law #3 — Every failure path returns a typed result with ``ok=False``
  rather than raising. Caller decides whether to emit missing_input or
  attempt another method.
* Law #9 — Never logs PNG bytes or full prompt content. Only sheet UUID
  prefix + token counts + booleans.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

# Model to use for vision calls. gpt-5.4-mini is the vision-capable fast
# model the existing discipline_resolver uses (see
# discipline_resolver.py:206). Hard-coded rather than env-driven so cost
# semantics in VisionBudget are predictable.
VISION_MODEL: str = "gpt-5.4-mini"


@dataclass(frozen=True)
class VisionCallResult:
    """Outcome of one vision call. ``ok`` is the gate caller branches on."""
    ok: bool
    json_payload: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str
    skip_reason: str  # populated only when ok=False


def _build_vision_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    png_bytes: bytes,
) -> list[dict[str, Any]]:
    """Build the Responses-API message structure with an inline base64 PNG.

    Mirrors the format proven by ``discipline_resolver._method_vision_title_block``
    (lines 180-203). The Responses API accepts ``input_image`` with either
    an ``image_url`` (http(s)) or a data URL (``data:image/png;base64,...``).
    We use the data URL so resolvers don't depend on a public Storage URL.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": data_url},
                {"type": "input_text", "text": user_prompt},
            ],
        },
    ]


async def call_vision_with_budget(
    *,
    png_bytes: bytes | None,
    system_prompt: str,
    user_prompt: str,
    vision_budget: Any | None,
    max_output_tokens: int = 512,
    timeout_seconds: float = 60.0,
) -> VisionCallResult:
    """Issue one vision call against the OpenAI Responses API.

    Returns ``VisionCallResult(ok=False, ...)`` instead of raising on any
    failure path: missing PNG, missing API key, exhausted budget, OpenAI
    error, or non-JSON response. Caller interprets ``skip_reason`` and
    emits a typed ``ResolverResult`` with a matching ``skip_reason``
    evidence key.

    Token usage is recorded against ``vision_budget`` on success. When
    the budget is None (pre-19.2 fixtures), the call is still issued
    but no accounting happens.
    """
    if not png_bytes:
        return VisionCallResult(
            ok=False, json_payload={},
            input_tokens=0, output_tokens=0, model=VISION_MODEL,
            skip_reason="no_png_bytes_available",
        )

    api_key = (
        os.getenv("OPENAI_API_KEY") or os.getenv("ASPIRE_OPENAI_API_KEY", "")
    )
    if not api_key:
        return VisionCallResult(
            ok=False, json_payload={},
            input_tokens=0, output_tokens=0, model=VISION_MODEL,
            skip_reason="no_api_key",
        )

    # Budget gate. Pre-call ``can_spend`` check; post-call ``record_call``.
    if vision_budget is not None:
        try:
            if not vision_budget.can_spend():
                vision_budget.record_abort()
                return VisionCallResult(
                    ok=False, json_payload={},
                    input_tokens=0, output_tokens=0, model=VISION_MODEL,
                    skip_reason="vision_budget_exhausted",
                )
        except Exception as exc:  # noqa: BLE001
            # Budget interface unexpected — fail closed (no vision call).
            _log.warning(
                "vision_helper: vision_budget.can_spend raised %s — aborting",
                type(exc).__name__,
            )
            return VisionCallResult(
                ok=False, json_payload={},
                input_tokens=0, output_tokens=0, model=VISION_MODEL,
                skip_reason="vision_budget_error",
            )

    try:
        from aspire_orchestrator.services.openai_client import generate_json_async
        from aspire_orchestrator.config.settings import settings

        messages = _build_vision_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            png_bytes=png_bytes,
        )
        result = await generate_json_async(
            model=VISION_MODEL,
            messages=messages,
            api_key=api_key,
            base_url=settings.openai_base_url,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            temperature=None,
            model_profile="primary_reasoner",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vision_helper: OpenAI call failed (%s) — skipping",
            type(exc).__name__,
        )
        return VisionCallResult(
            ok=False, json_payload={},
            input_tokens=0, output_tokens=0, model=VISION_MODEL,
            skip_reason="openai_call_failed",
        )

    payload = result.get("json") or {}
    usage = result.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)

    # Record actual usage against the budget AFTER the call returns.
    if vision_budget is not None:
        try:
            vision_budget.record_call(
                model=VISION_MODEL,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "vision_helper: vision_budget.record_call raised %s",
                type(exc).__name__,
            )

    if not isinstance(payload, dict) or not payload:
        return VisionCallResult(
            ok=False, json_payload={},
            input_tokens=input_tokens, output_tokens=output_tokens,
            model=VISION_MODEL,
            skip_reason="empty_or_non_dict_response",
        )

    return VisionCallResult(
        ok=True, json_payload=payload,
        input_tokens=input_tokens, output_tokens=output_tokens,
        model=VISION_MODEL,
        skip_reason="",
    )


__all__ = ["call_vision_with_budget", "VisionCallResult", "VISION_MODEL"]
