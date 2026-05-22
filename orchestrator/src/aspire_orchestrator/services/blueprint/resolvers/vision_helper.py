"""Shared vision-call helper for Wave 19.2 cascade resolvers.

Wave 19.2.1 (Commit 4.1) — Hybrid Azure-layout + Vision interpretation.

Production diagnostic on Gulfstream sheet 39 (fire-protection) showed
that gpt-5.4-mini cannot READ legend / schedule fine print on a raw
200-DPI full-sheet PNG. Model honestly returned "unclear" while the
sheet literally contained ``NET TOTAL: 33`` in a sprinkler legend
table. Azure prebuilt-layout DOES extract those tables (proven by
Commit 2.7's 6/6 schedule wins on the same project). The breakthrough
is to combine them: hand vision the Azure structured layout AS
context, then ask it to interpret the question against that context
using the PNG as visual backup.

``call_vision_with_budget(..., extracted_layout=...)`` accepts an
optional Azure ``analyzeResult`` dict. When provided, a compact layout
summary (tables + paragraphs only — coordinates stripped) is injected
into the user content BEFORE the image with explicit instructions:
"answer using the structured data first, the PNG as visual context."

``run_azure_layout_pre_pass()`` is the convenience pre-call wrapper that
each vision resolver uses to fetch Azure layout for a single PNG and
returns the ``analyzeResult`` (or None on any failure). Errors are
swallowed — vision call still runs without context if Azure fails.

Law compliance
--------------
* Law #1 — Helper returns the parsed JSON + usage stats; the caller decides
  what to do with it.
* Law #2 — Token usage is surfaced to the caller's evidence dict; no
  receipts are emitted here.
* Law #3 — Every failure path returns a typed result with ``ok=False``
  rather than raising. Caller decides whether to emit missing_input or
  attempt another method. Azure pre-pass failures degrade silently to
  PNG-only vision (not catastrophic — pre-19.2.1 behavior).
* Law #9 — Never logs raw PNG bytes, raw Azure cell text, or full prompt
  content. Layout summary truncated cell text to 80 chars; receipts only
  carry counts.
"""
from __future__ import annotations

import base64
import json
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


# Per-cell text truncation when compacting Azure layout for the model
# prompt. Keeps token usage bounded; cell text rarely exceeds 80 chars in
# practice (legend entries, equipment IDs, numeric counts).
_AZURE_CELL_TEXT_MAX_CHARS: int = 80
# Maximum tables to include in the compact layout summary. Sealed
# engineering sheets occasionally have 20+ tables (full schedule sheets);
# the most-relevant ones are usually first. 12 keeps prompts under ~3K
# tokens of layout context even on dense sheets.
_AZURE_LAYOUT_MAX_TABLES: int = 12


def compact_azure_layout(analyze_result: dict[str, Any] | None) -> dict[str, Any]:
    """Compact an Azure ``analyzeResult`` to a token-economical JSON.

    Strips coordinates, polygons, and BoundingRegions. Keeps only
    ``tables`` (with row/column indices + truncated cell ``content``)
    and a short ``paragraphs`` sample. Returns ``{}`` when input is
    None / non-dict.

    Truncation rationale:
      * Cell coordinates are useless without the PNG (we send the PNG
        separately to vision). Stripping them saves ~70% of layout
        token cost.
      * Paragraphs are noisy on engineering sheets (every text run is
        a paragraph). Limit to the first 20 to seed the model with
        title-block-ish text.
    """
    if not isinstance(analyze_result, dict):
        return {}

    tables_raw = analyze_result.get("tables") or []
    compact_tables: list[dict[str, Any]] = []
    for table in tables_raw[:_AZURE_LAYOUT_MAX_TABLES]:
        if not isinstance(table, dict):
            continue
        cells_raw = table.get("cells") or []
        compact_cells: list[dict[str, Any]] = []
        for cell in cells_raw:
            if not isinstance(cell, dict):
                continue
            text = str(cell.get("content") or "")[:_AZURE_CELL_TEXT_MAX_CHARS]
            compact_cells.append({
                "r": int(cell.get("rowIndex") or 0),
                "c": int(cell.get("columnIndex") or 0),
                "t": text,
            })
        compact_tables.append({
            "rows": int(table.get("rowCount") or 0),
            "cols": int(table.get("columnCount") or 0),
            "cells": compact_cells,
        })

    paragraphs_raw = analyze_result.get("paragraphs") or []
    compact_paragraphs: list[str] = []
    for p in paragraphs_raw[:20]:
        if not isinstance(p, dict):
            continue
        content = str(p.get("content") or "")[:120]
        if content:
            compact_paragraphs.append(content)

    return {
        "tables": compact_tables,
        "paragraphs": compact_paragraphs,
        "tables_total_in_source": len(tables_raw),
    }


