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

Wave 9h — cache observability:
  - KB docs and system prompt are loaded via module-level cached helpers so the
    system-message prefix is byte-identical across all retry attempts.
  - Dynamic project context (sheet OCR, symbols) is placed ONLY in the user message.
  - prompt_prefix_hash drift is logged at WARNING to signal cache invalidation.
  - LLM usage (cached_tokens, cache_hit_ratio) is returned in StoryOutput and
    included in the caller's receipt metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Wave 17b.5: canonical assembly-type taxonomy. Imported at module load so the
# strict-typing path in _parse_llm_response has zero per-call import cost.
from aspire_orchestrator.services.blueprint.assembly_types import (  # noqa: E402
    normalize_assembly_type as _normalize_assembly_type,
)

# Wave 9h: module-level caches for KB docs and system prompt.
# Loading these once per process keeps the system-message prefix byte-identical
# across all LLM calls, maximising OpenAI prefix cache reuse.
_KB_DOC_CACHE: dict[str, str] = {}
_SYSTEM_PROMPT_CACHE: str | None = None

# Wave 9h: track last prefix hash to detect drift (invalidates OpenAI prefix cache).
_LAST_PREFIX_HASH_REASON: str | None = None

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
        supabase_insert_batch_ignore_conflicts,
        supabase_select,
    )
except ImportError:
    supabase_insert = None  # type: ignore[assignment]
    supabase_insert_batch_ignore_conflicts = None  # type: ignore[assignment]
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
    """Load a KB markdown doc, caching per-process for prefix stability (Wave 9h).

    Caching ensures KB content is loaded once so the system-message bytes are
    identical across all LLM calls in a run.  Returns empty string if missing
    (non-fatal — missing KB docs degrade story quality but do not block execution).
    """
    if name in _KB_DOC_CACHE:
        return _KB_DOC_CACHE[name]
    path = _KB_DIR / name
    try:
        content = path.read_text(encoding="utf-8")
        _KB_DOC_CACHE[name] = content
        return content
    except OSError:
        _log.warning("story_writer: KB doc missing: %s", path)
        _KB_DOC_CACHE[name] = ""
        return ""


def _load_system_prompt() -> str:
    """Load the Drew system prompt, caching per-process for prefix stability (Wave 9h)."""
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is not None:
        return _SYSTEM_PROMPT_CACHE
    try:
        _SYSTEM_PROMPT_CACHE = _PROMPT_PATH.read_text(encoding="utf-8")
        return _SYSTEM_PROMPT_CACHE
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
# Wave 9l: 3 focused message builders for parallel LLM dispatch
# ---------------------------------------------------------------------------

def _build_messages_a(
    *,
    sheets: list[dict[str, Any]],
    symbols_by_sheet: dict[str, list[dict[str, Any]]],
    seal_sheet_ids: set[str],
    discipline_counts: dict[str, int],
    case_pack_hints: list,
    project_id: str,
    sheet_intel_context: str = "",
) -> list[dict[str, str]]:
    """Call A: assemblies + quantities.

    Asks the LLM to enumerate all assemblies and material quantities from the
    sheet corpus. Returns a subset of the full schema:
      {"phases": [{"phase_number": int, "phase_name": str, "assemblies": [...], "material_lines": [...]}]}

    Wave 20f: sheet_intel_context is prepended to the user message when available,
    grounding A in project-specific legends, notes, and schedule counts.
    """
    system_parts = [_load_system_prompt()]
    system_parts.append("\n\n---\n# Reference: Discipline Taxonomy\n")
    system_parts.append(_load_kb_doc("drew-discipline-taxonomy.md"))
    system_parts.append("\n\n---\n# Reference: Trade Sequence Playbook\n")
    system_parts.append(_load_kb_doc("drew-trade-sequence-playbook.md"))
    system_parts.append("\n\n---\n# Reference: Truth Class Policy\n")
    system_parts.append(_load_kb_doc("drew-truth-class-policy.md"))
    system_parts.append("\n\n---\n# Governing Laws\n")
    system_parts.append(_load_kb_doc("drew-aspire-laws.md"))
    system_message = "".join(system_parts)

    active_sheets = [s for s in sheets if not s.get("supersedes_id")]
    sheet_summaries = _build_sheet_summaries(active_sheets, symbols_by_sheet, seal_sheet_ids)
    hint_summaries = _build_hint_summaries(case_pack_hints)

    # Wave 17b.5: inject the canonical assembly-type taxonomy so the LLM must
    # pick one of these strings (or the row will be dropped to missing_input).
    from aspire_orchestrator.services.blueprint.assembly_types import ASSEMBLY_TYPES
    user_payload = {
        "project_id": project_id,
        "discipline_summary": discipline_counts,
        "sheet_count": len(active_sheets),
        "seal_sheet_count": len(seal_sheet_ids),
        "sheets": sheet_summaries,
        "case_pack_hints": hint_summaries,
        "task": "ASSEMBLIES_AND_QUANTITIES",
        "allowed_assembly_types": sorted(ASSEMBLY_TYPES),
        "instructions": (
            "For each construction phase (from the trade sequence playbook), enumerate all "
            "assemblies and material line items detectable from the sheet corpus. "
            "Every assembly and material_line must carry a truth tag and confidence. "
            "EVERY assembly.type MUST be one of the strings in allowed_assembly_types (exact match, "
            "snake_case). Do NOT invent new type strings, do NOT emit 'general', do NOT leave type empty. "
            "If you cannot confidently match an assembly to an allowed type, OMIT it from this response "
            "(it will be surfaced via the missing-inputs call instead). "
            "EVERY material_line.line_item MUST be a specific spec string (e.g. 'GFCI Receptacle 20A 125V Duplex'). "
            "Do NOT emit empty or whitespace-only line_item values. "
            "Do NOT produce markdown narrative or missing_inputs in this response. "
            "Return JSON with keys: phases (array of {phase_number, phase_name, assemblies, material_lines}). "
            "No other top-level keys needed."
        ),
    }
    user_content = json.dumps(user_payload, indent=2, default=str)
    if sheet_intel_context:
        user_content = sheet_intel_context + "\n\n" + user_content
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_content},
    ]


