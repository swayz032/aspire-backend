"""Discipline Tagger — LLM-based sheet classification for Drew Blueprint Engine.

Uses the Drew model to classify each sheet's discipline (A, S, M, E, P, FP, C, L,
Specs, Schedules, Addenda) based on the first 600 chars of OCR text.

Batches up to 5 sheets per LLM call to save tokens.
Sheets with confidence < 0.70 are marked with discipline=None and produce a
blueprint_missing_inputs row for contractor verification.

Law compliance:
  #7 — No autonomous decisions. Returns tagged list; Drew.classify() persists.
  #9 — First 600 chars of OCR text are sent to LLM; raw page content is not logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aspire_orchestrator.config.settings import resolve_openai_api_key, settings

logger = logging.getLogger(__name__)

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
    """Load discipline taxonomy KB doc. Raises if missing."""
    if not _TAXONOMY_PATH.exists():
        raise RuntimeError(
            f"Drew discipline taxonomy missing: {_TAXONOMY_PATH}. "
            "Run Wave 2 migration to populate KB scaffold."
        )
    return _TAXONOMY_PATH.read_text(encoding="utf-8")


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
    """
    if not sheets:
        return []

    taxonomy = _load_taxonomy()
    system_prompt = _build_system_prompt(taxonomy)
    api_key = resolve_openai_api_key()

    # Import here to avoid circular imports (services → openai_client → settings)
    from aspire_orchestrator.services.openai_client import generate_text_async

    all_tags: list[DisciplineTag] = []

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

        try:
            raw_response = await generate_text_async(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                api_key=api_key,
                timeout_seconds=float(settings.openai_timeout_seconds),
                max_output_tokens=1024,
            )
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

    return all_tags
