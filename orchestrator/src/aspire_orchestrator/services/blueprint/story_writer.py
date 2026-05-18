"""Story writer — Stage 4 REASON core.

Reads sheets + symbols + missing_inputs for a project, calls the LLM with KB
injection, parses the structured response, and persists story phases, assemblies,
material lines, and new missing_inputs.

Law compliance:
  Law #1: Returns structured data only. No autonomous decisions.
  Law #2: Every invocation has a receipt emitted by the caller (Drew.reason).
  Law #3: Missing env, invalid LLM response after 1 retry → raises RuntimeError.
  Law #6: All queries and inserts are scoped by suite_id. RLS enforced at DB.
  Law #9: Story markdown never logged. Only counts and truth_distribution logged.

Token budget:
  Target: 30–50k input tokens, 8–12k output tokens.
  Control: OCR text truncated to 600 chars/sheet; KB docs injected as compressed
  reference blocks; symbols summarized as a count-by-class dict per sheet.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Module-level re-exports so tests can patch at the correct module path.
# (aspire_orchestrator.services.blueprint.story_writer.generate_json_async, etc.)
# Wrapped in try/except so the module loads in environments where optional
# dependencies (openai, httpx) are not installed.
try:
    from aspire_orchestrator.services.openai_client import generate_json_async  # noqa: F401
except ImportError:
    generate_json_async = None  # type: ignore[assignment]

try:
    from aspire_orchestrator.services.supabase_client import (  # noqa: F401
        supabase_insert,
        supabase_select,
    )
except ImportError:
    supabase_insert = None  # type: ignore[assignment]
    supabase_select = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# KB paths — loaded once at module import
# ---------------------------------------------------------------------------
_KB_DIR = Path(__file__).parent / "kb" / "drew"
_PROMPT_PATH = Path(__file__).parent / "prompts" / "drew_system_prompt.md"

_MAX_OCR_CHARS_PER_SHEET = 600
_LLM_TIMEOUT_SECONDS = 120.0  # REASON is a heavier call; 2-min ceiling
_TRUTH_CONFIDENCE_FLOORS = {
    "derived": 0.85,
    "assumed": 0.70,
}
_SEAL_BOOST = 0.05

# Confidence tag patterns emitted by the LLM and parsed here
_VALID_TRUTH_TAGS = {"observed", "derived", "assumed", "field_confirmed"}


def _load_kb_doc(name: str) -> str:
    """Load a KB markdown doc, returning empty string if missing (non-fatal)."""
    path = _KB_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        _log.warning("story_writer: KB doc missing: %s", path)
        return ""


def _load_system_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Drew system prompt missing at {_PROMPT_PATH} — cannot run REASON (Law #3)"
        ) from exc


def _resolve_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY") or os.getenv("ASPIRE_OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY env var required for REASON stage (Law #3 fail-closed)"
        )
    return key


# ---------------------------------------------------------------------------
# LLM prompt construction
# ---------------------------------------------------------------------------

def _build_llm_context(
    *,
    sheets: list[dict[str, Any]],
    symbols_by_sheet: dict[str, list[dict[str, Any]]],
    existing_missing_input_descriptions: set[str],
    case_pack_hints: list,  # list[CasePackHint]
    seal_sheet_ids: set[str],
    discipline_counts: dict[str, int],
    project_id: str,
) -> list[dict[str, str]]:
    """Build the messages list for the LLM call.

    System prompt = Drew system prompt + 4 KB blocks.
    User message = structured JSON of sheets + symbols + hints.
    """
    # --- System prompt (KB injections appended) ---
    system_parts = [_load_system_prompt()]

    system_parts.append("\n\n---\n# Reference: Discipline Taxonomy\n")
    system_parts.append(_load_kb_doc("drew-discipline-taxonomy.md"))

    system_parts.append("\n\n---\n# Reference: Trade Sequence Playbook\n")
    system_parts.append(_load_kb_doc("drew-trade-sequence-playbook.md"))

    system_parts.append("\n\n---\n# Reference: Truth Class Policy\n")
    system_parts.append(_load_kb_doc("drew-truth-class-policy.md"))

    system_parts.append("\n\n---\n# Reference: Storytelling Examples\n")
    system_parts.append(_load_kb_doc("drew-storytelling-examples.md"))

    system_parts.append("\n\n---\n# Governing Laws\n")
    system_parts.append(_load_kb_doc("drew-aspire-laws.md"))

    system_message = "".join(system_parts)

    # --- User message: structured project context ---
    # Sheet summaries: OCR truncated, symbols as count dict
    sheet_summaries = []
    active_sheets = [s for s in sheets if not s.get("supersedes_id")]
    for sheet in active_sheets:
        sheet_id = str(sheet.get("id", ""))
        ocr_raw = str(sheet.get("ocr_text") or "")
        ocr_excerpt = ocr_raw[:_MAX_OCR_CHARS_PER_SHEET]
        if len(ocr_raw) > _MAX_OCR_CHARS_PER_SHEET:
            ocr_excerpt += "…"

        # Symbols: count-by-class for this sheet
        sheet_symbols = symbols_by_sheet.get(sheet_id, [])
        symbol_counts: dict[str, int] = {}
        symbol_mean_conf: float = 0.0
        if sheet_symbols:
            for sym in sheet_symbols:
                cls = str(sym.get("class") or "unknown")
                symbol_counts[cls] = symbol_counts.get(cls, 0) + 1
            confidences = [float(s.get("confidence") or 0) for s in sheet_symbols]
            symbol_mean_conf = sum(confidences) / len(confidences)

        sheet_summaries.append({
            "sheet_id": sheet_id,
            "sheet_number": str(sheet.get("sheet_number") or ""),
            "discipline": str(sheet.get("discipline") or ""),
            "scale": str(sheet.get("scale") or ""),
            "seal_detected": sheet_id in seal_sheet_ids,
            "revision": str(sheet.get("revision") or ""),
            "ocr_excerpt": ocr_excerpt,
            "symbol_counts": symbol_counts,
            "symbol_mean_confidence": round(symbol_mean_conf, 3),
        })

    # Case-pack hints
    hint_summaries = [
        {
            "project_id": h.project_id,
            "discipline_mix": h.discipline_mix,
            "phase_count": h.phase_count,
            "story_excerpt": h.story_excerpt,
            "mean_confidence": h.mean_confidence,
        }
        for h in case_pack_hints
    ]

    user_payload = {
        "project_id": project_id,
        "discipline_summary": discipline_counts,
        "sheet_count": len(active_sheets),
        "seal_sheet_count": len(seal_sheet_ids),
        "sheets": sheet_summaries,
        "existing_missing_input_descriptions": list(existing_missing_input_descriptions)[:20],
        "case_pack_hints": hint_summaries,
        "instructions": (
            "Produce a phased construction story for this project using the trade sequence "
            "playbook. Every fact must carry a truth tag. Emit missing_inputs for gaps. "
            "Apply +0.05 confidence boost to derived/assumed facts from seal_detected=true sheets. "
            "Return valid JSON matching the schema described in the system prompt."
        ),
    }

    user_message = json.dumps(user_payload, indent=2, default=str)

    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


# ---------------------------------------------------------------------------
# LLM response parser
# ---------------------------------------------------------------------------

def _parse_llm_response(
    raw: dict[str, Any],
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    seal_sheet_ids: set[str],
    model_version: str,
) -> dict[str, Any]:
    """Parse and validate the LLM structured output.

    Expected LLM response schema:
    {
        "phases": [
            {
                "phase_number": int,
                "phase_name": str,
                "markdown": str,
                "assemblies": [
                    {"type": str, "quantity": float | null, "unit": str | null,
                     "truth": str, "confidence": float | null,
                     "tariff_flag": str | null}
                ],
                "material_lines": [
                    {"line_item": str, "quantity": float | null, "unit": str | null,
                     "truth": str, "confidence": float | null,
                     "tariff_flag": str | null}
                ]
            }
        ],
        "missing_inputs": [
            {"description": str, "suggested_resolution": str | null}
        ],
        "truth_distribution": {"observed": int, "derived": int, "assumed": int},
        "mean_confidence": float
    }

    Defense-in-depth: any fact without a valid truth tag is silently dropped.
    Any fact whose confidence is below its class floor is emitted as missing_input
    instead (unless it originates from a sealed sheet, where +0.05 boost applies).
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    story_rows: list[dict[str, Any]] = []
    assembly_rows: list[dict[str, Any]] = []
    material_rows: list[dict[str, Any]] = []
    missing_input_rows: list[dict[str, Any]] = []

    truth_dist: dict[str, int] = {"observed": 0, "derived": 0, "assumed": 0, "missing": 0}
    dropped_untagged = 0
    confidence_values: list[float] = []

    phases: list[dict[str, Any]] = raw.get("phases") or []

    for phase_obj in phases:
        phase_number = int(phase_obj.get("phase_number") or 0)
        phase_name = str(phase_obj.get("phase_name") or f"Phase {phase_number}")
        markdown = str(phase_obj.get("markdown") or "")

        # Per-phase truth_distribution for this story row
        phase_truth_dist: dict[str, int] = {"observed": 0, "derived": 0, "assumed": 0}

        # --- Assemblies ---
        for asm in (phase_obj.get("assemblies") or []):
            truth_tag = str(asm.get("truth") or "").lower()
            if truth_tag not in _VALID_TRUTH_TAGS:
                dropped_untagged += 1
                continue

            confidence = _maybe_float(asm.get("confidence"))
            boosted = _apply_seal_boost(confidence, asm, seal_sheet_ids)

            if _should_demote(truth_tag, boosted):
                # Emit as missing_input instead
                desc = f"Assembly '{asm.get('type', 'unknown')}' confidence {boosted:.2f} below floor — needs field confirmation."
                missing_input_rows.append({
                    "id": str(uuid.uuid4()),
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "project_id": project_id,
                    "description": desc,
                    "suggested_resolution": "Confirm assembly scope from field measurements or additional drawings.",
                    "created_at": now_iso,
                })
                truth_dist["missing"] += 1
                continue

            tariff_flag = _normalize_tariff_flag(asm.get("tariff_flag"))
            assembly_rows.append({
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "office_id": office_id,
                "project_id": project_id,
                "type": str(asm.get("type") or ""),
                "quantity": _maybe_float(asm.get("quantity")),
                "unit": _maybe_str(asm.get("unit")),
                "truth": truth_tag,
                "created_at": now_iso,
            })
            phase_truth_dist[truth_tag] = phase_truth_dist.get(truth_tag, 0) + 1
            truth_dist[truth_tag] = truth_dist.get(truth_tag, 0) + 1
            if boosted is not None:
                confidence_values.append(boosted)

        # --- Material lines ---
        for mat in (phase_obj.get("material_lines") or []):
            truth_tag = str(mat.get("truth") or "").lower()
            if truth_tag not in _VALID_TRUTH_TAGS:
                dropped_untagged += 1
                continue

            confidence = _maybe_float(mat.get("confidence"))
            boosted = _apply_seal_boost(confidence, mat, seal_sheet_ids)

            if _should_demote(truth_tag, boosted):
                desc = f"Material '{mat.get('line_item', 'unknown')}' confidence {boosted:.2f} below floor — needs field confirmation."
                missing_input_rows.append({
                    "id": str(uuid.uuid4()),
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "project_id": project_id,
                    "description": desc,
                    "suggested_resolution": "Confirm material specification from drawings or field.",
                    "created_at": now_iso,
                })
                truth_dist["missing"] += 1
                continue

            tariff_flag = _normalize_tariff_flag(mat.get("tariff_flag"))
            material_rows.append({
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "office_id": office_id,
                "project_id": project_id,
                "line_item": str(mat.get("line_item") or ""),
                "quantity": _maybe_float(mat.get("quantity")),
                "unit": _maybe_str(mat.get("unit")),
                "truth": truth_tag,
                "tariff_flag": tariff_flag,
                "created_at": now_iso,
            })
            phase_truth_dist[truth_tag] = phase_truth_dist.get(truth_tag, 0) + 1
            truth_dist[truth_tag] = truth_dist.get(truth_tag, 0) + 1
            if boosted is not None:
                confidence_values.append(boosted)

        # Story row for this phase
        story_rows.append({
            "id": str(uuid.uuid4()),
            "suite_id": suite_id,
            "office_id": office_id,
            "project_id": project_id,
            "phase": phase_number,
            "markdown": markdown,
            "truth_distribution": phase_truth_dist,
            "created_at": now_iso,
        })

    # --- Missing inputs from LLM response ---
    for mi in (raw.get("missing_inputs") or []):
        desc = str(mi.get("description") or "").strip()
        if not desc:
            continue
        missing_input_rows.append({
            "id": str(uuid.uuid4()),
            "suite_id": suite_id,
            "office_id": office_id,
            "project_id": project_id,
            "description": desc,
            "suggested_resolution": _maybe_str(mi.get("suggested_resolution")),
            "created_at": now_iso,
        })
        truth_dist["missing"] += 1

    mean_conf = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0

    if dropped_untagged:
        _log.warning(
            "story_writer: dropped %d untagged facts for project=%s (Law #2 defense)",
            dropped_untagged,
            project_id[:8],
        )

    return {
        "story_rows": story_rows,
        "assembly_rows": assembly_rows,
        "material_rows": material_rows,
        "missing_input_rows": missing_input_rows,
        "truth_distribution": truth_dist,
        "mean_confidence": round(mean_conf, 4),
        "dropped_untagged": dropped_untagged,
    }


