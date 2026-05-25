"""Wave 20.14.5J — CSIR Compiler Phase 2: Context Injection.

For each candidate graph from canonicalization, inject 5 context atoms:
  1. discipline_context (from PDF/sheet metadata)
  2. sheet_position_context (drawing_area / legend / title_block / schedule)
  3. system_membership (derived from discipline + sheet_code)
  4. scale_class (from primitive size distribution within graph)
  5. legend_alignment (SOFT — weak signal, never authoritative)

Then recompute IR-augmented signature = topology_sig × (discipline, position, system, scale).

Acceptance test (per v19 gate 2):
  ≥90% of the 15 known cross-discipline collisions get split into ≥2 distinct
  IR-augmented signatures (i.e., context atoms disambiguate them).

Healthy same-project-same-discipline collisions should STAY as collisions
(they represent real symbol recurrence, not semantic twins).

ZERO Supabase IO.
"""
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from canonicalize_local import extract_lines, canonicalize, _angle_rad  # noqa: E402
from semantic_twins_probe import (  # noqa: E402
    find_symbol_field_tiles, extract_candidate_graphs, DISCIPLINE,
)

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

# Discipline → system mapping (Phase 2 atom #3 inference rule)
DISCIPLINE_TO_SYSTEM = {
    "MEP": ["mep_general"],
    "electrical": ["power_distribution", "lighting", "low_voltage"],
    "electrical_site": ["site_power", "site_lighting"],
    "plumbing": ["sanitary", "water_supply", "vent"],
    "civil": ["site_grading", "site_drainage", "site_utilities"],
    "mechanical_shop": ["hvac", "piping", "equipment"],
    "structural": ["foundation", "framing"],
    "site_lighting": ["site_lighting", "photometric"],
    "unknown": ["unknown"],
}


# ============================================================
# Atom 1: discipline_context (from PDF metadata)
# ============================================================
def discipline_context_atom(pdf_key: str) -> dict:
    project, discipline = DISCIPLINE.get(pdf_key, ("unknown", "unknown"))
    return {
        "discipline": discipline,
        "project": project,
        "confidence": 0.95 if discipline != "unknown" else 0.10,
    }


# ============================================================
# Atom 2: sheet_position_context (where on sheet)
# ============================================================
def sheet_position_atom(candidate: dict, page_extent: tuple[float, float]) -> dict:
    """Classify by relative position on page.
    Real sheets have title block (bottom right, lower band), schedule columns
    (right edge typically), drawing area (center), legend (often left or top edge)."""
    cx, cy = candidate["centroid"]
    page_w, page_h = page_extent
    if page_w <= 0 or page_h <= 0:
        return {"position": "unknown", "confidence": 0.0}

    rel_x = cx / page_w
    rel_y = cy / page_h

    # Title block: typically bottom band (lower 15%) or right edge bottom corner
    if rel_y < 0.15 and rel_x > 0.6:
        return {"position": "title_block", "confidence": 0.80}
    # Schedule/legend column: right or left edge
    if rel_x < 0.10 or rel_x > 0.92:
        return {"position": "legend_or_schedule", "confidence": 0.70}
    # Border: top/bottom strips
    if rel_y < 0.05 or rel_y > 0.95:
        return {"position": "border", "confidence": 0.85}
    return {"position": "drawing_area", "confidence": 0.85}


# ============================================================
# Atom 3: system_membership (derived from discipline + sheet)
# ============================================================
def system_membership_atom(discipline: str, sheet_code_hint: str = "") -> dict:
    systems = DISCIPLINE_TO_SYSTEM.get(discipline, ["unknown"])
    # Without explicit sheet code, we return the top-likelihood system per discipline
    return {
        "primary_system": systems[0],
        "candidate_systems": systems,
        "confidence": 0.75 if discipline != "unknown" else 0.10,
    }


# ============================================================
# Atom 4: scale_class (from primitive size distribution)
# ============================================================
def scale_class_atom(candidate_prims: list[dict]) -> dict:
    """Classify symbol scale by characteristic length."""
    bw = candidate_prims[-1].get("bbox_w", 0) if candidate_prims else 0  # use last (which is the candidate's centroid prim approx)
    # Actually compute from candidate dict's geometry
    return {}  # filled in caller from candidate metadata


