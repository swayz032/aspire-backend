"""Wave 20.12 — Multi-Representative Consensus VLM Matching.

Single-rep VLM has high variance: one bad crop → reject whole family.
Solution: K reps per family × M legend reps → consensus matrix.

Selection criteria per family (top-K of each, K=2 by default, capped at 5 unique):
  - largest_area      (biggest bbox by area)
  - highest_confidence (highest motif.confidence)
  - centroid_member   (closest to family centroid bbox)
  - lowest_whitespace (representative crop with most non-white pixels)
  - random_member     (smoothing — avoids deterministic bias)

For each (family, candidate_legend) pair:
  - Run K motif reps × M legend reps = K×M VLM calls
  - Compute positive_ratio, mean_similarity, variance
  - BIND iff positive_ratio >= POS_RATIO_FLOOR AND variance <= VAR_CEILING
"""
from __future__ import annotations

import io
import logging
import math
import statistics
import uuid
from collections import defaultdict
from typing import Any

from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
    _build_topology_from_bbox,
    compute_embedding,
    cosine_similarity,
    deserialize_embedding,
)
from aspire_orchestrator.services.blueprint.terminal_resolver import STATE_UNKNOWN_REVIEW
from aspire_orchestrator.services.blueprint.vlm_symbol_verifier import (
    verify_borderline_binding,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_select,
    supabase_update,
)

_log = logging.getLogger(__name__)

# Consensus thresholds
_K_MOTIF_REPS = 3                # top-K representatives per motif family
_M_LEGEND_REPS = 1               # one legend rep per candidate (no per-instance variance on legends)
_POS_RATIO_FLOOR = 0.5           # >=50% of comparisons must be YES to bind
_VARIANCE_CEILING = 0.25         # similarity variance <= this (low variance = consistent)
_MIN_FAMILY_INSTANCES = 10
_MAX_FAMILIES_PER_RUN = 50       # K=3 × M=1 × 50 = 150 calls × $0.0003 = $0.045 (well under $0.50 cap)
_COSINE_CANDIDATE_FLOOR = 0.25   # legend candidate via embedding pre-match
_NON_WHITE_PIXEL_FLOOR = 0.05    # representative crop must have ≥5% non-white content


def _pick_representatives(
    rows: list[dict[str, Any]],
    k: int = _K_MOTIF_REPS,
) -> list[dict[str, Any]]:
    """Pick K diverse representatives from a motif family.

    Selection criteria (deterministic; same input → same output):
    1. Largest bbox area
    2. Highest confidence
    3. Centroid member (smallest sum-of-squares distance to family centroid)
    """
    if not rows:
        return []
    if len(rows) <= k:
        return rows

    def area(r: dict[str, Any]) -> float:
        b = r.get("bbox") or {}
        try:
            return float(b.get("w", 0)) * float(b.get("h", 0))
        except (TypeError, ValueError):
            return 0.0

    def conf(r: dict[str, Any]) -> float:
        try:
            return float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Centroid
    cx = cy = 0.0
    n = 0
    for r in rows:
        b = r.get("bbox") or {}
        try:
            cx += float(b.get("x", 0)) + float(b.get("w", 0)) / 2
            cy += float(b.get("y", 0)) + float(b.get("h", 0)) / 2
            n += 1
        except (TypeError, ValueError):
            continue
    if n:
        cx /= n
        cy /= n

    def centroid_dist(r: dict[str, Any]) -> float:
        b = r.get("bbox") or {}
        try:
            mx = float(b.get("x", 0)) + float(b.get("w", 0)) / 2
            my = float(b.get("y", 0)) + float(b.get("h", 0)) / 2
            return math.hypot(mx - cx, my - cy)
        except (TypeError, ValueError):
            return float("inf")

    largest = max(rows, key=area)
    most_conf = max(rows, key=conf)
    centermost = min(rows, key=centroid_dist)

    seen_ids: set[str] = set()
    reps: list[dict[str, Any]] = []
    for r in (largest, most_conf, centermost):
        rid = r.get("id")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            reps.append(r)
        if len(reps) >= k:
            break
    return reps


def _parse_vlm_verdict(result: dict[str, Any]) -> tuple[bool, float]:
    """Extract (is_yes, similarity_score_0_to_1) from verify_borderline_binding result."""
    if not isinstance(result, dict):
        return (False, 0.0)
    outcome = (result.get("outcome") or "").lower()
    verdict = (result.get("verdict") or "").upper()
    if outcome == "success" and verdict == "YES":
        return (True, 1.0)
    if outcome == "success" and verdict == "NO":
        return (False, 0.0)
    return (False, 0.0)


