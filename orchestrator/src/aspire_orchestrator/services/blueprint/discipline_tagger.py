"""Discipline Tagger — LLM-based sheet classification for Drew Blueprint Engine.

Uses the Drew model to classify each sheet's discipline (A, S, M, E, P, FP, C, L,
Specs, Schedules, Addenda) based on the first 600 chars of OCR text.

Batches up to 5 sheets per LLM call to save tokens.
Sheets with confidence < 0.70 are marked with discipline=None and produce a
blueprint_missing_inputs row for contractor verification.

Law compliance:
  #7 — No autonomous decisions. Returns tagged list; Drew.classify() persists.
  #9 — First 600 chars of OCR text are sent to LLM; raw page content is not logged.

Wave 9h — cache observability:
  - System prompt is built once per process from static KB file (_TAXONOMY_CACHE).
  - Per-call dynamic context (sheet OCR) goes ONLY in the user message, keeping
    the system-message prefix byte-identical so OpenAI prefix caching is effective.
  - prompt_prefix_hash is logged at WARNING if it drifts within a single project run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aspire_orchestrator.config.settings import resolve_openai_api_key, settings
from aspire_orchestrator.services.llm_response_cache import (
    compute_content_hash,
    get_cached_llm_response,
    set_cached_llm_response,
)

logger = logging.getLogger(__name__)

# Wave 9h: module-level taxonomy cache — loaded once per process so the
# system-message prefix is byte-identical across all calls for this model/stage.
_TAXONOMY_CACHE: str | None = None

# Wave 9h: track the last seen prompt_prefix_hash per (model, stage) pair so we
# can WARN when it drifts (which would invalidate OpenAI's prefix cache).
_LAST_PREFIX_HASH: dict[str, str] = {}  # key: "{model}:classify"

_TAXONOMY_PATH = (
    Path(__file__).parent.parent.parent
    / "services"
    / "blueprint"
    / "kb"
    / "drew"
    / "drew-discipline-taxonomy.md"
)

_BATCH_SIZE = 5
_OCR_SNIPPET_CHARS = 600
_CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class DisciplineTag:
    """LLM-assigned discipline for a single sheet.

    Attributes:
        sheet_id: DB UUID of the blueprint_sheet row.
        discipline: Discipline code or None if below confidence threshold.
        confidence: 0.0–1.0 LLM confidence.
        reasoning: Short explanation from LLM (for audit trail).
        needs_review: True when confidence < threshold.
    """

    sheet_id: str
    discipline: str | None
    confidence: float
    reasoning: str
    needs_review: bool


@dataclass(frozen=True)
class _SheetInput:
    """Minimal sheet data for tagger input."""

    sheet_id: str
    sheet_number: str | None
    ocr_snippet: str


def _load_taxonomy() -> str:
    """Load discipline taxonomy KB doc.  Cached per-process for prefix stability (Wave 9h).

    Caching ensures the system-message prefix is byte-identical across all LLM
    calls in a run, so OpenAI's automatic prefix caching is effective.
    Raises RuntimeError if the taxonomy file is missing.
    """
    global _TAXONOMY_CACHE
    if _TAXONOMY_CACHE is not None:
        return _TAXONOMY_CACHE
    if not _TAXONOMY_PATH.exists():
        raise RuntimeError(
            f"Drew discipline taxonomy missing: {_TAXONOMY_PATH}. "
            "Run Wave 2 migration to populate KB scaffold."
        )
    _TAXONOMY_CACHE = _TAXONOMY_PATH.read_text(encoding="utf-8")
    return _TAXONOMY_CACHE


def _build_system_prompt(taxonomy: str) -> str:
    return f"""You are Drew, Aspire's Blueprint Story Engine.
Your task: classify construction blueprint sheets by discipline.

Discipline taxonomy reference:
{taxonomy}

Return ONLY a JSON array. Each element:
{{
  "sheet_id": "<sheet_id>",
  "discipline": "<A|S|M|E|P|FP|C|L|Specs|Schedules|Addenda>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one-line justification>"
}}

