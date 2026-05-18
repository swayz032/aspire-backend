"""Pipeline stage progress tracker for blueprint_projects.

Updates the `stage_progress` JSONB column on `blueprint_projects` via a
PostgREST PATCH using the jsonb_set SQL expression emitted through the
Supabase PATCH endpoint.

Stage values: "not_started" | "in_progress" | "done" | "failed"
Stage names:  "ingest" | "classify" | "see" | "reason" | "procure"

Law compliance:
  Law #2: Does not emit receipts itself — callers emit receipts covering the
          full stage lifecycle. Progress updates are internal state bookkeeping.
  Law #6: All updates filter by suite_id in addition to id, preventing
          cross-tenant writes.
  Law #3: Logs and swallows errors (progress tracking is best-effort; the stage
          result itself is the authoritative outcome — not the progress field).

Note on PostgREST jsonb_set:
  PostgREST does not support jsonb_set() via PATCH out of the box. We use the
  `data->>key` approach: PATCH with the full existing jsonb merged with the new
  key is the safe pattern. However, to avoid a read-then-write race, we use the
  Supabase RPC `jsonb_set_key` if available, falling back to a PATCH of the
  entire stage_progress object (requires a prior SELECT to read current state).

  Chosen approach: use the `PATCH` endpoint with a computed merge. Since
  stage_progress has exactly 5 fixed keys, a full-object PATCH (overwriting all
  5 keys) is acceptable — but it requires reading current state first.

  Simpler: use the Postgres operator via a raw SQL expression in the PATCH body.
  PostgREST supports `stage_progress=jsonb_set(stage_progress,'{key}','"val"')`
  via a custom RPC if we define one. Since we cannot guarantee the RPC exists,
  we do a two-step: SELECT then PATCH, wrapped in a try/except so progress
  failures never propagate upward.
"""

from __future__ import annotations

import logging
from typing import Literal

from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

logger = logging.getLogger(__name__)

StageState = Literal["not_started", "in_progress", "done", "failed"]
StageName = Literal["ingest", "classify", "see", "reason", "procure"]

_DEFAULT_PROGRESS: dict[str, str] = {
    "ingest": "not_started",
    "classify": "not_started",
    "see": "not_started",
    "reason": "not_started",
    "procure": "not_started",
}


async def set_stage_progress(
    *,
    project_id: str,
    stage: str,
    state: str,
    suite_id: str,
) -> None:
    """Update one stage key in blueprint_projects.stage_progress.

    Args:
        project_id: UUID of the blueprint project row.
        stage: Stage name ("ingest", "classify", "see", "reason", "procure").
        state: New state value ("not_started", "in_progress", "done", "failed").
        suite_id: Tenant UUID — used in both the SELECT filter and PATCH filter
                  to enforce tenant isolation (Law #6).

    Errors are logged and swallowed — progress tracking is best-effort.
    The authoritative pipeline result is the stage's return value and receipt.
    """
    try:
        # Step 1: Read current stage_progress for this project
        rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
        if not rows:
            logger.warning(
                "stage_progress: project not found project=%s suite=%s stage=%s state=%s",
                project_id[:8],
                suite_id[:8],
                stage,
                state,
            )
            return

        current_progress: dict[str, str] = dict(_DEFAULT_PROGRESS)
        db_progress = rows[0].get("stage_progress")
        if isinstance(db_progress, dict):
            current_progress.update(db_progress)

        # Step 2: Merge the new stage state
        current_progress[stage] = state

        # Step 3: PATCH the full stage_progress object
        await supabase_update(
            "blueprint_projects",
            f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            {"stage_progress": current_progress},
        )

        logger.info(
            "stage_progress: project=%s stage=%s state=%s suite=%s",
            project_id[:8],
            stage,
            state,
            suite_id[:8],
        )

    except SupabaseClientError as exc:
        logger.warning(
            "stage_progress: DB error project=%s stage=%s state=%s error=%s",
            project_id[:8],
            stage,
            state,
            type(exc).__name__,
        )
    except Exception as exc:
        logger.warning(
            "stage_progress: unexpected error project=%s stage=%s error=%s",
            project_id[:8],
            stage,
            type(exc).__name__,
        )