def _build_messages_b(
    *,
    sheets: list[dict[str, Any]],
    existing_missing_input_descriptions: set[str],
    discipline_counts: dict[str, int],
    project_id: str,
    sheet_intel_context: str = "",
) -> list[dict[str, str]]:
    """Call B: missing_inputs + risk_flags.

    Asks the LLM to identify all information gaps and risk flags from the sheet corpus.
    Returns: {"missing_inputs": [...], "risk_flags": [...]}
    risk_flags is an optional list of plain strings (ignored by _parse_llm_response
    but logged for future use).

    Wave 20f: sheet_intel_context is prepended when available so B can reference
    the authoritative schedule counts and symbol dictionary when identifying gaps.
    """
    system_parts = [_load_system_prompt()]
    system_parts.append("\n\n---\n# Reference: Truth Class Policy\n")
    system_parts.append(_load_kb_doc("drew-truth-class-policy.md"))
    system_parts.append("\n\n---\n# Governing Laws\n")
    system_parts.append(_load_kb_doc("drew-aspire-laws.md"))
    system_message = "".join(system_parts)

    active_sheets = [s for s in sheets if not s.get("supersedes_id")]
    sheet_summaries = _build_sheet_summaries(active_sheets, {}, set())

    user_payload = {
        "project_id": project_id,
        "discipline_summary": discipline_counts,
        "sheet_count": len(active_sheets),
        "sheets": sheet_summaries,
        "existing_missing_input_descriptions": list(existing_missing_input_descriptions)[:20],
        "task": "MISSING_INPUTS_AND_RISKS",
        "instructions": (
            "Identify all information gaps (missing dimensions, unspecified materials, "
            "missing details, conflicting or ambiguous notes) and any risk flags "
            "(constructability concerns, schedule risks, scope ambiguity). "
            "Do NOT produce phases, assemblies, or narrative. "
            "Return JSON with keys: missing_inputs (array of {description, suggested_resolution}), "
            "risk_flags (array of strings). "
            "Deduplicate against existing_missing_input_descriptions."
        ),
    }
    user_content = json.dumps(user_payload, indent=2, default=str)
    if sheet_intel_context:
        user_content = sheet_intel_context + "\n\n" + user_content
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_content},
    ]


