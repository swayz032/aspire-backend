"""Wave 20.14.5K — Adversarial Semantic Twins Probe (within-corpus).

Tests whether SDPE alone can distinguish semantically different graphs
that share similar topology. This is the v18 architectural risk:

  Same topology + different real-world meaning = "semantic twins"

If twins collapse to the same band → SDPE alone is insufficient → IR
layer (Wave 20.14.5J) must add context-aware atoms (discipline, sheet
context, semantic role) BEFORE grammars.

Method:
  1. Per PDF, canonicalize + extract candidate small graphs (3-15 prims
     in SYMBOL_FIELD tiles)
  2. Compute topology signature per candidate (composition hash)
  3. Build cross-PDF signature collision map
  4. Categorize collisions:
     - same_discipline_same_project = expected (real recurrence)
     - cross_discipline = SEMANTIC TWIN RISK (sprinkler/detector/receptacle)
     - cross_project_same_discipline = OK (same symbol family across projects)
     - cross_everything = HARD COLLISION (need context to disambiguate)

Zero Supabase IO.
"""
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from canonicalize_local import (  # noqa: E402
    extract_lines, canonicalize, TILE_PT, _angle_rad,
)

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

# Discipline tags per PDF (from corpus metadata)
DISCIPLINE = {
    # CORRECTED 2026-05-24 per user ground truth
    "21030 ENG_Rev1 240801_Signed": ("Gulfstream", "MEP"),
    "29187P-ES (3)": ("BMI_metal", "metal_building"),       # CORRECTED: user-confirmed metal building (text-free vectorized PDF, filename heuristic was misleading)
    "Open 29187P-ES (3)": ("BMI_metal", "metal_building"),  # CORRECTED: same content variant
    "Precast Drainage": ("Precast", "mechanical_shop"),
    "Concrete blueprints Mangonia Park": ("Mangonia", "structural"),
    "E-1 (1)": ("E_project", "electrical"),
    "E-2 (1)": ("E_project", "electrical"),
    "E-3 (1)": ("E_project", "electrical"),
    "E-4 (1)": ("E_project", "electrical"),
    "P-1 (1)": ("E_project", "plumbing"),
    "21030 ENG-C2.2 GSM": ("21030", "civil"),
    "21030 ENG-C2.2 GSM (1)": ("21030", "civil"),
    "2101LP-R1-LP1-1of1 (11) (7)": ("2101LP", "site_lighting"),
    "Revised light pole plan": ("light_pole", "site_lighting"),
}

# Candidate extraction parameters (lightweight SAGG-like)
_TILE_PT_LOCAL = 50.0
_CLUSTER_RADIUS_PT = 25.0
_MIN_GRAPH = 3
_MAX_GRAPH = 15
_MIN_VISIBLE_PRIMS = 3