def _should_demote(truth_tag: str, confidence: float | None) -> bool:
    """Return True if this fact must be demoted to missing_input."""
    if confidence is None:
        return False  # No confidence provided — trust the LLM's classification
    floor = _TRUTH_CONFIDENCE_FLOORS.get(truth_tag)
    if floor is None:
        return False  # observed / field_confirmed have no floor
    return confidence < floor


def _apply_seal_boost(
    confidence: float | None,
    fact: dict[str, Any],
    seal_sheet_ids: set[str],
) -> float | None:
    """Apply +0.05 confidence boost if the source sheet has seal_detected=true."""
    if confidence is None:
        return None
    source_sheet_id = str(fact.get("source_sheet_id") or "")
    if source_sheet_id and source_sheet_id in seal_sheet_ids:
        return min(1.0, confidence + _SEAL_BOOST)
    return confidence


def _maybe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _maybe_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _normalize_tariff_flag(val: Any) -> str:
    valid = {"section_232_steel", "section_232_aluminum", "softwood_lumber", "none"}
    if val is None:
        return "none"
    s = str(val).lower().strip()
    return s if s in valid else "none"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def write_story(
    project_id: str,
    *,
    suite_id: str,
    office_id: str | None,
    correlation_id: str,
    model: str,
) -> "StoryOutput":  # noqa: F821  (StoryOutput imported below to avoid circular)
    """Read sheets + symbols + missing_inputs, call LLM, persist story.

    Args:
        project_id: Blueprint project UUID.
        suite_id: Tenant scope (Law #6).
        office_id: Sub-tenant scope (optional).
        correlation_id: Trace ID for receipts.
        model: LLM model name (from Drew.model, env-resolved).

    Returns:
        StoryOutput with counts, mean_confidence, truth_distribution, model_version.

    Raises:
        RuntimeError: On missing env vars, all sheets superseded, or invalid
                      LLM schema after one retry (fail-closed, Law #3).
    """
    from aspire_orchestrator.services.blueprint.schemas.story import StoryOutput
    from aspire_orchestrator.services.blueprint.case_pack_memory import retrieve_case_pack_hints
    from aspire_orchestrator.services.supabase_client import SupabaseClientError
    from aspire_orchestrator.config.settings import settings
    # Reference module-level re-exports for test mockability
    import aspire_orchestrator.services.blueprint.story_writer as _sw
    _generate_json_async = _sw.generate_json_async
    _supabase_insert = _sw.supabase_insert
    _supabase_select = _sw.supabase_select

    api_key = _resolve_api_key()

    # ── 1. Load active sheets (exclude superseded) ─────────────────────────
    try:
        all_sheets = await _supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except SupabaseClientError as exc:
        raise RuntimeError(
            f"REASON: failed to load sheets for project {project_id[:8]}: {exc}"
        ) from exc

    if not all_sheets:
        raise RuntimeError(
            f"REASON: no sheets found for project {project_id[:8]} "
            f"(suite={suite_id[:8]}) — cannot produce story (Law #3 fail-closed)"
        )

    active_sheets = [s for s in all_sheets if not s.get("supersedes_id")]
    if not active_sheets:
        raise RuntimeError(
            f"REASON: all {len(all_sheets)} sheets are superseded for project "
            f"{project_id[:8]} — cannot produce story"
        )

    # Collect seal-detected sheet IDs for confidence boost
    seal_sheet_ids: set[str] = {
        str(s["id"]) for s in active_sheets if s.get("seal_detected")
    }

    # Discipline counts for case-pack similarity scoring
    discipline_counts: dict[str, int] = {}
    for sheet in active_sheets:
        d = str(sheet.get("discipline") or "")
        if d:
            discipline_counts[d] = discipline_counts.get(d, 0) + 1

    # ── 2. Load symbols for active sheets ─────────────────────────────────
    active_sheet_ids = [str(s["id"]) for s in active_sheets]
    symbols_by_sheet: dict[str, list[dict[str, Any]]] = {sid: [] for sid in active_sheet_ids}

    for sheet_id in active_sheet_ids:
        try:
            sheet_symbols = await _supabase_select(
                "blueprint_symbols",
                filters=f"sheet_id=eq.{sheet_id}&suite_id=eq.{suite_id}",
            )
            symbols_by_sheet[sheet_id] = sheet_symbols
        except SupabaseClientError:
            _log.warning(
                "story_writer: failed to load symbols for sheet=%s",
                sheet_id[:8],
            )
            # Non-fatal — SEE may not have run or weights were unavailable

    # ── 3. Load existing missing_inputs (avoid duplicates) ────────────────
    try:
        existing_mis = await _supabase_select(
            "blueprint_missing_inputs",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
        existing_descriptions: set[str] = {
            str(r.get("description") or "")
            for r in existing_mis
        }
    except SupabaseClientError:
        existing_descriptions = set()
        _log.warning(
            "story_writer: failed to load existing missing_inputs for project=%s",
            project_id[:8],
        )

    # ── 4. Case-pack memory (tenant-scoped moat) ───────────────────────────
    case_pack_hints = await retrieve_case_pack_hints(
        suite_id=suite_id,
        project_context={
            "discipline_mix": list(discipline_counts.keys()),
            "sheet_count": len(active_sheets),
        },
        k=3,
    )

    # ── 5. Build LLM context ──────────────────────────────────────────────
    messages = _build_llm_context(
        sheets=active_sheets,
        symbols_by_sheet=symbols_by_sheet,
        existing_missing_input_descriptions=existing_descriptions,
        case_pack_hints=case_pack_hints,
        seal_sheet_ids=seal_sheet_ids,
        discipline_counts=discipline_counts,
        project_id=project_id,
    )

    # ── 6. Call LLM with structured output (one retry on invalid schema) ──
    raw_response: dict[str, Any] = {}
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            raw_response = await _generate_json_async(
                model=model,
                messages=messages,
                api_key=api_key,
                base_url=settings.openai_base_url,
                timeout_seconds=_LLM_TIMEOUT_SECONDS,
                max_output_tokens=12_000,
                temperature=None,  # reasoning model — no temperature
                model_profile="primary_reasoner",
            )
            if raw_response and raw_response.get("phases"):
                break  # Valid response
            _log.warning(
                "story_writer: LLM returned empty phases on attempt %d for project=%s",
                attempt + 1,
                project_id[:8],
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _log.warning(
                "story_writer: LLM call failed attempt %d for project=%s: %s",
                attempt + 1,
                project_id[:8],
                type(exc).__name__,
            )

    if not raw_response or not raw_response.get("phases"):
        # Both attempts failed — emit invalid_output receipt via caller, then raise
        raise RuntimeError(
            f"REASON: LLM returned invalid/empty response for project {project_id[:8]} "
            f"after 2 attempts. Last error: {last_exc} (Law #3 fail-closed)"
        )

    # ── 7. Parse and validate response ────────────────────────────────────
    parsed = _parse_llm_response(
        raw_response,
        project_id=project_id,
        suite_id=suite_id,
        office_id=office_id,
        seal_sheet_ids=seal_sheet_ids,
        model_version=model,
    )

    story_rows = parsed["story_rows"]
    assembly_rows = parsed["assembly_rows"]
    material_rows = parsed["material_rows"]
    new_missing_input_rows = [
        mi for mi in parsed["missing_input_rows"]
        if str(mi.get("description") or "") not in existing_descriptions
    ]
    truth_dist: dict[str, int] = parsed["truth_distribution"]
    mean_conf: float = parsed["mean_confidence"]

    # ── 8. Persist to DB ──────────────────────────────────────────────────
    story_id = story_rows[0]["id"] if story_rows else str(uuid.uuid4())

    # Story phases
    for row in story_rows:
        try:
            await _supabase_insert("blueprint_story", row)
        except SupabaseClientError as exc:
            _log.error(
                "story_writer: failed to insert story row project=%s phase=%s: %s",
                project_id[:8],
                row.get("phase"),
                type(exc).__name__,
            )
            raise RuntimeError(
                f"REASON: failed to persist story phase {row.get('phase')} "
                f"for project {project_id[:8]}: {exc}"
            ) from exc

    # Assemblies
    for row in assembly_rows:
        try:
            await _supabase_insert("blueprint_assemblies", row)
        except SupabaseClientError as exc:
            _log.warning(
                "story_writer: failed to insert assembly for project=%s: %s",
                project_id[:8],
                type(exc).__name__,
            )
            # Non-fatal — story is the primary output

    # Materials
    for row in material_rows:
        try:
            await _supabase_insert("blueprint_materials", row)
        except SupabaseClientError as exc:
            _log.warning(
                "story_writer: failed to insert material for project=%s: %s",
                project_id[:8],
                type(exc).__name__,
            )

    # Missing inputs (deduplicated)
    for row in new_missing_input_rows:
        try:
            await _supabase_insert("blueprint_missing_inputs", row)
        except SupabaseClientError as exc:
            _log.warning(
                "story_writer: failed to insert missing_input for project=%s: %s",
                project_id[:8],
                type(exc).__name__,
            )

    # ── 9. Return StoryOutput (no story markdown in return — Law #9) ──────
    _log.info(
        "story_writer: project=%s phases=%d assemblies=%d materials=%d "
        "missing=%d mean_conf=%.3f corr=%s",
        project_id[:8],
        len(story_rows),
        len(assembly_rows),
        len(material_rows),
        len(new_missing_input_rows),
        mean_conf,
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    return StoryOutput(
        story_id=story_id,
        project_id=project_id,
        suite_id=suite_id,
        phase_count=len(story_rows),
        assembly_count=len(assembly_rows),
        material_count=len(material_rows),
        missing_input_count=len(new_missing_input_rows) + len(existing_descriptions),
        mean_confidence=mean_conf,
        truth_distribution=truth_dist,
        model_version=model,
    )