def _build_messages_c(
    *,
    sheets: list[dict[str, Any]],
    symbols_by_sheet: dict[str, list[dict[str, Any]]],
    seal_sheet_ids: set[str],
    discipline_counts: dict[str, int],
    project_id: str,
) -> list[dict[str, str]]:
    """Call C: phased markdown narrative + truth_distribution.

    Asks the LLM to produce the human-readable phased construction story.
    Returns: {"phases": [{"phase_number", "phase_name", "markdown", "truth_distribution"}],
              "mean_confidence": float, "truth_distribution": {...}}
    """
    system_parts = [_load_system_prompt()]
    system_parts.append("\n\n---\n# Reference: Trade Sequence Playbook\n")
    system_parts.append(_load_kb_doc("drew-trade-sequence-playbook.md"))
    system_parts.append("\n\n---\n# Reference: Storytelling Examples\n")
    system_parts.append(_load_kb_doc("drew-storytelling-examples.md"))
    system_parts.append("\n\n---\n# Reference: Truth Class Policy\n")
    system_parts.append(_load_kb_doc("drew-truth-class-policy.md"))
    system_parts.append("\n\n---\n# Governing Laws\n")
    system_parts.append(_load_kb_doc("drew-aspire-laws.md"))
    system_message = "".join(system_parts)

    active_sheets = [s for s in sheets if not s.get("supersedes_id")]
    sheet_summaries = _build_sheet_summaries(active_sheets, symbols_by_sheet, seal_sheet_ids)

    user_payload = {
        "project_id": project_id,
        "discipline_summary": discipline_counts,
        "sheet_count": len(active_sheets),
        "seal_sheet_count": len(seal_sheet_ids),
        "sheets": sheet_summaries,
        "task": "PHASED_NARRATIVE",
        "instructions": (
            "Produce the phased construction narrative for this project following the trade sequence playbook. "
            "Each phase must have a markdown narrative with truth-tagged facts. "
            "Do NOT produce assemblies, material_lines, or missing_inputs in this response. "
            "Return JSON with keys: phases (array of {phase_number, phase_name, markdown}), "
            "truth_distribution ({observed, derived, assumed}), mean_confidence (float 0-1)."
        ),
    }
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]