async def run_azure_layout_pre_pass(
    *,
    png_bytes: bytes | None,
    suite_id: str,
    office_id: str,
    correlation_id: str = "",
) -> dict[str, Any] | None:
    """Run Azure Document Intelligence prebuilt-layout on a PNG.

    Returns the raw ``analyzeResult`` dict on success, or ``None`` on any
    failure (missing creds, transport error, timeout, malformed response).
    Never raises. Callers pass the result to ``call_vision_with_budget``
    via ``extracted_layout=...`` — vision will run with PNG-only context
    if this returns None.

    Cost note: each Azure prebuilt-layout call is ~$0.01-0.03 USD. NOT
    metered through ``VisionBudget`` (which is OpenAI-spend only); Azure
    spend is tracked via its own provider's circuit-breaker. Caller may
    surface ``azure_layout_pre_pass_used`` (True/False) on evidence for
    cascade receipt audit.
    """
    if not png_bytes:
        return None

    try:
        from aspire_orchestrator.providers.azure_doc_intel_client import (
            get_azure_doc_intel_client,
        )
        client = get_azure_doc_intel_client()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vision_helper.azure_pre_pass: client construction failed (%s)",
            type(exc).__name__,
        )
        return None

    try:
        response = await client.analyze_layout(
            pdf_bytes=png_bytes,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vision_helper.azure_pre_pass: analyze_layout raised (%s)",
            type(exc).__name__,
        )
        return None

    if not response.success:
        _log.info(
            "vision_helper.azure_pre_pass: non-success status=%d err=%s",
            response.status_code,
            response.error_code.value if response.error_code else "n/a",
        )
        return None

    analyze_result = response.body.get("analyzeResult") if isinstance(
        response.body, dict
    ) else None
    if not isinstance(analyze_result, dict):
        return None
    return analyze_result


def _build_vision_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    png_bytes: bytes,
    extracted_layout: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the Responses-API message structure with an inline base64 PNG.

    Wave 19.2.1: when ``extracted_layout`` is provided, a compact JSON
    summary is injected as a text block BEFORE the image. This was the
    breakthrough — at 200 DPI a full-sheet PNG renders legend / schedule
    fine print too small for the model to read, but Azure prebuilt-layout
    extracts the cells reliably and we hand them to the model as context.

    Layout block is wrapped in explicit instructions so the model
    prioritises structured data over its visual interpretation.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"

    user_content: list[dict[str, Any]] = []
    if extracted_layout:
        compact = compact_azure_layout(extracted_layout)
        if compact.get("tables") or compact.get("paragraphs"):
            layout_block = (
                "Here is the structured layout extracted from this sheet by "
                "Azure Document Intelligence prebuilt-layout. The tables and "
                "paragraphs below are direct OCR cell content — trust this "
                "structured data first; use the PNG only as visual context "
                "to disambiguate. Do NOT say content is unclear if it is "
                "present in the structured layout below.\n\n"
                f"{json.dumps(compact, separators=(',', ':'))}"
            )
            user_content.append({"type": "input_text", "text": layout_block})
    user_content.append({"type": "input_image", "image_url": data_url})
    user_content.append({"type": "input_text", "text": user_prompt})

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


async def call_vision_with_budget(
    *,
    png_bytes: bytes | None,
    system_prompt: str,
    user_prompt: str,
    vision_budget: Any | None,
    extracted_layout: dict[str, Any] | None = None,
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

    Wave 19.2.1: ``extracted_layout`` is an optional Azure
    ``analyzeResult`` dict (from ``run_azure_layout_pre_pass``). When
    provided, a compact summary is injected before the image — turns
    the model from "guess what this PNG shows" into "interpret this
    structured data with the PNG as context". This is the only way
    sealed-engineering-sheet fine print becomes readable.
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
            extracted_layout=extracted_layout,
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


__all__ = [
    "call_vision_with_budget",
    "VisionCallResult",
    "VISION_MODEL",
    "run_azure_layout_pre_pass",
    "compact_azure_layout",
]
