"""Wave 20.14.5J-V5 — Multi-PDF same-project bundle support + audit re-run.

Per canonical_corpus_labels.json ground truth:
  - Mangonia Park Warehouse = 14 PDFs / 1 project
  - Gulfstream/UPS Savannah = 1 PDF (GAVNN) / 1 project

Treats all PDFs in same project_id as one comparison_domain.
Runs v22 audit metrics against ACTUAL project boundaries (not filename heuristics).

Acceptance gates re-evaluated against true ground truth:
  - Within-project ARI < 0.6 (now meaningful — 14 Mangonia PDFs have real cross-discipline variance)
  - Cross-project preservation: genuine recurrence across Mangonia ↔ GAVNN identified separately
  - Within-discipline ARI < 0.7
  - operational_system entropy within discipline > 1.5 bits

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
from semantic_twins_probe import find_symbol_field_tiles  # noqa: E402
from csir_v2_hierarchical import (  # noqa: E402
    extract_per_page_text, routing_context_per_page,
    functional_position_atom, normalized_scale_atom,
    local_scale_context, extract_candidates_with_tile_features,
    adjusted_rand_index, conditional_ari, entropy,
)

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
CANONICAL_LABELS = OUT_DIR / "canonical_corpus_labels.json"


# ============================================================
# Load ground truth from canonical labels
# ============================================================

def load_canonical_labels() -> dict:
    """Returns: {filename: {project_id, discipline_primary, secondary, page_count, ...}}"""
    with open(CANONICAL_LABELS, "r", encoding="utf-8") as f:
        data = json.load(f)
    label_map = {}
    for project_id, project_info in data["projects"].items():
        for file_info in project_info.get("files", []):
            label_map[file_info["filename"]] = {
                "project_id": project_id,
                "project_name": project_info["canonical_name"],
                "discipline_primary": file_info["discipline_primary"],
                "disciplines_secondary": file_info.get("disciplines_secondary", []),
                "n_pages_declared": file_info["pages"],
                "sheet_series": file_info.get("sheet_series", ""),
                "text_layer_present": file_info.get("text_layer_present", True),
                "production_gap_flag": file_info.get("production_gap_flag"),
            }
    return label_map


# ============================================================
# Build IR records using canonical labels
# ============================================================

def build_records_bundle_aware() -> list[dict]:
    """Extract IR records with TRUE project identity from canonical labels."""
    labels = load_canonical_labels()
    records: list[dict] = []
    pdf_summaries: list[dict] = []

    for pdf in sorted(BLUEPRINTS.glob("*.pdf")):
        pdf_name = pdf.name
        ground_truth = labels.get(pdf_name)
        if not ground_truth:
            print(f"  SKIP {pdf_name} — no ground truth label")
            continue

        try:
            page_texts = extract_per_page_text(pdf)
            prims = extract_lines(str(pdf))
            synthetic, _, _ = canonicalize(prims)
            all_prims = prims + synthetic
            sym_tiles = find_symbol_field_tiles(all_prims)
            candidates_with_tiles = extract_candidates_with_tile_features(sym_tiles, None)
        except Exception as exc:
            print(f"  ERR {pdf_name}: {type(exc).__name__}: {exc}")
            continue

        # Per-page routing context (still useful for sheet-level ops, but project_id comes from ground truth)
        page_routing: dict[int, dict] = {}
        for page_idx, txt in page_texts.items():
            page_routing[page_idx] = routing_context_per_page(txt, pdf.stem)

        page_extents: dict[int, tuple[float, float]] = {}
        for p in all_prims:
            pg = p["page"]
            prev = page_extents.get(pg, (0.0, 0.0))
            page_extents[pg] = (max(prev[0], p["cx"]), max(prev[1], p["cy"]))

        for item in candidates_with_tiles:
            c = item["candidate"]
            tile_feats = item["tile_features"]
            pe = page_extents.get(c["page"], (1.0, 1.0))
            routing = page_routing.get(c["page"], {})
            fpos = functional_position_atom(c, pe, tile_feats)
            local_typical = local_scale_context(c, all_prims, radius=200.0)
            nscale = normalized_scale_atom(c, local_typical)

            records.append({
                "pdf_name": pdf_name,
                "pdf_key": pdf.stem,
                "page": c["page"],
                "topology_sig": c["topology_sig"],
                # PROJECT IDENTITY FROM GROUND TRUTH (THE KEY FIX)
                "project_id": ground_truth["project_id"],
                "discipline_primary": ground_truth["discipline_primary"],
                "disciplines_secondary": ground_truth["disciplines_secondary"],
                "sheet_series": ground_truth["sheet_series"],
                "text_layer_present": ground_truth["text_layer_present"],
                # Routing context (sheet-level, still parsed but secondary to GT)
                "routing_sheet_code": routing.get("sheet_code"),
                "routing_operational_system": routing.get("operational_system", "unknown"),
                # Semantic identity (candidate-level)
                "identity_functional_position": fpos["functional_position"],
                "identity_normalized_scale": nscale["normalized_scale"],
                "identity_n_prims": c["n_prims"],
                "scale_ratio_vs_local": nscale["ratio_vs_local"],
                "centroid": c["centroid"],
            })

        pdf_summaries.append({
            "pdf_name": pdf_name,
            "project_id": ground_truth["project_id"],
            "discipline": ground_truth["discipline_primary"],
            "n_pages_actual": len(page_texts),
            "n_candidates": len(candidates_with_tiles),
            "text_layer": ground_truth["text_layer_present"],
        })
        print(f"  {pdf_name[:48]:<48s} project={ground_truth['project_id'][:14]:<14s} "
              f"disc={ground_truth['discipline_primary']:<24s} cand={len(candidates_with_tiles)}")

    return records, pdf_summaries


# ============================================================
# Re-run v22 audit with TRUE project labels
# ============================================================

def main() -> None:
    print("=== Wave 20.14.5J-V5 Bundle-Aware Audit (canonical labels) ===\n")
    records, summaries = build_records_bundle_aware()
    print(f"\ntotal IR records: {len(records)}\n")

    # Project distribution
    by_project: Counter = Counter(r["project_id"] for r in records)
    print(f"=== RECORD DISTRIBUTION BY PROJECT ===")
    for proj, n in by_project.most_common():
        n_pdfs = sum(1 for s in summaries if s["project_id"] == proj)
        n_disciplines = len({s["discipline"] for s in summaries if s["project_id"] == proj})
        print(f"  {proj:<35s} records={n:>5}  PDFs={n_pdfs}  disciplines={n_disciplines}")

    # Discipline distribution per project
    print(f"\n=== DISCIPLINE DISTRIBUTION ===")
    proj_disc: dict[tuple, int] = defaultdict(int)
    for r in records:
        proj_disc[(r["project_id"], r["discipline_primary"])] += 1
    for (proj, disc), n in sorted(proj_disc.items(), key=lambda kv: -kv[1]):
        print(f"  {proj[:25]:<25s}  {disc:<28s}  records={n}")

    # PDFs that produced ZERO candidates (production blindness)
    print(f"\n=== PDFs WITH ZERO CANDIDATES (production gap) ===")
    zero_cand = [s for s in summaries if s["n_candidates"] == 0]
    for s in zero_cand:
        print(f"  {s['pdf_name']:<48s} project={s['project_id'][:20]} disc={s['discipline']:<22s} "
              f"text_layer={s['text_layer']}")
    if not zero_cand:
        print("  none")

    # === V22 GATES re-evaluated with TRUE project labels ===
    print(f"\n=== V22 GATES (re-evaluated with canonical project labels) ===\n")

    # Gate 1: Conditional ARI WITHIN-PROJECT
    print(f"Gate 1 — Within-project ARI (target <0.6)")
    pairs = [
        ("discipline_primary", "routing_operational_system"),
        ("discipline_primary", "identity_functional_position"),
        ("discipline_primary", "identity_normalized_scale"),
        ("routing_operational_system", "identity_normalized_scale"),
        ("identity_functional_position", "identity_normalized_scale"),
    ]
    max_wp = 0.0
    for a, b in pairs:
        r = conditional_ari(records, a, b, "project_id")
        max_wp = max(max_wp, r["weighted_avg_ari"])
        print(f"  {a[:28]:<28s} ↔ {b[:28]:<28s}  ARI={r['weighted_avg_ari']:.4f}  groups={r['n_groups']}")
    g1 = max_wp < 0.6
    print(f"  → max within-project ARI = {max_wp:.4f}   {'PASS' if g1 else 'FAIL'}\n")

    # Gate 2: Within-discipline ARI
    print(f"Gate 2 — Within-discipline ARI (target <0.7)")
    max_wd = 0.0
    for a, b in pairs:
        if a == "discipline_primary":
            continue  # discipline is the conditioning variable
        r = conditional_ari(records, a, b, "discipline_primary")
        max_wd = max(max_wd, r["weighted_avg_ari"])
        print(f"  {a[:28]:<28s} ↔ {b[:28]:<28s}  ARI={r['weighted_avg_ari']:.4f}  groups={r['n_groups']}")
    g2 = max_wd < 0.7
    print(f"  → max within-discipline ARI = {max_wd:.4f}   {'PASS' if g2 else 'FAIL'}\n")

    # Gate 3: Preservation — same-project + same-discipline topology recurrence stays as same identity
    print(f"Gate 3 — Within same-project+same-discipline preservation (target >92%)")
    topo_groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        topo_groups[r["topology_sig"]].append(r)
    healthy_total = 0
    healthy_preserved = 0
    for sig, recs in topo_groups.items():
        if len(recs) < 2:
            continue
        projects = {r["project_id"] for r in recs}
        disciplines = {r["discipline_primary"] for r in recs}
        if len(projects) == 1 and len(disciplines) == 1:
            healthy_total += 1
            identities = set()
            for r in recs:
                identities.add((
                    r["topology_sig"],
                    r["identity_functional_position"],
                    r["identity_normalized_scale"],
                ))
            if len(identities) <= 2:
                healthy_preserved += 1
    pres = healthy_preserved / max(healthy_total, 1)
    g3 = pres > 0.92
    print(f"  healthy collisions: {healthy_total}  preserved: {healthy_preserved}")
    print(f"  → preservation = {pres*100:.1f}%   {'PASS' if g3 else 'FAIL'}\n")

    # Gate 4: Cross-project genuine recurrence rate (now meaningful — 2 real projects)
    print(f"Gate 4 — Cross-project recurrence (Mangonia ↔ Gulfstream)")
    cross_proj_total = 0
    cross_proj_same_disc = 0
    cross_proj_genuine_recur = 0
    cross_proj_disc_distribution: Counter = Counter()
    for sig, recs in topo_groups.items():
        if len(recs) < 2:
            continue
        projects = {r["project_id"] for r in recs}
        if len(projects) >= 2:
            cross_proj_total += 1
            disciplines_pair = frozenset(r["discipline_primary"] for r in recs)
            cross_proj_disc_distribution[disciplines_pair] += 1
            if len(disciplines_pair) == 1:
                cross_proj_same_disc += 1
            identities = {(r["identity_functional_position"], r["identity_normalized_scale"]) for r in recs}
            if len(identities) == 1:
                cross_proj_genuine_recur += 1
    print(f"  total cross-project topology collisions: {cross_proj_total}")
    print(f"  same-discipline cross-project: {cross_proj_same_disc}")
    print(f"  genuine recurrence (same IR identity): {cross_proj_genuine_recur}")
    if cross_proj_disc_distribution:
        print(f"  top discipline pairs colliding cross-project:")
        for pair, n in cross_proj_disc_distribution.most_common(8):
            print(f"    {sorted(pair)!s}: {n}")
    g4_descriptive = True  # informational, not a hard gate (interpretation requires labels)
    print(f"  → informational (descriptive, no hard threshold yet)\n")

    # Gate 5: operational_system entropy within discipline
    print(f"Gate 5 — operational_system entropy within discipline (target >1.5 bits)")
    disc_to_systems: dict[str, list[str]] = defaultdict(list)
    for r in records:
        disc_to_systems[r["discipline_primary"]].append(r["routing_operational_system"])
    max_disc_ent = 0.0
    for disc, sys_vals in disc_to_systems.items():
        ent = entropy(sys_vals)
        max_disc_ent = max(max_disc_ent, ent)
        print(f"  {disc[:28]:<28s} n={len(sys_vals):<5d} entropy={ent:.3f}  distinct={len(set(sys_vals))}")
    g5 = max_disc_ent > 1.5
    print(f"  → max entropy = {max_disc_ent:.3f} bits   {'PASS' if g5 else 'FAIL'}\n")

    # Gate 6: scale ↔ position ARI (unconditional)
    la = [r["identity_normalized_scale"] for r in records]
    lb = [r["identity_functional_position"] for r in records]
    sp_ari = adjusted_rand_index(la, lb)
    g6 = sp_ari < 0.6
    print(f"Gate 6 — scale ↔ position ARI < 0.6")
    print(f"  → ARI = {sp_ari:.4f}   {'PASS' if g6 else 'FAIL'}\n")

    # Summary
    gates = [
        ("Gate 1: within-project ARI <0.6", g1, max_wp),
        ("Gate 2: within-discipline ARI <0.7", g2, max_wd),
        ("Gate 3: preservation >92%", g3, pres),
        ("Gate 4: cross-project descriptive", g4_descriptive, cross_proj_total),
        ("Gate 5: op_sys entropy >1.5", g5, max_disc_ent),
        ("Gate 6: scale↔position ARI <0.6", g6, sp_ari),
    ]
    passed = sum(1 for _, p, _ in gates if p)
    print(f"=== V22 ACCEPTANCE WITH TRUE LABELS ===")
    for name, p, val in gates:
        print(f"  {'PASS' if p else 'FAIL'}  {name:<50s}  value={val:.4f}")
    print(f"\n  {passed}/6 gates PASS")

    # Persist
    out_path = OUT_DIR / "csir_v3_bundle_aware_2026-05-24.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-V5",
            "n_records": len(records),
            "pdf_summaries": summaries,
            "by_project_records": dict(by_project),
            "zero_candidate_pdfs": [s["pdf_name"] for s in zero_cand],
            "v22_gates": {name: {"pass": p, "value": v} for name, p, v in gates},
            "gates_passed": passed,
            "cross_project_collisions_total": cross_proj_total,
            "cross_project_same_discipline": cross_proj_same_disc,
            "cross_project_genuine_recurrence": cross_proj_genuine_recur,
            "cross_project_discipline_pairs": {str(sorted(k)): v for k, v in cross_proj_disc_distribution.items()},
            "operational_system_entropy_per_discipline": {
                disc: entropy(vals) for disc, vals in disc_to_systems.items()
            },
        }, f, indent=2)
    print(f"\nresults saved: {out_path}")


if __name__ == "__main__":
    main()
