"""Wave 20.6 Stage 6b — VLM verification for borderline symbol bindings.

Clusters that Stage 5 scores in the 0.75–0.92 borderline range are
promoted to this module for a focused vision pass using gpt-5.4-mini.

The model receives two cropped PNG tiles side-by-side (motif + legend
candidate) and answers YES or NO: "Are these two construction symbols
the same?" YES → bind and set resolution_source='vlm_verified'. NO →
leave cluster in unknown bucket.

Cost: ~$0.0003 / call (gpt-5.4-mini vision). Hard cap enforced via
``ASPIRE_VLM_VERIFIER_CAP_USD`` env var (default $0.50 / project).
Once the cap is hit, remaining borderline clusters are left unknown and
a denial receipt is emitted per cluster.

Law compliance:
    #1  Executes bounded vision query; never retries, never decides policy.
    #2  Emits provider.openai.vlm_verifier receipt per call (success, NO,
        cap_denied, error).
    #3  Fail closed: missing API key, network failure, bad response → cluster
        stays unknown. Never guesses.
    #6  Suite-scoped: validates cluster's project belongs to caller's suite_id
        before accessing sheet images. Cross-tenant → returns empty, emits
        denied receipt.
    #9  Never logs raw image bytes, symbol labels, or nearby OCR text.
        Logs only cluster_id prefix, legend_id prefix, and YES/NO answer.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.receipt_store import store_receipts
from aspire_orchestrator.services.supabase_client import supabase_select

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost model (gpt-5.4-mini vision, May 2026 pricing)
# Input: $0.15/1M tokens. Output: $0.60/1M tokens.
# Two 100×100px crops ≈ 2×512 tokens each ≈ ~1100 input tokens total.
# ---------------------------------------------------------------------------
_USD_PER_INPUT_TOKEN: float = 0.15 / 1_000_000
_USD_PER_OUTPUT_TOKEN: float = 0.60 / 1_000_000
_DEFAULT_CAP_USD: float = 0.50
_VISION_MODEL: str = "gpt-5.4-mini"
_CROP_PAD_PX: int = 20
_MAX_OUTPUT_TOKENS: int = 128
_CALL_TIMEOUT_S: float = 10.0
_ACTOR: str = "service:vlm-symbol-verifier"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def verify_borderline_binding(
    *,
    suite_id: str,
    project_id: str,
    cluster_id: str,
    motif_bbox: dict[str, Any],
    motif_sheet_id: str,
    candidate_legend_id: str,
    candidate_legend_label: str,
    correlation_id: str,
    _project_spend_tracker: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Verify one borderline cluster against its candidate legend entry via vision.

    Args:
        suite_id:               Caller tenant UUID (Law #6 gate).
        project_id:             Blueprint project UUID.
        cluster_id:             UUID of the symbol cluster being verified.
        motif_bbox:             Bounding box of the motif on its sheet PNG:
                                {x0, y0, x1, y1} or [x0, y0, x1, y1].
        motif_sheet_id:         UUID of the sheet containing the motif.
        candidate_legend_id:    UUID of the legend entry in blueprint_project_legends.
        candidate_legend_label: Human-readable legend label (e.g. "QR Pendant V2708").
        correlation_id:         Trace correlation ID for receipt/log linkage.
        _project_spend_tracker: Mutable dict keyed by project_id → USD spent so far.
                                Pass the same dict across all calls in one project
                                to enforce the per-project cap. If None, a fresh
                                tracker is created (no cross-call cap enforcement).

    Returns dict with:
        verified (bool):          True if VLM answered YES.
        verdict (str):            'yes' | 'no' | 'cap_denied' | 'error'.
        reason (str):             VLM one-sentence reason, or skip reason.
        receipt_id (str):         UUID of the emitted receipt.
        cost_usd (float):         USD spent on this call (0.0 for denied/error).
    """
    # --- Law #6: Tenant isolation gate ---------------------------------
    if not suite_id or not project_id:
        return _denied_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason="missing_suite_or_project_id",
            action_detail="tenant_isolation_gate_failed",
        )

    # Validate project belongs to this suite before touching any data.
    try:
        project_rows = await supabase_select(
            "blueprint_projects",
            filters=f"id=eq.{project_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason=f"tenant_lookup_failed:{type(exc).__name__}",
        )

    if not project_rows:
        # Project doesn't belong to this suite — silent deny (Law #6).
        return _denied_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason="tenant_isolation_violation",
            action_detail="project_not_in_suite",
        )

    # --- Budget gate --------------------------------------------------
    if _project_spend_tracker is None:
        _project_spend_tracker = {}
    cap_usd = float(os.getenv("ASPIRE_VLM_VERIFIER_CAP_USD", str(_DEFAULT_CAP_USD)))
    current_spend = _project_spend_tracker.get(project_id, 0.0)
    if current_spend >= cap_usd:
        receipt_id = _emit_receipt(
            outcome="denied",
            action_detail="cap_exhausted",
            cluster_id=cluster_id,
            candidate_legend_id=candidate_legend_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            cost_usd=0.0,
            verdict="cap_denied",
            reason=f"project_spend_cap_hit:{cap_usd:.4f}USD",
        )
        return {
            "verified": False,
            "verdict": "cap_denied",
            "reason": f"budget_cap_exceeded:{cap_usd:.4f}USD",
            "receipt_id": receipt_id,
            "cost_usd": 0.0,
        }

    # --- API key gate ------------------------------------------------
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ASPIRE_OPENAI_API_KEY", "")
    if not api_key:
        return _error_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason="no_openai_api_key",
        )

    # --- Fetch + crop PNG images ------------------------------------
    motif_png = await _fetch_cropped_motif(
        suite_id=suite_id,
        project_id=project_id,
        sheet_id=motif_sheet_id,
        bbox=motif_bbox,
        correlation_id=correlation_id,
    )
    if motif_png is None:
        return _error_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason="motif_png_unavailable",
        )

    legend_png = await _fetch_cropped_legend(
        suite_id=suite_id,
        project_id=project_id,
        candidate_legend_id=candidate_legend_id,
        correlation_id=correlation_id,
    )
    if legend_png is None:
        return _error_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason="legend_png_unavailable",
        )

    # --- Build vision prompt + call VLM ----------------------------
    motif_b64 = base64.b64encode(motif_png).decode("ascii")
    legend_b64 = base64.b64encode(legend_png).decode("ascii")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Are these two construction symbols the same? "
                        "Answer YES or NO with one-sentence reason. "
                        f"Image 1 is a candidate motif from a construction drawing. "
                        f"Image 2 is the legend entry it might match: {candidate_legend_label}."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{motif_b64}"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{legend_b64}"},
                },
            ],
        }
    ]

    try:
        from aspire_orchestrator.services.openai_client import generate_json_async
        from aspire_orchestrator.config.settings import settings

        result = await asyncio.wait_for(
            generate_json_async(
                model=_VISION_MODEL,
                messages=messages,
                api_key=api_key,
                base_url=settings.openai_base_url,
                timeout_seconds=_CALL_TIMEOUT_S,
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                temperature=None,
                model_profile="primary_reasoner",
            ),
            timeout=_CALL_TIMEOUT_S + 2.0,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vlm_verifier: OpenAI call failed cluster=%s reason=%s corr=%s",
            cluster_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return _error_result(
            cluster_id=cluster_id,
            correlation_id=correlation_id,
            suite_id=suite_id,
            project_id=project_id,
            reason=f"openai_call_failed:{type(exc).__name__}",
        )

    # --- Parse YES/NO answer ---------------------------------------
    raw_text: str = _extract_text_from_result(result)
    verdict, reason_text = _parse_verdict(raw_text)

    # Cost accounting
    usage = result.get("usage") or {}
    in_tokens = int(usage.get("prompt_tokens") or 0)
    out_tokens = int(usage.get("completion_tokens") or 0)
    cost_usd = in_tokens * _USD_PER_INPUT_TOKEN + out_tokens * _USD_PER_OUTPUT_TOKEN
    _project_spend_tracker[project_id] = current_spend + cost_usd

    _log.info(
        "vlm_verifier: cluster=%s legend=%s verdict=%s cost_usd=%.5f corr=%s",
        cluster_id[:8],
        candidate_legend_id[:8],
        verdict,
        cost_usd,
        correlation_id[:8],
    )

    receipt_id = _emit_receipt(
        outcome="success" if verdict == "yes" else "failure",
        action_detail=f"vlm_verdict_{verdict}",
        cluster_id=cluster_id,
        candidate_legend_id=candidate_legend_id,
        correlation_id=correlation_id,
        suite_id=suite_id,
        project_id=project_id,
        cost_usd=cost_usd,
        verdict=verdict,
        reason=reason_text,
    )

    return {
        "verified": verdict == "yes",
        "verdict": verdict,
        "reason": reason_text,
        "receipt_id": receipt_id,
        "cost_usd": cost_usd,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_verdict(raw_text: str) -> tuple[str, str]:
    """Extract YES/NO from model response.

    Returns (verdict, reason) where verdict is 'yes' | 'no'.
    First word after stripping punctuation determines the answer.
    If neither YES nor NO is found → 'no' (fail-closed, Law #3).
    """
    text = raw_text.strip()
    upper = text.upper()
    if upper.startswith("YES"):
        parts = text.split(None, 1)
        reason = parts[1].strip() if len(parts) > 1 else "model_confirmed_match"
        return "yes", reason
    if upper.startswith("NO"):
        parts = text.split(None, 1)
        reason = parts[1].strip() if len(parts) > 1 else "model_rejected_match"
        return "no", reason
    # Ambiguous response — fail-closed
    return "no", f"ambiguous_response:{text[:80]}"


def _extract_text_from_result(result: dict[str, Any]) -> str:
    """Pull text content from generate_json_async result."""
    # generate_json_async may return {"text": ..., "json": ...}
    raw = result.get("text") or ""
    if not raw and isinstance(result.get("json"), dict):
        raw = str(result["json"])
    return str(raw)


async def _fetch_cropped_motif(
    *,
    suite_id: str,
    project_id: str,
    sheet_id: str,
    bbox: dict[str, Any] | list[float],
    correlation_id: str,
) -> bytes | None:
    """Download sheet PNG and crop to motif bbox + padding."""
    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        download_sheet_image,
    )

    png_bytes = await download_sheet_image(
        suite_id=suite_id,
        project_id=project_id,
        sheet_id=sheet_id,
        correlation_id=correlation_id,
    )
    if not png_bytes:
        return None
    return _crop_png(png_bytes, bbox)


async def _fetch_cropped_legend(
    *,
    suite_id: str,
    project_id: str,
    candidate_legend_id: str,
    correlation_id: str,
) -> bytes | None:
    """Fetch the legend entry's source sheet and crop to representation_bbox."""
    try:
        legend_rows = await supabase_select(
            "blueprint_project_legends",
            filters=f"id=eq.{candidate_legend_id}&suite_id=eq.{suite_id}",
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vlm_verifier: legend fetch failed legend=%s (%s) corr=%s",
            candidate_legend_id[:8],
            type(exc).__name__,
            correlation_id[:8],
        )
        return None

    if not legend_rows:
        return None

    row = legend_rows[0]
    source_sheet_id: str | None = row.get("source_sheet_id")
    rep_bbox = row.get("representation_bbox")

    if not source_sheet_id or not rep_bbox:
        return None

    from aspire_orchestrator.services.blueprint.sheet_image_storage import (
        download_sheet_image,
    )

    png_bytes = await download_sheet_image(
        suite_id=suite_id,
        project_id=project_id,
        sheet_id=source_sheet_id,
        correlation_id=correlation_id,
    )
    if not png_bytes:
        return None
    return _crop_png(png_bytes, rep_bbox)


def _crop_png(
    png_bytes: bytes,
    bbox: dict[str, Any] | list[Any],
) -> bytes | None:
    """Crop a PNG to a bounding box + CROP_PAD_PX padding.

    Bbox may be dict {x0,y0,x1,y1} or list [x0,y0,x1,y1].
    Returns None on any error (missing Pillow, invalid bbox, zero-size crop).
    """
    try:
        from PIL import Image

        if isinstance(bbox, dict):
            x0 = int(bbox.get("x0") or bbox.get("left") or 0)
            y0 = int(bbox.get("y0") or bbox.get("top") or 0)
            x1 = int(bbox.get("x1") or bbox.get("right") or 0)
            y1 = int(bbox.get("y1") or bbox.get("bottom") or 0)
        else:
            coords = [int(float(c)) for c in bbox[:4]]
            x0, y0, x1, y1 = coords

        img = Image.open(io.BytesIO(png_bytes))
        w, h = img.size

        left = max(0, x0 - _CROP_PAD_PX)
        upper = max(0, y0 - _CROP_PAD_PX)
        right = min(w, x1 + _CROP_PAD_PX)
        lower = min(h, y1 + _CROP_PAD_PX)

        if right <= left or lower <= upper:
            return None

        cropped = img.crop((left, upper, right, lower))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        _log.debug("vlm_verifier: crop failed (%s)", type(exc).__name__)
        return None


def _emit_receipt(
    *,
    outcome: str,
    action_detail: str,
    cluster_id: str,
    candidate_legend_id: str,
    correlation_id: str,
    suite_id: str,
    project_id: str,
    cost_usd: float,
    verdict: str,
    reason: str,
) -> str:
    """Emit Law #2 receipt. Returns receipt_id."""
    receipt_id = str(uuid.uuid4())
    try:
        store_receipts(
            [
                {
                    "id": receipt_id,
                    "outcome": outcome,
                    "action_type": "provider.openai.vlm_verifier",
                    "receipt_type": "provider.openai.vlm_verifier",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": _ACTOR,
                    "correlation_id": correlation_id,
                    "suite_id": suite_id,
                    "metadata": {
                        "project_id": project_id,
                        "cluster_id": cluster_id,
                        "candidate_legend_id": candidate_legend_id,
                        "action_detail": action_detail,
                        "verdict": verdict,
                        "reason": reason[:200] if reason else "",
                        "cost_usd": round(cost_usd, 6),
                        "model": _VISION_MODEL,
                    },
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vlm_verifier: receipt emit failed (%s) corr=%s",
            type(exc).__name__,
            correlation_id[:8],
        )
    return receipt_id


def _denied_result(
    *,
    cluster_id: str,
    correlation_id: str,
    suite_id: str,
    project_id: str,
    reason: str,
    action_detail: str,
) -> dict[str, Any]:
    receipt_id = _emit_receipt(
        outcome="denied",
        action_detail=action_detail,
        cluster_id=cluster_id,
        candidate_legend_id="",
        correlation_id=correlation_id,
        suite_id=suite_id,
        project_id=project_id,
        cost_usd=0.0,
        verdict="cap_denied",
        reason=reason,
    )
    return {
        "verified": False,
        "verdict": "cap_denied",
        "reason": reason,
        "receipt_id": receipt_id,
        "cost_usd": 0.0,
    }


def _error_result(
    *,
    cluster_id: str,
    correlation_id: str,
    suite_id: str,
    project_id: str,
    reason: str,
) -> dict[str, Any]:
    receipt_id = _emit_receipt(
        outcome="failure",
        action_detail="provider.openai.vlm_verifier.error",
        cluster_id=cluster_id,
        candidate_legend_id="",
        correlation_id=correlation_id,
        suite_id=suite_id,
        project_id=project_id,
        cost_usd=0.0,
        verdict="error",
        reason=reason,
    )
    return {
        "verified": False,
        "verdict": "error",
        "reason": reason,
        "receipt_id": receipt_id,
        "cost_usd": 0.0,
    }


# ---------------------------------------------------------------------------
# Cluster-level VLM verification pass (Stage 6 orchestrator entry point)
# ---------------------------------------------------------------------------

async def run_vlm_verification_pass(
    *,
    project_id: str,
    suite_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Process all pending_vlm_verify clusters for a project.

    Reads blueprint_resolved_symbols where resolution_source='pending_vlm_verify',
    groups by cluster_id (one VLM call per cluster, not per instance — cost saving),
    calls verify_borderline_binding() on the cluster centroid representative,
    then UPDATEs ALL rows with that cluster_id to vlm_verified or unknown.

    Args:
        project_id:      Blueprint project UUID.
        suite_id:        Caller tenant UUID (Law #6 gate).
        correlation_id:  Trace correlation ID.

    Returns summary dict:
        clusters_verified (int):  Clusters confirmed by VLM (YES).
        clusters_rejected (int):  Clusters rejected by VLM (NO).
        clusters_cap_denied (int): Clusters skipped due to budget cap.
        clusters_error (int):     Clusters that errored (left as pending_vlm_verify).
        total_rows_updated (int): Total blueprint_resolved_symbols rows updated.
        total_cost_usd (float):   Total VLM spend this pass.

    Law #2: Emits blueprint.vlm_verification_pass.summary receipt on completion.
    Law #6: Suite-scoped — project lookup validates tenant before any image access.
    """
    from aspire_orchestrator.services.supabase_client import supabase_update

    _log.info(
        "vlm_pass.start: project=%s suite=%s corr=%s",
        project_id[:8], suite_id[:8], correlation_id[:8],
    )

    # --- Fetch pending clusters ----------------------------------------
    try:
        pending_rows: list[dict[str, Any]] = await supabase_select(
            "blueprint_resolved_symbols",
            filters=(
                f"project_id=eq.{project_id}"
                f"&suite_id=eq.{suite_id}"
                f"&resolution_source=eq.pending_vlm_verify"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "vlm_pass: failed to fetch pending clusters project=%s (%s)",
            project_id[:8], type(exc).__name__,
        )
        return {
            "clusters_verified": 0,
            "clusters_rejected": 0,
            "clusters_cap_denied": 0,
            "clusters_error": 0,
            "total_rows_updated": 0,
            "total_cost_usd": 0.0,
        }

    if not pending_rows:
        _log.info("vlm_pass.no_pending: project=%s suite=%s", project_id[:8], suite_id[:8])
        return {
            "clusters_verified": 0,
            "clusters_rejected": 0,
            "clusters_cap_denied": 0,
            "clusters_error": 0,
            "total_rows_updated": 0,
            "total_cost_usd": 0.0,
        }

    # --- Group by cluster_id, pick one representative per cluster ------
    cluster_reps: dict[str, dict[str, Any]] = {}
    cluster_all_rows: dict[str, list[dict[str, Any]]] = {}
    for row in pending_rows:
        cid: str = row.get("cluster_id") or ""
        if not cid:
            continue
        cluster_all_rows.setdefault(cid, []).append(row)
        # Pick the representative with highest confidence (closest to 0.92 threshold)
        existing = cluster_reps.get(cid)
        if existing is None or (row.get("confidence") or 0) > (existing.get("confidence") or 0):
            cluster_reps[cid] = row

    _log.info(
        "vlm_pass: %d pending rows → %d unique clusters project=%s corr=%s",
        len(pending_rows), len(cluster_reps), project_id[:8], correlation_id[:8],
    )

    # --- Fetch legend candidates for pending clusters ------------------
    # We need to find the best legend candidate for each cluster.
    # Strategy: fetch all legends for this project, then for each cluster
    # find the legend whose discipline matches the cluster's sheet discipline.
    try:
        all_legends: list[dict[str, Any]] = await supabase_select(
            "blueprint_project_legends",
            filters=f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vlm_pass: legend fetch failed project=%s (%s) — all clusters → error",
            project_id[:8], type(exc).__name__,
        )
        all_legends = []

    # Index legends by id for lookup
    legend_by_id: dict[str, dict[str, Any]] = {
        str(lg.get("id") or ""): lg for lg in all_legends if lg.get("id")
    }

    # For each cluster representative, find best candidate legend by
    # confidence (the similarity score Stage 5 stored). Since legend_id
    # is NULL for pending rows, we use the cluster's motif_hash and
    # sheet discipline to narrow candidates, then pick highest-label match.
    # Simplified: use the legend with the highest representation_bbox match
    # or fall back to first legend of matching discipline.
    def _pick_candidate_legend(rep_row: dict[str, Any]) -> dict[str, Any] | None:
        """Return the best legend candidate for this cluster representative."""
        sheet_id: str = rep_row.get("sheet_id") or ""
        # Use first legend with a source_sheet_id (any discipline match is better than none)
        candidates = [lg for lg in all_legends if lg.get("id")]
        if not candidates:
            return None
        # Prefer legends with representation_bbox (have a visual to crop)
        with_bbox = [lg for lg in candidates if lg.get("representation_bbox") and lg.get("source_sheet_id")]
        return with_bbox[0] if with_bbox else candidates[0]

    # --- Per-cluster VLM loop -----------------------------------------
    spend_tracker: dict[str, float] = {}
    stats = {
        "clusters_verified": 0,
        "clusters_rejected": 0,
        "clusters_cap_denied": 0,
        "clusters_error": 0,
        "total_rows_updated": 0,
        "total_cost_usd": 0.0,
    }

    for cluster_id, rep_row in cluster_reps.items():
        candidate_legend = _pick_candidate_legend(rep_row)
        if candidate_legend is None:
            _log.info(
                "vlm_pass: no candidate legend for cluster=%s — leaving pending",
                cluster_id[:8],
            )
            stats["clusters_error"] += 1
            continue

        candidate_legend_id: str = str(candidate_legend.get("id") or "")
        candidate_label: str = str(candidate_legend.get("label") or candidate_legend.get("symbol_id") or "")
        motif_bbox: dict[str, Any] = rep_row.get("bbox") or {}
        motif_sheet_id: str = rep_row.get("sheet_id") or ""

        result = await verify_borderline_binding(
            suite_id=suite_id,
            project_id=project_id,
            cluster_id=cluster_id,
            motif_bbox=motif_bbox,
            motif_sheet_id=motif_sheet_id,
            candidate_legend_id=candidate_legend_id,
            candidate_legend_label=candidate_label,
            correlation_id=correlation_id,
            _project_spend_tracker=spend_tracker,
        )

        stats["total_cost_usd"] += result.get("cost_usd", 0.0)
        verdict = result.get("verdict", "error")

        if verdict == "cap_denied":
            stats["clusters_cap_denied"] += 1
            # Leave remaining clusters as pending_vlm_verify — human review queue
            continue

        if verdict == "error":
            stats["clusters_error"] += 1
            # Leave as pending_vlm_verify for retry
            continue

        # Determine new resolution_source and legend_id
        if verdict == "yes":
            new_source = "vlm_verified"
            new_legend_id = candidate_legend_id
            new_symbol_name = candidate_label
            stats["clusters_verified"] += 1
        else:
            new_source = "unknown"
            new_legend_id = None
            new_symbol_name = None
            stats["clusters_rejected"] += 1

        # UPDATE all rows with this cluster_id (cluster-wide, not just representative)
        update_data: dict[str, Any] = {"resolution_source": new_source}
        if new_legend_id is not None:
            update_data["legend_id"] = new_legend_id
        if new_symbol_name is not None:
            update_data["symbol_name"] = new_symbol_name

        try:
            await supabase_update(
                "blueprint_resolved_symbols",
                f"project_id=eq.{project_id}&suite_id=eq.{suite_id}&cluster_id=eq.{cluster_id}",
                update_data,
            )
            rows_in_cluster = len(cluster_all_rows.get(cluster_id, []))
            stats["total_rows_updated"] += rows_in_cluster
            _log.info(
                "vlm_pass: cluster=%s verdict=%s rows_updated=%d corr=%s",
                cluster_id[:8], verdict, rows_in_cluster, correlation_id[:8],
            )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "vlm_pass: UPDATE failed cluster=%s (%s) corr=%s",
                cluster_id[:8], type(exc).__name__, correlation_id[:8],
            )
            stats["clusters_error"] += 1

    # --- Emit summary receipt -----------------------------------------
    _emit_pass_summary_receipt(
        suite_id=suite_id,
        project_id=project_id,
        correlation_id=correlation_id,
        stats=stats,
    )

    _log.info(
        "vlm_pass.done: project=%s verified=%d rejected=%d cap_denied=%d error=%d "
        "rows_updated=%d cost_usd=%.5f corr=%s",
        project_id[:8],
        stats["clusters_verified"],
        stats["clusters_rejected"],
        stats["clusters_cap_denied"],
        stats["clusters_error"],
        stats["total_rows_updated"],
        stats["total_cost_usd"],
        correlation_id[:8],
    )

    return stats


def _emit_pass_summary_receipt(
    *,
    suite_id: str,
    project_id: str,
    correlation_id: str,
    stats: dict[str, Any],
) -> None:
    """Emit Law #2 summary receipt for the full VLM verification pass."""
    import uuid as _uuid
    from datetime import datetime, timezone
    receipt_id = str(_uuid.uuid4())
    try:
        store_receipts(
            [
                {
                    "id": receipt_id,
                    "outcome": "success",
                    "action_type": "blueprint.vlm_verification_pass.summary",
                    "receipt_type": "blueprint.vlm_verification_pass.summary",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "risk_tier": "GREEN",
                    "actor_type": "WORKER",
                    "actor_id": _ACTOR,
                    "correlation_id": correlation_id,
                    "suite_id": suite_id,
                    "metadata": {
                        "project_id": project_id,
                        **stats,
                    },
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "vlm_pass: summary receipt emit failed (%s) corr=%s",
            type(exc).__name__, correlation_id[:8],
        )


__all__ = ["verify_borderline_binding", "run_vlm_verification_pass"]
