"""Case-pack memory retriever — tenant-scoped prior project hints.

Law #6: Queries are ALWAYS filtered by suite_id. RLS at DB layer provides
defense-in-depth. Drew learns the specific contractor's voice and estimating
style from their own prior projects only — never cross-tenant.

Cold-start (no prior projects for this tenant) returns an empty list gracefully.
The story writer handles that case without degrading story quality.

Stage 4 REASON is GREEN (read-only). No receipts are emitted here — the
parent write_story() caller emits a single consolidated receipt for the full
REASON stage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)

# Module-level re-export for test mockability (wrapped for environments without httpx)
try:
    from aspire_orchestrator.services.supabase_client import supabase_select  # noqa: F401
except ImportError:
    supabase_select = None  # type: ignore[assignment]

_OCR_HINT_MAX_CHARS = 400  # Budget: hint excerpts are shorter than main sheets


@dataclass(frozen=True)
class CasePackHint:
    """A prior project story excerpt returned as a reasoning hint.

    Used to give Drew tonal and estimating consistency with the tenant's
    existing body of work.
    """

    project_id: str
    phase_count: int
    discipline_mix: list[str]
    story_excerpt: str  # Truncated markdown from the first story phase
    mean_confidence: float


async def retrieve_case_pack_hints(
    *,
    suite_id: str,
    project_context: dict,  # dict with optional keys: discipline_mix, sheet_count
    k: int = 3,
) -> list[CasePackHint]:
    """Return up to K prior project story excerpts for this tenant.

    Implementation v1: lightweight similarity — no vector DB. Filters by
    discipline overlap and sheet count proximity (±50%).

    Args:
        suite_id: Tenant identifier. REQUIRED — queries are always scoped.
        project_context: Context about the current project. Accepts:
            - discipline_mix: list[str] — disciplines present in current project
            - sheet_count: int — total sheets in current project
        k: Maximum number of hints to return. Default 3.

    Returns:
        List of CasePackHint for top-K similar prior projects, or [] on cold-start.

    Raises:
        Nothing — cold-start (empty return) is the correct behavior on any
        retrieval failure. Errors are logged at WARNING level.
    """
    if not suite_id:
        _log.warning("case_pack_memory: suite_id missing — returning empty hints (Law #6)")
        return []

    try:
        return await _retrieve(suite_id=suite_id, project_context=project_context, k=k)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "case_pack_memory: retrieval failed for suite=%s (%s) — proceeding cold-start",
            suite_id[:8],
            type(exc).__name__,
        )
        return []


async def _retrieve(
    *,
    suite_id: str,
    project_context: dict,
    k: int,
) -> list[CasePackHint]:
    """Inner retrieval with Supabase query."""
    from aspire_orchestrator.services.supabase_client import SupabaseClientError
    import aspire_orchestrator.services.blueprint.case_pack_memory as _cpm
    _supabase_select = _cpm.supabase_select

    # Query blueprint_story rows for this tenant (RLS auto-enforced at DB layer,
    # plus explicit suite_id filter for defense-in-depth — Law #6).
    try:
        story_rows = await _supabase_select(
            "blueprint_story",
            filters=f"suite_id=eq.{suite_id}",
            order_by="created_at.desc",
            limit=50,  # Fetch enough candidates to apply similarity filter
        )
    except SupabaseClientError as exc:
        _log.warning(
            "case_pack_memory: blueprint_story query failed suite=%s: %s",
            suite_id[:8],
            type(exc).__name__,
        )
        return []

    if not story_rows:
        _log.info(
            "case_pack_memory: cold-start — no prior stories for suite=%s",
            suite_id[:8],
        )
        return []

    # Load sheet metadata for similarity scoring (discipline_mix, sheet_count)
    # Collect unique project_ids from story rows
    project_ids = list({str(r["project_id"]) for r in story_rows if r.get("project_id")})
    if not project_ids:
        return []

    # Fetch project sheets to determine discipline mix and sheet count
    project_sheet_map: dict[str, dict] = {}
    for pid in project_ids[:20]:  # Cap to avoid large fan-out queries
        try:
            sheets = await _supabase_select(
                "blueprint_sheets",
                filters=f"project_id=eq.{pid}&suite_id=eq.{suite_id}",
                limit=200,
            )
            if sheets:
                disciplines = list({
                    str(s["discipline"]) for s in sheets if s.get("discipline")
                })
                project_sheet_map[pid] = {
                    "sheet_count": len(sheets),
                    "discipline_mix": disciplines,
                }
        except SupabaseClientError:
            continue  # Skip projects where sheet query fails

    # Score and rank prior projects by similarity to current project
    current_disciplines = set(project_context.get("discipline_mix") or [])
    current_sheet_count = int(project_context.get("sheet_count") or 0)

    scored: list[tuple[float, dict, dict]] = []
    for row in story_rows:
        pid = str(row.get("project_id", ""))
        if pid not in project_sheet_map:
            continue
        sheet_meta = project_sheet_map[pid]

        # Discipline similarity: Jaccard coefficient
        prior_disciplines = set(sheet_meta["discipline_mix"])
        if current_disciplines and prior_disciplines:
            intersection = len(current_disciplines & prior_disciplines)
            union = len(current_disciplines | prior_disciplines)
            discipline_score = intersection / union if union else 0.0
        else:
            discipline_score = 0.5  # neutral when no discipline data

        # Sheet count proximity: within ±50% = max score, else linearly decays
        prior_count = sheet_meta["sheet_count"]
        if current_sheet_count > 0 and prior_count > 0:
            ratio = min(current_sheet_count, prior_count) / max(current_sheet_count, prior_count)
            count_score = ratio  # 1.0 = identical size, 0.5 = half the size
        else:
            count_score = 0.5  # neutral

        combined_score = 0.6 * discipline_score + 0.4 * count_score
        scored.append((combined_score, row, sheet_meta))

    # Sort descending by score, take top K
    scored.sort(key=lambda x: x[0], reverse=True)
    top_k = scored[:k]

    hints: list[CasePackHint] = []
    for score, row, sheet_meta in top_k:
        if score < 0.1:
            break  # No meaningful similarity

        markdown = str(row.get("markdown") or "")
        # Truncate to keep LLM context budget manageable
        excerpt = markdown[:_OCR_HINT_MAX_CHARS]
        if len(markdown) > _OCR_HINT_MAX_CHARS:
            excerpt += "…"

        truth_dist: dict = row.get("truth_distribution") or {}
        total_facts = sum(
            truth_dist.get(k2, 0) for k2 in ("observed", "derived", "assumed")
        )
        if total_facts > 0:
            mean_conf = (
                truth_dist.get("observed", 0) * 1.0
                + truth_dist.get("derived", 0) * 0.90
                + truth_dist.get("assumed", 0) * 0.75
            ) / total_facts
        else:
            mean_conf = 0.0

        hints.append(CasePackHint(
            project_id=str(row.get("project_id", "")),
            phase_count=int(row.get("phase") or 0),
            discipline_mix=sheet_meta["discipline_mix"],
            story_excerpt=excerpt,
            mean_confidence=round(mean_conf, 3),
        ))

    _log.info(
        "case_pack_memory: returning %d hints for suite=%s (from %d candidates)",
        len(hints),
        suite_id[:8],
        len(story_rows),
    )
    return hints