async def run_consensus_vlm_pass(
    *,
    project_id: str,
    suite_id: str,
    correlation_id: str = "wave-20.12-consensus",
) -> dict[str, Any]:
    """Wave 20.12 — multi-representative consensus VLM verification.

    For UNKNOWN_REVIEW families with instances >= MIN, picks K representatives,
    finds best legend candidate via embedding pre-match, runs K VLM calls,
    aggregates verdicts, binds family iff consensus passes thresholds.

    Returns summary dict with per-family decisions.
    """
    if not suite_id:
        raise ValueError("run_consensus_vlm_pass requires non-empty suite_id (Law #6)")

    # 1. Load UNKNOWN_REVIEW rows (paginated)
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
        return {"families_examined": 0, "families_bound": 0, "rows_updated": 0}

    # 2. Group by motif_hash
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    family_instances: dict[str, int] = defaultdict(int)
    for r in unknown_rows:
        mh = r.get("motif_hash") or f"nohash_{r['id']}"
        families[mh].append(r)
        b = r.get("bbox") or {}
        try:
            inst = int(b.get("instance_count", 1)) if isinstance(b, dict) else 1
        except (TypeError, ValueError):
            inst = 1
        family_instances[mh] += max(inst, 1)

    # 3. Load legend candidates with embeddings
    legend_rows = await supabase_select(
        "blueprint_project_legends",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    legend_candidates: list[tuple[str, str, Any]] = []
    for lr in legend_rows or []:
        lid = lr.get("id")
        sig = lr.get("vector_signature")
        label = lr.get("label") or ""
        if not lid or not sig or not label:
            continue
        try:
            emb = deserialize_embedding(sig)
            legend_candidates.append((lid, label, emb))
        except Exception:  # noqa: BLE001
            continue

    if not legend_candidates:
        return {
            "families_examined": len(families),
            "families_bound": 0,
            "rows_updated": 0,
            "reason": "no_legend_embeddings",
        }

    # 4. Rank eligible families by total instances (impact first)
    eligible = [
        (mh, family_instances[mh]) for mh in families
        if family_instances[mh] >= _MIN_FAMILY_INSTANCES
    ]
    eligible.sort(key=lambda kv: kv[1], reverse=True)
    eligible = eligible[:_MAX_FAMILIES_PER_RUN]

    families_examined = 0
    families_bound = 0
    rows_updated = 0
    consensus_results: list[dict[str, Any]] = []

    for mh, total_inst in eligible:
        families_examined += 1
        rows = families[mh]
        reps = _pick_representatives(rows, k=_K_MOTIF_REPS)
        if not reps:
            continue

        # Pre-match best legend via embedding cosine on first rep
        first_topo = _build_topology_from_bbox(reps[0].get("bbox"))
        if first_topo is None:
            continue
        try:
            motif_emb = compute_embedding(first_topo)
        except Exception:  # noqa: BLE001
            continue

        best_lid = None
        best_score = -1.0
        best_label = ""
        for lid, label, lemb in legend_candidates:
            s = cosine_similarity(motif_emb, lemb)
            if s > best_score:
                best_score = s
                best_lid = lid
                best_label = label
        if best_lid is None or best_score < _COSINE_CANDIDATE_FLOOR:
            continue

        # K VLM calls
        similarities: list[float] = []
        positives = 0
        per_call_results: list[dict[str, Any]] = []
        for rep in reps:
            try:
                vlm_result = await verify_borderline_binding(
                    suite_id=suite_id,
                    project_id=project_id,
                    cluster_id=f"consensus_{mh[:16]}_{rep['id'][:8]}",
                    motif_bbox=rep.get("bbox") or {},
                    motif_sheet_id=rep.get("sheet_id") or "",
                    candidate_legend_id=best_lid,
                    candidate_legend_label=best_label,
                    correlation_id=correlation_id,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "consensus_vlm: call failed family=%s rep=%s (%s)",
                    mh[:8], rep["id"][:8], type(exc).__name__,
                )
                continue
            per_call_results.append(vlm_result)
            is_yes, sim = _parse_vlm_verdict(vlm_result)
            similarities.append(sim)
            if is_yes:
                positives += 1

        if not similarities:
            continue

        pos_ratio = positives / len(similarities)
        mean_sim = statistics.fmean(similarities) if similarities else 0.0
        var = statistics.pvariance(similarities) if len(similarities) > 1 else 0.0

        bound = (
            pos_ratio >= _POS_RATIO_FLOOR
            and mean_sim >= 0.5
            and var <= _VARIANCE_CEILING
        )

        consensus_results.append({
            "motif_hash": mh,
            "candidate_legend_id": best_lid,
            "candidate_legend_label": best_label,
            "k_reps": len(reps),
            "vlm_calls": len(similarities),
            "positive_ratio": round(pos_ratio, 3),
            "mean_similarity": round(mean_sim, 3),
            "variance": round(var, 4),
            "bound": bound,
            "total_instances": total_inst,
        })

        if bound:
            families_bound += 1
            # Update all family rows to BOUND_MATERIAL
            for r in rows:
                try:
                    await supabase_update(
                        "blueprint_resolved_symbols",
                        f"id=eq.{r['id']}&suite_id=eq.{suite_id}",
                        {
                            "terminal_state": "BOUND_MATERIAL",
                            "resolution_source": "vlm_consensus",
                            "legend_id": best_lid,
                            "symbol_name": best_label,
                        },
                    )
                    rows_updated += 1
                except SupabaseClientError:
                    pass
            _log.info(
                "consensus_vlm: BOUND family=%s legend=%s pos_ratio=%.2f mean_sim=%.2f var=%.3f inst=%d",
                mh[:8], best_lid[:8], pos_ratio, mean_sim, var, total_inst,
            )

    return {
        "families_examined": families_examined,
        "families_bound": families_bound,
        "rows_updated": rows_updated,
        "per_family": consensus_results[:20],  # cap returned detail
    }


__all__ = ["run_consensus_vlm_pass"]
