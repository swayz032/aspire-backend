"""Wave 20.8.2 — VLM tiebreaker for UNKNOWN_REVIEW families post TR-Layer.

After the deterministic TR-Layer routes families to terminal states, the
high-instance UNKNOWN_REVIEW families get one focused VLM verification pass
against their best-matching legend candidate.

Pipeline:
  1. Read all UNKNOWN_REVIEW rows from blueprint_resolved_symbols
  2. Group by motif_hash → families (one VLM call per family, not per row)
  3. For each family with instance_count >= threshold:
      a. Compute representative embedding from any member's bbox
      b. Find best-match legend via cosine similarity (top-1)
      c. Update all family rows to resolution_source='pending_vlm_verify',
         legend_id=candidate
  4. Run vlm_symbol_verifier.run_vlm_verification_pass()
  5. After: pending_vlm_verify rows become BOUND_MATERIAL (terminal_state)
     if VLM said YES, else stay UNKNOWN_REVIEW

Cost: ~$0.0003 per family × ~50 families = $0.015 (well under cap).
"""
from __future__ import annotations

import logging
import struct
from collections import defaultdict
from typing import Any

from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
    EMBEDDING_DIM,
    compute_embedding,
    cosine_similarity,
    deserialize_embedding,
)
from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
    _build_topology_from_bbox,
)
from aspire_orchestrator.services.blueprint.terminal_resolver import STATE_UNKNOWN_REVIEW
from aspire_orchestrator.services.blueprint.vlm_symbol_verifier import (
    run_vlm_verification_pass,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

_log = logging.getLogger(__name__)

_MIN_FAMILY_INSTANCES_FOR_VLM = 25  # only verify families with this many instances
_COSINE_MATCH_FLOOR = 0.55  # legend candidate must be at least this similar


async def run_tr_vlm_tiebreaker(
    *,
    project_id: str,
    suite_id: str,
    correlation_id: str = "tr-vlm-tiebreaker",
) -> dict[str, Any]:
    """Step 1+2: pre-match UNKNOWN families to legend candidates → mark for VLM.
    Step 3: run VLM verification pass.

    Returns summary dict with counts.
    """
    if not suite_id:
        raise ValueError("run_tr_vlm_tiebreaker requires non-empty suite_id (Law #6)")

    # 1. Load UNKNOWN_REVIEW resolved_symbols rows grouped by motif_hash (family)
    unknown_rows: list[dict[str, Any]] = []
    page_size = 500
    last_id = ""
    while True:
        cursor = f"&id=gt.{last_id}" if last_id else ""
        page = await supabase_select(
            "blueprint_resolved_symbols",
            filters=(
                f"project_id=eq.{project_id}&suite_id=eq.{suite_id}"
                f"&terminal_state=eq.{STATE_UNKNOWN_REVIEW}{cursor}&order=id"
            ),
            limit=page_size,
        )
        if not page:
            break
        unknown_rows.extend(page)
        last_id = page[-1]["id"]
        if len(page) < page_size:
            break

    if not unknown_rows:
        _log.info("tr_vlm: no UNKNOWN_REVIEW rows for project=%s", project_id[:8])
        return {
            "families_examined": 0,
            "families_marked_for_vlm": 0,
            "vlm_result": None,
        }

    # 2. Group by motif_hash + aggregate instance counts
    families: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"rows": [], "total_instances": 0, "rep_bbox": None, "rep_sheet_id": None}
    )
    for r in unknown_rows:
        mh = r.get("motif_hash") or "_nohash"
        bbox = r.get("bbox") or {}
        try:
            inst = int(bbox.get("instance_count", 1)) if isinstance(bbox, dict) else 1
        except (TypeError, ValueError):
            inst = 1
        inst = max(inst, 1)
        f = families[mh]
        f["rows"].append(r)
        f["total_instances"] += inst
        if f["rep_bbox"] is None:
            f["rep_bbox"] = bbox
            f["rep_sheet_id"] = r.get("sheet_id")

    # 3. Load legend dictionary with vector_signature
    legend_rows = await supabase_select(
        "blueprint_project_legends",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    legend_embeddings: list[tuple[str, str, Any]] = []  # (id, label, embedding_array)
    for lr in legend_rows or []:
        lid = lr.get("id")
        sig = lr.get("vector_signature")
        if not lid or not sig:
            continue
        try:
            emb = deserialize_embedding(sig)
            legend_embeddings.append((lid, lr.get("label") or "", emb))
        except Exception:  # noqa: BLE001
            continue

    if not legend_embeddings:
        _log.warning("tr_vlm: no legend embeddings — cannot tiebreak project=%s", project_id[:8])
        return {
            "families_examined": len(families),
            "families_marked_for_vlm": 0,
            "vlm_result": None,
        }

    # 4. For each high-instance family, compute embedding + find best legend
    families_marked = 0
    families_examined = len(families)
    candidate_rows_to_update: list[tuple[str, str]] = []  # (row_id, candidate_legend_id)
    cluster_assignments: dict[str, str] = {}  # cluster_id → legend_id

    for mh, fam in families.items():
        if fam["total_instances"] < _MIN_FAMILY_INSTANCES_FOR_VLM:
            continue
        topo = _build_topology_from_bbox(fam["rep_bbox"])
        if topo is None:
            continue
        try:
            motif_emb = compute_embedding(topo)
        except Exception:  # noqa: BLE001
            continue

        # Top-1 legend match
        best_score = -1.0
        best_lid = None
        best_label = ""
        for lid, label, lemb in legend_embeddings:
            score = cosine_similarity(motif_emb, lemb)
            if score > best_score:
                best_score = score
                best_lid = lid
                best_label = label

        if best_lid is None or best_score < _COSINE_MATCH_FLOOR:
            continue

        cluster_id = f"sig_{mh[:16]}"
        cluster_assignments[cluster_id] = best_lid
        for row in fam["rows"]:
            candidate_rows_to_update.append((row["id"], best_lid))
        families_marked += 1
        _log.info(
            "tr_vlm: marked family=%s instances=%d legend=%s (cosine=%.3f) %s",
            mh[:8], fam["total_instances"], best_lid[:8] if best_lid else "?",
            best_score, best_label[:40],
        )

    # 5. Update rows in Supabase: resolution_source='pending_vlm_verify' + legend_id
    update_errors = 0
    for row_id, lid in candidate_rows_to_update:
        try:
            await supabase_update(
                "blueprint_resolved_symbols",
                f"id=eq.{row_id}&suite_id=eq.{suite_id}",
                {"resolution_source": "pending_vlm_verify", "legend_id": lid},
            )
        except SupabaseClientError:
            update_errors += 1

    _log.info(
        "tr_vlm: examined=%d marked=%d rows_updated=%d errors=%d",
        families_examined, families_marked, len(candidate_rows_to_update) - update_errors,
        update_errors,
    )

    # 6. Run VLM verification pass
    vlm_result = None
    if families_marked > 0:
        try:
            vlm_result = await run_vlm_verification_pass(
                project_id=project_id, suite_id=suite_id, correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("tr_vlm: VLM pass failed (%s)", type(exc).__name__)
            vlm_result = {"error": type(exc).__name__}

    return {
        "families_examined": families_examined,
        "families_marked_for_vlm": families_marked,
        "rows_updated": len(candidate_rows_to_update) - update_errors,
        "vlm_result": vlm_result,
    }


__all__ = ["run_tr_vlm_tiebreaker"]
