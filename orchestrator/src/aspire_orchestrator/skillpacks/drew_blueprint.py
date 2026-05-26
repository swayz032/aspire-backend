# [STATUS: v1-active, BYPASS] — Reachable via /v1/agents/invoke-sync.
# Bypasses LangGraph: no policy gate, no token mint, no central receipt audit.
# Migration debt — route through /v1/intents in a later wave.
"""Drew Blueprint Story Engine — Wave 3: SEE implemented (INGEST + CLASSIFY + SEE live).

Reads architectural blueprints, builds a multi-discipline understanding, and produces
a phase-by-phase build narrative with line-item materials.

Pipeline tasks (orchestrator-driven, Drew never decides):
    INGEST   → parse PDFs (LlamaParse primary, Azure Doc Intel fallback)
    CLASSIFY → assign discipline + sheet metadata
    SEE      → YOLOv11 symbol detection + scale calibration + engineer-seal flag  [Wave 3]
    REASON   → derive assemblies, materials, story-by-phase  [stub]
    PROCURE  → push material requests to supplier playbooks  [stub]

Law compliance:
  - Law #1: Skill pack runs bounded tasks; orchestrator decides invocation.
  - Law #2: Every method emits a receipt via _emit_receipt.
  - Law #3: Fails closed on unknown task or missing prompt/model env in production.
  - Law #6: All DB writes include suite_id; queries filter via RLS.
  - Law #7: No autonomous decisions; tasks are dispatched in by name.
  - Law #9: PDF bytes never logged; only len(), hash prefixes, and page counts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts

# ---------------------------------------------------------------------------
# DLP singleton — used to redact OCR text before DB writes (Law #9, P0-10)
# ---------------------------------------------------------------------------
try:
    from aspire_orchestrator.services.dlp import DLPService as _DLPService
    _dlp = _DLPService()
except Exception:
    _dlp = None  # type: ignore[assignment]


def _dlp_redact(text: str) -> tuple[str, int]:
    """Redact PII from OCR text.  Returns (redacted_text, redaction_count).

    Uses the shared DLPService (Presidio + regex fallback, Law #9).
    If DLP is unavailable returns ("[DLP_UNAVAILABLE — content withheld]", -2)
    so the placeholder is stored instead of raw text (Law #3 fail-closed,
    Law #9 no PII). The caller emits a blueprint.ingest.dlp_unavailable receipt.

    redaction_count sentinel values:
      -2 = DLP service unavailable (fail-closed placeholder stored)
      -1 = DEPRECATED (was fail-open; no longer produced)
       0 = DLP ran, no PII detected
      >0 = DLP ran, N redactions applied

    This is a v1 Presidio + regex pass.  A deeper follow-up is queued Wave 10.
    """
    if _dlp is None:
        # Law #3: fail closed — never write unredacted text when DLP is down.
        return "[DLP_UNAVAILABLE — content withheld]", -2
    redacted = _dlp.redact_text_ingestion(text)
    # Heuristic count: number of redaction placeholders inserted
    import re as _re
    count = len(_re.findall(r"<[A-Z_]+_REDACTED>", redacted))
    return redacted, count


PROMPT_PATH = (
    Path(__file__).parent.parent
    / "services"
    / "blueprint"
    / "prompts"
    / "drew_system_prompt.md"
)

ACTOR_DREW = "skillpack:drew-blueprint"
RECEIPT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# P1 fix: per-project concurrency guards for multi-statement read-modify-write
# stages (SEE, REASON, PROCURE).
#
# These stages use a SELECT → DELETE → INSERT / UPDATE pattern that is not
# safe under concurrent execution against the same project_id.  A second
# concurrent call would observe stale rows, delete them, and re-insert,
# effectively doubling symbol/story/material rows.
#
# Mitigation (single-replica):
#   asyncio.Lock keyed by project_id.  The second concurrent coroutine on the
#   SAME asyncio event loop blocks until the first completes.  This is safe
#   for Railway single-replica deployments.
#
# Limitation: does NOT prevent duplicate execution across multiple replicas
# (Railway autoscale or blue-green deployments).  The correct multi-replica
# fix is a Postgres advisory lock (`pg_try_advisory_xact_lock`).  That requires
# a migration to expose an RPC helper callable from the Supabase PostgREST
# layer — deferred to a follow-up migration per wave scope.
#
# When that migration lands, replace the asyncio.Lock logic below with:
#   result = await supabase_rpc("try_advisory_lock", {"lock_key": f"blueprint_{stage}:{project_id}"})
#   if not result.get("acquired"):
#       emit blueprint.<stage>.concurrent_skip receipt
#       return {"status": "skipped", "reason": "concurrent_run"}
# ---------------------------------------------------------------------------
_stage_locks: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# CancelledError guard helper (P0 fix, Wave 9f)
# ---------------------------------------------------------------------------

async def _run_missing_input_cascade(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    correlation_id: str,
) -> dict[str, Any]:
    """Wave 19c: Run the self-resolution cascade on all unresolved missing_inputs.

    Loads all unresolved rows for this project, builds MissingInputCandidate
    objects with full project context (sheets + OCR + symbols), dispatches
    cascade in parallel, and marks resolved rows in the DB.

    This is called by _async_reason_pipeline AFTER write_story returns.
    Also called by classify pipeline after discipline_verifier flags.

    Returns a summary dict: {resolved: int, unresolved: int, skipped: int}

    Law compliance:
      Law #1 — Returns outcome; Drew decides persistence.
      Law #2 — Each cascade run emits its own receipt via resolve_or_emit.
      Law #3 — Never raises; any cascade error is caught and logged.
      Law #6 — All queries filter by suite_id (RLS).
    """
    import logging as _logging
    _casclog = _logging.getLogger(__name__)

    try:
        from aspire_orchestrator.services.supabase_client import (
            supabase_select as _sel,
            supabase_update as _upd,
        )
        # Note: ``build_candidate`` + ``resolve_or_emit`` are no longer
        # imported here — Wave 19.1c (Commit 2.5) moved dispatch to
        # ``resolve_batch`` which owns provider construction and per-
        # candidate building. See the import inside the dispatch block below.

        # Load unresolved missing_inputs for this project
        unresolved_rows = await _sel(
            "blueprint_missing_inputs",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&resolved_at=is.null",
        )
        if not unresolved_rows:
            return {"resolved": 0, "unresolved": 0, "skipped": 0}

        # Load project context once for all candidates
        sheets = await _sel(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
        active_sheets = [s for s in (sheets or []) if not s.get("supersedes_id")]

        # Build symbols_by_sheet index
        from aspire_orchestrator.services.supabase_client import SupabaseClientError as _SCE
        symbols_by_sheet: dict[str, list[dict[str, Any]]] = {}
        sheet_ids_csv = ",".join(str(s["id"]) for s in active_sheets if s.get("id"))
        if sheet_ids_csv:
            try:
                sym_rows = await _sel(
                    "blueprint_symbols",
                    filters=f"sheet_id=in.({sheet_ids_csv})&suite_id=eq.{suite_id}",
                )
                for row in (sym_rows or []):
                    sid = str(row.get("sheet_id") or "")
                    if sid:
                        symbols_by_sheet.setdefault(sid, []).append(row)
            except _SCE:
                pass  # non-fatal — resolvers degrade gracefully

        # Build ocr_by_sheet from already-loaded sheets
        ocr_by_sheet: dict[str, str] = {
            str(s["id"]): str(s.get("ocr_text") or "")
            for s in active_sheets if s.get("id")
        }

        # Wave 19.1c (Commit 2.5) — dispatch via ``resolve_batch`` so the
        # cascade owns SheetDataProvider construction and shares ONE provider
        # across all candidates in the dispatch. The earlier Wave 19.1a/b
        # path constructed the provider here in Drew; the standalone backfill
        # script lacked the same wiring and silently degraded every resolver
        # to no-PNG mode. ``resolve_batch`` closes that gap for all callers.
        from aspire_orchestrator.services.blueprint.missing_input_cascade import (
            resolve_batch as _resolve_batch,
        )
        batch = await _resolve_batch(
            unresolved_rows,
            suite_id=suite_id,
            project_id=project_id,
            correlation_id=correlation_id,
            active_sheets=active_sheets,
            symbols_by_sheet=symbols_by_sheet,
            ocr_by_sheet=ocr_by_sheet,
        )

        resolved_count = 0
        unresolved_count = 0
        skipped_count = batch.skipped_count  # dropped/no-resolver outcomes

        for outcome in batch.outcomes:
            if outcome.resolved:
                # Mark resolved in DB
                # resolved_by is uuid (auth.users FK) — use NULL for automated cascade.
                # resolved_method is the new text column added in Wave 19.
                try:
                    await _upd(
                        "blueprint_missing_inputs",
                        f"id=eq.{outcome.missing_input_id}&suite_id=eq.{suite_id}",
                        {
                            "resolved_method": f"cascade:wave19:{outcome.best_result.method if outcome.best_result else 'unknown'}",
                            "resolved_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    resolved_count += 1
                except Exception as _upd_exc:
                    _casclog.warning(
                        "drew.cascade: failed to mark resolved mi_id=%s: %s",
                        outcome.missing_input_id[:8], type(_upd_exc).__name__,
                    )
                    skipped_count += 1
            elif outcome.best_result is None and not outcome.all_results:
                # Already counted in batch.skipped_count above; don't double.
                pass
            else:
                unresolved_count += 1

        _casclog.info(
            "drew.cascade: project=%s resolved=%d unresolved=%d skipped=%d "
            "provider_cache=%s",
            project_id[:8], resolved_count, unresolved_count, skipped_count,
            batch.provider_cache_stats,
        )
        return {
            "resolved": resolved_count,
            "unresolved": unresolved_count,
            "skipped": skipped_count,
        }

    except Exception as exc:  # noqa: BLE001
        return {"resolved": 0, "unresolved": 0, "skipped": 0, "error": type(exc).__name__}


async def _mark_stage_failed_best_effort(
    project_id: str, stage: str, suite_id: str
) -> None:
    """Mark a stage as failed, swallowing any errors (best-effort only).

    Used as the CancelledError handler in each _async_*_pipeline so that
    asyncio task cancellation (server shutdown, timeout) leaves stage_progress
    in a deterministic 'failed' state rather than stuck as 'in_progress'.
    """
    try:
        from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress as _ssp
        await _ssp(project_id=project_id, stage=stage, state="failed", suite_id=suite_id)
    except Exception:
        pass


def _get_stage_lock(stage: str, project_id: str) -> asyncio.Lock:
    """Return the per-(stage, project_id) asyncio.Lock, creating it on first call.

    Dict access is thread-safe in CPython (GIL), and since asyncio is single-
    threaded, there is no race between .locked() check and lock acquisition.
    """
    key = f"{stage}:{project_id}"
    if key not in _stage_locks:
        _stage_locks[key] = asyncio.Lock()
    return _stage_locks[key]


def _compute_inputs_hash(inputs: dict[str, Any]) -> str:
    """SHA256 of canonicalized inputs for receipt linkage (mirrors Adam)."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


_DREW_STATUS_TO_OUTCOME: dict[str, str] = {
    "ok": "success",
    "dedup": "success",
    "failed": "failed",
    "error": "failed",
    "denied": "denied",
    "timeout": "failed",
}


def _build_receipt(
    *,
    correlation_id: str,
    event_type: str,
    status: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    suite_id: str | None = None,
) -> dict[str, Any]:
    """Build a Drew receipt using canonical receipt_store field names (Law #2, P0-4).

    Field mapping (canonical → _map_receipt_to_row keys):
      id              → receipt_id (PK)
      outcome         → mapped to status enum (SUCCEEDED/FAILED/DENIED/PENDING)
      action_type     → action jsonb.action_type
      receipt_type    → receipt_type column
      created_at      → created_at column (was ts / None before fix)
      trace_id        → trace context column
      risk_tier       → action jsonb.risk_tier

    Backward-compat aliases are kept so existing callers using receipt_id/status/ts
    still work: those keys are written alongside the canonical names.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    outcome = _DREW_STATUS_TO_OUTCOME.get(status, "failed")

    # Resolve trace_id from middleware context (correlation middleware sets it)
    try:
        from aspire_orchestrator.middleware.correlation import get_trace_id
        trace_id = get_trace_id() or correlation_id
    except Exception:
        trace_id = correlation_id

    receipt: dict[str, Any] = {
        # Canonical field names read by _map_receipt_to_row
        "id": str(uuid.uuid4()),
        "outcome": outcome,
        "action_type": event_type,
        "receipt_type": event_type,
        "created_at": now_iso,
        "trace_id": trace_id,
        "risk_tier": risk_tier.upper(),
        # Governance + actor
        "actor_type": "WORKER",
        "actor_id": ACTOR_DREW,
        "correlation_id": correlation_id,
        # Extra context
        "receipt_version": RECEIPT_VERSION,
        "actor": ACTOR_DREW,
        "inputs_hash": _compute_inputs_hash(inputs),
        "policy": {
            "decision": "allow" if status != "denied" else "deny",
            "policy_id": "drew-blueprint-v1",
            "reasons": [],
        },
        "redactions": [],
        # Backward-compat aliases (kept so legacy code that reads receipt_id/status/ts works)
        "receipt_id": None,  # will be filled below to equal id
        "status": status,
        "ts": now_iso,
        "event_type": event_type,
    }
    # Ensure receipt_id alias = canonical id
    receipt["receipt_id"] = receipt["id"]

    if suite_id:
        receipt["suite_id"] = suite_id
    if capability_token_id:
        receipt["capability_token_id"] = capability_token_id
    else:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "drew receipt built without capability_token_id (dispatch path may not have minted one)"
        )

    if metadata:
        receipt["metadata"] = metadata
    return receipt


class Drew:
    """Drew skill pack — blueprint ingestion + story generation."""

    actor: str = "drew"

    def __init__(self) -> None:
        if not PROMPT_PATH.exists():
            raise RuntimeError(f"Drew system prompt missing: {PROMPT_PATH}")
        self.system_prompt: str = PROMPT_PATH.read_text(encoding="utf-8")

        env = os.getenv("ASPIRE_ENV", "development")
        if env == "production":
            model = os.getenv("ASPIRE_DREW_MODEL_PROD") or os.getenv("DREW_MODEL_PROD")
            if not model:
                raise RuntimeError(
                    "ASPIRE_DREW_MODEL_PROD env var required in production (Law #3)",
                )
            self.model: str = model
        else:
            self.model = (
                os.getenv("ASPIRE_DREW_MODEL_DEV")
                or os.getenv("DREW_MODEL_DEV")
                or "gpt-5.4-mini"
            )

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------
    def run_agentic_loop(
        self,
        task: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        """Route an inbound task to its handler. Fails closed on unknown task."""
        dispatch: dict[str, Any] = {
            "INGEST": self.ingest,
            "CLASSIFY": self.classify,
            "UNIVERSAL_SHEET_INTELLIGENCE": self.universal_sheet_intelligence,
            "SEE": self.see,
            "REASON": self.reason,
            "PROCURE": self.procure,
            "MATERIAL_OVERRIDE": self.material_override,
            "MATERIAL_SKIP": self.material_skip,
        }
        handler = dispatch.get(task)
        if handler is None:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="drew.unknown_task",
                status="denied",
                inputs={"task": task},
            )
            return {"status": "deny", "reason": f"unknown task: {task}"}
        return handler(payload, correlation_id)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Stage 1: INGEST (Wave 2A — real implementation)
    # ------------------------------------------------------------------
    def ingest(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Ingest a blueprint PDF: split, OCR, store sheets, upload thumbnails.

        Payload keys:
          - pdf_bytes: base64-encoded PDF binary
          - filename: str (for receipt metadata)
          - suite_id: UUID str
          - office_id: UUID str

        Returns:
          {"status": "ok", "stage": "ingest", "project_id": str,
           "sheet_count": int, "sheet_ids": [str]}

        Idempotency: SHA-256 of the PDF bytes is checked against blueprint_projects.
        If a matching project already exists, returns it without re-ingesting.

        Law #6: All inserts include suite_id; RLS enforces isolation.
        Law #9: PDF bytes never logged; only len(), hash prefix, and page count.
        """
        # Validate required payload keys
        required_keys = ("pdf_bytes", "suite_id", "office_id")
        for key in required_keys:
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.ingest",
                    status="failed",
                    inputs={"task": "INGEST", "missing_key": key},
                )
                return {"status": "error", "stage": "ingest", "reason": f"missing payload key: {key}"}

        suite_id: str = str(payload["suite_id"])
        office_id: str = str(payload["office_id"])
        filename: str = str(payload.get("filename", "blueprint.pdf"))

        # Decode base64 PDF bytes
        try:
            pdf_bytes: bytes = base64.b64decode(payload["pdf_bytes"])
        except Exception as exc:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.ingest",
                status="failed",
                inputs={"task": "INGEST", "suite_id": suite_id},
            )
            return {"status": "error", "stage": "ingest", "reason": f"invalid base64 pdf_bytes: {type(exc).__name__}"}

        # Compute project hash for idempotency check (Law #3: fail-closed on dup)
        project_hash: str = hashlib.sha256(pdf_bytes).hexdigest()

        # Mark stage as in_progress before starting pipeline (best-effort)
        # We need a project_id first, so we optimistically set it after creation.
        # The _async_ingest_pipeline handles in_progress + done/failed internally.

        # Run the async pipeline in a sync context (Drew.ingest is sync; called
        # from run_agentic_loop which is sync). Use asyncio.run or get_event_loop.
        try:
            result = _run_async_ingest(
                pdf_bytes=pdf_bytes,
                project_hash=project_hash,
                suite_id=suite_id,
                office_id=office_id,
                filename=filename,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.ingest",
                status="failed",
                inputs={"task": "INGEST", "suite_id": suite_id, "project_hash": project_hash[:8]},
                metadata={"error": type(exc).__name__},
            )
            return {"status": "error", "stage": "ingest", "reason": f"ingest pipeline failed: {type(exc).__name__}"}

        # Emit receipt on success or dedup
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.ingest",
            status=result["status"],
            inputs={"task": "INGEST", "suite_id": suite_id, "project_hash": project_hash[:8]},
            metadata={
                "project_id": result.get("project_id"),
                "sheet_count": result.get("sheet_count", 0),
                "provider_mix": result.get("provider_mix", {}),
                "filename": filename,
                "pdf_size_bytes": len(pdf_bytes),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Stage 2: CLASSIFY (Wave 2A — real implementation)
    # ------------------------------------------------------------------
    def classify(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Classify blueprint sheets: discipline tagging + revision linking.

        Payload keys:
          - project_id: UUID str
          - suite_id: UUID str

        Returns:
          {"status": "ok", "stage": "classify", "project_id": str,
           "discipline_counts": {...}, "revisions": int}

        Law #6: DB queries filter by suite_id via RLS.
        """
        required_keys = ("project_id", "suite_id")
        for key in required_keys:
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.classify",
                    status="failed",
                    inputs={"task": "CLASSIFY", "missing_key": key},
                )
                return {"status": "error", "stage": "classify", "reason": f"missing payload key: {key}"}

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])

        # Mark in_progress before classifying
        _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="classify", state="in_progress")

        try:
            result = _run_async_classify(
                project_id=project_id,
                suite_id=suite_id,
                model=self.model,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="classify", state="failed")
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.classify",
                status="failed",
                inputs={"task": "CLASSIFY", "project_id": project_id},
                metadata={"error": type(exc).__name__},
            )
            return {"status": "error", "stage": "classify", "reason": f"classify pipeline failed: {type(exc).__name__}"}

        _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="classify", state="done")
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.classify",
            status=result["status"],
            inputs={"task": "CLASSIFY", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "discipline_counts": result.get("discipline_counts", {}),
                "revisions": result.get("revisions", 0),
                "needs_review_count": result.get("needs_review_count", 0),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Stage 2.5: UNIVERSAL_SHEET_INTELLIGENCE (Wave 20c — between CLASSIFY and SEE)
    # ------------------------------------------------------------------
    def universal_sheet_intelligence(
        self, payload: dict[str, Any], correlation_id: str
    ) -> dict[str, Any]:
        """Run Azure layout extraction on every sheet in the project.

        Payload keys:
          - project_id: UUID str
          - suite_id:   UUID str
          - office_id:  UUID str (optional)

        Returns:
          {"status": "ok", "stage": "universal_sheet_intelligence",
           "project_id": str, "sheets_total": int, "sheets_succeeded": int,
           "sheets_failed": int, "total_azure_ms": int}

        Law #2: emits blueprint.universal_sheet_intelligence.summary receipt.
        Law #6: all DB queries filter by suite_id.
        """
        required_keys = ("project_id", "suite_id")
        for key in required_keys:
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.universal_sheet_intelligence",
                    status="failed",
                    inputs={"task": "UNIVERSAL_SHEET_INTELLIGENCE", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "universal_sheet_intelligence",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None

        _run_async_set_stage(
            project_id=project_id,
            suite_id=suite_id,
            stage="universal_intel",
            state="in_progress",
        )

        try:
            result = _run_async_universal_sheet_intel(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(
                project_id=project_id,
                suite_id=suite_id,
                stage="universal_intel",
                state="failed",
            )
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.universal_sheet_intelligence",
                status="failed",
                inputs={
                    "task": "UNIVERSAL_SHEET_INTELLIGENCE",
                    "project_id": project_id,
                },
                metadata={"error": type(exc).__name__},
            )
            return {
                "status": "error",
                "stage": "universal_sheet_intelligence",
                "reason": f"pipeline failed: {type(exc).__name__}",
            }

        final_state = "done" if result.get("sheets_succeeded", 0) > 0 else "failed"
        _run_async_set_stage(
            project_id=project_id,
            suite_id=suite_id,
            stage="universal_intel",
            state=final_state,
        )
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.universal_sheet_intelligence.summary",
            status=result.get("status", "ok"),
            inputs={"task": "UNIVERSAL_SHEET_INTELLIGENCE", "project_id": project_id},
            metadata={
                "sheets_total": result.get("sheets_total", 0),
                "sheets_succeeded": result.get("sheets_succeeded", 0),
                "sheets_failed": result.get("sheets_failed", 0),
                "total_azure_ms": result.get("total_azure_ms", 0),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Stage 3: SEE (Wave 3 — real implementation, stage_progress wired in Wave 2.5)
    # ------------------------------------------------------------------
    def see(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Run YOLOv11 symbol detection + scale calibration + seal flagging.

        Payload keys (required):
          - project_id: UUID str — the project to scan.
          - suite_id:   UUID str — tenant scope.
          - pdf_bytes:  base64-encoded PDF binary — re-rendered to recover
                        the per-sheet 200 DPI images (we never persist images).

        Optional:
          - office_id:  UUID str
          - confidence_floor: float (default 0.70)

        Returns:
          {"status": "ok"|"error"|"failed", "stage": "see",
           "project_id": str, "sheet_count": int, "symbol_count": int,
           "seal_sheets": int, "missing_inputs": int,
           "mean_confidence": float, "model_version": str}

        Image source: Wave 1 pdf_splitter renders pages at 200 DPI but the
        raster bytes are not stored in DB. The caller resupplies pdf_bytes
        in the payload; we re-split and match by SHA-256 hash to the
        blueprint_sheets rows persisted by INGEST.

        Receipts:
          - blueprint.see — emitted once per call. metadata includes the
            aggregate symbol_count, mean_confidence, seal_sheets,
            missing_inputs, sheet_count, model_version.

        Stage progress (Wave 2.5):
          - On entry with valid project_id+suite_id: stage_progress["see"]="in_progress"
          - On success: stage_progress["see"]="done"
          - On failure: stage_progress["see"]="failed"
        """
        # Validate required payload keys
        for key in ("project_id", "suite_id", "pdf_bytes"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.see",
                    status="failed",
                    inputs={"task": "SEE", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "see",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None
        confidence_floor: float = float(payload.get("confidence_floor", 0.70))

        # Wave 2.5: mark stage in_progress (no-op if project_id/suite_id falsy)
        if project_id and suite_id:
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="in_progress")

        try:
            pdf_bytes: bytes = base64.b64decode(payload["pdf_bytes"])
        except Exception as exc:
            if project_id and suite_id:
                _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="failed")
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.see",
                status="failed",
                inputs={"task": "SEE", "project_id": project_id},
                metadata={"error": f"invalid base64 pdf_bytes: {type(exc).__name__}"},
            )
            return {
                "status": "error",
                "stage": "see",
                "reason": f"invalid base64 pdf_bytes: {type(exc).__name__}",
            }

        try:
            result = _run_async_see(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                pdf_bytes=pdf_bytes,
                confidence_floor=confidence_floor,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            if project_id and suite_id:
                _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state="failed")
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.see",
                status="failed",
                inputs={"task": "SEE", "project_id": project_id, "suite_id": suite_id},
                metadata={"error": type(exc).__name__},
            )
            return {
                "status": "error",
                "stage": "see",
                "reason": f"see pipeline failed: {type(exc).__name__}",
            }

        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.see",
            status=result["status"],
            inputs={"task": "SEE", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "sheet_count": result.get("sheet_count", 0),
                "symbol_count": result.get("symbol_count", 0),
                "mean_confidence": result.get("mean_confidence", 0.0),
                "seal_sheets": result.get("seal_sheets", 0),
                "missing_inputs": result.get("missing_inputs", 0),
                "model_version": result.get("model_version", os.getenv("ASPIRE_DREW_YOLO_MODEL", "yolo11n.pt")),
            },
        )
        # Wave 2.5: mark stage done/failed based on result
        if project_id and suite_id:
            final_state = "done" if result.get("status") == "ok" else "failed"
            _run_async_set_stage(project_id=project_id, suite_id=suite_id, stage="see", state=final_state)
        return result

    # ------------------------------------------------------------------
    # Stage 4: REASON (Wave 4 — real implementation)
    # ------------------------------------------------------------------
    def reason(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Derive assemblies, materials, phased story from sheets + symbols.

        Payload keys (required):
          - project_id: UUID str
          - suite_id:   UUID str

        Optional:
          - office_id: UUID str

        Returns:
          {"status": "ok"|"error", "stage": "reason",
           "project_id": str, "phase_count": int, "assembly_count": int,
           "material_count": int, "missing_input_count": int,
           "mean_confidence": float, "truth_distribution": dict,
           "model_version": str}

        Receipts:
          - blueprint.reason — emitted once per call. metadata includes counts
            and truth_distribution (never the story markdown — Law #9).

        Stage progress (Wave 2.5):
          - in_progress on entry with valid project_id+suite_id
          - done on success
          - failed on exception
        """
        # Validate required payload keys
        for key in ("project_id", "suite_id"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.reason",
                    status="failed",
                    inputs={"task": "REASON", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "reason",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None

        # Wave 2.5: mark stage in_progress
        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="reason", state="in_progress"
        )

        try:
            result = _run_async_reason(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                model=self.model,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(
                project_id=project_id, suite_id=suite_id, stage="reason", state="failed"
            )
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.reason",
                status="failed",
                inputs={"task": "REASON", "project_id": project_id, "suite_id": suite_id},
                metadata={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            return {
                "status": "error",
                "stage": "reason",
                "reason": f"reason pipeline failed: {type(exc).__name__}",
            }

        # Emit receipt (counts only — no markdown, Law #9)
        # Wave 9h: include LLM cache observability fields in receipt metadata
        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.reason",
            status=result.get("status", "ok"),
            inputs={"task": "REASON", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "phase_count": result.get("phase_count", 0),
                "assembly_count": result.get("assembly_count", 0),
                "material_count": result.get("material_count", 0),
                "missing_input_count": result.get("missing_input_count", 0),
                "mean_confidence": result.get("mean_confidence", 0.0),
                "truth_distribution": result.get("truth_distribution", {}),
                "model_version": result.get("model_version", self.model),
                # Wave 9h cache metrics
                "llm_prompt_tokens": result.get("llm_prompt_tokens", 0),
                "llm_cached_tokens": result.get("llm_cached_tokens", 0),
                "llm_cache_hit_ratio": result.get("llm_cache_hit_ratio", 0.0),
                "llm_completion_tokens": result.get("llm_completion_tokens", 0),
            },
        )

        final_state = "done" if result.get("status") == "ok" else "failed"
        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="reason", state=final_state
        )
        return result

    def procure(self, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        """Procure: tariff classification + supplier matching for all blueprint_materials.

        Risk tier: GREEN (read + classify). Push-to-materials is YELLOW (Law #4) and
        requires a capability token for materials.bundle.add — enforced at gateway.
        Law #6: All DB reads/writes scoped by suite_id.
        Law #9: line_item text truncated to 100 chars in logs and receipts.
        """
        for key in ("project_id", "suite_id"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.procure",
                    status="failed",
                    inputs={"task": "PROCURE", "missing_key": key},
                )
                return {
                    "status": "error",
                    "stage": "procure",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        suite_id: str = str(payload["suite_id"])
        office_id: str | None = str(payload["office_id"]) if payload.get("office_id") else None
        geofence_miles: float = float(payload.get("geofence_miles", 25.0))

        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="procure", state="in_progress"
        )

        try:
            result = _run_async_procure(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                geofence_miles=geofence_miles,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            _run_async_set_stage(
                project_id=project_id, suite_id=suite_id, stage="procure", state="failed"
            )
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.procure",
                status="failed",
                inputs={"task": "PROCURE", "project_id": project_id, "suite_id": suite_id},
                metadata={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
            return {
                "status": "error",
                "stage": "procure",
                "reason": f"procure pipeline failed: {type(exc).__name__}",
            }

        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.procure",
            status=result.get("status", "ok"),
            inputs={"task": "PROCURE", "project_id": project_id, "suite_id": suite_id},
            metadata={
                "materials_processed": result.get("materials_processed", 0),
                "tariff_flagged": result.get("tariff_flagged", 0),
                "tariff_breakdown": result.get("tariff_breakdown", {}),
                "suppliers_matched": result.get("suppliers_matched", 0),
                "supplier_match_rate": result.get("supplier_match_rate", 0.0),
                "missing_inputs_added": result.get("missing_inputs_added", 0),
            },
        )

        final_state = "done" if result.get("status") == "ok" else "failed"
        _run_async_set_stage(
            project_id=project_id, suite_id=suite_id, stage="procure", state=final_state
        )
        return result

    # ------------------------------------------------------------------
    # Wave 5.1a-5: MATERIAL_OVERRIDE (YELLOW — user replaces a Drew pick)
    # ------------------------------------------------------------------
    def material_override(
        self, payload: dict[str, Any], correlation_id: str
    ) -> dict[str, Any]:
        """User overrides a Drew-sourced material pick.

        Payload keys:
          - project_id: UUID str
          - material_id: UUID str  — the original blueprint_materials row to supersede
          - suite_id: UUID str
          - override: dict — partial material fields (spec_text, quantity, unit,
                             supplier_name, supplier_id, unit_price, notes)
          - reason: str  — 'spec_mismatch' | 'vendor_pref' | 'price' |
                           'availability' | 'other'

        Law #2 (append-only): inserts a NEW blueprint_materials row with
          supersedes_id = original material_id and truth = "user_overridden".
          Never UPDATEs the original row.
        Law #6: suite_id verified on both load and insert.
        Law #9: supplier_name / notes truncated to <=80 chars in receipt metadata.
        """
        for key in ("project_id", "material_id", "suite_id"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.material_pick.override",
                    status="failed",
                    inputs={"task": "MATERIAL_OVERRIDE", "missing_key": key},
                    risk_tier="yellow",
                )
                return {
                    "status": "error",
                    "task": "MATERIAL_OVERRIDE",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        material_id: str = str(payload["material_id"])
        suite_id: str = str(payload["suite_id"])
        override: dict[str, Any] = dict(payload.get("override") or {})
        reason: str = str(payload.get("reason") or "other")

        if not override:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.material_pick.override",
                status="failed",
                inputs={"task": "MATERIAL_OVERRIDE", "project_id": project_id,
                        "material_id": material_id, "suite_id": suite_id},
                risk_tier="yellow",
            )
            return {
                "status": "error",
                "task": "MATERIAL_OVERRIDE",
                "reason": "override must include at least one field",
            }

        try:
            result = _run_async_material_override(
                project_id=project_id,
                material_id=material_id,
                suite_id=suite_id,
                override=override,
                reason=reason,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.material_pick.override",
                status="failed",
                inputs={
                    "task": "MATERIAL_OVERRIDE",
                    "project_id": project_id,
                    "material_id": material_id,
                    "suite_id": suite_id,
                },
                metadata={"error": type(exc).__name__, "message": str(exc)[:200]},
                risk_tier="yellow",
            )
            return {
                "status": "error",
                "task": "MATERIAL_OVERRIDE",
                "reason": f"override failed: {type(exc).__name__}",
            }

        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.material_pick.override",
            status=result.get("status", "ok"),
            inputs={
                "task": "MATERIAL_OVERRIDE",
                "project_id": project_id,
                "material_id": material_id,
                "suite_id": suite_id,
                "reason": reason,
            },
            metadata={
                "new_row_id": result.get("new_row_id"),
                "supersedes_id": material_id,
                # Law #9: truncate free-text fields
                "supplier_name": str(override.get("supplier_name") or "")[:80],
                "notes": str(override.get("notes") or "")[:80],
            },
            risk_tier="yellow",
        )
        return result

    # ------------------------------------------------------------------
    # Wave 5.1a-5: MATERIAL_SKIP (YELLOW — user skips a Drew pick)
    # ------------------------------------------------------------------
    def material_skip(
        self, payload: dict[str, Any], correlation_id: str
    ) -> dict[str, Any]:
        """User skips a Drew-sourced material (won't propagate to bundle/estimate).

        Payload keys:
          - project_id: UUID str
          - material_id: UUID str  — original row to supersede
          - suite_id: UUID str
          - reason: str (optional, default "user_skipped")

        Law #2 (append-only): inserts a NEW blueprint_materials row with
          supersedes_id = original material_id and truth = "user_skipped".
          Never UPDATEs the original row.
        Law #6: suite_id verified on both load and insert.
        """
        for key in ("project_id", "material_id", "suite_id"):
            if key not in payload:
                self._emit_receipt(
                    correlation_id=correlation_id,
                    event_type="blueprint.material_pick.skip",
                    status="failed",
                    inputs={"task": "MATERIAL_SKIP", "missing_key": key},
                    risk_tier="yellow",
                )
                return {
                    "status": "error",
                    "task": "MATERIAL_SKIP",
                    "reason": f"missing payload key: {key}",
                }

        project_id: str = str(payload["project_id"])
        material_id: str = str(payload["material_id"])
        suite_id: str = str(payload["suite_id"])
        reason: str = str(payload.get("reason") or "user_skipped")

        try:
            result = _run_async_material_skip(
                project_id=project_id,
                material_id=material_id,
                suite_id=suite_id,
                reason=reason,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            self._emit_receipt(
                correlation_id=correlation_id,
                event_type="blueprint.material_pick.skip",
                status="failed",
                inputs={
                    "task": "MATERIAL_SKIP",
                    "project_id": project_id,
                    "material_id": material_id,
                    "suite_id": suite_id,
                },
                metadata={"error": type(exc).__name__, "message": str(exc)[:200]},
                risk_tier="yellow",
            )
            return {
                "status": "error",
                "task": "MATERIAL_SKIP",
                "reason": f"skip failed: {type(exc).__name__}",
            }

        self._emit_receipt(
            correlation_id=correlation_id,
            event_type="blueprint.material_pick.skip",
            status=result.get("status", "ok"),
            inputs={
                "task": "MATERIAL_SKIP",
                "project_id": project_id,
                "material_id": material_id,
                "suite_id": suite_id,
                "reason": reason,
            },
            metadata={
                "new_row_id": result.get("new_row_id"),
                "supersedes_id": material_id,
            },
            risk_tier="yellow",
        )
        return result

    # ------------------------------------------------------------------
    # Receipt emission (Law #2)
    # ------------------------------------------------------------------
    def _emit_receipt(
        self,
        *,
        correlation_id: str,
        event_type: str,
        status: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        risk_tier: str = "green",
        capability_token_id: str | None = None,
    ) -> dict[str, Any]:
        """Emit a Drew receipt. risk_tier must be passed explicitly at each call site.

        Blueprint pipeline stages are always "green".
        MATERIAL_OVERRIDE / MATERIAL_SKIP are "yellow" (Law #4).
        """
        suite_id: str | None = inputs.get("suite_id")  # thread suite_id into receipt
        receipt = _build_receipt(
            correlation_id=correlation_id,
            event_type=event_type,
            status=status,
            inputs=inputs,
            metadata=metadata,
            risk_tier=risk_tier,
            capability_token_id=capability_token_id,
            suite_id=suite_id,
        )
        store_receipts([receipt])
        return receipt


# ---------------------------------------------------------------------------
# Async pipeline helpers (called via asyncio bridge from sync .ingest/.classify/.see)
# ---------------------------------------------------------------------------

def _run_async_set_stage(
    *,
    project_id: str,
    suite_id: str,
    stage: str,
    state: str,
) -> None:
    """Bridge: run async set_stage_progress from sync context (legacy/sync wrappers only).

    The dispatch-path _async_*_pipeline coroutines call set_stage_progress directly
    via await. This bridge is kept exclusively for the sync Drew.* wrappers
    (run_agentic_loop path).

    P1-9 fix: timeout reduced from 10s to 3s (stage writes are fast). Logs a
    warning on timeout rather than silently swallowing it.
    Swallows all errors — progress tracking is best-effort (Law #3 carve-out:
    stage_progress is bookkeeping, not the authoritative pipeline result).
    """
    import logging as _lg
    _log = _lg.getLogger(__name__)

    if not project_id or not suite_id:
        return

    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress

    async def _run() -> None:
        await set_stage_progress(
            project_id=project_id,
            stage=stage,
            state=state,
            suite_id=suite_id,
        )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                try:
                    future.result(timeout=3)  # P1-9: reduced from 10s to 3s
                except concurrent.futures.TimeoutError:
                    _log.warning(
                        "_run_async_set_stage timed out project=%s stage=%s state=%s",
                        project_id[:8], stage, state,
                    )
        else:
            loop.run_until_complete(_run())
    except RuntimeError:
        try:
            asyncio.run(_run())
        except Exception:
            pass
    except Exception:
        pass  # Never propagate — progress is best-effort


def _run_async_ingest(
    *,
    pdf_bytes: bytes,
    project_hash: str,
    suite_id: str,
    office_id: str,
    filename: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async ingest pipeline from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Running under uvicorn/asyncio — use a new event loop in thread pool
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_ingest_pipeline(
                        pdf_bytes=pdf_bytes,
                        project_hash=project_hash,
                        suite_id=suite_id,
                        office_id=office_id,
                        filename=filename,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=120)
        else:
            return loop.run_until_complete(
                _async_ingest_pipeline(
                    pdf_bytes=pdf_bytes,
                    project_hash=project_hash,
                    suite_id=suite_id,
                    office_id=office_id,
                    filename=filename,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_ingest_pipeline(
                pdf_bytes=pdf_bytes,
                project_hash=project_hash,
                suite_id=suite_id,
                office_id=office_id,
                filename=filename,
                correlation_id=correlation_id,
            )
        )


async def _async_ingest_pipeline(
    *,
    pdf_bytes: bytes,
    project_hash: str,
    suite_id: str,
    office_id: str,
    filename: str,
    correlation_id: str,
    project_id: str | None = None,
    _skip_project_creation: bool = False,
) -> dict[str, Any]:
    """Async ingest: idempotency check → OCR → store sheets → upload thumbnails.

    When project_id is provided and _skip_project_creation=True (dispatch path),
    skips dedup check + row creation — the dispatch route already handled both.
    All downstream sheet/thumbnail logic is unchanged.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_insert,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.ocr_coordinator import extract_sheet_corpus
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
    from aspire_orchestrator.services.blueprint.thumbnail_storage import upload_sheet_thumbnail
    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        upload_sheet_image as _upload_sheet_image,
    )
    from aspire_orchestrator.services.receipt_store import store_receipts

    # Idempotency check: look for existing project with same content hash.
    # Skipped when _skip_project_creation=True (dispatch path already handled dedup).
    # P2-1 fix: query uses content_hash column (canonical), not address (legacy).
    # NOTE: projects created before this fix stored the hash in `address`.
    # A one-time data migration is needed:
    #   UPDATE blueprint_projects SET content_hash = address
    #   WHERE content_hash IS NULL AND address ~ '^[a-f0-9]{64}$';
    # Queue as follow-up SQL migration before Wave 10 deploy.
    if not _skip_project_creation:
        try:
            existing = await supabase_select(
                "blueprint_projects",
                filters=f"suite_id=eq.{suite_id}&content_hash=eq.{project_hash}",
                limit=1,
            )
            if existing:
                candidate_id = str(existing[0]["id"])
                # Validate dedup hit: the existing project must have completed INGEST
                # and have at least one sheet.  A row with ingest != "done" or zero sheets
                # is a stale/broken project from a prior failed run — treat as a new upload.
                candidate_progress = existing[0].get("stage_progress") or {}
                ingest_state = candidate_progress.get("ingest", "not_started") if isinstance(candidate_progress, dict) else "not_started"
                existing_sheets = await supabase_select(
                    "blueprint_sheets",
                    filters=f"project_id=eq.{candidate_id}&suite_id=eq.{suite_id}",
                )
                sheet_ids = [str(s["id"]) for s in existing_sheets]

                if ingest_state == "done" and sheet_ids:
                    _log.info(
                        "drew.ingest: dedup hit for hash=%s suite=%s, returning existing project=%s sheets=%d",
                        project_hash[:8],
                        suite_id[:8],
                        candidate_id[:8],
                        len(sheet_ids),
                    )
                    return {
                        "status": "dedup",
                        "stage": "ingest",
                        "project_id": candidate_id,
                        "sheet_count": len(sheet_ids),
                        "sheet_ids": sheet_ids,
                        "provider_mix": {},
                    }
                else:
                    # Stale project: ingest never completed or has no sheets.
                    # Emit a receipt for observability (Law #2) then fall through
                    # to create a fresh project.
                    _log.warning(
                        "drew.ingest: dedup_invalid_stale hash=%s suite=%s stale_project=%s "
                        "ingest_state=%s sheet_count=%d — treating as new upload",
                        project_hash[:8],
                        suite_id[:8],
                        candidate_id[:8],
                        ingest_state,
                        len(sheet_ids),
                    )
                    store_receipts([{
                        "id": str(uuid.uuid4()),
                        "outcome": "failed",
                        "action_type": "blueprint.upload.dedup_invalid_stale",
                        "receipt_type": "blueprint.upload.dedup_invalid_stale",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "risk_tier": "GREEN",
                        "actor_type": "WORKER",
                        "actor_id": ACTOR_DREW,
                        "correlation_id": correlation_id,
                        "suite_id": suite_id,
                        "inputs_hash": _compute_inputs_hash({"project_hash": project_hash[:8], "suite_id": suite_id}),
                        "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                        "metadata": {
                            "stale_project_id": candidate_id,
                            "ingest_state": ingest_state,
                            "sheet_count": len(sheet_ids),
                        },
                    }])
                    # Fall through — create a new project row below
        except SupabaseClientError as exc:
            _log.warning(
                "drew.ingest: idempotency check failed (%s), proceeding with fresh ingest",
                type(exc).__name__,
            )

        # P2-1 fix: write hash to content_hash (canonical column), not address.
        # address column is reserved for the job-site postal address (contractor context).
        project_id = str(uuid.uuid4())
        try:
            await supabase_insert(
                "blueprint_projects",
                {
                    "id": project_id,
                    "suite_id": suite_id,
                    "office_id": office_id,
                    "content_hash": project_hash,  # Canonical column for dedup key
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except SupabaseClientError as exc:
            raise RuntimeError(f"Failed to create blueprint_project: {exc}") from exc
    else:
        # Dispatch path: project_id was provided by the dispatch route (already created)
        if not project_id:
            raise RuntimeError(
                "_skip_project_creation=True requires project_id to be provided"
            )
        _log.info(
            "drew.ingest: using pre-created project_id=%s (dispatch path) suite=%s corr=%s",
            project_id[:8],
            suite_id[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )

    # P0-3 fix: mark ingest in_progress directly in the async pipeline
    # (previously only set in sync wrapper; dispatch path bypassed it).
    try:
        await set_stage_progress(
            project_id=project_id,
            stage="ingest",
            state="in_progress",
            suite_id=suite_id,
        )
    except Exception as _sp_exc:
        _log.warning("drew.ingest: set_stage_progress(in_progress) failed: %s", _sp_exc)

    _log.info(
        "drew.ingest: created project=%s, hash=%s, corr=%s",
        project_id[:8],
        project_hash[:8],
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # OCR: extract sheet corpus
    try:
        corpus = await extract_sheet_corpus(
            pdf_bytes,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    except asyncio.CancelledError:
        # P0 fix (Wave 9f): mark ingest failed before re-raising on task cancellation.
        await _mark_stage_failed_best_effort(project_id, "ingest", suite_id)
        raise

    # Wave 9l: concurrent per-sheet processing.
    # Rasterization (asyncio.to_thread via PyMuPDF) + thumbnail + full-res image
    # upload run up to ASPIRE_DREW_INGEST_CONCURRENCY sheets in parallel.
    # PyMuPDF page rasterization releases the GIL, making asyncio.to_thread
    # concurrent on multi-core Railway CPUs.
    # Sheet_number order is preserved by gathering results indexed by position.
    _ingest_sem = asyncio.Semaphore(int(os.getenv("ASPIRE_DREW_INGEST_CONCURRENCY", "4")))

    # Pre-assign sheet IDs deterministically so the order in sheet_ids matches
    # corpus.sheets after gather (gather preserves task order).
    _assigned_ids: list[str] = [str(uuid.uuid4()) for _ in corpus.sheets]

    async def _process_ingest_sheet(
        idx: int,
        ocr_sheet: Any,
    ) -> dict[str, Any]:
        """Process one OCR sheet: DLP + DB insert + thumbnail + full-res image.

        Returns a dict with:
          sheet_id: str | None (None if DB insert failed)
          thumbnail_failure: bool
          dlp_redactions: int
        """
        sheet_id = _assigned_ids[idx]
        result: dict[str, Any] = {
            "sheet_id": None,
            "thumbnail_failure": False,
            "dlp_redactions": 0,
        }

        async with _ingest_sem:
            # P0-10 fix: apply DLP before writing OCR text to DB (Law #9).
            ocr_text_raw = ocr_sheet.text
            ocr_text_clean, redaction_count = _dlp_redact(ocr_text_raw)
            if redaction_count == -2:
                # Law #3 / Law #9: DLP service unavailable — placeholder was stored.
                # Emit dlp_unavailable receipt so ops can detect the gap.
                _log.warning(
                    "drew.ingest: DLP unavailable for sheet page=%d project=%s — "
                    "placeholder stored (Law #3 fail-closed)",
                    ocr_sheet.page_number, project_id[:8],
                )
                store_receipts([{
                    "id": str(uuid.uuid4()),
                    "outcome": "failed",
                    "action_type": "blueprint.ingest.dlp_unavailable",
                    "receipt_type": "blueprint.ingest.dlp_unavailable",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": ACTOR_DREW,
                    "correlation_id": correlation_id,
                    "suite_id": suite_id,
                    "metadata": {
                        "sheet_page": ocr_sheet.page_number,
                        "project_id": project_id,
                        "reason": "dlp_service_unavailable",
                        "action_taken": "placeholder_stored",
                    },
                    "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                }])
            else:
                # DLP ran (redaction_count >= 0) — always emit a scan receipt.
                # This fires whether or not any PII was found (zero-hit path included).
                result["dlp_redactions"] = redaction_count
                store_receipts([{
                    "id": str(uuid.uuid4()),
                    "outcome": "success",
                    "action_type": "blueprint.ingest.dlp_scanned",
                    "receipt_type": "blueprint.ingest.dlp_scanned",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": ACTOR_DREW,
                    "correlation_id": correlation_id,
                    "suite_id": suite_id,
                    "metadata": {
                        "sheet_page": ocr_sheet.page_number,
                        "project_id": project_id,
                        "redaction_count": redaction_count,
                        "pii_detected": redaction_count > 0,
                    },
                    "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                }])

            # DB insert — includes image_storage_path as null initially;
            # updated below after full-res upload.
            try:
                await supabase_insert(
                    "blueprint_sheets",
                    {
                        "id": sheet_id,
                        "suite_id": suite_id,
                        "office_id": office_id,
                        "project_id": project_id,
                        "sheet_number": str(ocr_sheet.page_number),
                        "ocr_text": ocr_text_clean,  # P0-10: write DLP-cleaned text
                        "hash": ocr_sheet.page_hash,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                result["sheet_id"] = sheet_id
            except SupabaseClientError as exc:
                _log.error(
                    "drew.ingest: failed to insert sheet page=%d error=%s",
                    ocr_sheet.page_number,
                    type(exc).__name__,
                )
                # Continue — partial ingest better than total failure.
                return result

            if ocr_sheet.image_bytes:
                # 1. Thumbnail upload (signed URL for UI display)
                signed_url = await upload_sheet_thumbnail(
                    suite_id=suite_id,
                    project_id=project_id,
                    sheet_id=sheet_id,
                    png_bytes=ocr_sheet.image_bytes,
                    correlation_id=correlation_id,
                )
                if signed_url:
                    try:
                        await supabase_update(
                            "blueprint_sheets",
                            f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                            {"thumbnail_url": signed_url},
                        )
                    except SupabaseClientError:
                        _log.warning(
                            "drew.ingest: failed to write thumbnail_url for sheet=%s",
                            sheet_id[:8],
                        )
                else:
                    # Thumbnail upload failed — emit receipt for observability (Law #2)
                    result["thumbnail_failure"] = True
                    store_receipts([{
                        "id": str(uuid.uuid4()),
                        "outcome": "failed",
                        "action_type": "blueprint.ingest.thumbnail_upload_failed",
                        "receipt_type": "blueprint.ingest.thumbnail_upload_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "risk_tier": "GREEN",
                        "actor_type": "WORKER",
                        "actor_id": ACTOR_DREW,
                        "correlation_id": correlation_id,
                        "suite_id": suite_id,
                        "inputs_hash": _compute_inputs_hash({"sheet_id": sheet_id}),
                        "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                        "metadata": {
                            "sheet_id": sheet_id,
                            "project_id": project_id,
                        },
                    }])

                # Wave 9n Fix 2: Full-res image upload — REQUIRED for SEE cache.
                # SEE stage runs in 30-60s when all sheets are cached; without the
                # cache SEE falls back to PDF re-render (~50s extra).
                # Strategy: try once; if None or raises, retry once after 0.5s.
                # If second attempt also fails, emit image_cache_failed receipt and
                # set image_storage_path=None (SEE will re-render this sheet only).
                image_path: str | None = None
                _img_attempts = 0
                while _img_attempts < 2:
                    try:
                        image_path = await _upload_sheet_image(
                            suite_id,
                            project_id,
                            sheet_id,
                            ocr_sheet.image_bytes,
                            correlation_id,
                        )
                        if image_path is not None:
                            break  # Success
                        # upload returned None (storage error without raising)
                        if _img_attempts == 0:
                            _log.warning(
                                "drew.ingest: full-res upload returned None sheet=%s, retrying once",
                                sheet_id[:8],
                            )
                            await asyncio.sleep(0.5)
                            _img_attempts += 1
                        else:
                            break  # Second attempt also returned None
                    except Exception as _img_exc:
                        if _img_attempts == 0:
                            _log.warning(
                                "drew.ingest: full-res upload raised %s sheet=%s, retrying once",
                                type(_img_exc).__name__,
                                sheet_id[:8],
                            )
                            await asyncio.sleep(0.5)
                            _img_attempts += 1
                        else:
                            _log.warning(
                                "drew.ingest: full-res upload failed twice sheet=%s (%s) — "
                                "SEE will re-render this sheet",
                                sheet_id[:8],
                                type(_img_exc).__name__,
                            )
                            break

                if image_path:
                    try:
                        await supabase_update(
                            "blueprint_sheets",
                            f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                            {"image_storage_path": image_path},
                        )
                    except SupabaseClientError:
                        _log.warning(
                            "drew.ingest: failed to write image_storage_path for sheet=%s — "
                            "SEE will re-render (graceful degradation)",
                            sheet_id[:8],
                        )
                else:
                    # Law #2: emit image_cache_failed receipt so ops can audit
                    # which sheets will force PDF re-render during SEE.
                    _log.warning(
                        "drew.ingest: full-res image upload failed after retry sheet=%s — "
                        "emitting image_cache_failed receipt, SEE will re-render (corr=%s)",
                        sheet_id[:8],
                        correlation_id[:8],
                    )
                    store_receipts([{
                        "id": str(uuid.uuid4()),
                        "outcome": "failed",
                        "action_type": "blueprint.ingest.image_cache_failed",
                        "receipt_type": "blueprint.ingest.image_cache_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "risk_tier": "GREEN",
                        "actor_type": "WORKER",
                        "actor_id": ACTOR_DREW,
                        "correlation_id": correlation_id,
                        "suite_id": suite_id,
                        "metadata": {
                            "sheet_id": sheet_id,
                            "project_id": project_id,
                            "page_number": ocr_sheet.page_number,
                            "reason": "upload_failed_after_retry",
                        },
                        "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                    }])

        return result

    # Run all sheets concurrently, bounded by semaphore.
    # return_exceptions=True prevents one sheet failure from cancelling others.
    per_sheet_ingest_results: list[Any] = await asyncio.gather(
        *(_process_ingest_sheet(i, s) for i, s in enumerate(corpus.sheets)),
        return_exceptions=True,
    )

    # Collect results preserving sheet_number order (gather preserves task order).
    sheet_ids: list[str] = []
    thumbnail_failures: int = 0
    dlp_total_redactions: int = 0

    for r in per_sheet_ingest_results:
        if isinstance(r, BaseException):
            _log.warning(
                "drew.ingest: per-sheet coroutine raised %s — skipping sheet",
                type(r).__name__,
            )
            continue
        if r.get("sheet_id"):
            sheet_ids.append(r["sheet_id"])
        if r.get("thumbnail_failure"):
            thumbnail_failures += 1
        dlp_total_redactions += r.get("dlp_redactions", 0)

    # P0-3: mark ingest done/failed based on whether sheets were stored.
    # The stage_progress write uses try/except to remain best-effort.
    final_state = "done" if sheet_ids else "failed"
    try:
        await set_stage_progress(
            project_id=project_id,
            stage="ingest",
            state=final_state,
            suite_id=suite_id,
        )
    except Exception as _sp_exc:
        _log.warning(
            "drew.ingest: set_stage_progress(%s) failed: %s", final_state, _sp_exc
        )

    _log.info(
        "drew.ingest: stored %d sheets for project=%s thumbnail_failures=%d "
        "dlp_redactions=%d corr=%s",
        len(sheet_ids),
        project_id[:8],
        thumbnail_failures,
        dlp_total_redactions,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    return {
        "status": "ok",
        "stage": "ingest",
        "project_id": project_id,
        "sheet_count": len(sheet_ids),
        "sheet_ids": sheet_ids,
        "provider_mix": corpus.provider_mix,
    }


def _run_async_classify(
    *,
    project_id: str,
    suite_id: str,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async classify pipeline from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_classify_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        model=model,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=120)
        else:
            return loop.run_until_complete(
                _async_classify_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    model=model,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_classify_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                model=model,
                correlation_id=correlation_id,
            )
        )


async def _async_classify_pipeline(
    *,
    project_id: str,
    suite_id: str,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Async classify: discipline tagging + revision detection + DB updates.

    P0-3 fix: stage_progress updated directly inside this coroutine (dispatch path).
    P1-10 fix: zero sheets → status="failed" (was incorrectly "ok").
    P2-2 fix: DLP applied to tag.reasoning before writing to blueprint_missing_inputs.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.discipline_tagger import tag_disciplines
    from aspire_orchestrator.services.blueprint.revision_detector import detect_revisions
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress

    # P0-3: mark in_progress at the start of the async pipeline (dispatch path)
    try:
        await set_stage_progress(
            project_id=project_id, stage="classify", state="in_progress", suite_id=suite_id
        )
    except Exception as _sp_exc:
        _log.warning("drew.classify: set_stage_progress(in_progress) failed: %s", _sp_exc)

    # Load sheets for this project (RLS enforces suite_id at DB level)
    try:
        sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except asyncio.CancelledError:
        # P0 fix (Wave 9f): mark stage failed before re-raising on task cancellation.
        await _mark_stage_failed_best_effort(project_id, "classify", suite_id)
        raise
    except SupabaseClientError as exc:
        # P0-3: mark failed before raising
        try:
            await set_stage_progress(
                project_id=project_id, stage="classify", state="failed", suite_id=suite_id
            )
        except Exception:
            pass
        raise RuntimeError(f"Failed to load sheets for project {project_id[:8]}: {exc}") from exc

    if not sheets:
        # P1-10 fix: zero sheets is a pipeline failure, not a success.
        # The dispatch caller checks for non-ok status and emits a failed receipt.
        _log.warning(
            "drew.classify: no sheets found for project=%s — returning failed, corr=%s",
            project_id[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        try:
            await set_stage_progress(
                project_id=project_id, stage="classify", state="failed", suite_id=suite_id
            )
        except Exception:
            pass
        return {
            "status": "failed",
            "reason": "no_sheets_to_classify",
            "stage": "classify",
            "project_id": project_id,
            "discipline_counts": {},
            "revisions": 0,
            "needs_review_count": 0,
        }

    # Discipline tagging
    tags = await tag_disciplines(
        sheets,
        model=model,
        correlation_id=correlation_id,
    )

    # Build sheet_id → tag index and persist discipline/confidence
    tag_index = {t.sheet_id: t for t in tags}
    discipline_counts: dict[str, int] = {}
    needs_review_count = 0

    # ---------------------------------------------------------------
    # Wave 17b.6 — load symbols once for Signal 2 of the verifier.
    # blueprint_symbols has no project_id column; it joins via sheet_id.
    # ---------------------------------------------------------------
    from aspire_orchestrator.services.blueprint.discipline_verifier import (
        verify_classification,
    )

    sheet_ids_csv = ",".join(str(s["id"]) for s in sheets)
    symbols_by_sheet: dict[str, list[dict[str, Any]]] = {}
    if sheet_ids_csv:
        try:
            symbol_rows = await supabase_select(
                "blueprint_symbols",
                filters=f"sheet_id=in.({sheet_ids_csv})&suite_id=eq.{suite_id}",
            )
            for row in symbol_rows or []:
                sid = str(row.get("sheet_id") or "")
                if not sid:
                    continue
                symbols_by_sheet.setdefault(sid, []).append(row)
        except SupabaseClientError as exc:
            _log.warning(
                "drew.classify: failed to load symbols for verifier: %s",
                type(exc).__name__,
            )

    verify_promoted = 0
    verify_confirmed = 0
    verify_flagged = 0
    verify_demoted = 0

    for sheet in sheets:
        sheet_id = str(sheet["id"])
        tag = tag_index.get(sheet_id)
        if not tag:
            continue

        # Wave 11p: extract sheet_code + sheet_title from OCR title block.
        # Done BEFORE verification so the verifier sees the latest sheet_code.
        update_data: dict[str, Any] = {}
        try:
            from aspire_orchestrator.services.blueprint.sheet_code_extractor import (
                extract_sheet_code_title,
            )

            existing_code = sheet.get("sheet_code")
            existing_title = sheet.get("sheet_title")
            if not existing_code or not existing_title:
                extracted = extract_sheet_code_title(sheet.get("ocr_text"))
                if extracted.sheet_code and not existing_code:
                    update_data["sheet_code"] = extracted.sheet_code
                    sheet["sheet_code"] = extracted.sheet_code  # feed verifier
                if extracted.sheet_title and not existing_title:
                    update_data["sheet_title"] = extracted.sheet_title
                    sheet["sheet_title"] = extracted.sheet_title
        except Exception as exc:
            _log.warning(
                "drew.classify: sheet_code extract failed sheet=%s err=%s",
                sheet_id[:8],
                type(exc).__name__,
            )

        # Wave 17b.6 — run cross-check verifier.
        verdict = verify_classification(
            sheet_id=sheet_id,
            sheet_number=sheet.get("sheet_number"),
            discipline_assigned=tag.discipline,
            sheet_code=sheet.get("sheet_code"),
            ocr_text=sheet.get("ocr_text"),
            symbols_on_sheet=symbols_by_sheet.get(sheet_id, []),
            neighbor_sheets=[s for s in sheets if str(s["id"]) != sheet_id],
        )

        if verdict.verdict == "promote":
            verify_promoted += 1
        elif verdict.verdict == "confirm":
            verify_confirmed += 1
        elif verdict.verdict == "flag":
            verify_flagged += 1
        else:
            verify_demoted += 1

        # Apply verdict to the sheet row.
        final_discipline = verdict.discipline_final
        if final_discipline:
            discipline_counts[final_discipline] = (
                discipline_counts.get(final_discipline, 0) + 1
            )
            update_data["discipline"] = final_discipline
        # Demote: clear the discipline column when no signal upholds it.
        # supabase_update will write NULL when the column is in update_data
        # with a None value; only do this if there was previously a value.
        elif tag.discipline is not None and verdict.verdict == "demote":
            update_data["discipline"] = None

        # Record needs_review for callers (truth != field_confirmed/derived).
        if verdict.truth in ("assumed", "missing_input"):
            needs_review_count += 1

        # When verdict flags or demotes, persist a SPECIFIC missing_input row.
        if verdict.missing_input_reason is not None:
            # Law #9: DLP-redact before persisting the description.
            reason_clean, _rc = _dlp_redact(verdict.missing_input_reason)
            try:
                from aspire_orchestrator.services.supabase_client import (
                    supabase_insert_batch_ignore_conflicts,
                )
                # Wave 19a: ON CONFLICT (project_id, description_hash) DO NOTHING
                # prevents exact-text duplicates from discipline_verifier re-runs.
                await supabase_insert_batch_ignore_conflicts(
                    "blueprint_missing_inputs",
                    [{
                        "id": str(uuid.uuid4()),
                        "suite_id": suite_id,
                        "project_id": project_id,
                        "description": reason_clean,
                        "suggested_resolution": (
                            "Open the sheet, verify the discipline, and update "
                            "the assignment manually."
                        ),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }],
                    conflict_target="project_id,description_hash",
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.classify: failed to insert verifier missing_input "
                    "for sheet %s: %s",
                    sheet_id[:8],
                    type(exc).__name__,
                )

        if update_data:
            try:
                await supabase_update(
                    "blueprint_sheets",
                    f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                    update_data,
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.classify: failed to update sheet %s: %s",
                    sheet_id[:8],
                    type(exc).__name__,
                )

    # Emit Wave 17b.6 verifier receipt (Law #2). Best-effort.
    try:
        from aspire_orchestrator.services.receipt_store import store_receipts as _store_receipts
        store_receipts_payload = [{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "drew.classify.verified",
            "receipt_type": "drew.classify.verified",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": "skillpack:drew-blueprint",
            "correlation_id": correlation_id,
            "metadata": {
                "stage": "classify",
                "project_id": project_id,
                "promoted_to_confirmed": verify_promoted,
                "confirmed_count": verify_confirmed,
                "flagged_count": verify_flagged,
                "demoted_to_missing": verify_demoted,
                "agreed_count": verify_promoted + verify_confirmed,
                "disagreed_count": verify_flagged + verify_demoted,
            },
        }]
        _store_receipts(store_receipts_payload)
    except Exception as _r_exc:
        _log.warning(
            "drew.classify: failed to emit verified receipt: %s",
            type(_r_exc).__name__,
        )

    # Revision detection
    revision_links = detect_revisions(sheets)
    revision_count = len(revision_links)

    for link in revision_links:
        try:
            await supabase_update(
                "blueprint_sheets",
                f"id=eq.{link.superseding_sheet_id}&suite_id=eq.{suite_id}",
                {"supersedes_id": link.superseded_sheet_id},
            )
        except SupabaseClientError as exc:
            _log.warning(
                "drew.classify: failed to update supersedes_id for sheet %s: %s",
                link.superseding_sheet_id[:8],
                type(exc).__name__,
            )

    _log.info(
        "drew.classify: project=%s, disciplines=%s, revisions=%d, needs_review=%d, corr=%s",
        project_id[:8],
        discipline_counts,
        revision_count,
        needs_review_count,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # P0-3: mark classify done in the async pipeline (dispatch path)
    try:
        await set_stage_progress(
            project_id=project_id, stage="classify", state="done", suite_id=suite_id
        )
    except Exception as _sp_exc:
        _log.warning("drew.classify: set_stage_progress(done) failed: %s", _sp_exc)

    # Wave 19c: cascade self-resolution — run on discipline-flagged missing_inputs
    # immediately after classify so they don't wait until REASON.
    # office_id not carried through classify pipeline — pass None (cascade is
    # suite-scoped and RLS handles isolation without office_id).
    try:
        await _run_missing_input_cascade(
            project_id=project_id,
            suite_id=suite_id,
            office_id=None,
            correlation_id=correlation_id,
        )
    except Exception as _casc_exc:
        _log.warning(
            "drew.classify: cascade self-resolution failed (non-blocking) project=%s: %s",
            project_id[:8], type(_casc_exc).__name__,
        )

    # Wave 9h: cache_hit_ratio and token counts are logged inside tag_disciplines.
    # No separate accumulation needed here — stage summary log is in the tagger.
    return {
        "status": "ok",
        "stage": "classify",
        "project_id": project_id,
        "discipline_counts": discipline_counts,
        "revisions": revision_count,
        "needs_review_count": needs_review_count,
    }


# ---------------------------------------------------------------------------
# Stage 2.5 UNIVERSAL_SHEET_INTELLIGENCE — async helpers (Wave 20c)
# ---------------------------------------------------------------------------

def _run_async_universal_sheet_intel(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async universal sheet intelligence pipeline from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_universal_sheet_intel_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=3600)  # 39 sheets × ~90s/sheet max
        else:
            return loop.run_until_complete(
                _async_universal_sheet_intel_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_universal_sheet_intel_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                correlation_id=correlation_id,
            )
        )


async def _async_universal_sheet_intel_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    correlation_id: str,
) -> dict[str, Any]:
    """Per-sheet Azure layout extraction for all sheets in the project.

    - Loads all blueprint_sheets for the project.
    - Downloads each sheet PNG from Supabase Storage.
    - Calls extract_sheet_intelligence on each sheet (semaphore-capped).
    - Upserts results into blueprint_sheet_intelligence (idempotent on project_id+sheet_id).
    - Fail-soft: single sheet failure → log + continue.
      Stage fails only if >50% of sheets fail.
    - Returns summary: {sheets_total, sheets_succeeded, sheets_failed, total_azure_ms}.

    Law #2: caller emits the summary receipt; each extract_sheet_intelligence call
            emits its own provider.azure.sheet_layout receipt.
    Law #3: if no sheets exist, returns status=failed immediately.
    Law #6: all DB reads filter by suite_id.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_upsert,
    )
    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        download_sheet_image as _download_sheet_image,
    )
    from aspire_orchestrator.services.blueprint.universal_sheet_extractor import (
        extract_sheet_intelligence,
    )
    from aspire_orchestrator.providers.base_client import ProviderError
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress

    concurrency = int(os.getenv("ASPIRE_DREW_OCR_CONCURRENCY", "15"))
    sem = asyncio.Semaphore(concurrency)

    # Load all sheets for this project (active sheets only — exclude superseded)
    try:
        all_sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except (SupabaseClientError, asyncio.CancelledError) as exc:
        _log.error(
            "drew.universal_intel: failed to load sheets project=%s: %s",
            project_id[:8], type(exc).__name__,
        )
        return {
            "status": "failed",
            "stage": "universal_sheet_intelligence",
            "project_id": project_id,
            "sheets_total": 0,
            "sheets_succeeded": 0,
            "sheets_failed": 0,
            "total_azure_ms": 0,
        }

    active_sheets = [s for s in (all_sheets or []) if not s.get("supersedes_id")]
    sheets_total = len(active_sheets)

    if sheets_total == 0:
        _log.warning(
            "drew.universal_intel: no active sheets found project=%s — stage failed",
            project_id[:8],
        )
        return {
            "status": "failed",
            "stage": "universal_sheet_intelligence",
            "project_id": project_id,
            "sheets_total": 0,
            "sheets_succeeded": 0,
            "sheets_failed": 0,
            "total_azure_ms": 0,
        }

    _log.info(
        "drew.universal_intel: starting project=%s sheets=%d concurrency=%d corr=%s",
        project_id[:8], sheets_total, concurrency,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    async def _process_one_sheet(sheet: dict[str, Any]) -> dict[str, Any]:
        """Download PNG + run Azure layout + upsert result. Returns outcome dict."""
        sheet_id: str = str(sheet.get("id") or "")
        if not sheet_id:
            return {"sheet_id": "", "success": False, "azure_ms": 0, "reason": "no_sheet_id"}

        async with sem:
            # Download sheet PNG (INGEST-stage cache)
            png_bytes = await _download_sheet_image(
                suite_id=suite_id,
                project_id=project_id,
                sheet_id=sheet_id,
                correlation_id=correlation_id,
            )
            if not png_bytes:
                _log.warning(
                    "drew.universal_intel: no PNG for sheet=%s project=%s — skipping",
                    sheet_id[:8], project_id[:8],
                )
                return {
                    "sheet_id": sheet_id,
                    "success": False,
                    "azure_ms": 0,
                    "reason": "no_png",
                }

            try:
                row = await extract_sheet_intelligence(
                    suite_id=suite_id,
                    office_id=office_id,
                    project_id=project_id,
                    sheet_id=sheet_id,
                    sheet_png_bytes=png_bytes,
                    correlation_id=correlation_id,
                )
            except ProviderError as exc:
                _log.warning(
                    "drew.universal_intel: Azure failed sheet=%s project=%s: %s",
                    sheet_id[:8], project_id[:8], exc,
                )
                return {
                    "sheet_id": sheet_id,
                    "success": False,
                    "azure_ms": 0,
                    "reason": f"azure_error:{type(exc).__name__}",
                }
            except Exception as exc:
                _log.warning(
                    "drew.universal_intel: unexpected error sheet=%s project=%s: %s",
                    sheet_id[:8], project_id[:8], type(exc).__name__,
                )
                return {
                    "sheet_id": sheet_id,
                    "success": False,
                    "azure_ms": 0,
                    "reason": f"error:{type(exc).__name__}",
                }

            azure_ms: int = row.get("azure_call_ms") or 0

            # Upsert into blueprint_sheet_intelligence — idempotent on (project_id, sheet_id)
            try:
                await supabase_upsert(
                    "blueprint_sheet_intelligence",
                    row,
                    on_conflict="project_id,sheet_id",
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "drew.universal_intel: upsert failed sheet=%s project=%s: %s",
                    sheet_id[:8], project_id[:8], type(exc).__name__,
                )
                return {
                    "sheet_id": sheet_id,
                    "success": False,
                    "azure_ms": azure_ms,
                    "reason": f"upsert_error:{type(exc).__name__}",
                }

            _log.info(
                "drew.universal_intel: sheet=%s done azure_ms=%d",
                sheet_id[:8], azure_ms,
            )
            return {"sheet_id": sheet_id, "success": True, "azure_ms": azure_ms}

    # Capture row count before writes for persistence assertion (Track B Pass 1.C)
    try:
        from aspire_orchestrator.services.supabase_client import count_table_rows as _count_rows
        _intel_count_before = await _count_rows(
            "blueprint_sheet_intelligence",
            project_id=project_id,
            suite_id=suite_id,
        )
    except Exception:
        _intel_count_before = 0

    # Dispatch all sheets concurrently under semaphore cap
    tasks = [_process_one_sheet(sheet) for sheet in active_sheets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sheets_succeeded = 0
    sheets_failed = 0
    total_azure_ms = 0

    for outcome in results:
        if isinstance(outcome, BaseException):
            sheets_failed += 1
            _log.warning(
                "drew.universal_intel: gather task raised %s — counted as failure",
                type(outcome).__name__,
            )
        elif outcome.get("success"):
            sheets_succeeded += 1
            total_azure_ms += outcome.get("azure_ms", 0)
        else:
            sheets_failed += 1

    # Fail-soft: only mark stage failed if >50% of sheets failed
    failure_rate = sheets_failed / sheets_total if sheets_total > 0 else 0.0
    status = "failed" if failure_rate > 0.50 else "ok"

    _log.info(
        "drew.universal_intel: project=%s total=%d succeeded=%d failed=%d "
        "total_azure_ms=%d status=%s corr=%s",
        project_id[:8], sheets_total, sheets_succeeded, sheets_failed,
        total_azure_ms, status,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # Persistence assertion — blueprint_sheet_intelligence must have gained rows (Track B Pass 1.C)
    try:
        from aspire_orchestrator.services.supabase_client import count_table_rows as _count_rows_after
        from aspire_orchestrator.services.receipt_store import emit_persistence_asserted_receipt as _pa
        _intel_count_after = await _count_rows_after(
            "blueprint_sheet_intelligence",
            project_id=project_id,
            suite_id=suite_id,
        )
        await _pa(
            stage="blueprint.ingest.azure_doc_intel_used",
            expected_table="blueprint_sheet_intelligence",
            expected_min=1,
            count_before=_intel_count_before,
            count_after=_intel_count_after,
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            redacted_outputs={
                "sheets_total": sheets_total,
                "sheets_succeeded": sheets_succeeded,
                "sheets_failed": sheets_failed,
            },
        )
    except Exception as _pa_exc:  # noqa: BLE001
        _log.warning(
            "drew.universal_intel: persistence assertion failed (%s) — proceeding",
            type(_pa_exc).__name__,
        )

    return {
        "status": status,
        "stage": "universal_sheet_intelligence",
        "project_id": project_id,
        "sheets_total": sheets_total,
        "sheets_succeeded": sheets_succeeded,
        "sheets_failed": sheets_failed,
        "total_azure_ms": total_azure_ms,
    }


# ---------------------------------------------------------------------------
# Stage 3 SEE — async helpers
# ---------------------------------------------------------------------------

def _run_async_see(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    pdf_bytes: bytes,
    confidence_floor: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async SEE pipeline from sync context (mirrors ingest/classify)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_see_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        pdf_bytes=pdf_bytes,
                        confidence_floor=confidence_floor,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=600)  # SEE is slower; 10min ceiling
        else:
            return loop.run_until_complete(
                _async_see_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    pdf_bytes=pdf_bytes,
                    confidence_floor=confidence_floor,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_see_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                pdf_bytes=pdf_bytes,
                confidence_floor=confidence_floor,
                correlation_id=correlation_id,
            )
        )


async def _async_see_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    pdf_bytes: bytes,
    confidence_floor: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Per-sheet SEE: YOLO symbols + scale + seal. Persists symbols/scale/flag.

    Sheet matching strategy: pdf_splitter re-renders the PDF and produces the
    same SHA-256 hashes the INGEST stage stored. We index sheet rows by hash
    so we know which DB row each rendered image belongs to.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_delete,
        supabase_insert,
        supabase_select,
        supabase_update,
    )
    from aspire_orchestrator.services.blueprint.pdf_splitter import split_pdf_to_sheets
    from aspire_orchestrator.services.blueprint.symbol_detector import (
        SymbolDetectorError,
        detect_symbols,
    )
    from aspire_orchestrator.services.blueprint.see_naming import (
        interpretable_fallback,
        is_opaque_name,
        should_suppress_yolo_class,
    )
    from aspire_orchestrator.services.blueprint.scale_calibrator import calibrate_scale
    from aspire_orchestrator.services.blueprint.seal_detector import detect_engineer_seal
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        download_sheet_image as _download_sheet_image,
    )

    # P1 fix: application-level mutex to guard the SELECT→DELETE→INSERT pattern
    # against concurrent SEE dispatch on the same project_id (single-replica safety).
    # Multi-replica protection requires a Postgres advisory lock migration — see
    # _get_stage_lock docstring for the migration path.
    _see_lock = _get_stage_lock("see", project_id)
    if _see_lock.locked():
        # A concurrent SEE run is already in-flight for this project_id.
        # Emit a skip receipt and return early without touching blueprint_symbols.
        _log.warning(
            "drew.see: concurrent run detected for project=%s — emitting skip receipt",
            project_id[:8],
        )
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "failed",
            "action_type": "blueprint.see.concurrent_skip",
            "receipt_type": "blueprint.see.concurrent_skip",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {"project_id": project_id, "reason": "concurrent_run"},
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
        return {"status": "skipped", "reason": "concurrent_run", "stage": "see", "project_id": project_id}

    # Lock is not held — acquire it for the duration of the SEE body.
    # asyncio cooperative scheduling means no other coroutine can interleave
    # between .locked() check and .acquire() call below (no await in between).
    await _see_lock.acquire()

    # P0-3: mark see in_progress in the async pipeline (dispatch path)
    try:
        await set_stage_progress(
            project_id=project_id, stage="see", state="in_progress", suite_id=suite_id
        )
    except asyncio.CancelledError:
        _see_lock.release()
        raise
    except Exception as _sp_exc:
        _log.warning("drew.see: set_stage_progress(in_progress) failed: %s", _sp_exc)

    # P0-7: idempotency clear — delete existing blueprint_symbols rows for this project
    # before re-inserting.
    #
    # Wave 10c schema fix: blueprint_symbols has NO project_id column — it joins to
    # the project via blueprint_sheets.project_id (see migration 20260517000001).
    # The old `project_id=eq.<id>` filter returned a 400 every SEE run, polluting
    # logs and (correctly) failing the idempotency clear silently. Now we resolve
    # sheet IDs first then filter symbols by `sheet_id=in.(...)`.
    try:
        _project_sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        ) or []
        _sheet_ids = [s["id"] for s in _project_sheets if s.get("id")]

        _existing_symbols = []
        if _sheet_ids:
            # PostgREST `in.(uuid1,uuid2,...)` — parenthesized, comma-separated, no spaces.
            _id_list = ",".join(_sheet_ids)
            _existing_symbols = await supabase_select(
                "blueprint_symbols",
                filters=f"sheet_id=in.({_id_list})&suite_id=eq.{suite_id}",
            ) or []
        _deleted_count = len(_existing_symbols)
        if _deleted_count > 0 and _sheet_ids:
            _id_list = ",".join(_sheet_ids)
            await supabase_delete(
                "blueprint_symbols",
                f"sheet_id=in.({_id_list})&suite_id=eq.{suite_id}",
            )
            store_receipts([{
                "id": str(uuid.uuid4()),
                "outcome": "success",
                "action_type": "blueprint.see.idempotency_clear",
                "receipt_type": "blueprint.see.idempotency_clear",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "risk_tier": "GREEN",
                "actor_type": "WORKER",
                "actor_id": ACTOR_DREW,
                "correlation_id": correlation_id,
                "suite_id": suite_id,
                "metadata": {
                    "project_id": project_id,
                    "deleted_symbols": _deleted_count,
                },
                "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
            }])
            _log.info(
                "drew.see: idempotency_clear deleted %d symbols for project=%s",
                _deleted_count, project_id[:8],
            )
    except Exception as _ic_exc:
        _log.warning("drew.see: idempotency_clear failed (%s) — proceeding", type(_ic_exc).__name__)

    # Load all sheet rows for this project (RLS-scoped).
    try:
        sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except asyncio.CancelledError:
        # P0 fix (Wave 9f): mark see failed before re-raising on task cancellation.
        await _mark_stage_failed_best_effort(project_id, "see", suite_id)
        _see_lock.release()
        raise
    except SupabaseClientError as exc:
        try:
            await set_stage_progress(
                project_id=project_id, stage="see", state="failed", suite_id=suite_id
            )
        except Exception:
            pass
        _see_lock.release()
        raise RuntimeError(f"Failed to load sheets for project {project_id[:8]}: {exc}") from exc

    if not sheets:
        _log.warning(
            "drew.see: no sheets found for project=%s, corr=%s",
            project_id[:8],
            correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
        )
        try:
            await set_stage_progress(
                project_id=project_id, stage="see", state="failed", suite_id=suite_id
            )
        except Exception:
            pass
        _see_lock.release()
        return {
            "status": "failed",
            "reason": "no_sheets_for_see",
            "stage": "see",
            "project_id": project_id,
            "sheet_count": 0,
            "symbol_count": 0,
            "mean_confidence": 0.0,
            "seal_sheets": 0,
            "missing_inputs": 0,
            "model_version": os.getenv("ASPIRE_DREW_YOLO_MODEL", "yolo11n.pt"),
        }

    # ─────────────────────────────────────────────────────────────────
    # Wave 11j: VGSE (Vector Graph Symbol Extraction) — additive primary
    # detector for vector-native PDFs. Runs BEFORE YOLO/CPU inference and
    # writes deterministic-signature detections to blueprint_symbols.
    # YOLO continues unchanged below as supplement / raster fallback.
    # ─────────────────────────────────────────────────────────────────
    try:
        from aspire_orchestrator.services.blueprint.vgse_pipeline import (
            detections_to_blueprint_symbol_rows,
            run_vgse_for_project,
        )
        import tempfile as _tf
        with _tf.NamedTemporaryFile(delete=False, suffix=".pdf") as _vgse_tmp:
            _vgse_tmp.write(pdf_bytes)
            _vgse_pdf_path = _vgse_tmp.name
        _vgse_sheet_ids_by_page: dict[int, str] = {}
        for _s in sheets:
            try:
                _vgse_sheet_ids_by_page[int(_s.get("sheet_number") or 0)] = str(_s["id"])
            except (ValueError, KeyError, TypeError):
                continue
        _vgse_summary = await run_vgse_for_project(
            pdf_path=_vgse_pdf_path,
            suite_id=suite_id,
            office_id=office_id,
            project_id=project_id,
            sheet_ids_by_page=_vgse_sheet_ids_by_page,
            correlation_id=correlation_id,
        )
        # Wave 17b.3: pass sheet→discipline map so opaque motif names get an
        # interpretable fallback ('electrical_symbol_high' etc.) at row-build time.
        _vgse_discipline_by_sheet_id: dict[str, str | None] = {
            str(s["id"]): s.get("discipline")
            for s in sheets
            if s.get("id")
        }
        _vgse_rows = detections_to_blueprint_symbol_rows(
            _vgse_summary.get("detections", []),
            discipline_by_sheet_id=_vgse_discipline_by_sheet_id,
        )
        _vgse_inserted = 0
        for _row in _vgse_rows:
            try:
                await supabase_insert("blueprint_symbols", _row)
                _vgse_inserted += 1
            except SupabaseClientError as _vgse_exc:
                _log.warning("drew.see[vgse]: insert failed (%s) sig=%s",
                            type(_vgse_exc).__name__, _row.get("bbox", {}).get("signature_hash", "?")[:8])
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "blueprint.see.vgse.completed",
            "receipt_type": "blueprint.see.vgse.completed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "metadata": {
                "project_id": project_id,
                "vector_pages": _vgse_summary.get("vector_pages", 0),
                "raster_pages": _vgse_summary.get("raster_pages", 0),
                "motifs_found": _vgse_summary.get("total_motifs_found", 0),
                "total_instances": _vgse_summary.get("total_instances", 0),
                "memory_hits": _vgse_summary.get("memory_hits", 0),
                "legend_hits": _vgse_summary.get("legend_hits", 0),
                "unanchored": _vgse_summary.get("unanchored", 0),
                "rows_inserted": _vgse_inserted,
                "elapsed_ms": _vgse_summary.get("elapsed_ms", 0),
            },
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
        _log.info(
            "drew.see[vgse]: project=%s motifs=%d instances=%d rows=%d (memory=%d legend=%d unanchored=%d)",
            project_id[:8],
            _vgse_summary.get("total_motifs_found", 0),
            _vgse_summary.get("total_instances", 0),
            _vgse_inserted,
            _vgse_summary.get("memory_hits", 0),
            _vgse_summary.get("legend_hits", 0),
            _vgse_summary.get("unanchored", 0),
        )
        try:
            os.unlink(_vgse_pdf_path)
        except OSError:
            pass
    except asyncio.CancelledError:
        raise
    except Exception as _vgse_exc:  # noqa: BLE001
        # Fail-soft: VGSE is additive. Never block the rest of SEE.
        _log.warning("drew.see[vgse]: pipeline failed (%s) — proceeding with YOLO only", type(_vgse_exc).__name__)

    # ─────────────────────────────────────────────────────────────────
    # Wave 18 — Wave 11g-v2 SEE integration (feature-flagged).
    # Vector → graph → WL-mine → cascade-resolve → blueprint_symbols.
    # Default OFF; flip ASPIRE_USE_WAVE_11G2=true on Railway to enable.
    # Runs additively; VGSE rows above are untouched.
    # ─────────────────────────────────────────────────────────────────
    try:
        from aspire_orchestrator.services.blueprint import wave_11g2_see as _w11g2
        if _w11g2.is_enabled():
            _wl_sheet_ids_by_page: dict[int, str] = {}
            _wl_discipline_by_page: dict[int, str | None] = {}
            for _s in sheets:
                try:
                    _pn = int(_s.get("sheet_number") or 0)
                except (ValueError, KeyError, TypeError):
                    continue
                _wl_sheet_ids_by_page[_pn] = str(_s["id"])
                _wl_discipline_by_page[_pn] = _s.get("discipline")
            _wl_summary = await _w11g2.run_wave_11g2_see_for_project(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                pdf_bytes=pdf_bytes,
                sheet_ids_by_page=_wl_sheet_ids_by_page,
                correlation_id=correlation_id,
                discipline_by_page=_wl_discipline_by_page,
            )
            _log.info(
                "drew.see[11g2]: project=%s pages=%d motifs=%d instances=%d rows=%d",
                project_id[:8],
                _wl_summary.get("pages_processed", 0),
                _wl_summary.get("total_motifs", 0),
                _wl_summary.get("total_instances", 0),
                _wl_summary.get("rows_inserted", 0),
            )
    except asyncio.CancelledError:
        raise
    except Exception as _wl_exc:  # noqa: BLE001
        # Fail-soft: Wave 11g-v2 is additive. Never block the rest of SEE.
        _log.warning(
            "drew.see[11g2]: pipeline failed (%s) — proceeding without WL motifs",
            type(_wl_exc).__name__,
        )

    # Wave 9l: Try to load per-sheet 200 DPI PNGs from the INGEST-stage cache
    # (blueprint-sheet-images bucket) before falling back to PDF re-render.
    # This eliminates the root-cause 520+s SEE timeout: INGEST already rasterized
    # every sheet; SEE was throwing that work away with a redundant split_pdf_to_sheets call.
    #
    # Cache-hit path:  ALL sheets have image_storage_path set → skip pdf_splitter entirely.
    # Fallback path:   Any sheet lacks image_storage_path → re-render full PDF (legacy).
    from aspire_orchestrator.services.blueprint.pdf_splitter import SheetExtract as _SheetExtract

    sheets_with_path = [s for s in sheets if s.get("image_storage_path")]
    all_cached = len(sheets_with_path) == len(sheets) and len(sheets) > 0

    extracts: list[Any]  # list[SheetExtract] — populated via cache or pdf_splitter

    if all_cached:
        # Download cached PNGs concurrently (bounded by the same SEE concurrency env var).
        _dl_sem = asyncio.Semaphore(int(os.getenv("ASPIRE_DREW_SEE_CONCURRENCY", "4")))

        async def _download_cached(sheet_row: dict[str, Any]) -> Any | None:
            sheet_id = str(sheet_row["id"])
            async with _dl_sem:
                png_bytes = await _download_sheet_image(
                    suite_id,
                    project_id,
                    sheet_id,
                    correlation_id,
                )
            if png_bytes is None:
                return None
            return _SheetExtract(
                page_number=int(sheet_row.get("sheet_number") or 0),
                text=str(sheet_row.get("ocr_text") or ""),
                image_bytes=png_bytes,
                page_hash=str(sheet_row.get("hash") or ""),
            )

        _dl_results = await asyncio.gather(
            *(_download_cached(s) for s in sheets),
            return_exceptions=True,
        )

        # If any download failed, fall back to PDF re-render.
        _dl_ok = [
            r for r in _dl_results
            if r is not None and not isinstance(r, BaseException)
        ]
        _dl_hits = len(_dl_ok)
        _dl_total = len(sheets)
        _cache_hit_ratio = _dl_hits / _dl_total if _dl_total else 0.0

        if _dl_hits == _dl_total:
            # Full cache hit — use downloaded PNGs, skip PDF splitter entirely.
            extracts = _dl_ok
            _log.info(
                "drew.see: cache_hit_ratio=1.0 all %d sheets loaded from storage — "
                "skipping pdf_splitter, corr=%s",
                _dl_total,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
        else:
            # Partial cache hit — fall back to PDF re-render for correctness.
            _log.warning(
                "drew.see: partial cache hit (%d/%d) — falling back to pdf_splitter, corr=%s",
                _dl_hits, _dl_total,
                correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
            )
            all_cached = False  # trigger fallback path below
            extracts = []  # will be populated in fallback

        # Emit cache hit ratio receipt regardless of hit/miss (Law #2 observability).
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "blueprint.see.cache_hit_ratio",
            "receipt_type": "blueprint.see.cache_hit_ratio",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {
                "project_id": project_id,
                "cache_hits": _dl_hits,
                "total_sheets": _dl_total,
                "cache_hit_ratio": round(_cache_hit_ratio, 4),
                "source": "blueprint-sheet-images",
            },
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])

    if not all_cached:
        # Fallback: re-render PDF to recover per-sheet 200 DPI PNG bytes.
        # Fires when: (a) no sheets have image_storage_path, or (b) partial download failures.
        try:
            extracts = split_pdf_to_sheets(pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            try:
                await set_stage_progress(
                    project_id=project_id, stage="see", state="failed", suite_id=suite_id
                )
            except Exception:
                pass
            _see_lock.release()
            raise RuntimeError(f"pdf_splitter failed in SEE: {type(exc).__name__}") from exc

    # Index DB rows by hash (the canonical join key).
    sheet_by_hash: dict[str, dict[str, Any]] = {
        str(row.get("hash")): row for row in sheets if row.get("hash")
    }

    # ───── Wave 9k: per-discipline skip filter ─────
    # Cover/Title/Specs/Schedule/Addenda sheets carry zero detectable symbols
    # (text-heavy spec docs / title blocks). Skipping them eliminates wasted
    # YOLO inference time. Symbol-producing disciplines configurable via env.
    _disciplines_env = os.getenv("ASPIRE_DREW_SEE_DISCIPLINES", "A,S,M,E,P,FP,C,L")
    _allowed_disciplines: set[str] = {
        d.strip() for d in _disciplines_env.split(",") if d.strip()
    }

    skipped_sheet_ids: list[str] = []
    skipped_disciplines: dict[str, int] = {}
    extracts_to_process: list[Any] = []
    for extract in extracts:
        sheet_row = sheet_by_hash.get(extract.page_hash)
        if not sheet_row:
            continue
        discipline = sheet_row.get("discipline")
        if discipline in _allowed_disciplines:
            extracts_to_process.append(extract)
        else:
            skipped_sheet_ids.append(str(sheet_row["id"]))
            key = str(discipline) if discipline else "unknown"
            skipped_disciplines[key] = skipped_disciplines.get(key, 0) + 1

    if skipped_sheet_ids:
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "blueprint.see.skipped_disciplines",
            "receipt_type": "blueprint.see.skipped_disciplines",
            "event_type": "blueprint.see.skipped_disciplines",
            "status": "success",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {
                "project_id": project_id,
                "skipped_sheet_ids": skipped_sheet_ids,
                "skipped_disciplines": skipped_disciplines,
                "reason": "discipline_not_symbol_producing",
                "allowed_disciplines": sorted(_allowed_disciplines),
            },
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
        _log.info(
            "drew.see: skipped %d sheets (disciplines=%s) for project=%s",
            len(skipped_sheet_ids), skipped_disciplines, project_id[:8],
        )

    # ───── Wave 9k: bounded concurrent per-sheet inference ─────
    # YOLO inference is CPU-bound; symbol_detector.detect_symbols offloads
    # model.predict via asyncio.to_thread, so a bounded gather here yields
    # genuine parallelism on multi-core CPU (Railway shared CPU benefits
    # because the event loop is freed for the next sheet while one waits
    # on its predict thread).
    _max_concurrency = int(os.getenv("ASPIRE_DREW_SEE_CONCURRENCY", "4"))
    _max_concurrency = max(1, _max_concurrency)
    _sem = asyncio.Semaphore(_max_concurrency)

    default_model_version = os.getenv("ASPIRE_DREW_YOLO_MODEL", "yolo11n.pt")

    async def _process_sheet(extract: Any) -> dict[str, Any]:
        """Process one sheet: YOLO + scale + seal + DB writes. Returns per-sheet stats."""
        sheet_row = sheet_by_hash.get(extract.page_hash)
        # Shouldn't be None — extracts_to_process was filtered — but guard.
        if not sheet_row:
            return {"symbols": 0, "conf_sum": 0.0, "conf_count": 0, "seal": 0, "missing": 0, "model": default_model_version}
        sheet_id = str(sheet_row["id"])

        async with _sem:
            # ───── 1. YOLO symbol detection ─────
            try:
                detections = await detect_symbols(
                    extract.image_bytes,
                    sheet_id=sheet_id,
                    correlation_id=correlation_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    confidence_floor=confidence_floor,
                )
            except SymbolDetectorError as exc:
                raise RuntimeError(
                    f"YOLO weights unavailable: {exc}. "
                    f"Run `python -c \"from ultralytics import YOLO; YOLO('{default_model_version}')\"` to fetch."
                ) from exc

            local_symbols = 0
            local_conf_sum = 0.0
            local_conf_count = 0
            local_model = default_model_version
            # Wave 17b.3: per-sheet naming-pipeline counters. Aggregated into the
            # blueprint.see.symbol_filter receipt at end of run.
            local_yolo_detections = len(detections)
            local_coco_suppressed = 0
            local_fallback_named = 0
            local_kept_real_name = 0
            sheet_discipline = sheet_row.get("discipline")

            for det in detections:
                # Wave 17b.3: kill COCO/unmapped leaks before they reach the UI.
                suppress, _reason = should_suppress_yolo_class(det.class_name)
                if suppress:
                    local_coco_suppressed += 1
                    continue
                # Surviving rows: rewrite opaque names with interpretable fallback.
                # (YOLO output is unlikely to be opaque today — it's either a real
                # mapped class or it was suppressed above — but this keeps the
                # write path symmetric with VGSE so future model swaps are safe.)
                original_class = det.class_name
                row_used_fallback = is_opaque_name(original_class)
                if row_used_fallback:
                    final_class = interpretable_fallback(
                        original_class, sheet_discipline, det.confidence
                    )
                    local_fallback_named += 1
                else:
                    final_class = original_class
                    local_kept_real_name += 1
                # Preserve original under bbox.original_class so Wave 18 spatial OCR
                # can dedupe / replace. Law #2 — never lose source of truth.
                bbox_out = dict(det.bbox)
                bbox_out["original_class"] = original_class
                bbox_out["name_source"] = "fallback" if row_used_fallback else "yolo_mapped"
                try:
                    await supabase_insert(
                        "blueprint_symbols",
                        {
                            "id": str(uuid.uuid4()),
                            "suite_id": suite_id,
                            "office_id": office_id,
                            "sheet_id": sheet_id,
                            "class": final_class,
                            "bbox": bbox_out,
                            "confidence": det.confidence,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    local_symbols += 1
                    local_conf_sum += det.confidence
                    local_conf_count += 1
                    local_model = det.model_version
                except SupabaseClientError as exc:
                    # Wave 9n Fix 4: log the actual error message (not just class name)
                    # so Supabase RLS/column errors are visible in Railway logs.
                    _log.warning(
                        "drew.see: failed to insert symbol sheet=%s (%s: %s)",
                        sheet_id[:8],
                        type(exc).__name__,
                        str(exc)[:200],
                    )
                    # Law #2: emit a receipt for audit — ops can query these to find
                    # RLS or schema mismatches without digging through log streams.
                    store_receipts([{
                        "id": str(uuid.uuid4()),
                        "outcome": "failed",
                        "action_type": "blueprint.see.symbol_insert_failed",
                        "receipt_type": "blueprint.see.symbol_insert_failed",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "risk_tier": "GREEN",
                        "actor_type": "WORKER",
                        "actor_id": ACTOR_DREW,
                        "correlation_id": correlation_id,
                        "suite_id": suite_id,
                        "metadata": {
                            "sheet_id": sheet_id,
                            "project_id": project_id,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:200],
                        },
                        "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                    }])

            # ───── 2. Scale calibration ─────
            sheet_text: str = str(sheet_row.get("ocr_text") or extract.text or "")
            calibration = calibrate_scale(extract.image_bytes, sheet_text)
            local_missing = 0
            if calibration.scale_factor > 0 and calibration.confidence >= 0.70:
                scale_str = (
                    calibration.text_match
                    or f"{calibration.scale_factor:.6f}{calibration.units}/px"
                )
                try:
                    await supabase_update(
                        "blueprint_sheets",
                        f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                        {"scale": scale_str[:120]},
                    )
                except SupabaseClientError as exc:
                    # Wave 9n Fix 4: include actual error message for RLS/schema diagnosis.
                    _log.warning(
                        "drew.see: failed to update scale sheet=%s (%s: %s)",
                        sheet_id[:8],
                        type(exc).__name__,
                        str(exc)[:200],
                    )
            elif calibration.method != "none" and calibration.confidence < 0.50:
                try:
                    await supabase_insert(
                        "blueprint_missing_inputs",
                        {
                            "id": str(uuid.uuid4()),
                            "suite_id": suite_id,
                            "project_id": project_id,
                            "description": (
                                f"Sheet {sheet_row.get('sheet_number', sheet_id[:8])}: "
                                f"scale calibration confidence {calibration.confidence:.0%} "
                                f"(method={calibration.method}). Please confirm drawing scale."
                            ),
                            "suggested_resolution": (
                                "Enter the explicit drawing scale (e.g. 1/4\" = 1'-0\" or 1:50)."
                            ),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    local_missing = 1
                except SupabaseClientError as exc:
                    _log.warning(
                        "drew.see: failed to insert scale missing_input sheet=%s (%s)",
                        sheet_id[:8],
                        type(exc).__name__,
                    )

            # ───── 3. Engineer-seal detection ─────
            seal = detect_engineer_seal(extract.image_bytes)
            local_seal = 0
            if seal.seal_detected:
                local_seal = 1
                try:
                    await supabase_update(
                        "blueprint_sheets",
                        f"id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                        {"seal_detected": True},
                    )
                except SupabaseClientError as exc:
                    # Wave 9n Fix 4: include actual error message for RLS/schema diagnosis.
                    _log.warning(
                        "drew.see: failed to set seal_detected sheet=%s (%s: %s)",
                        sheet_id[:8],
                        type(exc).__name__,
                        str(exc)[:200],
                    )

            return {
                "symbols": local_symbols,
                "conf_sum": local_conf_sum,
                "conf_count": local_conf_count,
                "seal": local_seal,
                "missing": local_missing,
                "model": local_model,
                # Wave 17b.3 — naming-pipeline observability
                "yolo_detections": local_yolo_detections,
                "coco_suppressed": local_coco_suppressed,
                "fallback_named": local_fallback_named,
                "kept_real_name": local_kept_real_name,
            }

    # Run all in-scope sheets concurrently bounded by _max_concurrency.
    # return_exceptions=True so a single sheet failure doesn't kill the batch;
    # CancelledError propagates correctly when the outer wait_for fires.
    per_sheet_results = await asyncio.gather(
        *(_process_sheet(e) for e in extracts_to_process),
        return_exceptions=True,
    )

    # If the outer wait_for cancelled us, every task above has been cancelled
    # too. Re-raise so the stage-failed receipt path in see() runs.
    for r in per_sheet_results:
        if isinstance(r, asyncio.CancelledError):
            _see_lock.release()
            raise r

    total_symbols = 0
    seal_sheets = 0
    missing_inputs = 0
    confidence_sum = 0.0
    confidence_count = 0
    model_version = default_model_version
    # Wave 17b.3 — naming-pipeline aggregates
    agg_yolo_detections = 0
    agg_coco_suppressed = 0
    agg_fallback_named = 0
    agg_kept_real_name = 0
    for r in per_sheet_results:
        if isinstance(r, BaseException):
            # SymbolDetectorError wrapped as RuntimeError surfaces here; fail-closed
            # per Law #3 — bubble up so see() can mark the stage failed.
            _see_lock.release()
            raise r
        agg_yolo_detections += r.get("yolo_detections", 0)
        agg_coco_suppressed += r.get("coco_suppressed", 0)
        agg_fallback_named += r.get("fallback_named", 0)
        agg_kept_real_name += r.get("kept_real_name", 0)
        total_symbols += r["symbols"]
        seal_sheets += r["seal"]
        missing_inputs += r["missing"]
        confidence_sum += r["conf_sum"]
        confidence_count += r["conf_count"]
        if r["model"]:
            model_version = r["model"]

    mean_conf = (confidence_sum / confidence_count) if confidence_count else 0.0

    _log.info(
        "drew.see: project=%s sheets=%d symbols=%d mean_conf=%.2f seals=%d "
        "missing=%d corr=%s",
        project_id[:8],
        len(sheets),
        total_symbols,
        mean_conf,
        seal_sheets,
        missing_inputs,
        correlation_id[:8] if len(correlation_id) > 8 else correlation_id,
    )

    # Wave 17b.3: aggregated naming-pipeline receipt. Gives ops a single row to
    # answer "how many garbage YOLO labels did we suppress, and how many symbols
    # got an interpretable fallback name?" without grepping logs.
    try:
        _vgse_memory_hits = 0
        _vgse_legend_hits = 0
        _vgse_unanchored = 0
        try:
            _vgse_memory_hits = int(_vgse_summary.get("memory_hits", 0))  # type: ignore[name-defined]
            _vgse_legend_hits = int(_vgse_summary.get("legend_hits", 0))  # type: ignore[name-defined]
            _vgse_unanchored = int(_vgse_summary.get("unanchored", 0))    # type: ignore[name-defined]
        except (NameError, AttributeError):
            # VGSE block didn't run (failed earlier or skipped) — leave at 0.
            pass
        final_kept = agg_kept_real_name + agg_fallback_named + _vgse_memory_hits + _vgse_legend_hits + _vgse_unanchored
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "blueprint.see.symbol_filter",
            "receipt_type": "blueprint.see.symbol_filter",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "metadata": {
                "project_id": project_id,
                "yolo_detections": agg_yolo_detections,
                "coco_suppressed": agg_coco_suppressed,
                "vgse_motifs": _vgse_memory_hits + _vgse_legend_hits + _vgse_unanchored,
                "memory_named": _vgse_memory_hits,
                "legend_named": _vgse_legend_hits,
                "fallback_named": agg_fallback_named + _vgse_unanchored,
                "yolo_kept_real_name": agg_kept_real_name,
                "final_kept": final_kept,
            },
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
    except Exception as _rec_exc:  # noqa: BLE001 — receipts must never block completion
        _log.warning("drew.see: symbol_filter receipt emission failed (%s)", type(_rec_exc).__name__)

    # Wave 10c: ground-truth completion receipt BEFORE state mutation. Receipts are
    # immutable (Law #2), so even if set_stage_progress fails, ops can query
    # `receipts WHERE receipt_type='blueprint.see.completed' AND metadata->>'project_id'=...`
    # to determine real completion. Aligned with 2026 outbox-pattern recommendation:
    # the entity emits a domain event first, the state writer runs separately with retries.
    try:
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "blueprint.see.completed",
            "receipt_type": "blueprint.see.completed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "office_id": office_id,
            "metadata": {
                "project_id": project_id,
                "sheet_count": len(sheets),
                "symbol_count": total_symbols,
                "mean_confidence": round(mean_conf, 4),
                "seal_sheets": seal_sheets,
                "missing_inputs": missing_inputs,
            },
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
    except Exception as _rec_exc:  # noqa: BLE001 — receipt emission must never block stage completion
        _log.warning("drew.see: completed receipt emission failed (%s)", type(_rec_exc).__name__)

    # P0-3: mark see done in the async pipeline (dispatch path)
    # Wave 10c: retry once on transient Supabase errors so a single hiccup doesn't
    # leave stage_progress out-of-sync with the ground-truth receipt above.
    for _attempt in range(2):
        try:
            await set_stage_progress(
                project_id=project_id, stage="see", state="done", suite_id=suite_id
            )
            break
        except Exception as _sp_exc:
            if _attempt == 0:
                _log.warning("drew.see: set_stage_progress(done) attempt 1 failed (%s) — retrying", type(_sp_exc).__name__)
                await asyncio.sleep(0.5)
                continue
            _log.error("drew.see: set_stage_progress(done) failed after 2 attempts: %s — ground-truth receipt blueprint.see.completed has been emitted, ops can backfill stage_progress from receipts", _sp_exc)

    _see_lock.release()
    return {
        "status": "ok",
        "stage": "see",
        "project_id": project_id,
        "sheet_count": len(sheets),
        "symbol_count": total_symbols,
        "mean_confidence": round(mean_conf, 4),
        "seal_sheets": seal_sheets,
        "missing_inputs": missing_inputs,
        "model_version": model_version,
    }


# ---------------------------------------------------------------------------
# Stage 4 REASON — async helpers
# ---------------------------------------------------------------------------

def _run_async_reason(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async REASON pipeline from sync context (mirrors ingest/classify/see)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_reason_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        model=model,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=300)  # REASON: 5-min ceiling
        else:
            return loop.run_until_complete(
                _async_reason_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    model=model,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_reason_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                model=model,
                correlation_id=correlation_id,
            )
        )


async def _async_reason_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    model: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Async REASON: call story_writer.write_story and map result to stage dict.

    P0-3 fix: stage_progress updated directly in this coroutine (dispatch path).
    P0-7 fix: delete existing blueprint_story/assemblies/materials rows for
              idempotency before re-writing. Emits a reason.idempotency_clear receipt.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.blueprint.story_writer import write_story
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
    from aspire_orchestrator.services.supabase_client import supabase_delete

    # P0-3: mark reason in_progress
    try:
        await set_stage_progress(
            project_id=project_id, stage="reason", state="in_progress", suite_id=suite_id
        )
    except Exception as _sp_exc:
        _log.warning("drew.reason: set_stage_progress(in_progress) failed: %s", _sp_exc)

    # P0-7: idempotency clear — delete existing story/assemblies/materials for this project.
    # Count existing rows first (supabase_delete returns None, not a count).
    _total_deleted = 0
    for _tbl in ("blueprint_story", "blueprint_assemblies", "blueprint_materials"):
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select as _sel
            _existing = await _sel(
                _tbl,
                filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            )
            _cnt = len(_existing) if _existing else 0
            if _cnt > 0:
                await supabase_delete(
                    _tbl,
                    f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
                )
                _total_deleted += _cnt
        except Exception as _del_exc:
            _log.warning(
                "drew.reason: idempotency_clear %s failed (%s) — proceeding",
                _tbl, type(_del_exc).__name__,
            )
    if _total_deleted > 0:
        store_receipts([{
            "id": str(uuid.uuid4()),
            "outcome": "success",
            "action_type": "blueprint.reason.idempotency_clear",
            "receipt_type": "blueprint.reason.idempotency_clear",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "risk_tier": "GREEN",
            "actor_type": "WORKER",
            "actor_id": ACTOR_DREW,
            "correlation_id": correlation_id,
            "suite_id": suite_id,
            "metadata": {
                "project_id": project_id,
                "deleted_rows": _total_deleted,
            },
            "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
        }])
        _log.info(
            "drew.reason: idempotency_clear deleted %d rows for project=%s",
            _total_deleted, project_id[:8],
        )

    # Wave 9h: clear LLM response cache so stale responses don't survive a re-run.
    try:
        from aspire_orchestrator.services.llm_response_cache import clear_llm_cache_for_project
        await clear_llm_cache_for_project(project_id, suite_id)
    except Exception as _cache_exc:
        _log.warning("drew.reason: llm_response_cache clear failed (non-blocking): %s", _cache_exc)

    try:
        output = await write_story(
            project_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
            model=model,
        )
    except asyncio.CancelledError:
        # P0 fix (Wave 9f): mark stage failed before re-raising so sweeper does not
        # pick up a stuck 'in_progress' row after asyncio task cancellation.
        await _mark_stage_failed_best_effort(project_id, "reason", suite_id)
        raise
    except Exception as _exc:
        try:
            await set_stage_progress(
                project_id=project_id, stage="reason", state="failed", suite_id=suite_id
            )
        except Exception:
            pass
        raise

    _log.info(
        "drew.reason: project=%s phases=%d assemblies=%d materials=%d "
        "missing=%d mean_conf=%.3f corr=%s",
        project_id[:8],
        output.phase_count,
        output.assembly_count,
        output.material_count,
        output.missing_input_count,
        output.mean_confidence,
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    # Wave 19c: cascade self-resolution — attempt to resolve all unresolved
    # missing_inputs before returning. Runs async, non-blocking; any cascade
    # failure is logged and skipped (never blocks REASON from completing).
    try:
        await _run_missing_input_cascade(
            project_id=project_id,
            suite_id=suite_id,
            office_id=office_id,
            correlation_id=correlation_id,
        )
    except Exception as _casc_exc:
        _log.warning(
            "drew.reason: cascade self-resolution failed (non-blocking) project=%s: %s",
            project_id[:8], type(_casc_exc).__name__,
        )

    # Wave 17b.5: stage receipt for assembly typing observability.
    # Emitted on every REASON run, regardless of count, so dashboards can graph
    # taxonomy coverage over time.
    store_receipts([{
        "id": str(uuid.uuid4()),
        "outcome": "success",
        "action_type": "drew.reason.assembly_typed",
        "receipt_type": "drew.reason.assembly_typed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "risk_tier": "GREEN",
        "actor_type": "WORKER",
        "actor_id": ACTOR_DREW,
        "correlation_id": correlation_id,
        "suite_id": suite_id,
        "metadata": {
            "project_id": project_id,
            "typed_count": output.typed_assembly_count,
            "missing_count": output.missing_assembly_count,
            "distinct_types": output.distinct_assembly_types,
            "type_breakdown_sample": dict(
                list(output.assembly_type_breakdown.items())[:10]
            ),
        },
        "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
    }])

    # P0-3: mark reason done
    try:
        await set_stage_progress(
            project_id=project_id, stage="reason", state="done", suite_id=suite_id
        )
    except Exception as _sp_exc:
        _log.warning("drew.reason: set_stage_progress(done) failed: %s", _sp_exc)

    return {
        "status": "ok",
        "stage": "reason",
        "project_id": project_id,
        "phase_count": output.phase_count,
        "assembly_count": output.assembly_count,
        "material_count": output.material_count,
        "missing_input_count": output.missing_input_count,
        "mean_confidence": output.mean_confidence,
        "truth_distribution": output.truth_distribution,
        "model_version": output.model_version,
        # Wave 9h: LLM cache observability fields surfaced in receipt metadata
        "llm_prompt_tokens": output.llm_usage.get("prompt_tokens", 0),
        "llm_cached_tokens": output.llm_usage.get("cached_tokens", 0),
        "llm_cache_hit_ratio": output.llm_usage.get("cache_hit_ratio", 0.0),
        "llm_completion_tokens": output.llm_usage.get("completion_tokens", 0),
        # Wave 17b.5: typing observability for parent receipt
        "typed_assembly_count": output.typed_assembly_count,
        "missing_assembly_count": output.missing_assembly_count,
        "distinct_assembly_types": output.distinct_assembly_types,
    }


# ---------------------------------------------------------------------------
# Stage 5 PROCURE — async helpers (Wave 5)
# ---------------------------------------------------------------------------

def _run_async_procure(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    geofence_miles: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Bridge: run async PROCURE pipeline from sync context (mirrors ingest/classify)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _async_procure_pipeline(
                        project_id=project_id,
                        suite_id=suite_id,
                        office_id=office_id,
                        geofence_miles=geofence_miles,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=300)  # PROCURE: 5-min ceiling
        else:
            return loop.run_until_complete(
                _async_procure_pipeline(
                    project_id=project_id,
                    suite_id=suite_id,
                    office_id=office_id,
                    geofence_miles=geofence_miles,
                    correlation_id=correlation_id,
                )
            )
    except RuntimeError:
        return asyncio.run(
            _async_procure_pipeline(
                project_id=project_id,
                suite_id=suite_id,
                office_id=office_id,
                geofence_miles=geofence_miles,
                correlation_id=correlation_id,
            )
        )


async def _async_procure_pipeline(
    *,
    project_id: str,
    suite_id: str,
    office_id: str | None,
    geofence_miles: float,
    correlation_id: str,
) -> dict[str, Any]:
    """Async PROCURE: tariff-flag + supplier-match all blueprint_materials for a project.

    P0-3 fix: stage_progress updated directly in this coroutine (dispatch path).
    P0-7 note: PROCURE does NOT delete blueprint_materials (REASON owns those rows).
               It UPDATEs existing rows (tariff_flag, supplier_id). Confirmed below.

    For each material row:
      1. detect_tariff_flag(line_item) → UPDATE tariff_flag column
      2. match_suppliers(...) → pick top supplier, UPDATE supplier_id column
         (uses google_places_id or "home_depot" as the supplier_id value)
      3. Compute tariff_exposure_usd and UPDATE the column when unit_cost available.

    Returns summary dict with counts used by Drew.procure() receipt.

    Law #6: All selects and updates include suite_id in filters.
    Law #9: Only line_item[:40] in info logs; no full addresses or PII.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_update,
        supabase_insert,
    )
    from aspire_orchestrator.services.blueprint.tariff_engine import (
        TariffFlag,
        detect_tariff_flag,
        estimate_tariff_impact_usd,
    )
    from aspire_orchestrator.services.blueprint.supplier_matcher import match_suppliers
    from aspire_orchestrator.services.blueprint.schemas.truth import TariffFlag as TariffFlagEnum
    from aspire_orchestrator.services.blueprint.stage_progress import set_stage_progress
    from aspire_orchestrator.services.blueprint.symbol_aggregator import (
        aggregate_symbols_to_materials,
    )

    # P0-3: mark procure in_progress in the async pipeline (dispatch path)
    try:
        await set_stage_progress(
            project_id=project_id, stage="procure", state="in_progress", suite_id=suite_id
        )
    except Exception as _sp_exc:
        _log.warning("drew.procure: set_stage_progress(in_progress) failed: %s", _sp_exc)

    # ── Wave 17b.5: symbol-aggregation pass ──────────────────────────────
    # If REASON left blueprint_materials empty or full of placeholder rows,
    # PROCURE seeds the table from the SEE stage's blueprint_symbols rows
    # (aggregated by class). This makes the materials surface deterministic
    # and reproducible from the diagnostic data, instead of LLM-only.
    #
    # Symbols are joined to sheets to filter by project_id (symbols table has
    # no project_id column — only sheet_id).
    aggregated_inserted = 0
    aggregated_unmapped_classes: dict[str, int] = {}
    aggregated_missing_inputs = 0
    try:
        project_sheets = await supabase_select(
            "blueprint_sheets",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
        project_sheet_ids = [str(s["id"]) for s in (project_sheets or [])]

        all_symbols: list[dict[str, Any]] = []
        for sheet_id in project_sheet_ids:
            try:
                syms = await supabase_select(
                    "blueprint_symbols",
                    filters=f"sheet_id=eq.{sheet_id}&suite_id=eq.{suite_id}",
                )
                if syms:
                    all_symbols.extend(syms)
            except SupabaseClientError:
                # Non-fatal — skip this sheet but continue.
                continue

        if all_symbols:
            agg = aggregate_symbols_to_materials(all_symbols)
            now_iso = datetime.now(timezone.utc).isoformat()

            # Existing line_items for this project — used to dedup so re-running
            # PROCURE is idempotent.
            try:
                existing_mats = await supabase_select(
                    "blueprint_materials",
                    filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
                )
                existing_line_items = {
                    str(m.get("line_item") or "").strip()
                    for m in (existing_mats or [])
                    if str(m.get("line_item") or "").strip()
                }
            except SupabaseClientError:
                existing_line_items = set()

            for am in agg.materials:
                if am.line_item.strip() in existing_line_items:
                    continue  # idempotency — already on the table
                flag = detect_tariff_flag(am.line_item)
                row = am.to_row(
                    row_id=str(uuid.uuid4()),
                    suite_id=suite_id,
                    office_id=office_id,
                    project_id=project_id,
                    created_at=now_iso,
                    tariff_flag=flag.value,
                    assembly_id=None,  # FK populated by future assembly→material linker
                )
                try:
                    await supabase_insert("blueprint_materials", row)
                    aggregated_inserted += 1
                except SupabaseClientError as _exc:
                    _log.warning(
                        "drew.procure: aggregated material insert failed class=%s err=%s",
                        am.symbol_class[:40], type(_exc).__name__,
                    )

            # Surface unmapped classes as missing_inputs so a human reviews them.
            aggregated_unmapped_classes = dict(agg.unmapped_classes)
            for cls, n in agg.unmapped_classes.items():
                desc = (
                    f"Symbol class '{cls[:60]}' appeared {n} times across the sheet set "
                    "but is not mapped to a material spec. Add a mapping in "
                    "symbol_aggregator._CLASS_TO_SPEC or confirm the YOLO label."
                )
                try:
                    await supabase_insert("blueprint_missing_inputs", {
                        "id": str(uuid.uuid4()),
                        "suite_id": suite_id,
                        "office_id": office_id,
                        "project_id": project_id,
                        "description": desc,
                        "suggested_resolution": (
                            "Add the class → (discipline, unit, line_item) entry to "
                            "symbol_aggregator._CLASS_TO_SPEC and re-run PROCURE."
                        ),
                        "created_at": now_iso,
                    })
                    aggregated_missing_inputs += 1
                except SupabaseClientError:
                    pass

            # Wave 17b.5 stage receipt: drew.procure.material_lines
            try:
                tariff_flagged_count = sum(
                    1 for am in agg.materials if detect_tariff_flag(am.line_item) != TariffFlag.NONE
                )
                total_qty = float(sum(am.quantity for am in agg.materials))
                store_receipts([{
                    "id": str(uuid.uuid4()),
                    "outcome": "success",
                    "action_type": "drew.procure.material_lines",
                    "receipt_type": "drew.procure.material_lines",
                    "created_at": now_iso,
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": ACTOR_DREW,
                    "correlation_id": correlation_id,
                    "suite_id": suite_id,
                    "metadata": {
                        "project_id": project_id,
                        "distinct_line_items": len(agg.materials),
                        "total_quantity": total_qty,
                        "tariff_flagged_count": tariff_flagged_count,
                        "aggregated_inserted": aggregated_inserted,
                        "unmapped_classes": len(agg.unmapped_classes),
                        "total_symbols_processed": agg.total_symbols_processed,
                    },
                    "policy": {"decision": "allow", "policy_id": "drew-blueprint-v1", "reasons": []},
                }])
            except Exception:
                pass

    except Exception as _agg_exc:
        # Aggregation is best-effort — never break the existing tariff/supplier
        # update loop if symbols are unavailable.
        _log.warning(
            "drew.procure: symbol aggregation skipped project=%s err=%s",
            project_id[:8], type(_agg_exc).__name__,
        )

    # Load all material rows for this project (RLS-scoped by suite_id)
    try:
        materials = await supabase_select(
            "blueprint_materials",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            order_by="created_at.asc",
        )
    except asyncio.CancelledError:
        # P0 fix (Wave 9f): mark stage failed before re-raising on task cancellation.
        await _mark_stage_failed_best_effort(project_id, "procure", suite_id)
        raise
    except SupabaseClientError as exc:
        raise RuntimeError(f"Failed to load blueprint_materials for project {project_id[:8]}: {exc}") from exc

    if not materials:
        _log.warning(
            "drew.procure: no materials found for project=%s suite=%s corr=%s",
            project_id[:8],
            suite_id[:8],
            correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
        )
        # P1 fix: mark procure done even when no materials exist, so the sweeper
        # does not treat this project as an orphan past the 3-min threshold.
        try:
            await set_stage_progress(
                project_id=project_id, stage="procure", state="done", suite_id=suite_id
            )
        except Exception as _sp_exc:
            _log.warning("drew.procure: set_stage_progress(done) [no-materials] failed: %s", _sp_exc)
        return {
            "status": "ok",
            "stage": "procure",
            "project_id": project_id,
            "materials_processed": 0,
            "tariff_flagged": 0,
            "tariff_breakdown": {},
            "suppliers_matched": 0,
            "supplier_match_rate": 0.0,
            "missing_inputs_added": 0,
        }

    materials_processed = 0
    tariff_flagged = 0
    tariff_breakdown: dict[str, int] = {}
    suppliers_matched = 0
    missing_inputs_added = 0

    for mat in materials:
        material_id = str(mat["id"])
        line_item: str = str(mat.get("line_item") or "")
        quantity: float | None = mat.get("quantity")
        # unit_cost not yet on schema — will be added via supplier price later
        unit_cost: float | None = None

        materials_processed += 1

        # ── 1. Tariff classification ──────────────────────────────────────
        flag = detect_tariff_flag(line_item)
        tariff_exposure: float | None = estimate_tariff_impact_usd(
            flag=flag,
            quantity=quantity,
            unit_cost_usd=unit_cost,
        )

        tariff_update: dict[str, Any] = {"tariff_flag": flag.value}
        if tariff_exposure is not None:
            tariff_update["tariff_exposure_usd"] = tariff_exposure

        try:
            await supabase_update(
                "blueprint_materials",
                f"id=eq.{material_id}&suite_id=eq.{suite_id}",
                tariff_update,
            )
        except SupabaseClientError as exc:
            _log.warning(
                "drew.procure: failed to update tariff_flag material=%s error=%s",
                material_id[:8],
                type(exc).__name__,
            )

        if flag != TariffFlag.NONE:
            tariff_flagged += 1
            tariff_breakdown[flag.value] = tariff_breakdown.get(flag.value, 0) + 1

        # ── 2. Supplier matching ──────────────────────────────────────────
        if line_item and office_id:
            try:
                search_result = await match_suppliers(
                    line_item,
                    suite_id=suite_id,
                    office_id=office_id,
                    project_id=project_id,
                    geofence_miles=geofence_miles,
                    correlation_id=correlation_id,
                )

                if search_result.missing_input_inserted:
                    missing_inputs_added += 1

                if search_result.matches:
                    # Pick top supplier: best-ranked (distance asc, in_stock first)
                    top = search_result.matches[0]
                    # Use place_id when available, else provider name as stable ID
                    if top.provider == "home_depot":
                        supplier_id = "home_depot"
                    else:
                        # Google Places results have place_id in raw — use name as stable key
                        supplier_id = f"gp:{top.name[:60]}"

                    try:
                        await supabase_update(
                            "blueprint_materials",
                            f"id=eq.{material_id}&suite_id=eq.{suite_id}",
                            {"supplier_id": supplier_id},
                        )
                        suppliers_matched += 1
                    except SupabaseClientError as exc:
                        _log.warning(
                            "drew.procure: failed to update supplier_id material=%s error=%s",
                            material_id[:8],
                            type(exc).__name__,
                        )

            except Exception as exc:
                _log.warning(
                    "drew.procure: supplier match failed material=%s error=%s",
                    material_id[:8],
                    type(exc).__name__,
                )
        elif not office_id:
            _log.info(
                "drew.procure: no office_id — skipping supplier match material=%s",
                material_id[:8],
            )

    supplier_match_rate = (
        round(suppliers_matched / materials_processed, 4) if materials_processed else 0.0
    )

    _log.info(
        "drew.procure: project=%s processed=%d tariff_flagged=%d suppliers_matched=%d "
        "missing_inputs=%d corr=%s",
        project_id[:8],
        materials_processed,
        tariff_flagged,
        suppliers_matched,
        missing_inputs_added,
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    # P0-3: mark procure done in the async pipeline (dispatch path)
    try:
        await set_stage_progress(
            project_id=project_id, stage="procure", state="done", suite_id=suite_id
        )
    except Exception as _sp_exc:
        _log.warning("drew.procure: set_stage_progress(done) failed: %s", _sp_exc)

    return {
        "status": "ok",
        "stage": "procure",
        "project_id": project_id,
        "materials_processed": materials_processed,
        "tariff_flagged": tariff_flagged,
        "tariff_breakdown": tariff_breakdown,
        "suppliers_matched": suppliers_matched,
        "supplier_match_rate": supplier_match_rate,
        "missing_inputs_added": missing_inputs_added + aggregated_missing_inputs,
        # Wave 17b.5 aggregation observability
        "aggregated_inserted": aggregated_inserted,
        "unmapped_symbol_classes": len(aggregated_unmapped_classes),
    }


# ---------------------------------------------------------------------------
# Wave 5.1a-5: async helpers for MATERIAL_OVERRIDE + MATERIAL_SKIP
# ---------------------------------------------------------------------------

async def _async_material_override(
    *,
    project_id: str,
    material_id: str,
    suite_id: str,
    override: dict[str, Any],
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Append-only override: insert new blueprint_materials row superseding the original.

    Law #2: original row is NEVER updated. New row gets truth=user_overridden,
    supersedes_id=original material_id.
    Law #6: verifies original row belongs to suite_id before inserting.
    Law #9: supplier_name/notes truncated in receipt (caller enforces).
    """
    import logging as _logging
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_insert,
    )

    # Verify original row exists and belongs to this suite (Law #6)
    try:
        original_rows = await supabase_select(
            "blueprint_materials",
            filters=f"id=eq.{material_id}&project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise RuntimeError(
            f"Failed to load original material {material_id[:8]}: {exc}"
        ) from exc

    if not original_rows:
        raise RuntimeError(
            f"Material {material_id[:8]} not found or not owned by suite {suite_id[:8]}"
        )

    original: dict[str, Any] = original_rows[0]
    now_iso = _dt.now(_tz.utc).isoformat()
    new_id = str(_uuid.uuid4())

    # Build new row — copy safe fields from original, apply override fields
    new_row: dict[str, Any] = {
        "id": new_id,
        "suite_id": suite_id,
        "office_id": original.get("office_id"),
        "project_id": project_id,
        # Carry forward original line_item unless caller explicitly overrides spec_text
        "line_item": str(override.get("spec_text") or original.get("line_item") or ""),
        "quantity": (
            float(override["quantity"])
            if override.get("quantity") is not None
            else original.get("quantity")
        ),
        "unit": str(override["unit"]) if override.get("unit") else original.get("unit"),
        "truth": "user_overridden",
        "tariff_flag": original.get("tariff_flag", "none"),
        # Allow caller to set supplier fields; fall back to original
        "supplier_id": str(override.get("supplier_id") or override.get("supplier_name") or "")[:60] or original.get("supplier_id"),
        "supersedes_id": material_id,
        "created_at": now_iso,
    }

    try:
        await supabase_insert("blueprint_materials", new_row)
    except SupabaseClientError as exc:
        raise RuntimeError(
            f"Failed to insert override row for material {material_id[:8]}: {exc}"
        ) from exc

    _log.info(
        "drew.material_override: project=%s original=%s new=%s suite=%s corr=%s",
        project_id[:8],
        material_id[:8],
        new_id[:8],
        suite_id[:8],
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    return {
        "status": "ok",
        "task": "MATERIAL_OVERRIDE",
        "project_id": project_id,
        "material_id": material_id,
        "new_row_id": new_id,
        "supersedes_id": material_id,
    }


async def _async_material_skip(
    *,
    project_id: str,
    material_id: str,
    suite_id: str,
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Append-only skip: insert new blueprint_materials row superseding the original.

    Law #2: original row is NEVER updated. New row gets truth=user_skipped,
    supersedes_id=original material_id.
    Law #6: verifies original row belongs to suite_id before inserting.
    """
    import logging as _logging
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    _log = _logging.getLogger(__name__)

    from aspire_orchestrator.services.supabase_client import (
        SupabaseClientError,
        supabase_select,
        supabase_insert,
    )

    # Verify original row exists and belongs to this suite (Law #6)
    try:
        original_rows = await supabase_select(
            "blueprint_materials",
            filters=f"id=eq.{material_id}&project_id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except SupabaseClientError as exc:
        raise RuntimeError(
            f"Failed to load original material {material_id[:8]}: {exc}"
        ) from exc

    if not original_rows:
        raise RuntimeError(
            f"Material {material_id[:8]} not found or not owned by suite {suite_id[:8]}"
        )

    original: dict[str, Any] = original_rows[0]
    now_iso = _dt.now(_tz.utc).isoformat()
    new_id = str(_uuid.uuid4())

    new_row: dict[str, Any] = {
        "id": new_id,
        "suite_id": suite_id,
        "office_id": original.get("office_id"),
        "project_id": project_id,
        "line_item": original.get("line_item", ""),
        "quantity": original.get("quantity"),
        "unit": original.get("unit"),
        "truth": "user_skipped",
        "tariff_flag": original.get("tariff_flag", "none"),
        "supplier_id": None,
        "supersedes_id": material_id,
        "created_at": now_iso,
    }

    try:
        await supabase_insert("blueprint_materials", new_row)
    except SupabaseClientError as exc:
        raise RuntimeError(
            f"Failed to insert skip row for material {material_id[:8]}: {exc}"
        ) from exc

    _log.info(
        "drew.material_skip: project=%s original=%s new=%s suite=%s reason=%s corr=%s",
        project_id[:8],
        material_id[:8],
        new_id[:8],
        suite_id[:8],
        reason[:40],
        correlation_id[:8] if len(correlation_id) >= 8 else correlation_id,
    )

    return {
        "status": "ok",
        "task": "MATERIAL_SKIP",
        "project_id": project_id,
        "material_id": material_id,
        "new_row_id": new_id,
        "supersedes_id": material_id,
    }


def _run_async_material_override(
    *,
    project_id: str,
    material_id: str,
    suite_id: str,
    override: dict[str, Any],
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Sync bridge for _async_material_override (mirrors _run_async_procure pattern)."""
    import asyncio as _asyncio

    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    _asyncio.run,
                    _async_material_override(
                        project_id=project_id,
                        material_id=material_id,
                        suite_id=suite_id,
                        override=override,
                        reason=reason,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=10)
        return loop.run_until_complete(
            _async_material_override(
                project_id=project_id,
                material_id=material_id,
                suite_id=suite_id,
                override=override,
                reason=reason,
                correlation_id=correlation_id,
            )
        )
    except Exception:
        return _asyncio.run(
            _async_material_override(
                project_id=project_id,
                material_id=material_id,
                suite_id=suite_id,
                override=override,
                reason=reason,
                correlation_id=correlation_id,
            )
        )


def _run_async_material_skip(
    *,
    project_id: str,
    material_id: str,
    suite_id: str,
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Sync bridge for _async_material_skip (mirrors _run_async_procure pattern)."""
    import asyncio as _asyncio

    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    _asyncio.run,
                    _async_material_skip(
                        project_id=project_id,
                        material_id=material_id,
                        suite_id=suite_id,
                        reason=reason,
                        correlation_id=correlation_id,
                    ),
                )
                return future.result(timeout=10)
        return loop.run_until_complete(
            _async_material_skip(
                project_id=project_id,
                material_id=material_id,
                suite_id=suite_id,
                reason=reason,
                correlation_id=correlation_id,
            )
        )
    except Exception:
        return _asyncio.run(
            _async_material_skip(
                project_id=project_id,
                material_id=material_id,
                suite_id=suite_id,
                reason=reason,
                correlation_id=correlation_id,
            )
        )

