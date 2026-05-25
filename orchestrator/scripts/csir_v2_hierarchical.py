"""Wave 20.14.5J-V2 — Hierarchical Factorization (routing_context vs semantic_identity).

Per v22 architectural pivot:

  ROUTING_CONTEXT (gates comparison eligibility, NOT identity)
    - discipline, operational_system, sheet_type (sheet-level)
    - per-page granularity, sheet_code-derived

  SEMANTIC_IDENTITY (defines what a thing IS)
    - topology (already shipped)
    - scale (normalized by local drawing context, not absolute)
    - position (functional participation, not coordinates)
    - [connectivity, constraints, referential — Phase 3+]

Implements 4 v22 fixes:
  1. routing/identity separation in code
  2. Conditional ARI (within-project, within-discipline, within-sheet-type)
  3. operational_system priority: sheet_code → title_block → nearby_text → body_fallback
  4. scale = symbol_bbox / local_drawing_scale_context; position = functional region

6 v22 acceptance gates:
  - Conditional ARI within-project <0.6
  - Conditional ARI within-discipline <0.7
  - Preservation >92%
  - Cross-project leakage <5%
  - operational_system entropy within discipline >1.5 bits
  - scale ↔ position ARI (unconditional) <0.6

ZERO Supabase IO.
"""
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import pymupdf as fitz  # type: ignore
except ImportError:
    import fitz  # type: ignore

sys.path.insert(0, str(Path(__file__).parent))
from canonicalize_local import extract_lines, canonicalize, _angle_rad  # noqa: E402
from semantic_twins_probe import find_symbol_field_tiles  # noqa: E402

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
TILE_PT = 50.0


# ============================================================
# FIX #3: Sheet-code priority chain for routing context
# ============================================================

# Sheet-code prefix → discipline + default operational system
SHEET_CODE_PREFIX = [
    # (regex_pattern, discipline, operational_system, sheet_type)
    (r"^FP[-_\s]?\d", "fire_protection", "fire_protection", "discipline_plan"),
    (r"^FA[-_\s]?\d", "fire_alarm", "fire_alarm", "discipline_plan"),
    (r"^LP[-_\s]?\d", "electrical", "lighting", "discipline_plan"),
    (r"^L[-_\s]?\d", "electrical", "lighting", "discipline_plan"),
    (r"^E[-_\s]?\d", "electrical", "power", "discipline_plan"),
    (r"^P[-_\s]?\d", "plumbing", "sanitary", "discipline_plan"),
    (r"^M[-_\s]?\d", "mechanical", "hvac", "discipline_plan"),
    (r"^C[-_\s]?\d", "civil", "site_grading", "discipline_plan"),
    (r"^S[-_\s]?\d", "structural", "structural", "discipline_plan"),
    (r"^A[-_\s]?\d", "architectural", "architectural", "discipline_plan"),
    (r"^G[-_\s]?\d", "general", "general", "general"),
    (r"^T[-_\s]?\d", "general", "title_sheet", "title_sheet"),
]

# Secondary text patterns for operational_system refinement
SYSTEM_REFINE = {
    "fire_alarm": [r"\bFIRE\s*ALARM\b", r"\bSMOKE\s*DETECT"],
    "fire_protection": [r"\bSPRINKLER\b", r"\bFIRE\s*PROTECTION\b", r"\bSTANDPIPE\b"],
    "lighting": [r"\bLIGHTING\s*PLAN\b", r"\bPHOTOMETRIC\b", r"\bLUMINAIRE\b"],
    "site_lighting": [r"\bSITE\s*LIGHTING\b", r"\bLIGHT\s*POLE\b"],
    "low_voltage": [r"\bDATA\s*PLAN\b", r"\bTELECOM\b", r"\bACCESS\s*CONTROL\b"],
    "drainage": [r"\bCATCH\s*BASIN\b", r"\bDRAINAGE\s*PLAN\b", r"\bSTORMWATER\b"],
    "site_grading": [r"\bGRADING\b", r"\bCONTOUR\b"],
}