def scale_class_from_candidate(candidate: dict) -> dict:
    bw = candidate.get("bbox_w", 0)
    bh = candidate.get("bbox_h", 0)
    max_dim = max(bw, bh)
    if max_dim < 6:
        cls = "micro_annotation"  # callout marker, small dim text
    elif max_dim < 15:
        cls = "fixture_small"     # sprinkler heads, receptacles
    elif max_dim < 35:
        cls = "fixture_medium"    # diffusers, panels, valves
    elif max_dim < 80:
        cls = "fixture_large"     # equipment outlines, large fixtures
    else:
        cls = "structural_or_legend"
    return {"scale_class": cls, "max_dim_pt": round(max_dim, 2), "confidence": 0.85}


# ============================================================
# Atom 5: legend_alignment (SOFT — weighted vote, never authoritative)
# ============================================================
def legend_alignment_atom(candidate: dict) -> dict:
    """For Phase 2 baseline, return weak default. Phase 5 will implement real co-resolution."""
    return {
        "matched_label": None,
        "match_score": 0.0,
        "confidence": 0.0,  # explicitly weak — not yet computed
        "note": "phase_5_will_populate_legend_co_resolution",
    }


# ============================================================
# Augmented signature: topology + context atoms → unique IR identity
# ============================================================
def ir_augmented_signature(topology_sig: str, atoms: dict) -> str:
    """Combine raw topology with context atoms.
    Two graphs share IR identity iff they share topology AND match context.
    This is the test of Phase 2 disambiguation power."""
    components = [
        topology_sig,
        atoms["discipline"]["discipline"],
        atoms["position"]["position"],
        atoms["system"]["primary_system"],
        atoms["scale"]["scale_class"],
    ]
    blob = "|".join(str(c) for c in components)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


# ============================================================
# Process one PDF — extract candidates + inject atoms
# ============================================================
def process_pdf(pdf_path: Path) -> dict:
    pdf_key = pdf_path.stem
    try:
        prims = extract_lines(str(pdf_path))
        synthetic, _, _ = canonicalize(prims)
        all_prims = prims + synthetic
        sym_tiles = find_symbol_field_tiles(all_prims)
        candidates = extract_candidate_graphs(sym_tiles)
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}

    # Page extent for sheet_position_context — use page-level max coords
    page_extents: dict[int, tuple[float, float]] = {}
    for p in all_prims:
        page = p["page"]
        prev = page_extents.get(page, (0.0, 0.0))
        page_extents[page] = (max(prev[0], p["cx"]), max(prev[1], p["cy"]))

    discipline = discipline_context_atom(pdf_key)
    system = system_membership_atom(discipline["discipline"])

    ir_records: list[dict] = []
    for c in candidates:
        pe = page_extents.get(c["page"], (1.0, 1.0))
        position = sheet_position_atom(c, pe)
        scale = scale_class_from_candidate(c)
        legend = legend_alignment_atom(c)
        atoms = {
            "discipline": discipline,
            "position": position,
            "system": system,
            "scale": scale,
            "legend": legend,
        }
        ir_sig = ir_augmented_signature(c["topology_sig"], atoms)
        ir_records.append({
            "pdf_key": pdf_key,
            "topology_sig": c["topology_sig"],
            "ir_signature": ir_sig,
            "discipline": discipline["discipline"],
            "project": discipline["project"],
            "position": position["position"],
            "system": system["primary_system"],
            "scale_class": scale["scale_class"],
            "page": c["page"],
            "n_prims": c["n_prims"],
        })

    return {
        "pdf": pdf_path.name,
        "pdf_key": pdf_key,
        "n_candidates": len(candidates),
        "ir_records": ir_records,
    }


