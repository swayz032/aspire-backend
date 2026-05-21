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
        source: "atlas" (deterministic) | "llm" (model) | "llm_retry" (second pass)
                | "llm_dropped" (omitted by LLM even after retry).
    """

    sheet_id: str
    discipline: str | None
    confidence: float
    reasoning: str
    needs_review: bool
    source: str = "llm"


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

    # NOTE (Wave 17b.1): omitted sheets are NOT auto-filled here. Caller is
    # responsible for detecting omissions and issuing a single retry before
    # writing missing_inputs. See `_classify_with_retry` below.
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

    # ---------------------------------------------------------------
    # Wave 17b.1 — Sheet-Type Atlas deterministic pre-classifier.
    # ---------------------------------------------------------------
    # Run regex/keyword classifier against every input. Sheets that match
    # any tier with conf >=0.80 are tagged HERE; the LLM only sees the
    # remainder. Massive cost + reliability win on the GAVNN-class job.
    from aspire_orchestrator.services.blueprint import sheet_type_atlas

    all_tags: list[DisciplineTag] = []
    llm_pending: list[dict[str, Any]] = []
    atlas_hits = 0

    for row in sheets:
        sheet_id = str(row["id"])
        match = sheet_type_atlas.classify(
            sheet_number=row.get("sheet_number"),
            sheet_code=row.get("sheet_code"),
            sheet_title=row.get("sheet_title"),
            ocr_text=row.get("ocr_text"),
        )
        if match is not None:
            atlas_hits += 1
            all_tags.append(
                DisciplineTag(
                    sheet_id=sheet_id,
                    discipline=match.discipline,
                    confidence=match.confidence,
                    reasoning=f"Atlas: {match.evidence}",
                    needs_review=False,
                    source="atlas",
                )
            )
        else:
            llm_pending.append(row)

    logger.info(
        "discipline_tagger: atlas pre-pass matched %d/%d sheets, %d remain for LLM, corr=%s",
        atlas_hits,
        len(sheets),
        len(llm_pending),
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # Emit a stage receipt for atlas activity (Law #2). Best-effort.
    if atlas_hits > 0:
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts
            import uuid as _uuid
            from datetime import datetime as _dt
            from datetime import timezone as _tz
            store_receipts([{
                "id": str(_uuid.uuid4()),
                "outcome": "success",
                "action_type": "drew.classify.atlas.matched",
                "receipt_type": "drew.classify.atlas.matched",
                "created_at": _dt.now(_tz.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": "skillpack:drew-blueprint",
                "correlation_id": correlation_id,
                "metadata": {
                    "stage": "classify",
                    "atlas_matched": atlas_hits,
                    "total_sheets": len(sheets),
                    "llm_pending": len(llm_pending),
                },
            }])
        except Exception:
            pass

    if not llm_pending:
        return all_tags

    # ---------------------------------------------------------------
    # LLM pass for Tier-4 sheets (those atlas could not classify).
    # ---------------------------------------------------------------
    taxonomy = _load_taxonomy()
    system_prompt = _build_system_prompt(taxonomy)
    api_key = resolve_openai_api_key()

    # Wave 9h: stage-level token accumulators
    _total_prompt_tokens: int = 0
    _total_cached_tokens: int = 0
    _total_completion_tokens: int = 0

    # Batch sheets into groups of _BATCH_SIZE
    for batch_start in range(0, len(llm_pending), _BATCH_SIZE):
        batch_rows = llm_pending[batch_start : batch_start + _BATCH_SIZE]
        batch_inputs: list[_SheetInput] = [
            _SheetInput(
                sheet_id=str(row["id"]),
                sheet_number=row.get("sheet_number"),
                ocr_snippet=(row.get("ocr_text") or "")[:_OCR_SNIPPET_CHARS],
            )
            for row in batch_rows
        ]

        logger.info(
            "discipline_tagger: batch %d/%d, sheets=%d, model=%s, corr=%s",
            batch_start // _BATCH_SIZE + 1,
            (len(llm_pending) + _BATCH_SIZE - 1) // _BATCH_SIZE,
            len(batch_inputs),
            model,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

        batch_tags, tok_p, tok_c, tok_o = await _classify_with_retry(
            batch_inputs=batch_inputs,
            system_prompt=system_prompt,
            api_key=api_key,
            model=model,
            correlation_id=correlation_id,
        )
        _total_prompt_tokens += tok_p
        _total_cached_tokens += tok_c
        _total_completion_tokens += tok_o
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


async def _classify_with_retry(
    *,
    batch_inputs: list[_SheetInput],
    system_prompt: str,
    api_key: str,
    model: str,
    correlation_id: str,
) -> tuple[list[DisciplineTag], int, int, int]:
    """Call the LLM for one batch. If sheets are omitted from the response,
    issue exactly ONE retry containing only the omitted sheets (with a hotter
    temperature for variance). If they remain omitted after retry, return
    them as zero-confidence drops so the caller can persist missing_inputs.

    Returns:
        (tags, total_prompt_tokens, total_cached_tokens, total_completion_tokens)
    """
    from aspire_orchestrator.services.openai_client import generate_text_async

    tags_first, usage_first, raw_first = await _run_llm_batch(
        batch_inputs=batch_inputs,
        system_prompt=system_prompt,
        api_key=api_key,
        model=model,
        correlation_id=correlation_id,
        temperature=None,  # default
        cache_layer=True,
    )
    tok_p = usage_first.get("prompt_tokens", 0)
    tok_c = usage_first.get("cached_tokens", 0)
    tok_o = usage_first.get("completion_tokens", 0)

    if tags_first is None:
        # LLM hard-failed — return zero-confidence drops for the whole batch.
        return (
            [
                DisciplineTag(
                    sheet_id=s.sheet_id,
                    discipline=None,
                    confidence=0.0,
                    reasoning=f"LLM call failed: {raw_first}",
                    needs_review=True,
                    source="llm_dropped",
                )
                for s in batch_inputs
            ],
            tok_p, tok_c, tok_o,
        )

    returned_ids = {t.sheet_id for t in tags_first}
    omitted = [s for s in batch_inputs if s.sheet_id not in returned_ids]
    if not omitted:
        return tags_first, tok_p, tok_c, tok_o

    logger.warning(
        "discipline_tagger: LLM omitted %d/%d sheets — issuing retry, corr=%s",
        len(omitted),
        len(batch_inputs),
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # Emit retry receipt (Law #2)
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts
        import uuid as _uuid
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        store_receipts([{
            "id": str(_uuid.uuid4()),
            "outcome": "success",
            "action_type": "drew.classify.llm.retry",
            "receipt_type": "drew.classify.llm.retry",
            "created_at": _dt.now(_tz.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint",
            "correlation_id": correlation_id,
            "metadata": {
                "stage": "classify",
                "omitted_count": len(omitted),
                "batch_size": len(batch_inputs),
            },
        }])
    except Exception:
        pass

    tags_retry, usage_retry, _raw_retry = await _run_llm_batch(
        batch_inputs=omitted,
        system_prompt=system_prompt,
        api_key=api_key,
        model=model,
        correlation_id=correlation_id,
        temperature=0.6,  # variance to escape the dropout pattern
        cache_layer=False,
    )
    tok_p += usage_retry.get("prompt_tokens", 0)
    tok_c += usage_retry.get("cached_tokens", 0)
    tok_o += usage_retry.get("completion_tokens", 0)

    final_tags: list[DisciplineTag] = list(tags_first)
    if tags_retry is None:
        # Retry hard-failed; mark omitted as drops.
        final_tags.extend(
            DisciplineTag(
                sheet_id=s.sheet_id,
                discipline=None,
                confidence=0.0,
                reasoning="LLM retry call failed",
                needs_review=True,
                source="llm_dropped",
            )
            for s in omitted
        )
        return final_tags, tok_p, tok_c, tok_o

    retry_index = {t.sheet_id: t for t in tags_retry}
    for s in omitted:
        retry_tag = retry_index.get(s.sheet_id)
        if retry_tag is not None:
            # Mark as llm_retry source for observability
            final_tags.append(
                DisciplineTag(
                    sheet_id=retry_tag.sheet_id,
                    discipline=retry_tag.discipline,
                    confidence=retry_tag.confidence,
                    reasoning=retry_tag.reasoning,
                    needs_review=retry_tag.needs_review,
                    source="llm_retry",
                )
            )
        else:
            # Still missing after retry → real drop, caller writes missing_input.
            final_tags.append(
                DisciplineTag(
                    sheet_id=s.sheet_id,
                    discipline=None,
                    confidence=0.0,
                    reasoning="LLM omitted sheet on initial call AND retry",
                    needs_review=True,
                    source="llm_dropped",
                )
            )

    return final_tags, tok_p, tok_c, tok_o


async def _run_llm_batch(
    *,
    batch_inputs: list[_SheetInput],
    system_prompt: str,
    api_key: str,
    model: str,
    correlation_id: str,
    temperature: float | None,
    cache_layer: bool,
) -> tuple[list[DisciplineTag] | None, dict[str, int], str]:
    """Single LLM call for one batch. Returns (tags_or_None, usage_dict, raw_or_err).

    tags is None only on hard exception. Cache lookup/write controlled via
    cache_layer (disabled on retry so we don't replay the same omission).
    """
    from aspire_orchestrator.services.openai_client import generate_text_async

    user_prompt = _build_user_prompt(batch_inputs)
    user_content_hash = compute_content_hash(user_prompt)

    if cache_layer:
        cached_llm = await get_cached_llm_response("classify", user_content_hash, model)
        if cached_llm is not None:
            logger.info(
                "9h classify_cache_hit stage=classify model=%s hash=%s corr=%s",
                model, user_content_hash[:8],
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
            try:
                from aspire_orchestrator.services.receipt_store import store_receipts
                import uuid as _uuid
                from datetime import datetime as _dt
                from datetime import timezone as _tz
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
                pass
            # Cache shape is {"tags": [<tag-dicts>]} (see set call below).
            # Older entries may be a bare list. Normalise.
            if isinstance(cached_llm, list):
                tags_list = cached_llm
            elif isinstance(cached_llm, dict) and isinstance(cached_llm.get("tags"), list):
                tags_list = cached_llm["tags"]
            else:
                tags_list = []
            raw_cached: str = json.dumps(tags_list)
            return _parse_llm_response(raw_cached, batch_inputs), {
                "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0,
            }, raw_cached

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    call_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_key": api_key,
        "timeout_seconds": float(settings.openai_timeout_seconds),
        "max_output_tokens": 1024,
    }
    if temperature is not None:
        call_kwargs["temperature"] = temperature

    try:
        llm_result = await generate_text_async(**call_kwargs)
    except TypeError:
        # generate_text_async may not accept temperature kwarg — drop it and retry.
        call_kwargs.pop("temperature", None)
        try:
            llm_result = await generate_text_async(**call_kwargs)
        except Exception as exc:
            logger.error(
                "discipline_tagger: LLM call failed (%s), corr=%s",
                type(exc).__name__,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
            return None, {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}, type(exc).__name__
    except Exception as exc:
        logger.error(
            "discipline_tagger: LLM call failed (%s), corr=%s",
            type(exc).__name__,
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        return None, {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}, type(exc).__name__

    raw_response: str = llm_result["text"]
    usage = llm_result.get("usage", {}) or {}
    usage_out = {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "cached_tokens": int(usage.get("cached_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }

    # Wave 9h: track prefix-hash stability
    if "prompt_prefix_hash" in usage:
        prefix_key = f"{model}:classify"
        observed_hash = usage["prompt_prefix_hash"]
        last_hash = _LAST_PREFIX_HASH.get(prefix_key)
        if last_hash is None:
            _LAST_PREFIX_HASH[prefix_key] = observed_hash
        elif last_hash != observed_hash:
            logger.warning(
                "9h: prefix_hash drift detected for %s — previous=%s current=%s",
                prefix_key, last_hash, observed_hash,
            )
            _LAST_PREFIX_HASH[prefix_key] = observed_hash

    parsed_tags = _parse_llm_response(raw_response, batch_inputs)

    if cache_layer:
        try:
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
            pass

    return parsed_tags, usage_out, raw_response