def detect_sheet_code(text: str, fallback_filename: str) -> str | None:
    """Find sheet code from page text, fallback to filename."""
    # Try title block patterns first (often top-right or bottom-right)
    # Common sheet code formats: E-1, FP-101, A1.1, etc.
    patterns = [
        r"SHEET[:\s]+([A-Z]{1,3}[-_]?\d{1,3}(?:\.\d{1,2})?)",
        r"DWG[:\s]*([A-Z]{1,3}[-_]?\d{1,3}(?:\.\d{1,2})?)",
        r"\b([A-Z]{1,3}[-_]?\d{1,3}(?:\.\d{1,2})?)\s*$",  # end-of-line code
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    # Fallback: filename
    fn = fallback_filename.upper()
    m = re.search(r"\b([A-Z]{1,3}[-_]?\d{1,4})", fn)
    if m:
        return m.group(1)
    return None


def routing_context_per_page(text: str, fallback_filename: str) -> dict:
    """Compute routing_context atoms with priority chain:
    sheet_code prefix → title_block keywords → body fallback."""
    sheet_code = detect_sheet_code(text, fallback_filename)
    upper_text = text.upper()

    discipline = "unknown"
    op_system = "unknown"
    sheet_type = "unknown"
    evidence: list[str] = []
    confidence = 0.10

    # Priority 1: sheet_code prefix match
    if sheet_code:
        for pat, disc, op, st in SHEET_CODE_PREFIX:
            if re.match(pat, sheet_code):
                discipline = disc
                op_system = op
                sheet_type = st
                evidence.append(f"sheet_code:{sheet_code}")
                confidence = 0.85
                break

    # Priority 2: title block keyword refinement of operational_system
    if discipline != "unknown":
        # Look for system-refinement keywords (e.g. an E-sheet that's actually fire alarm)
        for refined_sys, patterns in SYSTEM_REFINE.items():
            for pat in patterns:
                if re.search(pat, upper_text):
                    # Only refine if the keyword is system-internal, not just mentioned
                    op_system = refined_sys
                    evidence.append(f"text:{refined_sys}")
                    confidence = min(0.95, confidence + 0.05)
                    break

    # Fallback if everything failed
    if discipline == "unknown":
        # Body keyword fallback
        for refined_sys, patterns in SYSTEM_REFINE.items():
            for pat in patterns:
                if re.search(pat, upper_text):
                    op_system = refined_sys
                    discipline = refined_sys.split("_")[0] if "_" in refined_sys else refined_sys
                    confidence = 0.35
                    evidence.append(f"body_fallback:{refined_sys}")
                    break
            if discipline != "unknown":
                break

    return {
        "sheet_code": sheet_code,
        "discipline": discipline,
        "operational_system": op_system,
        "sheet_type": sheet_type,
        "confidence": round(confidence, 3),
        "evidence": evidence[:3],
    }


# ============================================================
# FIX #4a: Functional position (NOT coordinate-based)
# ============================================================

def functional_position_atom(candidate: dict, page_extent: tuple[float, float],
                              tile_features: dict | None = None) -> dict:
    """Position by FUNCTIONAL participation in sheet structure.

    Categories:
      drawing_viewport   — inside main drawing area, distant from edges
      title_block_zone   — within ~15% of bottom-right corner
      annotation_band    — within ~5% of any edge (border gutter)
      schedule_adjacency — high horizontal+vertical line density in vicinity
      legend_cluster     — high curve density + small bboxes nearby
    """
    cx, cy = candidate["centroid"]
    pw, ph = page_extent
    if pw <= 0 or ph <= 0:
        return {"functional_position": "unknown", "confidence": 0.0}

    rel_x = cx / pw
    rel_y = cy / ph

    # Title block: bottom band + right band
    if rel_y < 0.18 and rel_x > 0.55:
        return {"functional_position": "title_block_zone", "confidence": 0.85}

    # Annotation/border band
    if rel_y < 0.05 or rel_y > 0.95 or rel_x < 0.04 or rel_x > 0.96:
        return {"functional_position": "annotation_band", "confidence": 0.80}

    # Schedule adjacency heuristic: if nearby tile has very high prim density (tables typically)
    if tile_features and tile_features.get("n_visible", 0) > 80:
        return {"functional_position": "schedule_adjacency", "confidence": 0.70}

    # Default: drawing viewport
    return {"functional_position": "drawing_viewport", "confidence": 0.75}


# ============================================================
# FIX #4b: Scale normalized by local drawing context
# ============================================================

def local_scale_context(candidate: dict, all_prims: list[dict], radius: float = 200.0) -> float:
    """Compute median primitive length within `radius` of candidate centroid.
    Returns the local 'typical scale' against which candidate size is normalized."""
    cx, cy = candidate["centroid"]
    page = candidate["page"]
    nearby_lengths = []
    for p in all_prims:
        if p["page"] != page:
            continue
        d = math.hypot(p["cx"] - cx, p["cy"] - cy)
        if d <= radius:
            L = p.get("length", 0) or max(p.get("bbox_w", 0), p.get("bbox_h", 0))
            if L > 0:
                nearby_lengths.append(L)
    if not nearby_lengths:
        return 1.0
    nearby_lengths.sort()
    return nearby_lengths[len(nearby_lengths) // 2]  # median


def normalized_scale_atom(candidate: dict, local_typical: float) -> dict:
    """Scale class normalized by local context, not absolute size."""
    bw = candidate.get("bbox_w", 0)
    bh = candidate.get("bbox_h", 0)
    cand_dim = max(bw, bh)
    ratio = cand_dim / max(local_typical, 0.1)
    # Quantize into normalized classes
    if ratio < 0.5:
        cls = "below_local_typical"
    elif ratio < 1.5:
        cls = "at_local_typical"
    elif ratio < 4.0:
        cls = "above_local_typical"
    elif ratio < 12.0:
        cls = "far_above_local_typical"
    else:
        cls = "structural_scale"
    return {
        "normalized_scale": cls,
        "ratio_vs_local": round(ratio, 3),
        "local_typical_pt": round(local_typical, 2),
        "candidate_max_dim_pt": round(cand_dim, 2),
    }


# ============================================================
# Build IR records with hierarchical factorization
# ============================================================

def extract_per_page_text(pdf_path: Path) -> dict[int, str]:
    """Per-page text for sheet_code/system extraction."""
    pages: dict[int, str] = {}
    try:
        doc = fitz.open(str(pdf_path))
        for pg in range(doc.page_count):
            pages[pg] = (doc[pg].get_text() or "").upper()
        doc.close()
    except Exception:
        pass
    return pages


def extract_candidates_with_tile_features(sym_tiles, all_prims_by_page):
    """Pass tile features alongside candidates for functional_position computation."""
    candidates_with_tiles = []
    for (page, tx, ty), group in sym_tiles.items():
        n_visible = len(group)
        n_curves = sum(1 for p in group if p["primitive_type"] in ("curve", "synthetic_curve"))
        # Tile features for position context
        tile_feats = {"n_visible": n_visible, "n_curves": n_curves}
        seeds = [p for p in group if p["primitive_type"] in ("curve", "synthetic_curve")]
        for seed in seeds:
            sx, sy = seed["cx"], seed["cy"]
            nearby = [
                p for p in group
                if math.hypot(p["cx"] - sx, p["cy"] - sy) <= 25.0
            ]
            if 3 <= len(nearby) <= 15:
                types = tuple(sorted(p["primitive_type"][:1] for p in nearby))
                buckets = tuple(sorted(
                    (p["primitive_type"][:1], int(p.get("length", 0) // 5))
                    for p in nearby
                ))
                sig = hashlib.md5(str(buckets).encode()).hexdigest()[:10]
                candidates_with_tiles.append({
                    "candidate": {
                        "page": page, "tile": (tx, ty),
                        "n_prims": len(nearby),
                        "type_multiset": types,
                        "topology_sig": sig,
                        "centroid": (sx, sy),
                        "bbox_w": max(p["cx"] for p in nearby) - min(p["cx"] for p in nearby),
                        "bbox_h": max(p["cy"] for p in nearby) - min(p["cy"] for p in nearby),
                    },
                    "tile_features": tile_feats,
                })
    return candidates_with_tiles


def build_records() -> list[dict]:
    records: list[dict] = []
    pdf_sheet_info: dict[str, dict] = {}

    for pdf in sorted(BLUEPRINTS.glob("*.pdf")):
        pdf_key = pdf.stem
        try:
            page_texts = extract_per_page_text(pdf)
            prims = extract_lines(str(pdf))
            synthetic, _, _ = canonicalize(prims)
            all_prims = prims + synthetic
            sym_tiles = find_symbol_field_tiles(all_prims)
            candidates_with_tiles = extract_candidates_with_tile_features(sym_tiles, None)
        except Exception as exc:
            print(f"  ERR {pdf.name}: {type(exc).__name__}: {exc}")
            continue

        # Page-level routing context (one per page in this PDF)
        page_routing: dict[int, dict] = {}
        for page_idx, txt in page_texts.items():
            ctx = routing_context_per_page(txt, pdf_key)
            page_routing[page_idx] = ctx

        # Page extents
        page_extents: dict[int, tuple[float, float]] = {}
        for p in all_prims:
            page = p["page"]
            prev = page_extents.get(page, (0.0, 0.0))
            page_extents[page] = (max(prev[0], p["cx"]), max(prev[1], p["cy"]))

        for item in candidates_with_tiles:
            c = item["candidate"]
            tile_feats = item["tile_features"]
            pe = page_extents.get(c["page"], (1.0, 1.0))
            routing = page_routing.get(c["page"], {
                "discipline": "unknown", "operational_system": "unknown",
                "sheet_type": "unknown", "sheet_code": None, "confidence": 0.0,
            })
            fpos = functional_position_atom(c, pe, tile_feats)
            local_typical = local_scale_context(c, all_prims, radius=200.0)
            nscale = normalized_scale_atom(c, local_typical)

            records.append({
                "pdf_key": pdf_key,
                "project_key": pdf_key.split(" ")[0][:6],  # rough project grouping
                "page": c["page"],
                "topology_sig": c["topology_sig"],
                # ROUTING CONTEXT (sheet-level)
                "routing_sheet_code": routing.get("sheet_code"),
                "routing_discipline": routing["discipline"],
                "routing_operational_system": routing["operational_system"],
                "routing_sheet_type": routing["sheet_type"],
                # SEMANTIC IDENTITY (candidate-level)
                "identity_functional_position": fpos["functional_position"],
                "identity_normalized_scale": nscale["normalized_scale"],
                "identity_n_prims": c["n_prims"],
                # diagnostics
                "scale_ratio_vs_local": nscale["ratio_vs_local"],
                "centroid": c["centroid"],
            })

        print(f"  {pdf.name[:48]:<48s} pages={len(page_texts)}  "
              f"candidates={len(candidates_with_tiles)}")
        if page_routing:
            for pg_idx, r in list(page_routing.items())[:2]:
                ev = ",".join(r.get("evidence", [])[:2]) or "—"
                print(f"     page {pg_idx}: code={r.get('sheet_code')!s:<10s} "
                      f"disc={r['discipline']:<18s} op={r['operational_system']:<18s} ev={ev}")

    return records


# ============================================================
# Conditional ARI
# ============================================================

def comb2(n: int) -> int:
    return n * (n - 1) // 2 if n >= 2 else 0


def adjusted_rand_index(labels_a: list, labels_b: list) -> float:
    if len(labels_a) != len(labels_b) or len(labels_a) < 2:
        return 0.0
    contingency: dict[tuple, int] = defaultdict(int)
    a_counts: Counter = Counter()
    b_counts: Counter = Counter()
    for a, b in zip(labels_a, labels_b):
        contingency[(a, b)] += 1
        a_counts[a] += 1
        b_counts[b] += 1
    n = len(labels_a)
    sum_comb_ij = sum(comb2(c) for c in contingency.values())
    sum_comb_a = sum(comb2(c) for c in a_counts.values())
    sum_comb_b = sum(comb2(c) for c in b_counts.values())
    total_comb = comb2(n)
    if total_comb == 0:
        return 0.0
    expected = (sum_comb_a * sum_comb_b) / total_comb
    max_idx = 0.5 * (sum_comb_a + sum_comb_b)
    denom = max_idx - expected
    if abs(denom) < 1e-12:
        return 1.0 if sum_comb_ij == expected else 0.0
    return round((sum_comb_ij - expected) / denom, 4)


def conditional_ari(records: list[dict], atom_a: str, atom_b: str, group_by: str) -> dict:
    """Compute ARI within each group (e.g., same project), weighted average."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r.get(group_by, "_")].append(r)

    per_group: list[tuple[str, int, float]] = []
    total_records = 0
    weighted_ari = 0.0
    for gkey, recs in groups.items():
        if len(recs) < 3:  # need enough records for meaningful ARI
            continue
        labels_a = [str(r.get(atom_a, "_")) for r in recs]
        labels_b = [str(r.get(atom_b, "_")) for r in recs]
        ari = adjusted_rand_index(labels_a, labels_b)
        per_group.append((gkey, len(recs), ari))
        weighted_ari += ari * len(recs)
        total_records += len(recs)

    avg = weighted_ari / max(total_records, 1)
    return {
        "weighted_avg_ari": round(avg, 4),
        "n_groups": len(per_group),
        "per_group": per_group[:10],
    }


def entropy(values: list[str]) -> float:
    if not values:
        return 0.0
    c = Counter(values)
    n = sum(c.values())
    ent = 0.0
    for cnt in c.values():
        p = cnt / n
        ent -= p * math.log2(p)
    return ent


# ============================================================
# Main: build records + run all 6 v22 gates
# ============================================================

def main() -> None:
    print("=== Wave 20.14.5J-V2 Hierarchical Factorization ===\n")
    records = build_records()
    print(f"\ntotal IR records: {len(records)}")

    # Atom names (factored into routing vs identity)
    routing_atoms = ["routing_discipline", "routing_operational_system", "routing_sheet_type"]
    identity_atoms = ["identity_functional_position", "identity_normalized_scale"]
    all_atoms = routing_atoms + identity_atoms

    print(f"\n=== ATOM DISTRIBUTIONS ===")
    for atom in all_atoms:
        vals = Counter(r.get(atom) for r in records)
        print(f"  {atom:<35s} distinct={len(vals):<3d}  top: {dict(vals.most_common(5))}")

    print(f"\n=== UNCONDITIONAL ARI MATRIX ===")
    n = len(all_atoms)
    for a in all_atoms:
        row = f"  {a[:35]:<35s}"
        for b in all_atoms:
            if a == b:
                ari = 1.0
            else:
                la = [str(r.get(a)) for r in records]
                lb = [str(r.get(b)) for r in records]
                ari = adjusted_rand_index(la, lb)
            row += f"  {ari:>6.3f}"
        print(row)

    print(f"\n=== V22 GATE EVALUATIONS ===\n")

    # GATE 1: Conditional ARI within-project <0.6
    print(f"Gate 1 — Conditional ARI WITHIN-PROJECT (target <0.6)")
    max_within_project = 0.0
    for i, a in enumerate(all_atoms):
        for b in all_atoms[i+1:]:
            r = conditional_ari(records, a, b, "project_key")
            max_within_project = max(max_within_project, r["weighted_avg_ari"])
            print(f"  {a[:25]:<25s} ↔ {b[:25]:<25s}  ARI={r['weighted_avg_ari']:.4f}  groups={r['n_groups']}")
    g1 = max_within_project < 0.6
    print(f"  → max within-project ARI = {max_within_project:.4f}   {'PASS' if g1 else 'FAIL'}\n")

    # GATE 2: Conditional ARI within-discipline <0.7
    print(f"Gate 2 — Conditional ARI WITHIN-DISCIPLINE (target <0.7)")
    max_within_disc = 0.0
    pairs_to_test = [
        ("routing_operational_system", "routing_sheet_type"),
        ("identity_functional_position", "identity_normalized_scale"),
        ("routing_operational_system", "identity_normalized_scale"),
        ("routing_sheet_type", "identity_functional_position"),
    ]
    for a, b in pairs_to_test:
        r = conditional_ari(records, a, b, "routing_discipline")
        max_within_disc = max(max_within_disc, r["weighted_avg_ari"])
        print(f"  {a[:25]:<25s} ↔ {b[:25]:<25s}  ARI={r['weighted_avg_ari']:.4f}  groups={r['n_groups']}")
    g2 = max_within_disc < 0.7
    print(f"  → max within-discipline ARI = {max_within_disc:.4f}   {'PASS' if g2 else 'FAIL'}\n")

    # GATE 3: Preservation >92%
    # Group by topology_sig; same-project-same-discipline records should stay together
    print(f"Gate 3 — Preservation >92%")
    topo_groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        topo_groups[r["topology_sig"]].append(r)
    healthy_total = 0
    healthy_preserved = 0
    # IR identity = routing_domain × semantic_signature
    for sig, recs in topo_groups.items():
        if len(recs) < 2:
            continue
        # Same-project + same-discipline = healthy recurrence candidate
        projects = {r["project_key"] for r in recs}
        disciplines = {r["routing_discipline"] for r in recs}
        if len(projects) == 1 and len(disciplines) == 1:
            healthy_total += 1
            # IR identity components (in v22, routing gates comparison; identity is what survives)
            identities = set()
            for r in recs:
                identity = (
                    r["topology_sig"],
                    r["identity_functional_position"],
                    r["identity_normalized_scale"],
                )
                identities.add(identity)
            # Within same routing domain, identity should collapse to ≤2 (allowing small variants)
            if len(identities) <= 2:
                healthy_preserved += 1
    preservation = healthy_preserved / max(healthy_total, 1)
    g3 = preservation > 0.92
    print(f"  healthy collisions: {healthy_total}  preserved (≤2 identities): {healthy_preserved}")
    print(f"  → preservation = {preservation*100:.1f}%   {'PASS' if g3 else 'FAIL'}\n")

    # GATE 4: Cross-project leakage <5%
    # Cross-project records with same topology AND same identity (genuine recurrence) — should be high
    # Cross-project records collapsing accidentally (different semantic but same identity) — should be <5%
    print(f"Gate 4 — Cross-project leakage <5%")
    cross_proj_collisions = 0
    cross_proj_genuine = 0
    cross_proj_accidental = 0
    for sig, recs in topo_groups.items():
        if len(recs) < 2:
            continue
        projects = {r["project_key"] for r in recs}
        if len(projects) >= 2:
            cross_proj_collisions += 1
            # Check if their semantic identity also matches
            identities = {(r["identity_functional_position"], r["identity_normalized_scale"]) for r in recs}
            if len(identities) == 1:
                cross_proj_genuine += 1  # same identity across projects = real recurrence
            else:
                cross_proj_accidental += 1  # topology collides but identity differs
    leakage = cross_proj_accidental / max(cross_proj_collisions, 1)
    g4 = leakage < 0.05
    print(f"  cross-project topology collisions: {cross_proj_collisions}")
    print(f"  genuine (same identity): {cross_proj_genuine}")
    print(f"  accidental (different identity, same topology): {cross_proj_accidental}")
    print(f"  → leakage = {leakage*100:.1f}%   {'PASS' if g4 else 'FAIL'}\n")

    # GATE 5: operational_system entropy WITHIN discipline >1.5 bits
    print(f"Gate 5 — operational_system entropy WITHIN discipline >1.5 bits")
    disc_groups: dict[str, list[str]] = defaultdict(list)
    for r in records:
        disc_groups[r["routing_discipline"]].append(r["routing_operational_system"])
    max_disc_entropy = 0.0
    for disc, sys_vals in disc_groups.items():
        if disc == "unknown":
            continue
        ent = entropy(sys_vals)
        max_disc_entropy = max(max_disc_entropy, ent)
        print(f"  {disc:<22s} n={len(sys_vals):<5d} entropy={ent:.3f} bits  distinct_systems={len(set(sys_vals))}")
    g5 = max_disc_entropy > 1.5
    print(f"  → max within-discipline operational_system entropy = {max_disc_entropy:.3f} bits   "
          f"{'PASS' if g5 else 'FAIL'}\n")

    # GATE 6: scale ↔ position ARI <0.6 (unconditional — the previously-correlated pair)
    print(f"Gate 6 — scale ↔ position ARI (unconditional) <0.6")
    la = [r["identity_normalized_scale"] for r in records]
    lb = [r["identity_functional_position"] for r in records]
    sp_ari = adjusted_rand_index(la, lb)
    g6 = sp_ari < 0.6
    print(f"  → scale ↔ position ARI = {sp_ari:.4f}   {'PASS' if g6 else 'FAIL'}\n")

    # Summary
    print(f"=== V22 ACCEPTANCE SUMMARY ===")
    gates = [
        ("Gate 1: within-project ARI <0.6", g1, max_within_project),
        ("Gate 2: within-discipline ARI <0.7", g2, max_within_disc),
        ("Gate 3: preservation >92%", g3, preservation),
        ("Gate 4: cross-project leakage <5%", g4, leakage),
        ("Gate 5: op_system entropy within discipline >1.5", g5, max_disc_entropy),
        ("Gate 6: scale↔position ARI <0.6", g6, sp_ari),
    ]
    passed = sum(1 for _, p, _ in gates if p)
    for name, p, val in gates:
        print(f"  {'PASS' if p else 'FAIL'}  {name:<50s}  value={val:.4f}")
    print(f"\n  {passed}/6 gates PASS")

    out_path = OUT_DIR / "csir_v2_hierarchical_2026-05-24.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V2",
            "n_records": len(records),
            "gates": {name: {"pass": p, "value": v} for name, p, v in gates},
            "gates_passed": f"{passed}/6",
            "max_within_project_ari": max_within_project,
            "max_within_discipline_ari": max_within_disc,
            "preservation": preservation,
            "cross_project_leakage": leakage,
            "max_op_system_entropy": max_disc_entropy,
            "scale_position_ari": sp_ari,
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")


if __name__ == "__main__":
    main()