def find_symbol_field_tiles(prims: list[dict]) -> dict[tuple, list[dict]]:
    """Per-(page, tile) groups of primitives in SYMBOL_FIELD-like tiles.
    Lightweight: SYMBOL = has curves, low parallelism, modest density."""
    tiles: dict[tuple, list[dict]] = defaultdict(list)
    for p in prims:
        page = p["page"]
        tx = int(p["cx"] // _TILE_PT_LOCAL)
        ty = int(p["cy"] // _TILE_PT_LOCAL)
        tiles[(page, tx, ty)].append(p)

    sym_tiles: dict[tuple, list[dict]] = {}
    for key, group in tiles.items():
        n_total = len(group)
        n_curves = sum(1 for p in group if p["primitive_type"] in ("curve", "synthetic_curve"))
        if n_curves >= 2 and 3 <= n_total <= 60:
            # Quick parallelism check
            line_angles = [
                math.degrees(_angle_rad(p["x0"], p["y0"], p["x1"], p["y1"]))
                for p in group if p["primitive_type"] == "line"
            ]
            if len(line_angles) >= 3:
                bins: dict[int, int] = defaultdict(int)
                for a in line_angles:
                    bins[int(abs(a) // 10) % 18] += 1
                parallelism = max(bins.values()) / len(line_angles)
                if parallelism >= 0.55:
                    continue  # hatching-flavored, skip
            sym_tiles[key] = group
    return sym_tiles


def extract_candidate_graphs(sym_tiles: dict[tuple, list[dict]]) -> list[dict]:
    """Per SYMBOL_FIELD tile, find tightest cluster of 3-15 prims around each curve seed."""
    candidates: list[dict] = []
    for (page, tx, ty), group in sym_tiles.items():
        seeds = [p for p in group if p["primitive_type"] in ("curve", "synthetic_curve")]
        for seed in seeds:
            sx, sy = seed["cx"], seed["cy"]
            nearby = [
                p for p in group
                if math.hypot(p["cx"] - sx, p["cy"] - sy) <= _CLUSTER_RADIUS_PT
            ]
            if _MIN_GRAPH <= len(nearby) <= _MAX_GRAPH:
                # Topology signature: sorted primitive type multiset + role-class
                types = tuple(sorted(p["primitive_type"][:1] for p in nearby))
                # Geometric signature: sorted (length-bucket, type) tuples
                buckets = tuple(sorted(
                    (p["primitive_type"][:1], int(p.get("length", 0) // 5))
                    for p in nearby
                ))
                sig = hashlib.md5(str(buckets).encode()).hexdigest()[:10]
                candidates.append({
                    "page": page, "tile": (tx, ty),
                    "n_prims": len(nearby),
                    "type_multiset": types,
                    "topology_sig": sig,
                    "centroid": (sx, sy),
                    "bbox_w": max(p["cx"] for p in nearby) - min(p["cx"] for p in nearby),
                    "bbox_h": max(p["cy"] for p in nearby) - min(p["cy"] for p in nearby),
                })
    return candidates


def process_pdf(pdf_path: Path) -> dict:
    pdf_key = pdf_path.stem
    project, discipline = DISCIPLINE.get(pdf_key, ("unknown", "unknown"))
    try:
        prims = extract_lines(str(pdf_path))
        synthetic, _, _ = canonicalize(prims)
        all_prims = prims + synthetic
        sym_tiles = find_symbol_field_tiles(all_prims)
        candidates = extract_candidate_graphs(sym_tiles)
        return {
            "pdf": pdf_path.name,
            "pdf_key": pdf_key,
            "project": project,
            "discipline": discipline,
            "n_primitives": len(prims),
            "n_synthetic": len(synthetic),
            "n_symbol_tiles": len(sym_tiles),
            "n_candidates": len(candidates),
            "candidates": candidates,
        }
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}


def categorize_collision(rows_with_sig: list[dict]) -> str:
    """Classify a signature collision pattern."""
    projects = {r["project"] for r in rows_with_sig}
    disciplines = {r["discipline"] for r in rows_with_sig}
    if len(projects) == 1 and len(disciplines) == 1:
        return "same_project_same_discipline"  # expected recurrence
    if len(projects) == 1 and len(disciplines) > 1:
        return "same_project_cross_discipline"  # SEMANTIC TWIN RISK
    if len(projects) > 1 and len(disciplines) == 1:
        return "cross_project_same_discipline"  # symbol family across projects (OK)
    return "cross_everything"  # hardest collision


def main() -> None:
    print("=== Wave 20.14.5K Adversarial Semantic Twins Probe ===\n")
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))

    all_rows: list[dict] = []
    for pdf in pdfs:
        print(f"--- {pdf.name[:50]:<50s} ---")
        row = process_pdf(pdf)
        all_rows.append(row)
        if "error" in row:
            print(f"  ERROR: {row['error']}"); continue
        print(f"  project={row['project']:<12s} discipline={row['discipline']:<18s} "
              f"prims={row['n_primitives']:>7}  synth={row['n_synthetic']:>4}  "
              f"sym_tiles={row['n_symbol_tiles']:>4}  candidates={row['n_candidates']:>4}")

    # Build cross-PDF signature collision map
    sig_to_rows: dict[str, list[dict]] = defaultdict(list)
    for row in all_rows:
        if "error" in row:
            continue
        for c in row["candidates"]:
            sig_to_rows[c["topology_sig"]].append({
                "pdf_key": row["pdf_key"],
                "project": row["project"],
                "discipline": row["discipline"],
                "page": c["page"], "tile": c["tile"],
                "n_prims": c["n_prims"],
                "type_multiset": list(c["type_multiset"]),
                "centroid": c["centroid"],
            })

    # Categorize collisions
    coll_counts: Counter[str] = Counter()
    high_value_collisions = []  # cross-discipline cases (semantic twin risk)
    for sig, rows in sig_to_rows.items():
        if len(rows) < 2:
            continue
        cat = categorize_collision(rows)
        coll_counts[cat] += 1
        if cat in ("same_project_cross_discipline", "cross_everything"):
            high_value_collisions.append({"sig": sig, "category": cat, "rows": rows})

    total_sigs = len(sig_to_rows)
    repeating = sum(1 for rows in sig_to_rows.values() if len(rows) >= 2)

    print(f"\n=== TOPOLOGY SIGNATURE LANDSCAPE ===")
    print(f"distinct signatures: {total_sigs}")
    print(f"repeating (≥2 instances): {repeating}")
    print(f"singletons: {total_sigs - repeating}")

    print(f"\n=== COLLISION CATEGORY DISTRIBUTION ===")
    for cat in ("same_project_same_discipline", "cross_project_same_discipline",
                "same_project_cross_discipline", "cross_everything"):
        n = coll_counts.get(cat, 0)
        marker = "  ← SEMANTIC TWIN RISK" if cat in ("same_project_cross_discipline", "cross_everything") and n > 0 else ""
        print(f"  {cat:<35s} {n:>4}{marker}")

    print(f"\n=== SEMANTIC TWIN RISK ASSESSMENT ===")
    twin_total = coll_counts.get("same_project_cross_discipline", 0) + coll_counts.get("cross_everything", 0)
    if twin_total == 0:
        print(f"  No cross-discipline signature collisions detected.")
        print(f"  SDPE substrate is implicitly discipline-aware via primitive distribution.")
        print(f"  VERDICT: semantic twin risk NOT empirically confirmed at this stage.")
    else:
        print(f"  {twin_total} cross-discipline topology collisions found.")
        print(f"  VERDICT: SDPE alone CANNOT distinguish semantic twins — IR layer required.")
        print(f"\n  EXAMPLES (top 10):")
        for h in high_value_collisions[:10]:
            disciplines = sorted({r["discipline"] for r in h["rows"]})
            n_inst = len(h["rows"])
            print(f"    sig={h['sig']}  category={h['category']}  "
                  f"disciplines={disciplines}  instances={n_inst}  "
                  f"types={h['rows'][0]['type_multiset']}")

    # Cross-PDF same-project recurrence — sanity check
    print(f"\n=== SAME-PROJECT RECURRENCE SANITY ===")
    sp_count = coll_counts.get("same_project_same_discipline", 0)
    print(f"  Same-project-same-discipline collisions: {sp_count}")
    print(f"  (These are EXPECTED — same project should have repeating symbols)")

    # Persist
    out_path = OUT_DIR / "semantic_twins_probe_2026-05-24.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        # Compact: only signature -> categories for replay; drop raw centroid lists for size
        compact = {
            "summary": {
                "total_signatures": total_sigs,
                "repeating": repeating,
                "category_counts": dict(coll_counts),
                "twin_risk_total": twin_total,
            },
            "twin_examples": high_value_collisions[:30],
            "per_pdf_stats": [
                {k: v for k, v in r.items() if k != "candidates"}
                for r in all_rows
            ],
        }
        json.dump(compact, f, indent=2)
    print(f"\nprobe results saved: {out_path}")


if __name__ == "__main__":
    main()