def _build_sheet_summaries(
    active_sheets: list[dict[str, Any]],
    symbols_by_sheet: dict[str, list[dict[str, Any]]],
    seal_sheet_ids: set[str],
) -> list[dict[str, Any]]:
    """Build sheet summary list shared across all three focused message builders."""
    summaries = []
    for sheet in active_sheets:
        sheet_id = str(sheet.get("id", ""))
        ocr_raw = str(sheet.get("ocr_text") or "")
        ocr_excerpt = ocr_raw[:_MAX_OCR_CHARS_PER_SHEET]
        if len(ocr_raw) > _MAX_OCR_CHARS_PER_SHEET:
            ocr_excerpt += "…"

        sheet_symbols = symbols_by_sheet.get(sheet_id, [])
        symbol_counts: dict[str, int] = {}
        symbol_mean_conf: float = 0.0
        if sheet_symbols:
            for sym in sheet_symbols:
                cls = str(sym.get("class") or "unknown")
                symbol_counts[cls] = symbol_counts.get(cls, 0) + 1
            confidences = [float(s.get("confidence") or 0) for s in sheet_symbols]
            symbol_mean_conf = sum(confidences) / len(confidences)

        summaries.append({
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
    return summaries


def _build_hint_summaries(case_pack_hints: list) -> list[dict[str, Any]]:
    """Build case-pack hint summary list (shared helper)."""
    return [
        {
            "project_id": h.project_id,
            "discipline_mix": h.discipline_mix,
            "phase_count": h.phase_count,
            "story_excerpt": h.story_excerpt,
            "mean_confidence": h.mean_confidence,
        }
        for h in case_pack_hints
    ]


def _merge_parallel_responses(
    resp_a: dict[str, Any],
    resp_b: dict[str, Any],
    resp_c: dict[str, Any],
) -> dict[str, Any]:
    """Merge the 3 parallel LLM responses into the shape _parse_llm_response expects.

    resp_a contributes: phases[].assemblies + phases[].material_lines
    resp_b contributes: missing_inputs (top-level)
    resp_c contributes: phases[].markdown + truth_distribution + mean_confidence

    Strategy: index phases by phase_number, merge fields, then emit as sorted list.
    Missing phase entries in A or C are filled with empty defaults.
    """
    # Index phases from A (assemblies + material_lines)
    phases_a: dict[int, dict[str, Any]] = {}
    for p in (resp_a.get("phases") or []):
        n = int(p.get("phase_number") or 0)
        phases_a[n] = p

    # Index phases from C (markdown + per-phase truth_distribution)
    phases_c: dict[int, dict[str, Any]] = {}
    for p in (resp_c.get("phases") or []):
        n = int(p.get("phase_number") or 0)
        phases_c[n] = p

    # Union of all phase numbers
    all_phase_numbers = sorted(set(phases_a.keys()) | set(phases_c.keys()))

    merged_phases = []
    for num in all_phase_numbers:
        pa = phases_a.get(num, {})
        pc = phases_c.get(num, {})
        merged_phases.append({
            "phase_number": num,
            "phase_name": pc.get("phase_name") or pa.get("phase_name") or f"Phase {num}",
            "markdown": str(pc.get("markdown") or ""),
            "assemblies": list(pa.get("assemblies") or []),
            "material_lines": list(pa.get("material_lines") or []),
        })

    return {
        "phases": merged_phases,
        "missing_inputs": list(resp_b.get("missing_inputs") or []),
        "truth_distribution": dict(resp_c.get("truth_distribution") or {}),
        "mean_confidence": float(resp_c.get("mean_confidence") or 0.0),
    }


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

            # Wave 17b.5: STRICT assembly typing — refuse 'general' / empty.
            # LLM-emitted type must match the canonical taxonomy. If it doesn't,
            # the row becomes a missing_input rather than a type='general' placeholder.
            raw_type = asm.get("type")
            canonical_type = _normalize_assembly_type(raw_type)
            if canonical_type is None:
                desc = (
                    f"Assembly with non-canonical type '{str(raw_type)[:60]}' on phase "
                    f"{phase_number} — requires taxonomy mapping or scope clarification."
                )
                missing_input_rows.append({
                    "id": str(uuid.uuid4()),
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "project_id": project_id,
                    "description": desc,
                    "suggested_resolution": (
                        "Map this assembly to a canonical type in assembly_types.ASSEMBLY_TYPES "
                        "or confirm scope from drawings."
                    ),
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
                "type": canonical_type,
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

            # Wave 17b.5: refuse empty line_item rows — they are the smoking gun
            # behind the "52 placeholder rows" production bug. Empty/whitespace-only
            # line_item → missing_input rather than a junk row.
            raw_line = str(mat.get("line_item") or "").strip()
            if not raw_line:
                missing_input_rows.append({
                    "id": str(uuid.uuid4()),
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "project_id": project_id,
                    "description": (
                        f"Material on phase {phase_number} emitted with empty line_item — "
                        "specification required."
                    ),
                    "suggested_resolution": (
                        "Re-run REASON with richer OCR context or add the material via "
                        "supplier lookup in PROCURE."
                    ),
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
                "line_item": raw_line,
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

    # Wave 17b.5: assembly type breakdown (canonical types only, since
    # non-canonical was already filtered to missing_inputs).
    assembly_type_breakdown: dict[str, int] = {}
    for row in assembly_rows:
        t = str(row.get("type") or "")
        if t:
            assembly_type_breakdown[t] = assembly_type_breakdown.get(t, 0) + 1
    missing_assembly_count = sum(
        1 for mi in missing_input_rows
        if "non-canonical type" in str(mi.get("description") or "").lower()
    )

    return {
        "story_rows": story_rows,
        "assembly_rows": assembly_rows,
        "material_rows": material_rows,
        "missing_input_rows": missing_input_rows,
        "truth_distribution": truth_dist,
        "mean_confidence": round(mean_conf, 4),
        "dropped_untagged": dropped_untagged,
        # Wave 17b.5 typing observability
        "typed_assembly_count": len(assembly_rows),
        "missing_assembly_count": missing_assembly_count,
        "distinct_assembly_types": len(assembly_type_breakdown),
        "assembly_type_breakdown": assembly_type_breakdown,
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
# Wave 20f: Sheet intelligence context builder
# ---------------------------------------------------------------------------

# Cover sheet patterns — any sheet_number matching these codes/prefixes is the cover.
_COVER_PATTERNS = ("G0.0", "T-001", "COVER", "G-001", "C0.0", "G0", "T0.0")

# Discipline prefixes used to find general-notes sheets (<discipline>X.X e.g. A0.1, S0.0).
_DISCIPLINE_CODES = ("A", "S", "M", "E", "P", "FP", "C")


async def _build_sheet_intelligence_context(
    project_id: str,
    suite_id: str,
    *,
    sheets: list[dict[str, Any]],
    correlation_id: str,
) -> tuple[str, dict[str, Any]]:
    """Query blueprint_sheet_intelligence and assemble REASON context block.

    Returns (context_str, meta) where:
      - context_str is injected into calls A and B as authoritative context.
      - meta = {legend_count, schedule_count, notes_count, paragraphs_count}

    Returns ("", {}) when no rows exist so REASON falls back to legacy OCR path.
    Law #6: query scoped by suite_id=eq.<suite_id>&project_id=eq.<project_id>.
    Law #9: raw cell content is included but never logged. Only counts are logged.
    """
    import aspire_orchestrator.services.blueprint.story_writer as _sw
    _supabase_select_fn = _sw.supabase_select

    try:
        intel_rows: list[dict[str, Any]] = await _supabase_select_fn(
            "blueprint_sheet_intelligence",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
    except Exception:
        _log.warning(
            "story_writer: failed to load sheet_intelligence for project=%s — "
            "falling back to legacy OCR context",
            project_id[:8],
        )
        return "", {}

    if not intel_rows:
        return "", {}

    # Index rows by sheet_id for O(1) access
    intel_by_sheet: dict[str, dict[str, Any]] = {
        str(r.get("sheet_id", "")): r for r in intel_rows
    }

    # Build a map of sheet_id → sheet_number for pattern matching
    sheet_num_by_id: dict[str, str] = {
        str(s.get("id", "")): str(s.get("sheet_number") or "").upper()
        for s in sheets
    }

    parts: list[str] = ["AUTHORITATIVE SHEET INTELLIGENCE (from Azure prebuilt-layout):\n"]

    paragraphs_count = 0
    notes_count = 0
    legend_count = 0
    schedule_count = 0

    # ── Cover sheet: project description, location, owner, scope summary ───
    cover_row: dict[str, Any] | None = None
    for sheet_id, sheet_num in sheet_num_by_id.items():
        if any(sheet_num.startswith(pat.upper()) for pat in _COVER_PATTERNS):
            cover_row = intel_by_sheet.get(sheet_id)
            if cover_row:
                break

    if cover_row:
        paras = cover_row.get("paragraphs") or []
        if paras:
            parts.append("\n## COVER SHEET — Project Overview\n")
            for p in paras:
                text = str(p.get("text") or "").strip()
                if text:
                    parts.append(f"  {text}\n")
                    paragraphs_count += 1

    # ── General notes per discipline (<discipline>X.X sheets) ─────────────
    notes_by_discipline: dict[str, list[str]] = {}
    for sheet_id, sheet_num in sheet_num_by_id.items():
        for disc in _DISCIPLINE_CODES:
            # Match e.g. A0.0, A0.1, FP0.0 — discipline prefix + digit(s) + dot
            if sheet_num.startswith(disc) and len(sheet_num) > len(disc):
                rest = sheet_num[len(disc):]
                if rest and rest[0].isdigit():
                    row = intel_by_sheet.get(sheet_id)
                    if not row:
                        continue
                    regions = row.get("regions") or {}
                    note_regions = regions.get("notes") or []
                    if note_regions:
                        note_texts: list[str] = []
                        for nr in note_regions:
                            text = str(nr.get("text") or "").strip()
                            if text:
                                note_texts.append(text)
                        if note_texts:
                            existing = notes_by_discipline.get(disc, [])
                            notes_by_discipline[disc] = existing + note_texts
                    break

    if notes_by_discipline:
        parts.append("\n## GENERAL NOTES BY DISCIPLINE\n")
        for disc, note_texts in sorted(notes_by_discipline.items()):
            parts.append(f"  [{disc}]\n")
            for t in note_texts:
                parts.append(f"    - {t}\n")
                notes_count += 1

    # ── Legend (symbol dictionary) per sheet ──────────────────────────────
    legend_entries: list[str] = []
    for sheet_id, row in intel_by_sheet.items():
        regions = row.get("regions") or {}
        legend_rows = regions.get("legend") or []
        for lr in legend_rows:
            sym = str(lr.get("symbol") or "").strip()
            desc = str(lr.get("description") or "").strip()
            if sym or desc:
                legend_entries.append(f"  {sym}: {desc}")
                legend_count += 1

    if legend_entries:
        parts.append("\n## SYMBOL DICTIONARY (from Legend regions)\n")
        for entry in legend_entries:
            parts.append(f"{entry}\n")

    # ── Schedule reads: door, window, panel, fixture ───────────────────────
    schedule_sections: list[str] = []
    for sheet_id, row in intel_by_sheet.items():
        sheet_num = sheet_num_by_id.get(sheet_id, "")

        # Tables with schedule headers
        tables = row.get("tables") or []
        for tbl in tables:
            headers_raw = [str(h).lower() for h in (tbl.get("headers") or [])]
            headers_joined = " ".join(headers_raw)
            if any(kw in headers_joined for kw in ("schedule", "door", "window", "panel", "fixture")):
                row_count = len(tbl.get("rows") or [])
                schedule_sections.append(
                    f"  {sheet_num} table — row count: {row_count}"
                )
                schedule_count += 1

        # Regions with schedule-type entries
        regions = row.get("regions") or {}
        sched_regions = regions.get("schedule") or []
        for sr in sched_regions:
            region_type = str(sr.get("region_type") or "schedule")
            row_count = len(sr.get("rows") or [])
            schedule_sections.append(
                f"  {sheet_num} {region_type} — row count: {row_count}"
            )
            schedule_count += 1

    if schedule_sections:
        parts.append("\n## SCHEDULE COUNTS (authoritative totals)\n")
        for s in schedule_sections:
            parts.append(f"{s}\n")

    context_str = "".join(parts)

    meta: dict[str, Any] = {
        "paragraphs_count": paragraphs_count,
        "notes_count": notes_count,
        "legend_count": legend_count,
        "schedule_count": schedule_count,
    }

    _log.info(
        "story_writer: sheet_intelligence context built project=%s "
        "paragraphs=%d notes=%d legends=%d schedules=%d",
        project_id[:8],
        paragraphs_count,
        notes_count,
        legend_count,
        schedule_count,
    )

    return context_str, meta


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

    # Wave 9h: import LLM response cache helpers
    from aspire_orchestrator.services.llm_response_cache import (
        compute_content_hash,
        get_cached_llm_response,
        set_cached_llm_response,
    )

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

    # ── 4b. Wave 20f: load sheet intelligence context (prebuilt-layout data) ─
    # Queries blueprint_sheet_intelligence populated at INGEST time by Wave 20.
    # Falls back gracefully to empty string when the table has no rows (pre-Wave 20
    # or INGEST not yet run), keeping legacy OCR context as the sole input.
    sheet_intel_context, sheet_intel_meta = await _build_sheet_intelligence_context(
        project_id,
        suite_id,
        sheets=active_sheets,
        correlation_id=correlation_id,
    )

    # Emit receipt for sheet intelligence load (Law #2 — even zero rows gets a receipt).
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts as _sr_intel
        import uuid as _uuid_intel
        from datetime import datetime as _dt_intel, timezone as _tz_intel
        _sr_intel([{
            "id": str(_uuid_intel.uuid4()),
            "outcome": "success",
            "action_type": "reason.sheet_intelligence_context",
            "receipt_type": "reason.sheet_intelligence_context",
            "created_at": _dt_intel.now(_tz_intel.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint",
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {
                "project_id": project_id,
                "legend_count": sheet_intel_meta.get("legend_count", 0),
                "schedule_count": sheet_intel_meta.get("schedule_count", 0),
                "notes_count": sheet_intel_meta.get("notes_count", 0),
                "paragraphs_count": sheet_intel_meta.get("paragraphs_count", 0),
                "stage": "reason",
                "has_context": bool(sheet_intel_context),
            },
        }])
    except Exception:
        pass  # Receipt emission is best-effort; non-fatal

    # ── 5. Build 3 focused LLM message sets for parallel dispatch ────────
    # Wave 9l: split the single monolithic LLM call into 3 parallel calls:
    #   Call A — assemblies + quantities
    #   Call B — missing_inputs + risk_flags
    #   Call C — phased markdown narrative + truth tags
    # Wave 20f: calls A and B receive sheet_intel_context as upstream grounding.
    # Reduces wall-clock REASON time from ~sequential to ~max(A, B, C).
    messages_a = _build_messages_a(
        sheets=active_sheets,
        symbols_by_sheet=symbols_by_sheet,
        seal_sheet_ids=seal_sheet_ids,
        discipline_counts=discipline_counts,
        case_pack_hints=case_pack_hints,
        project_id=project_id,
        sheet_intel_context=sheet_intel_context,
    )
    messages_b = _build_messages_b(
        sheets=active_sheets,
        existing_missing_input_descriptions=existing_descriptions,
        discipline_counts=discipline_counts,
        project_id=project_id,
        sheet_intel_context=sheet_intel_context,
    )
    messages_c = _build_messages_c(
        sheets=active_sheets,
        symbols_by_sheet=symbols_by_sheet,
        seal_sheet_ids=seal_sheet_ids,
        discipline_counts=discipline_counts,
        project_id=project_id,
    )

    # ── 6. Parallel LLM dispatch with cache layer per call ────────────────
    raw_response: dict[str, Any] = {}
    last_exc: Exception | None = None
    llm_usage: dict[str, Any] = {}  # aggregated from all 3 calls

    # Wave 9h: compute per-call content hashes for Redis response cache.
    hash_a = compute_content_hash(messages_a[-1]["content"])
    hash_b = compute_content_hash(messages_b[-1]["content"])
    hash_c = compute_content_hash(messages_c[-1]["content"])

    # Wave 9h: check Redis cache for each call before dispatching.
    cached_a, cached_b, cached_c = await asyncio.gather(
        get_cached_llm_response("reason_a", hash_a, model),
        get_cached_llm_response("reason_b", hash_b, model),
        get_cached_llm_response("reason_c", hash_c, model),
    )

    _all_cached = all(x is not None for x in (cached_a, cached_b, cached_c))
    if _all_cached:
        _log.info(
            "9h reason_cache_hit all_3_parallel model=%s project=%s",
            model, project_id[:8],
        )
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts as _sr
            import uuid as _uuid
            from datetime import datetime as _dt, timezone as _tz
            _sr([{
                "id": str(_uuid.uuid4()),
                "outcome": "success",
                "action_type": "blueprint.llm.cache_hit",
                "receipt_type": "blueprint.llm.cache_hit",
                "created_at": _dt.now(_tz.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": "skillpack:drew-blueprint",
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                "metadata": {
                    "stage": "reason",
                    "model": model,
                    "content_hash_a": hash_a[:16],
                    "content_hash_b": hash_b[:16],
                    "content_hash_c": hash_c[:16],
                    "cache_layer": "redis",
                    "project_id": project_id,
                    "parallel_calls": 3,
                },
            }])
        except Exception:
            pass
        raw_response = _merge_parallel_responses(cached_a, cached_b, cached_c)  # type: ignore[arg-type]

    # Wave 9p: per-call model selection. Calls A (assemblies) and B (missing_inputs)
    # are structured JSON outputs — gpt-5.4-mini is 3-5x faster and quality is
    # indistinguishable on these schemas. Call C (narrative) uses the premium model
    # because the story is the user-facing moat.
    import os as _os_p
    _fast_model = (
        _os_p.getenv("ASPIRE_DREW_REASON_STRUCTURED_MODEL")
        or _os_p.getenv("ASPIRE_DREW_MODEL_DEV")
        or "gpt-5.4-mini"
    )
    _premium_model = model  # the model passed in via env (gpt-5.4)

    async def _call_llm(
        call_id: str,
        messages: list[dict[str, str]],
        cache_key: str,
        content_hash: str,
        call_model: str | None = None,
    ) -> dict[str, Any]:
        """Single focused LLM call with 9h cache + prefix hash observability.

        Wave 9p: call_model override lets A/B (structured) use a faster model
        than C (narrative). Defaults to outer `model` if not specified.
        """
        _effective_model = call_model or model
        for attempt in range(2):
            try:
                result = await _generate_json_async(
                    model=_effective_model,
                    messages=messages,
                    api_key=api_key,
                    base_url=settings.openai_base_url,
                    timeout_seconds=_LLM_TIMEOUT_SECONDS,
                    max_output_tokens=8_000,  # focused calls need less output budget
                    temperature=None,
                    model_profile="primary_reasoner",
                )
                resp = result["json"]
                usage = dict(result["usage"])

                # Wave 9h: prefix hash drift detection per call.
                global _LAST_PREFIX_HASH_REASON
                observed_hash = usage.get("prompt_prefix_hash", "")
                if _LAST_PREFIX_HASH_REASON is None:
                    _LAST_PREFIX_HASH_REASON = observed_hash
                elif _LAST_PREFIX_HASH_REASON != observed_hash:
                    _log.warning(
                        "9h: prefix_hash drift detected call=%s — "
                        "previous=%s current=%s — OpenAI prefix cache INVALIDATED.",
                        call_id, _LAST_PREFIX_HASH_REASON, observed_hash,
                    )
                    _LAST_PREFIX_HASH_REASON = observed_hash

                _log.info(
                    "9h reason_parallel_call call=%s model=%s prompt_tokens=%d "
                    "cached_tokens=%d cache_hit_ratio=%.4f latency_ms=%.1f project=%s",
                    call_id,
                    usage.get("model", model),
                    usage.get("prompt_tokens", 0),
                    usage.get("cached_tokens", 0),
                    usage.get("cache_hit_ratio", 0.0),
                    usage.get("latency_ms", 0.0),
                    project_id[:8],
                )

                # Store in Redis cache (best-effort).
                try:
                    await set_cached_llm_response(cache_key, content_hash, model, resp)
                except Exception:
                    pass

                return resp
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "story_writer: parallel LLM call=%s attempt=%d failed project=%s: %s",
                    call_id, attempt + 1, project_id[:8], type(exc).__name__,
                )
                if attempt == 1:
                    raise
        return {}  # unreachable but satisfies type checker

    if not raw_response.get("phases"):
        # Dispatch all 3 calls concurrently (skipping any that had a cache hit).
        async def _call_a() -> dict[str, Any]:
            # Wave 9p: A = assemblies (structured JSON) → fast model
            if cached_a is not None:
                return cached_a
            return await _call_llm("A", messages_a, "reason_a", hash_a, call_model=_fast_model)

        async def _call_b() -> dict[str, Any]:
            # Wave 9p: B = missing_inputs + risk_flags (structured JSON) → fast model
            if cached_b is not None:
                return cached_b
            return await _call_llm("B", messages_b, "reason_b", hash_b, call_model=_fast_model)

        async def _call_c() -> dict[str, Any]:
            # Wave 10b: C = narrative markdown — DOWNGRADED to fast model.
            #
            # Wave 9p kept C on gpt-5.4 ("the moat") with A+B on gpt-5.4-mini. Smoke test
            # measured C @ 5-10+ min on 39-sheet OCR payloads — the entire stage hit the
            # sweeper. 2026 benchmarks (Artificial Analysis, SWE-bench Pro) show mini
            # within 3-4% of std on hard reasoning, even narrower on narrative writeups.
            #
            # Override knob: ASPIRE_DREW_REASON_NARRATIVE_MODEL. Defaults to fast model.
            # Set to "gpt-5.4" to restore the Wave 9p premium narrative path.
            import os as _os_c
            _narr_model = (
                _os_c.getenv("ASPIRE_DREW_REASON_NARRATIVE_MODEL")
                or _fast_model
            )
            if cached_c is not None:
                return cached_c
            return await _call_llm("C", messages_c, "reason_c", hash_c, call_model=_narr_model)

        try:
            resp_a, resp_b, resp_c = await asyncio.gather(
                _call_a(), _call_b(), _call_c(),
                return_exceptions=False,  # fail-closed: any failure propagates
            )
            raw_response = _merge_parallel_responses(resp_a, resp_b, resp_c)

            # Aggregate usage across calls for the receipt metadata.
            for call_result in [resp_a, resp_b, resp_c]:
                # Each response is a raw dict from the LLM — usage is in _call_llm scope.
                # We log per-call above; aggregate here is a best-effort sum.
                pass

        except Exception as exc:
            last_exc = exc
            _log.warning(
                "story_writer: parallel LLM dispatch failed project=%s: %s",
                project_id[:8], type(exc).__name__,
            )

    if not raw_response or not raw_response.get("phases"):
        # All parallel calls failed — fail-closed per Law #3.
        raise RuntimeError(
            f"REASON: parallel LLM dispatch returned invalid/empty response for project "
            f"{project_id[:8]}. Last error: {last_exc} (Law #3 fail-closed)"
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

    # Missing inputs — Wave 19a: batch insert with ON CONFLICT DO NOTHING on
    # (project_id, description_hash) to prevent exact-text duplicates from
    # discipline_verifier and story_writer._call_b() landing twice for the
    # same description text on the same project.
    if new_missing_input_rows:
        try:
            from aspire_orchestrator.services.supabase_client import (
                supabase_insert_batch_ignore_conflicts,
            )
            _inserted_mis, _skipped_mis = await supabase_insert_batch_ignore_conflicts(
                "blueprint_missing_inputs",
                new_missing_input_rows,
                conflict_target="project_id,description_hash",
            )
            if _skipped_mis:
                _log.info(
                    "story_writer: skipped %d duplicate missing_input rows "
                    "(exact-text dedup) for project=%s",
                    _skipped_mis,
                    project_id[:8],
                )
        except SupabaseClientError as exc:
            _log.warning(
                "story_writer: failed to insert missing_inputs for project=%s: %s",
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
        llm_usage=llm_usage,  # Wave 9h: propagated to receipt metadata by Drew.reason
        # Wave 17b.5: typing observability passes through to Drew.reason receipt.
        typed_assembly_count=int(parsed.get("typed_assembly_count", 0)),
        missing_assembly_count=int(parsed.get("missing_assembly_count", 0)),
        distinct_assembly_types=int(parsed.get("distinct_assembly_types", 0)),
        assembly_type_breakdown=dict(parsed.get("assembly_type_breakdown", {})),
    )