Rules:
- discipline must be exactly one of: A, S, M, E, P, FP, C, L, Specs, Schedules, Addenda
- confidence = your certainty (0.0 = no idea, 1.0 = certain)
- If you cannot determine discipline with >=0.50 confidence, set discipline to null
- reasoning must be <=120 chars, no PII, no raw OCR content
- Output ONLY the JSON array — no markdown, no explanation
"""


def _build_user_prompt(batch: list[_SheetInput]) -> str:
    lines = ["Classify these blueprint sheets:\n"]
    for sheet in batch:
        lines.append(
            f'Sheet ID: {sheet.sheet_id}\n'
            f'Sheet Number: {sheet.sheet_number or "unknown"}\n'
            f'OCR text excerpt:\n---\n{sheet.ocr_snippet[:_OCR_SNIPPET_CHARS]}\n---\n'
        )
    return "\n".join(lines)


def _parse_llm_response(raw: str, batch: list[_SheetInput]) -> list[DisciplineTag]:
    """Parse LLM JSON response into DisciplineTag list. Returns empty tags on parse failure."""
    valid_disciplines = {"A", "S", "M", "E", "P", "FP", "C", "L", "Specs", "Schedules", "Addenda"}

    try:
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        parsed: list[dict[str, Any]] = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "discipline_tagger: LLM response was not valid JSON (len=%d)", len(raw)
        )
        # Return low-confidence unknowns for all sheets in batch
        return [
            DisciplineTag(
                sheet_id=s.sheet_id,
                discipline=None,
                confidence=0.0,
                reasoning="LLM response parse failure",
                needs_review=True,
            )
            for s in batch
        ]

    # Build index for fast sheet_id lookup
    batch_index = {s.sheet_id: s for s in batch}
    tags: list[DisciplineTag] = []

    for item in parsed:
        sheet_id = str(item.get("sheet_id", ""))
        discipline_raw: str | None = item.get("discipline")
        confidence: float = float(item.get("confidence", 0.0))
        reasoning: str = str(item.get("reasoning", ""))[:200]

        # Validate discipline value
        discipline: str | None = discipline_raw
        if discipline_raw and discipline_raw not in valid_disciplines:
            logger.warning(
                "discipline_tagger: invalid discipline '%s' for sheet %s",
                discipline_raw,
                sheet_id[:8] if len(sheet_id) > 8 else sheet_id,
            )
            discipline = None
            confidence = 0.0

        needs_review = confidence < _CONFIDENCE_THRESHOLD
        tags.append(
            DisciplineTag(
                sheet_id=sheet_id,
                discipline=discipline if not needs_review else discipline,
                confidence=confidence,
                reasoning=reasoning,
                needs_review=needs_review,
            )
        )

    # Fill in any sheets that LLM omitted
    returned_ids = {t.sheet_id for t in tags}
    for s in batch:
        if s.sheet_id not in returned_ids:
            tags.append(
                DisciplineTag(
                    sheet_id=s.sheet_id,
                    discipline=None,
                    confidence=0.0,
                    reasoning="LLM omitted this sheet from response",
                    needs_review=True,
                )
            )

    return tags


async def tag_disciplines(
    sheets: list[dict[str, Any]],
    *,
    model: str,
    correlation_id: str,
) -> list[DisciplineTag]:
    """Tag each sheet with its construction discipline via LLM.

    Args:
        sheets: List of sheet dicts from DB. Each must have keys:
                "id" (str), "sheet_number" (str|None), "ocr_text" (str|None).
        model: Model identifier (e.g. "gpt-5.4-mini").
        correlation_id: Trace ID for logging.

    Returns:
        List of DisciplineTag, one per input sheet (order preserved).
        Sheets with confidence < 0.70 have needs_review=True.

    Wave 9h: accumulates total_prompt_tokens, total_cached_tokens, total_completion_tokens
    across all batches and logs a summary with cache_hit_ratio at INFO level.
    """
    if not sheets:
        return []

    # Wave 9h: system prompt built from cached taxonomy — prefix is byte-identical
    # across all batches and runs, maximising OpenAI prefix cache reuse.
    taxonomy = _load_taxonomy()
    system_prompt = _build_system_prompt(taxonomy)
    api_key = resolve_openai_api_key()

    # Import here to avoid circular imports (services → openai_client → settings)
    from aspire_orchestrator.services.openai_client import generate_text_async

    all_tags: list[DisciplineTag] = []

    # Wave 9h: stage-level token accumulators
    _total_prompt_tokens: int = 0
    _total_cached_tokens: int = 0
    _total_completion_tokens: int = 0

    # Batch sheets into groups of _BATCH_SIZE
    for batch_start in range(0, len(sheets), _BATCH_SIZE):
        batch_rows = sheets[batch_start : batch_start + _BATCH_SIZE]
        batch_inputs: list[_SheetInput] = [
            _SheetInput(
                sheet_id=str(row["id"]),
                sheet_number=row.get("sheet_number"),
                ocr_snippet=(row.get("ocr_text") or "")[:_OCR_SNIPPET_CHARS],
            )
            for row in batch_rows
        ]

        user_prompt = _build_user_prompt(batch_inputs)

        logger.info(
            "discipline_tagger: batch %d/%d, sheets=%d, model=%s, corr=%s",
            batch_start // _BATCH_SIZE + 1,
            (len(sheets) + _BATCH_SIZE - 1) // _BATCH_SIZE,
            len(batch_inputs),
            model,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Wave 9h: compute content hash for Redis response cache key
        user_content_hash = compute_content_hash(user_prompt)

        # Wave 9h: check Redis response cache before calling OpenAI
        cached_llm = await get_cached_llm_response("classify", user_content_hash, model)
        if cached_llm is not None:
            logger.info(
                "9h classify_cache_hit stage=classify model=%s hash=%s corr=%s",
                model, user_content_hash[:8],
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
            # Emit receipt for cache hit (so ledger can track cache layer)
            try:
                from aspire_orchestrator.services.receipt_store import store_receipts
                import uuid as _uuid
                from datetime import datetime as _dt, timezone as _tz
                store_receipts([{
                    "id": str(_uuid.uuid4()),
                    "outcome": "success",
                    "action_type": "blueprint.llm.cache_hit",
                    "receipt_type": "blueprint.llm.cache_hit",
                    "created_at": _dt.now(_tz.utc).isoformat(),
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": "skillpack:drew-blueprint",
                    "correlation_id": correlation_id,
                    "metadata": {
                        "stage": "classify",
                        "model": model,
                        "content_hash": user_content_hash[:16],
                        "cache_layer": "redis",
                    },
                }])
            except Exception:
                pass  # Receipt emit is best-effort for cache hits
            raw_response: str = json.dumps(cached_llm) if isinstance(cached_llm, list) else str(cached_llm)
            batch_tags = _parse_llm_response(raw_response, batch_inputs)
            all_tags.extend(batch_tags)
            continue

        try:
            llm_result = await generate_text_async(
                model=model,
                messages=messages,
                api_key=api_key,
                timeout_seconds=float(settings.openai_timeout_seconds),
                max_output_tokens=1024,
            )
            raw_response = llm_result["text"]

            # Wave 9h: accumulate usage and verify prefix stability
            usage = llm_result["usage"]
            _total_prompt_tokens += usage["prompt_tokens"]
            _total_cached_tokens += usage["cached_tokens"]
            _total_completion_tokens += usage["completion_tokens"]

            # Wave 9h: warn if prompt_prefix_hash drifts within a project run
            prefix_key = f"{model}:classify"
            observed_hash = usage["prompt_prefix_hash"]
            last_hash = _LAST_PREFIX_HASH.get(prefix_key)
            if last_hash is None:
                _LAST_PREFIX_HASH[prefix_key] = observed_hash
            elif last_hash != observed_hash:
                logger.warning(
                    "9h: prefix_hash drift detected for %s — "
                    "previous=%s current=%s — OpenAI prefix cache INVALIDATED. "
                    "Ensure system prompt is byte-stable across batches.",
                    prefix_key, last_hash, observed_hash,
                )
                _LAST_PREFIX_HASH[prefix_key] = observed_hash

            # Wave 9h: store response in Redis cache (best-effort)
            try:
                parsed_tags = _parse_llm_response(raw_response, batch_inputs)
                # Cache as list of tag dicts
                tag_dicts = [
                    {
                        "sheet_id": t.sheet_id,
                        "discipline": t.discipline,
                        "confidence": t.confidence,
                        "reasoning": t.reasoning,
                    }
                    for t in parsed_tags
                ]
                await set_cached_llm_response("classify", user_content_hash, model, {"tags": tag_dicts})
            except Exception:
                pass  # Cache write is best-effort

        except Exception as exc:
            logger.error(
                "discipline_tagger: LLM call failed (%s), corr=%s",
                type(exc).__name__,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
            # Return low-confidence unknowns for this batch so pipeline continues
            all_tags.extend(
                DisciplineTag(
                    sheet_id=s.sheet_id,
                    discipline=None,
                    confidence=0.0,
                    reasoning=f"LLM call failed: {type(exc).__name__}",
                    needs_review=True,
                )
                for s in batch_inputs
            )
            continue

        batch_tags = _parse_llm_response(raw_response, batch_inputs)
        all_tags.extend(batch_tags)

    # Wave 9h: log stage-level cache summary
    if _total_prompt_tokens > 0:
        stage_cache_hit_ratio = _total_cached_tokens / max(_total_prompt_tokens, 1)
        logger.info(
            "9h classify_stage_cache_summary model=%s total_prompt_tokens=%d "
            "total_cached_tokens=%d stage_cache_hit_ratio=%.4f "
            "total_completion_tokens=%d corr=%s",
            model,
            _total_prompt_tokens,
            _total_cached_tokens,
            stage_cache_hit_ratio,
            _total_completion_tokens,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

    return all_tags