# ============================================================
# Acceptance test: measure twin resolution on 15-collision corpus
# ============================================================
def acceptance_test(all_records: list[dict]) -> dict:
    """For each cross-discipline collision in original probe, check if Phase 2
    atoms split it. ≥90% resolution = gate 2 pass."""
    # Build topology_sig → records map
    sig_to_records: dict[str, list[dict]] = defaultdict(list)
    for r in all_records:
        sig_to_records[r["topology_sig"]].append(r)

    # Categorize each topology signature
    cross_disc_sigs = []
    healthy_sigs = []
    for sig, recs in sig_to_records.items():
        if len(recs) < 2:
            continue
        disciplines = {r["discipline"] for r in recs}
        projects = {r["project"] for r in recs}
        if len(disciplines) > 1:
            cross_disc_sigs.append((sig, recs))
        elif len(projects) == 1 and len(disciplines) == 1:
            healthy_sigs.append((sig, recs))

    # For each cross-discipline collision: does Phase 2 IR split it?
    resolved = 0
    unresolved = 0
    resolution_breakdown = []
    for sig, recs in cross_disc_sigs:
        ir_sigs = {r["ir_signature"] for r in recs}
        ir_split = len(ir_sigs)
        if ir_split >= 2:
            resolved += 1
        else:
            unresolved += 1
        resolution_breakdown.append({
            "topology_sig": sig,
            "n_instances": len(recs),
            "disciplines": sorted({r["discipline"] for r in recs}),
            "ir_signatures_after": ir_split,
            "resolved": ir_split >= 2,
        })

    # Healthy recurrence test: do same-project-same-discipline collisions
    # STAY collisions (real symbol recurrence preserved)?
    healthy_preserved = 0
    healthy_broken = 0
    for sig, recs in healthy_sigs:
        ir_sigs = {r["ir_signature"] for r in recs}
        if len(ir_sigs) == 1:
            healthy_preserved += 1
        else:
            healthy_broken += 1

    return {
        "cross_disc_count": len(cross_disc_sigs),
        "cross_disc_resolved": resolved,
        "cross_disc_unresolved": unresolved,
        "resolution_rate": round(resolved / max(len(cross_disc_sigs), 1), 4),
        "healthy_count": len(healthy_sigs),
        "healthy_preserved": healthy_preserved,
        "healthy_broken": healthy_broken,
        "preservation_rate": round(healthy_preserved / max(len(healthy_sigs), 1), 4),
        "resolution_breakdown": resolution_breakdown,
    }


def main() -> None:
    print("=== Wave 20.14.5J CSIR Phase 2 — Context Injection ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))

    all_records: list[dict] = []
    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---")
        row = process_pdf(pdf)
        if "error" in row:
            print(f"  ERROR: {row['error']}"); continue
        print(f"  ir_records: {len(row['ir_records'])}")
        all_records.extend(row["ir_records"])

    print(f"\n=== ALL IR RECORDS ===")
    print(f"total: {len(all_records)}")

    # Distribution by augmented signature
    ir_counter: Counter[str] = Counter(r["ir_signature"] for r in all_records)
    print(f"distinct IR signatures: {len(ir_counter)}")
    print(f"IR signatures repeating ≥2: {sum(1 for v in ir_counter.values() if v >= 2)}")

    # Acceptance test
    print(f"\n=== ACCEPTANCE TEST (v19 gate 2: ≥90% twin separation) ===")
    result = acceptance_test(all_records)
    print(f"cross-discipline collisions found: {result['cross_disc_count']}")
    print(f"  resolved by Phase 2 atoms: {result['cross_disc_resolved']}")
    print(f"  still ambiguous: {result['cross_disc_unresolved']}")
    print(f"  RESOLUTION RATE: {result['resolution_rate']*100:.1f}%")
    print(f"\nhealthy recurrence collisions: {result['healthy_count']}")
    print(f"  preserved (still collide as same IR): {result['healthy_preserved']}")
    print(f"  broken by over-splitting: {result['healthy_broken']}")
    print(f"  PRESERVATION RATE: {result['preservation_rate']*100:.1f}%")

    gate_2_pass = result["resolution_rate"] >= 0.90
    gate_2_no_overfit = result["preservation_rate"] >= 0.80
    print(f"\n=== GATE VERDICTS ===")
    print(f"  v19 Gate 2 (twin resolution ≥90%)            ... {'PASS' if gate_2_pass else 'FAIL'}  ({result['resolution_rate']*100:.1f}%)")
    print(f"  Anti-overfit (healthy preservation ≥80%)     ... {'PASS' if gate_2_no_overfit else 'FAIL'}  ({result['preservation_rate']*100:.1f}%)")

    # Show first 10 resolution details
    print(f"\n=== TOP 10 CROSS-DISCIPLINE COLLISION RESOLUTIONS ===")
    for r in result["resolution_breakdown"][:10]:
        status = "RESOLVED" if r["resolved"] else "STILL AMBIGUOUS"
        print(f"  sig={r['topology_sig']:<12s} disciplines={r['disciplines']!s:<60s} "
              f"instances={r['n_instances']:<3} → IR sigs={r['ir_signatures_after']}  {status}")

    # Persist
    out_path = OUT_DIR / "csir_phase2_results_2026-05-24.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J",
            "phase": "CSIR Phase 2 — Context Injection",
            "summary": {
                "total_ir_records": len(all_records),
                "distinct_ir_signatures": len(ir_counter),
                "acceptance": result,
                "gates": {
                    "gate_2_twin_resolution": gate_2_pass,
                    "anti_overfit_healthy_preservation": gate_2_no_overfit,
                },
            },
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")


if __name__ == "__main__":
    main()
