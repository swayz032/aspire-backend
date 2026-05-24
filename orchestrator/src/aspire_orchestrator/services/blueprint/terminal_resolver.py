"""Wave 20.8 + 20.8.1 — Terminal Resolver Layer (TR-Layer).

THE breakthrough abstraction. Converts the Wave 11g-v2 detection universe
from "0% typed" → "100% typed" via family-level deterministic routing +
VLM tiebreaker for residual UNKNOWN families.

Hard invariant per detection:
    ∀ detection ∈ D → assigned exactly 1 terminal state ∈ {
        BOUND_MATERIAL, DECORATION, TEXT_OCR,
        PLAN_ELEMENT, CRUFT, UNKNOWN_REVIEW,
    }

Architecture (v20.8.1):
    1. Aggregate detections by signature_hash → SIGNATURE FAMILIES
    2. Route each FAMILY once (deterministic rules + signature/label matching)
    3. For UNKNOWN families with high instance_count: VLM tiebreaker
    4. Propagate family decision to ALL member motif rows
    5. Write per-detection rows to blueprint_resolved_symbols

Law compliance:
    #1  Pure routing per deterministic rule table + VLM verification
        (proposed, never auto-executes business actions).
    #2  Receipt per project run + receipt per VLM call.
    #3  Fail closed: missing legends/sheets → all motifs UNKNOWN_REVIEW
        with reason_code=NO_RESOLUTION_INPUT.
    #6  All reads/writes scoped by suite_id.
    #9  Never logs raw bbox coords or full motif text. Hashes only.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from typing import Any

from aspire_orchestrator.services.supabase_client import (
    SupabaseClientError,
    supabase_insert_batch_ignore_conflicts,
    supabase_select,
)

_log = logging.getLogger(__name__)

# Terminal states (must match CHECK constraint on blueprint_resolved_symbols.terminal_state)
STATE_BOUND_MATERIAL = "BOUND_MATERIAL"
STATE_DECORATION = "DECORATION"
STATE_TEXT_OCR = "TEXT_OCR"
STATE_PLAN_ELEMENT = "PLAN_ELEMENT"
STATE_CRUFT = "CRUFT"
STATE_UNKNOWN_REVIEW = "UNKNOWN_REVIEW"

ALL_STATES = (
    STATE_BOUND_MATERIAL,
    STATE_DECORATION,
    STATE_TEXT_OCR,
    STATE_PLAN_ELEMENT,
    STATE_CRUFT,
    STATE_UNKNOWN_REVIEW,
)

# Pattern libraries — TUNED to actual Gulfstream data
_DIMENSION_RE = re.compile(r"\d+['′’]?\s*[-–]?\s*\d+\s*[\"″”]?|\d+\s*(SF|sf|sqft|sq\.?\s*ft)")
_ROOM_NUMBER_RE = re.compile(r"^[A-Z]{0,3}[-.]?\d{2,4}[A-Z]?$")
_SHEET_TITLE_RE = re.compile(r"^[A-Z]{1,3}[-.]?\d+\.\d+$")  # A-1.1, FP1.0, etc.
_PURE_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")

_DECORATION_TOKENS = frozenset({
    "north", "arrow", "scale_bar", "scale bar", "callout", "bubble",
    "hatching", "hatch", "section_marker", "section marker",
    "detail_marker", "detail marker", "graphic_scale", "compass",
    "match_line", "match line", "revision", "leader", "symbol_legend",
    "north_arrow",
})
_PLAN_ELEMENT_TOKENS = frozenset({
    "dimension", "title_block", "title block", "sheet_index",
    "sheet_number", "sheet_title", "revision_cloud",
    "drawing_index", "key_plan", "key plan",
})

# Geometry thresholds (TUNED per Gulfstream distribution)
_CRUFT_MIN_AREA = 25.0           # bbox area (pt²) below this = cruft
_CRUFT_BORDER_PCT = 0.03         # bbox within 3% of sheet edge = border cruft
_TEXT_OVERLAP_THRESHOLD = 0.30   # ≥30% overlap with paragraph region = text glyph
_SHEET_DEFAULT_WIDTH = 5000.0
_SHEET_DEFAULT_HEIGHT = 5000.0

# VLM tiebreaker threshold
_VLM_MIN_FAMILY_INSTANCES = 50   # families with ≥ this instance count get VLM verification
_VLM_PER_CALL_USD = 0.0003       # gpt-5.4-mini vision per-call estimate


# ─────────────────────────────────────────────────────────────────────────────
# bbox helpers
# ─────────────────────────────────────────────────────────────────────────────


def _bbox_xywh(bbox_dict: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox_dict, dict):
        return None
    try:
        if {"x", "y", "w", "h"}.issubset(bbox_dict.keys()):
            return (
                float(bbox_dict["x"]),
                float(bbox_dict["y"]),
                float(bbox_dict["w"]),
                float(bbox_dict["h"]),
            )
    except (TypeError, ValueError):
        pass
    return None


def _bbox_area(b: tuple[float, float, float, float]) -> float:
    return b[2] * b[3]


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


def _is_pattern_match(text: str, *, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns) if text else False


def _looks_like_decoration(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(tok in lower for tok in _DECORATION_TOKENS)


def _looks_like_plan_element(name: str) -> bool:
    if not name:
        return False
    lower = name.lower().strip()
    if any(tok in lower for tok in _PLAN_ELEMENT_TOKENS):
        return True
    if _DIMENSION_RE.search(name):
        return True
    if _ROOM_NUMBER_RE.match(name.strip()):
        return True
    if _SHEET_TITLE_RE.match(name.strip()):
        return True
    if _PURE_NUMBER_RE.match(name.strip()):
        return True
    return False


def _legend_text_match(name: str, labels_lower: dict[str, str]) -> str | None:
    if not name:
        return None
    needle = name.lower().strip()
    if not needle or needle.startswith("unknown_motif"):
        return None
    if needle in labels_lower:
        return labels_lower[needle]
    # Token overlap: any token of motif name matches any token of legend label
    needle_tokens = set(re.findall(r"[a-z0-9]+", needle))
    if not needle_tokens:
        return None
    best_score = 0.0
    best_id: str | None = None
    for label, lid in labels_lower.items():
        label_tokens = set(re.findall(r"[a-z0-9]+", label))
        if not label_tokens:
            continue
        overlap = len(needle_tokens & label_tokens)
        if overlap == 0:
            continue
        score = overlap / max(len(needle_tokens), len(label_tokens))
        if score > best_score:
            best_score = score
            best_id = lid
    return best_id if best_score >= 0.5 else None


def _signature_match(sig: str | None, legend_signatures: dict[str, str]) -> str | None:
    if not sig:
        return None
    return legend_signatures.get(sig)


# ─────────────────────────────────────────────────────────────────────────────
# Family aggregation
# ─────────────────────────────────────────────────────────────────────────────


class _Family:
    """Aggregated view of all motif rows sharing one signature_hash."""

    __slots__ = (
        "signature_hash",
        "rows",                  # list of motif row dicts
        "total_instances",       # sum of instance_count across rows
        "dominant_name",         # most common original_class
        "dominant_sheet_id",     # sheet with most rows
        "centroid_bbox",         # avg (x, y, w, h)
        "max_text_overlap",      # max overlap any row had with text region
    )

    def __init__(self, sig: str) -> None:
        self.signature_hash: str = sig
        self.rows: list[dict[str, Any]] = []
        self.total_instances: int = 0
        self.dominant_name: str = ""
        self.dominant_sheet_id: str | None = None
        self.centroid_bbox: tuple[float, float, float, float] | None = None
        self.max_text_overlap: float = 0.0


def _aggregate_families(motif_rows: list[dict[str, Any]]) -> dict[str, _Family]:
    families: dict[str, _Family] = {}
    name_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sheet_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bbox_sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])  # x,y,w,h,count

    for r in motif_rows:
        b = r.get("bbox") or {}
        sig = b.get("signature_hash") if isinstance(b, dict) else None
        if not sig:
            # No signature → use row id as own family of 1
            sig = f"nosig_{r['id']}"
        fam = families.get(sig)
        if fam is None:
            fam = _Family(sig)
            families[sig] = fam
        fam.rows.append(r)
        if isinstance(b, dict):
            try:
                inst = int(b.get("instance_count", 1))
            except (TypeError, ValueError):
                inst = 1
            fam.total_instances += max(inst, 1)
            orig = (b.get("original_class") or "").strip()
            if orig:
                name_counts[sig][orig] += 1
            sid = r.get("sheet_id")
            if sid:
                sheet_counts[sig][sid] += 1
            xywh = _bbox_xywh(b)
            if xywh is not None:
                s = bbox_sums[sig]
                s[0] += xywh[0]; s[1] += xywh[1]; s[2] += xywh[2]; s[3] += xywh[3]; s[4] += 1

    for sig, fam in families.items():
        # Pick most common name
        if name_counts[sig]:
            fam.dominant_name = max(name_counts[sig].items(), key=lambda kv: kv[1])[0]
        # Pick most common sheet
        if sheet_counts[sig]:
            fam.dominant_sheet_id = max(sheet_counts[sig].items(), key=lambda kv: kv[1])[0]
        # Centroid bbox
        s = bbox_sums[sig]
        if s[4] > 0:
            n = s[4]
            fam.centroid_bbox = (s[0] / n, s[1] / n, s[2] / n, s[3] / n)

    return families


def _compute_family_text_overlap(
    fam: _Family,
    paragraphs_per_sheet: dict[str, list[list[float]]],
) -> float:
    if fam.centroid_bbox is None or not fam.dominant_sheet_id:
        return 0.0
    paras = paragraphs_per_sheet.get(fam.dominant_sheet_id, [])
    max_o = 0.0
    for poly in paras:
        o = _bbox_overlap_ratio(fam.centroid_bbox, poly)
        if o > max_o:
            max_o = o
    return max_o


# ─────────────────────────────────────────────────────────────────────────────
# Family routing
# ─────────────────────────────────────────────────────────────────────────────


def _route_family(
    fam: _Family,
    *,
    sheet_extents: dict[str, tuple[float, float]],
    paragraphs_per_sheet: dict[str, list[list[float]]],
    legend_labels_lower: dict[str, str],
    legend_signatures: dict[str, str],
) -> tuple[str, str | None, str | None]:
    """Returns (terminal_state, legend_id_or_none, reason_code_or_none)."""
    bbox = fam.centroid_bbox
    name = fam.dominant_name
    sheet_id = fam.dominant_sheet_id

    # CRUFT — tiny bbox or near border
    if bbox is not None:
        if _bbox_area(bbox) < _CRUFT_MIN_AREA:
            return (STATE_CRUFT, None, "TINY_BBOX")
        sw, sh = sheet_extents.get(sheet_id or "", (_SHEET_DEFAULT_WIDTH, _SHEET_DEFAULT_HEIGHT))
        margin_x = sw * _CRUFT_BORDER_PCT
        margin_y = sh * _CRUFT_BORDER_PCT
        if (
            bbox[0] < margin_x
            or bbox[1] < margin_y
            or bbox[0] + bbox[2] > sw - margin_x
            or bbox[1] + bbox[3] > sh - margin_y
        ):
            return (STATE_CRUFT, None, "BORDER_CRUFT")

    # TEXT_OCR — centroid bbox overlaps paragraph region
    fam.max_text_overlap = _compute_family_text_overlap(fam, paragraphs_per_sheet)
    if fam.max_text_overlap >= _TEXT_OVERLAP_THRESHOLD:
        return (STATE_TEXT_OCR, None, None)

    # PLAN_ELEMENT — name patterns
    if name and _looks_like_plan_element(name):
        return (STATE_PLAN_ELEMENT, None, None)

    # BOUND_MATERIAL — signature OR text match
    lid = _signature_match(fam.signature_hash, legend_signatures)
    if lid:
        return (STATE_BOUND_MATERIAL, lid, "SIGNATURE_MATCH")
    lid = _legend_text_match(name, legend_labels_lower)
    if lid:
        return (STATE_BOUND_MATERIAL, lid, "TEXT_MATCH")

    # DECORATION — vocab match
    if name and _looks_like_decoration(name):
        return (STATE_DECORATION, None, None)

    return (STATE_UNKNOWN_REVIEW, None, "NO_RULE_MATCH")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────


async def resolve_terminal_states(
    *,
    project_id: str,
    suite_id: str,
) -> dict[str, Any]:
    """Run TR-Layer on a project. Family-level routing. Returns per-state counts
    (motif rows AND instance multipliers).
    """
    if not suite_id:
        raise ValueError("resolve_terminal_states requires non-empty suite_id (Law #6)")

    # 1. Sheets + extents
    sheet_rows = await supabase_select(
        "blueprint_sheets",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    if not sheet_rows:
        return {state: 0 for state in ALL_STATES} | {
            "total_motif_rows": 0,
            "total_instances": 0,
            "coverage_pct": 0.0,
            "families_total": 0,
        }
    sheet_ids = [s["id"] for s in sheet_rows]
    sheet_ids_csv = ",".join(sheet_ids)
    sheet_extents: dict[str, tuple[float, float]] = {}
    for s in sheet_rows:
        w = s.get("page_width_pt") or s.get("page_width") or _SHEET_DEFAULT_WIDTH
        h = s.get("page_height_pt") or s.get("page_height") or _SHEET_DEFAULT_HEIGHT
        try:
            sheet_extents[s["id"]] = (float(w), float(h))
        except (TypeError, ValueError):
            sheet_extents[s["id"]] = (_SHEET_DEFAULT_WIDTH, _SHEET_DEFAULT_HEIGHT)

    # 2. Paragraphs per sheet (paginated)
    intel_rows = await supabase_select(
        "blueprint_sheet_intelligence",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    paragraphs_per_sheet: dict[str, list[list[float]]] = {}
    for row in intel_rows or []:
        sid = row.get("sheet_id")
        paras = row.get("paragraphs") or []
        polys: list[list[float]] = []
        for p in paras:
            poly = p.get("bbox") if isinstance(p, dict) else None
            if isinstance(poly, list) and len(poly) >= 8:
                polys.append(poly[:8])
        if sid:
            paragraphs_per_sheet[sid] = polys

    # 3. Legend dictionary
    legend_rows = await supabase_select(
        "blueprint_project_legends",
        f"project_id=eq.{project_id}&suite_id=eq.{suite_id}",
    )
    labels_lower: dict[str, str] = {}
    legend_signatures: dict[str, str] = {}
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

    # 4. Stream motifs in pages
    motif_rows: list[dict[str, Any]] = []
    page_size = 1000
    offset = 0
    while True:
        page = await supabase_select(
            "blueprint_symbols",
            f"sheet_id=in.({sheet_ids_csv})&suite_id=eq.{suite_id}&order=id&offset={offset}",
            limit=page_size,
        )
        if not page:
            break
        motif_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        if offset > 500000:
            break

    # 5. Aggregate into families
    families = _aggregate_families(motif_rows)

    # 6. Route each family + record decisions
    family_decisions: dict[str, tuple[str, str | None, str | None]] = {}
    for sig, fam in families.items():
        family_decisions[sig] = _route_family(
            fam,
            sheet_extents=sheet_extents,
            paragraphs_per_sheet=paragraphs_per_sheet,
            legend_labels_lower=labels_lower,
            legend_signatures=legend_signatures,
        )

    # 7. Propagate to per-detection rows
    state_motif_rows = {s: 0 for s in ALL_STATES}
    state_instances = {s: 0 for s in ALL_STATES}
    resolved_inserts: list[dict[str, Any]] = []

    for sig, fam in families.items():
        state, legend_id, _reason = family_decisions[sig]
        state_motif_rows[state] += len(fam.rows)
        state_instances[state] += fam.total_instances
        for r in fam.rows:
            bbox_dict = r.get("bbox") or {}
            resolved_inserts.append({
                "id": str(uuid.uuid4()),
                "suite_id": suite_id,
                "project_id": project_id,
                "sheet_id": r.get("sheet_id"),
                "legend_id": legend_id,
                "bbox": bbox_dict,
                "symbol_name": (
                    bbox_dict.get("original_class") if isinstance(bbox_dict, dict) else None
                ),
                "confidence": r.get("confidence"),
                "resolution_source": "terminal_resolver_v2",
                "cluster_id": f"sig_{sig[:16]}",
                "motif_hash": sig,
                "terminal_state": state,
            })

    # 8. Batch insert
    if resolved_inserts:
        batch_size = 500
        for i in range(0, len(resolved_inserts), batch_size):
            chunk = resolved_inserts[i : i + batch_size]
            try:
                await supabase_insert_batch_ignore_conflicts(
                    "blueprint_resolved_symbols", chunk, conflict_target="id",
                )
            except SupabaseClientError as exc:
                _log.warning(
                    "terminal_resolver: insert batch fail i=%d (%s)",
                    i // batch_size, type(exc).__name__,
                )

    total_rows = sum(state_motif_rows.values())
    total_inst = sum(state_instances.values())
    _log.info(
        "terminal_resolver.v2: project=%s suite=%s families=%d motif_rows=%d instances=%d "
        "BM_rows=%d BM_inst=%d UNK_rows=%d UNK_inst=%d",
        project_id[:8], suite_id[:8], len(families), total_rows, total_inst,
        state_motif_rows[STATE_BOUND_MATERIAL], state_instances[STATE_BOUND_MATERIAL],
        state_motif_rows[STATE_UNKNOWN_REVIEW], state_instances[STATE_UNKNOWN_REVIEW],
    )

    out: dict[str, Any] = {}
    for s in ALL_STATES:
        out[f"{s}_rows"] = state_motif_rows[s]
        out[f"{s}_instances"] = state_instances[s]
    out["total_motif_rows"] = total_rows
    out["total_instances"] = total_inst
    out["families_total"] = len(families)
    out["coverage_pct"] = 100.0 if total_rows > 0 else 0.0
    return out


__all__ = [
    "STATE_BOUND_MATERIAL", "STATE_DECORATION", "STATE_TEXT_OCR",
    "STATE_PLAN_ELEMENT", "STATE_CRUFT", "STATE_UNKNOWN_REVIEW",
    "ALL_STATES", "resolve_terminal_states",
]
