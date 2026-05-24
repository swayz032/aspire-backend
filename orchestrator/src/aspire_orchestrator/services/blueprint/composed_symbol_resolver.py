"""Wave 20.13.1 — Terminal state resolution at COMPOSED symbol level.

Wave 20.8 TR-Layer routed PRIMITIVES (Wave 11g-v2 motifs). Visual debug
proved primitives = fragments, not symbols → VLM correctly rejected all.

This module routes COMPOSED symbols (Wave 20.13 PCE output) against
legend dictionary. Each composed symbol = ~5-8 primitives assembled into
a glyph-level candidate. THAT'S the matching surface.

Routing per composed symbol:
  1. BOUND_MATERIAL: composed_signature exact-matches legend signature
     OR Jaccard >=0.5 token overlap with legend label
  2. PLAN_ELEMENT: bbox in revision/dimension regions
  3. TEXT_OCR: bbox overlaps paragraph >=30%
  4. CRUFT: bbox area too small after composition (shouldn't happen often)
  5. UNKNOWN_REVIEW: residual (next: VLM tiebreaker on these)

Instance counts: each composed symbol carries total_instance_count from
PCE (sum of constituent primitive instance_counts).
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from aspire_orchestrator.services.blueprint.symbol_family_embedder import (
    _build_topology_from_bbox,
    compute_embedding,
    cosine_similarity,
    deserialize_embedding,
)
from aspire_orchestrator.services.blueprint.terminal_resolver import (
    ALL_STATES,
    STATE_BOUND_MATERIAL,
    STATE_CRUFT,
    STATE_PLAN_ELEMENT,
    STATE_TEXT_OCR,
    STATE_UNKNOWN_REVIEW,
)
from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

_log = logging.getLogger(__name__)

_TEXT_OVERLAP_THRESHOLD = 0.30
_COMPOSED_MIN_AREA = 100.0
_TOKEN_OVERLAP_FLOOR = 0.5
_EMBEDDING_BIND_FLOOR = 0.85   # composed-symbol embedding ↔ legend embedding cosine for deterministic bind
_EMBEDDING_VLM_FLOOR = 0.60    # cosine ≥ this → flag for VLM tiebreaker

_DIMENSION_RE = re.compile(r"\d+['′’]?\s*[-–]?\s*\d+\s*[\"″”]?|\d+\s*(SF|sf|sqft)")
_ROOM_NUMBER_RE = re.compile(r"^[A-Z]{0,3}[-.]?\d{2,4}[A-Z]?$")


def _bbox_overlap_ratio(a: tuple[float, float, float, float], poly8: list[float]) -> float:
    if not poly8 or len(poly8) < 8:
        return 0.0
    ax, ay, aw, ah = a
    ax1, ay1 = ax + aw, ay + ah
    xs = [poly8[i] for i in (0, 2, 4, 6)]
    ys = [poly8[i] for i in (1, 3, 5, 7)]
    bx, by, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
    ix, iy = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix or iy1 <= iy:
        return 0.0
    inter = (ix1 - ix) * (iy1 - iy)
    return inter / max(aw * ah, 1e-6)


def _legend_token_match(name: str, labels_lower: dict[str, str]) -> str | None:
    if not name:
        return None
    needle = name.lower().strip()
    if needle in labels_lower:
        return labels_lower[needle]
    needle_tokens = set(re.findall(r"[a-z0-9]+", needle))
    if not needle_tokens:
        return None
    best, best_id = 0.0, None
    for label, lid in labels_lower.items():
        label_tokens = set(re.findall(r"[a-z0-9]+", label))
        if not label_tokens:
            continue
        overlap = len(needle_tokens & label_tokens)
        if overlap == 0:
            continue
        s = overlap / max(len(needle_tokens), len(label_tokens))
        if s > best:
            best, best_id = s, lid
    return best_id if best >= _TOKEN_OVERLAP_FLOOR else None


async def resolve_composed_symbols(
    *,
    project_id: str,
    suite_id: str,
) -> dict[str, Any]:
    """Route composed symbols (Wave 20.13 PCE output) into terminal states.
    Writes blueprint_resolved_symbols rows with resolution_source='composed_terminal_resolver'.
    """
    if not suite_id:
        raise ValueError("resolve_composed_symbols requires non-empty suite_id (Law #6)")

    # 1. Sheets + paragraphs
    sheet_rows = await supabase_select(
        "blueprint_sheets",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    if not sheet_rows:
        return {s: 0 for s in ALL_STATES} | {"total": 0, "coverage_pct": 0.0}
    sheet_ids = {s["id"] for s in sheet_rows}

    intel = await supabase_select(
        "blueprint_sheet_intelligence",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    paragraphs_per_sheet: dict[str, list[list[float]]] = {}
    for row in intel or []:
        sid = row.get("sheet_id")
        polys = []
        for p in row.get("paragraphs") or []:
            poly = p.get("bbox") if isinstance(p, dict) else None
            if isinstance(poly, list) and len(poly) >= 8:
                polys.append(poly[:8])
        if sid:
            paragraphs_per_sheet[sid] = polys

    # 2. Legend dictionary + embeddings
    legend_rows = await supabase_select(
        "blueprint_project_legends",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    labels_lower: dict[str, str] = {}
    legend_signatures: dict[str, str] = {}
    legend_embeddings: list[tuple[str, str, Any]] = []  # (id, label, np.ndarray)
    for lr in legend_rows or []:
        lid = lr.get("id")
        if not lid:
            continue
        label = (lr.get("label") or "").lower().strip()
        if label:
            labels_lower[label] = lid
        sig = lr.get("vector_signature")
        if sig:
            legend_signatures[sig] = lid
            try:
                emb = deserialize_embedding(sig)
                legend_embeddings.append((lid, label, emb))
            except Exception:  # noqa: BLE001
                pass

    # 3. Stream composed symbols paginated
    composed_rows: list[dict[str, Any]] = []
    last_id = ""
    while True:
        cursor = f"&id=gt.{last_id}" if last_id else ""
        page = await supabase_select(
            "blueprint_composed_symbols",
            f"project_id=eq.{project_id}&suite_id=eq.{suite_id}{cursor}&order=id",
            limit=500,
        )
        if not page:
            break
        composed_rows.extend(page)
        last_id = page[-1]["id"]
        if len(page) < 500:
            break

    # 4. Route each composed symbol
    state_counts = {s: 0 for s in ALL_STATES}
    instance_counts = {s: 0 for s in ALL_STATES}
    resolved_inserts: list[dict[str, Any]] = []

    for cs in composed_rows:
        bbox_dict = cs.get("bbox") or {}
        try:
            xywh = (
                float(bbox_dict.get("x", 0)),
                float(bbox_dict.get("y", 0)),
                float(bbox_dict.get("w", 0)),
                float(bbox_dict.get("h", 0)),
            )
        except (TypeError, ValueError):
            xywh = (0.0, 0.0, 0.0, 0.0)
        area = xywh[2] * xywh[3]
        try:
            inst = int(cs.get("total_instance_count") or 1)
        except (TypeError, ValueError):
            inst = 1
        inst = max(inst, 1)
        sheet_id = cs.get("sheet_id")
        composed_sig = cs.get("composed_signature")

        state = STATE_UNKNOWN_REVIEW
        legend_id = None
        binding_cosine = 0.0

        # CRUFT first only if obviously tiny
        if area < _COMPOSED_MIN_AREA:
            state = STATE_CRUFT
        else:
            # 1. Exact composed_signature match (rare but precise)
            if composed_sig and composed_sig in legend_signatures:
                state = STATE_BOUND_MATERIAL
                legend_id = legend_signatures[composed_sig]
                binding_cosine = 1.0

            # 2. Embedding cosine match — Wave 20.13.2 (composed bbox ↔ legend embedding)
            if state == STATE_UNKNOWN_REVIEW and legend_embeddings:
                topo = _build_topology_from_bbox(bbox_dict)
                if topo is not None:
                    try:
                        cs_emb = compute_embedding(topo)
                        best_sim, best_id = -1.0, None
                        for lid_c, _label, lemb in legend_embeddings:
                            s = cosine_similarity(cs_emb, lemb)
                            if s > best_sim:
                                best_sim, best_id = s, lid_c
                        if best_sim >= _EMBEDDING_BIND_FLOOR and best_id is not None:
                            state = STATE_BOUND_MATERIAL
                            legend_id = best_id
                            binding_cosine = best_sim
                    except Exception:  # noqa: BLE001
                        pass

            # 3. TEXT_OCR overlap
            if state == STATE_UNKNOWN_REVIEW and sheet_id in paragraphs_per_sheet:
                for poly in paragraphs_per_sheet[sheet_id]:
                    if _bbox_overlap_ratio(xywh, poly) >= _TEXT_OVERLAP_THRESHOLD:
                        state = STATE_TEXT_OCR
                        break

        state_counts[state] += 1
        instance_counts[state] += inst
        resolved_inserts.append({
            "id": str(uuid.uuid4()),
            "suite_id": suite_id,
            "project_id": project_id,
            "sheet_id": sheet_id,
            "legend_id": legend_id,
            "bbox": bbox_dict,
            "symbol_name": None,
            "confidence": float(binding_cosine if binding_cosine > 0 else (cs.get("plausibility_score") or 0.0)),
            "resolution_source": "composed_terminal_resolver" if state != STATE_BOUND_MATERIAL else "composed_embedding_match",
            "cluster_id": f"composed_{cs['id'][:16]}",
            "motif_hash": composed_sig,
            "terminal_state": state,
        })

    # 5. Batch insert
    inserted = 0
    if resolved_inserts:
        batch = 500
        for i in range(0, len(resolved_inserts), batch):
            chunk = resolved_inserts[i : i + batch]
            try:
                await supabase_insert_batch_ignore_conflicts(
                    "blueprint_resolved_symbols", chunk, conflict_target="id",
                )
                inserted += len(chunk)
            except SupabaseClientError as exc:
                _log.warning("composed_resolver: batch insert fail (%s)", type(exc).__name__)

    total = sum(state_counts.values())
    total_inst = sum(instance_counts.values())
    out = {}
    for s in ALL_STATES:
        out[f"{s}_rows"] = state_counts[s]
        out[f"{s}_instances"] = instance_counts[s]
    out["composed_total"] = total
    out["instance_total"] = total_inst
    out["inserted"] = inserted
    out["coverage_pct"] = 100.0 if total > 0 else 0.0
    return out


__all__ = ["resolve_composed_symbols"]
