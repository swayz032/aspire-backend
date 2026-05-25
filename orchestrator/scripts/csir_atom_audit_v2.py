"""Wave 20.14.5J-A-fix — Atom Audit v2 with operational_system + ARI redundancy.

Per v21 mandate, fixes two issues from v1 audit:

  ISSUE 1: system_membership was discipline-derived (zero mutual info)
    → REPLACE with operational_system_context extracted from sheet text
      (sheet_code patterns, title block keywords, legend text)
    → Now independently informative

  ISSUE 2: redundancy metric used signature string equality (broken)
    → REPLACE with Adjusted Rand Index over partition assignments
    → Detects partition-equivalent atoms regardless of label naming

Then:
  - Build pairwise ARI matrix across all atoms (IR health dashboard)
  - Run full audit on 15-collision corpus with reduced atom set
  - Acceptance per v21:
      Resolution ≥100%
      Preservation ≥90%
      All pairwise ARI <0.85
      Drift sensitivity <10%

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
from canonicalize_local import extract_lines, canonicalize  # noqa: E402
from semantic_twins_probe import (  # noqa: E402
    find_symbol_field_tiles, extract_candidate_graphs, DISCIPLINE,
)
from csir_phase2_context import (  # noqa: E402
    discipline_context_atom, sheet_position_atom,
    scale_class_from_candidate, legend_alignment_atom,
)

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

ATOMS = ["discipline", "position", "operational_system", "scale"]


# ============================================================
# Operational system patterns (independent of discipline)
# ============================================================

SYSTEM_PATTERNS = {
    "fire_alarm": [
        r"\bFIRE\s*ALARM\b", r"\bSMOKE\s*DETECT\w*", r"\bFA[-_\s]?\d",
    ],
    "fire_protection": [
        r"\bFIRE\s*PROTECTION\b", r"\bSPRINKLER\b", r"\bSTANDPIPE\b",
        r"\bFP[-_\s]?\d", r"\bWET\s*SYSTEM\b", r"\bDRY\s*SYSTEM\b",
    ],
    "lighting": [
        r"\bLIGHTING\b", r"\bLUMINAIRE\b", r"\bPHOTOMETRIC\b",
        r"\bLP[-_\s]?\d", r"\bLP1\b", r"\bLP2\b",
    ],
    "site_lighting": [
        r"\bSITE\s*LIGHTING\b", r"\bLIGHT\s*POLE\b", r"\bPOLE\s*PLAN\b",
    ],
    "power": [
        r"\bPOWER\s*PLAN\b", r"\bPANEL\s*SCHEDULE\b", r"\bONE[-\s]?LINE\b",
        r"\bRECEPTACLE\b",
    ],
    "low_voltage": [
        r"\bLOW\s*VOLTAGE\b", r"\bTELECOM\b", r"\bSECURITY\b",
        r"\bDATA\s*PLAN\b", r"\bACCESS\s*CONTROL\b", r"\bAV\b",
    ],
    "hvac": [
        r"\bHVAC\b", r"\bDUCTWORK\b", r"\bDIFFUSER\b", r"\bSUPPLY\s*AIR\b",
        r"\bRETURN\s*AIR\b", r"\bM[-_\s]?\d{2,3}\b",
    ],
    "sanitary": [
        r"\bSANITARY\b", r"\bWASTE\b", r"\bVENT\b", r"\bDRAIN\s*PLAN\b",
    ],
    "water_supply": [
        r"\bDOMESTIC\s*WATER\b", r"\bHOT\s*WATER\b", r"\bCOLD\s*WATER\b",
        r"\bWATER\s*SERVICE\b",
    ],
    "drainage": [
        r"\bDRAINAGE\b", r"\bSTORMWATER\b", r"\bCATCH\s*BASIN\b",
        r"\bPRECAST\b", r"\bMANHOLE\b",
    ],
    "site_grading": [
        r"\bGRADING\b", r"\bELEVATION\b", r"\bCONTOUR\b", r"\bGSM\b",
    ],
    "structural": [
        r"\bFOUNDATION\b", r"\bSTRUCTURAL\b", r"\bFRAMING\b",
        r"\bCONCRETE\s*BLUE\w*\b",
    ],
    "general_mep": [
        r"\bMEP\b", r"\bMECHANICAL[-\s]?ELECTRICAL[-\s]?PLUMBING\b",
    ],
}


def extract_pdf_text(pdf_path: Path) -> str:
    """Concatenate all text from PDF — used for operational_system pattern matching."""
    try:
        doc = fitz.open(str(pdf_path))
        chunks: list[str] = []
        for pg in range(min(doc.page_count, 6)):  # cap per-PDF text scan
            chunks.append(doc[pg].get_text() or "")
        doc.close()
        return "\n".join(chunks).upper()
    except Exception:
        return ""


def detect_operational_system(pdf_text: str, pdf_name: str) -> dict:
    """Match patterns against PDF text + filename. Return scored system."""
    haystack = pdf_text + "\n" + pdf_name.upper()
    scores: dict[str, int] = defaultdict(int)
    evidence: dict[str, list[str]] = defaultdict(list)
    for system, patterns in SYSTEM_PATTERNS.items():
        for pat in patterns:
            matches = re.findall(pat, haystack)
            if matches:
                scores[system] += len(matches)
                evidence[system].extend([m if isinstance(m, str) else str(m) for m in matches[:3]])
    if not scores:
        return {
            "primary_system": "unknown",
            "score": 0,
            "confidence": 0.10,
            "evidence": [],
        }
    primary = max(scores.items(), key=lambda kv: kv[1])
    total = sum(scores.values()) or 1
    return {
        "primary_system": primary[0],
        "score": primary[1],
        "confidence": round(min(0.95, 0.4 + 0.5 * (primary[1] / total)), 3),
        "evidence": evidence[primary[0]][:5],
        "all_scores": dict(scores),
    }


# ============================================================
# Adjusted Rand Index (manual implementation)
# ============================================================

def comb2(n: int) -> int:
    return n * (n - 1) // 2 if n >= 2 else 0


def adjusted_rand_index(labels_a: list, labels_b: list) -> float:
    """ARI between two partition assignments. 1.0 = identical, ~0 = random."""
    if len(labels_a) != len(labels_b) or len(labels_a) == 0:
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


# ============================================================
# Build IR records with new atom set
# ============================================================

def build_records() -> list[dict]:
    records: list[dict] = []
    pdf_op_systems: dict[str, dict] = {}

    for pdf in sorted(BLUEPRINTS.glob("*.pdf")):
        pdf_key = pdf.stem
        try:
            text = extract_pdf_text(pdf)
            op_sys = detect_operational_system(text, pdf_key)
            pdf_op_systems[pdf_key] = op_sys

            prims = extract_lines(str(pdf))
            synthetic, _, _ = canonicalize(prims)
            all_prims = prims + synthetic
            sym_tiles = find_symbol_field_tiles(all_prims)
            candidates = extract_candidate_graphs(sym_tiles)
        except Exception as exc:
            print(f"  ERR {pdf.name}: {type(exc).__name__}: {exc}")
            continue

        page_extents: dict[int, tuple[float, float]] = {}
        for p in all_prims:
            page = p["page"]
            prev = page_extents.get(page, (0.0, 0.0))
            page_extents[page] = (max(prev[0], p["cx"]), max(prev[1], p["cy"]))

        disc = discipline_context_atom(pdf_key)

        for c in candidates:
            pe = page_extents.get(c["page"], (1.0, 1.0))
            pos = sheet_position_atom(c, pe)
            scl = scale_class_from_candidate(c)
            records.append({
                "pdf_key": pdf_key,
                "topology_sig": c["topology_sig"],
                "discipline": disc["discipline"],
                "project": disc["project"],
                "position": pos["position"],
                "operational_system": op_sys["primary_system"],  # NEW
                "operational_system_conf": op_sys["confidence"],
                "scale_class": scl["scale_class"],
                "page": c["page"],
                "n_prims": c["n_prims"],
            })
        print(f"  {pdf.name[:48]:<48s} disc={disc['discipline']:<18s} "
              f"op_sys={op_sys['primary_system']:<18s} ({op_sys['confidence']:.2f})  → {len(candidates)} cand")
    return records, pdf_op_systems


# ============================================================
# Audit measurements (using ARI for redundancy)
# ============================================================

def make_signature(topology_sig: str, atom_values: dict, included: list[str]) -> str:
    parts = [topology_sig]
    for a in ATOMS:
        if a in included:
            parts.append(str(atom_values.get(a, "_")))
        else:
            parts.append("_OMITTED_")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def atom_values_for(r: dict) -> dict:
    return {
        "discipline": r["discipline"],
        "position": r["position"],
        "operational_system": r["operational_system"],
        "scale": r["scale_class"],
    }


def measure_partition(records: list[dict], included: list[str]) -> dict:
    """Compute resolution + preservation for a given atom subset."""
    sig_per_rec: list[str] = []
    for r in records:
        ir = make_signature(r["topology_sig"], atom_values_for(r), included)
        sig_per_rec.append(ir)

    # Group by topology_sig and inspect IR-split
    topo_to_recs: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        topo_to_recs[r["topology_sig"]].append(i)

    cross_disc_total = 0
    cross_disc_resolved = 0
    healthy_total = 0
    healthy_preserved = 0
    for topo_sig, idxs in topo_to_recs.items():
        if len(idxs) < 2:
            continue
        disciplines = {records[i]["discipline"] for i in idxs}
        projects = {records[i]["project"] for i in idxs}
        ir_sigs = {sig_per_rec[i] for i in idxs}
        if len(disciplines) > 1:
            cross_disc_total += 1
            if len(ir_sigs) >= 2:
                cross_disc_resolved += 1
        elif len(disciplines) == 1 and len(projects) == 1:
            healthy_total += 1
            if len(ir_sigs) == 1:
                healthy_preserved += 1

    return {
        "atoms_used": list(included),
        "cross_disc_total": cross_disc_total,
        "cross_disc_resolved": cross_disc_resolved,
        "resolution_rate": round(cross_disc_resolved / max(cross_disc_total, 1), 4),
        "healthy_total": healthy_total,
        "healthy_preserved": healthy_preserved,
        "preservation_rate": round(healthy_preserved / max(healthy_total, 1), 4),
        "distinct_ir_sigs": len(set(sig_per_rec)),
        "_sig_per_rec": sig_per_rec,
    }


def per_atom_audit(records: list[dict]) -> dict:
    full = measure_partition(records, ATOMS[:])
    topo_only = measure_partition(records, [])

    per_atom: dict[str, dict] = {}
    for atom in ATOMS:
        without = measure_partition(records, [a for a in ATOMS if a != atom])
        solo = measure_partition(records, [atom])

        # drift sensitivity
        atom_key_map = {
            "discipline": "discipline", "position": "position",
            "operational_system": "operational_system", "scale": "scale_class",
        }
        ak = atom_key_map[atom]
        topo_vals: dict[str, list[str]] = defaultdict(list)
        for r in records:
            topo_vals[r["topology_sig"]].append(str(r.get(ak, "")))
        multi = [(s, vs) for s, vs in topo_vals.items() if len(vs) >= 2]
        if multi:
            drifting = sum(1 for s, vs in multi if len(set(vs)) > 1)
            drift = drifting / len(multi)
        else:
            drift = 0.0

        false_partition = max(0.0, topo_only["preservation_rate"] - solo["preservation_rate"])
        info_gain = full["resolution_rate"] - without["resolution_rate"]

        per_atom[atom] = {
            "information_gain": round(info_gain, 4),
            "standalone_resolution": round(solo["resolution_rate"], 4),
            "standalone_preservation": round(solo["preservation_rate"], 4),
            "drift_sensitivity": round(drift, 4),
            "false_partition_rate": round(false_partition, 4),
        }

    return {
        "baseline_topology_only": topo_only,
        "baseline_full_atoms": full,
        "per_atom": per_atom,
    }


def pairwise_ari_matrix(records: list[dict]) -> dict:
    """Solo-atom partition assignments + pairwise ARI."""
    solo_labels: dict[str, list[str]] = {}
    for atom in ATOMS:
        labels = []
        for r in records:
            ir = make_signature(r["topology_sig"], atom_values_for(r), [atom])
            labels.append(ir)
        solo_labels[atom] = labels

    matrix = {}
    for a in ATOMS:
        matrix[a] = {}
        for b in ATOMS:
            if a == b:
                matrix[a][b] = 1.0
                continue
            ari = adjusted_rand_index(solo_labels[a], solo_labels[b])
            matrix[a][b] = ari
    return matrix


def verdict_per_atom(per_atom: dict, ari_matrix: dict) -> dict:
    """v21 admission gates: standalone>5%, preservation>85%, ARI<0.85, drift<15%, false<8%."""
    verdicts = {}
    for atom, m in per_atom.items():
        flags = []
        # Find peak ARI with any other atom
        max_ari = 0.0
        peer = None
        for other, ari in ari_matrix.get(atom, {}).items():
            if other == atom:
                continue
            if ari > max_ari:
                max_ari = ari
                peer = other

        if m["standalone_resolution"] < 0.05:
            flags.append("LOW_STANDALONE_RESOLUTION")
        if m["standalone_preservation"] < 0.85:
            flags.append("LOW_PRESERVATION")
        if max_ari >= 0.85:
            flags.append(f"REDUNDANT_WITH_{peer}_ari={max_ari:.2f}")
        if m["drift_sensitivity"] > 0.15:
            flags.append("HIGH_DRIFT")
        if m["false_partition_rate"] > 0.08:
            flags.append("OVER_PARTITIONING")

        if not flags:
            verdict = "KEEP"
        elif any("REDUNDANT" in f or "LOW_STANDALONE" in f or "OVER_PARTITIONING" in f for f in flags):
            verdict = "REJECT"
        else:
            verdict = "FLAG"
        verdicts[atom] = {"verdict": verdict, "flags": flags, "max_pairwise_ari": round(max_ari, 4)}
    return verdicts


def main() -> None:
    print("=== Wave 20.14.5J-A-fix Atom Audit v2 (operational_system + ARI) ===\n")
    print("--- Building IR records with operational_system_context ---")
    records, pdf_op = build_records()
    print(f"\ntotal IR records: {len(records)}")

    print(f"\n=== OPERATIONAL SYSTEM ASSIGNMENT PER PDF ===")
    for k, v in pdf_op.items():
        ev_str = ", ".join(str(e) for e in v.get("evidence", [])[:3]) or "—"
        print(f"  {k[:40]:<40s} → {v['primary_system']:<18s} conf={v['confidence']:.2f}  evidence: {ev_str}")

    print(f"\n--- Audit metrics ---")
    audit = per_atom_audit(records)
    print(f"  baseline topology-only: res={audit['baseline_topology_only']['resolution_rate']*100:.1f}%  "
          f"presv={audit['baseline_topology_only']['preservation_rate']*100:.1f}%")
    print(f"  baseline full atoms:    res={audit['baseline_full_atoms']['resolution_rate']*100:.1f}%  "
          f"presv={audit['baseline_full_atoms']['preservation_rate']*100:.1f}%")

    print(f"\n{'Atom':<22s} {'InfoGain':>9s} {'SoloRes':>9s} {'SoloPres':>9s} {'Drift':>8s} {'FalseP':>8s}")
    print("-" * 78)
    for atom, m in audit["per_atom"].items():
        print(f"{atom:<22s} {m['information_gain']*100:>8.1f}% "
              f"{m['standalone_resolution']*100:>8.1f}% "
              f"{m['standalone_preservation']*100:>8.1f}% "
              f"{m['drift_sensitivity']*100:>7.1f}% "
              f"{m['false_partition_rate']*100:>7.1f}%")

    print(f"\n=== PAIRWISE ARI MATRIX (1.0 = identical partition) ===")
    matrix = pairwise_ari_matrix(records)
    header = " " * 22 + "  ".join(f"{a[:14]:<14s}" for a in ATOMS)
    print(header)
    for a in ATOMS:
        row = f"{a[:22]:<22s}"
        for b in ATOMS:
            row += f"  {matrix[a][b]:<14.4f}"
        print(row)

    verdicts = verdict_per_atom(audit["per_atom"], matrix)
    print(f"\n=== PER-ATOM VERDICTS (v21 admission gates) ===")
    for atom, v in verdicts.items():
        flags_str = ", ".join(v["flags"]) if v["flags"] else "—"
        print(f"  {atom:<22s} {v['verdict']:<7s}  max_pair_ARI={v['max_pairwise_ari']:.3f}  flags: {flags_str}")

    full = audit["baseline_full_atoms"]
    joint_pass = full["resolution_rate"] >= 1.0 and full["preservation_rate"] >= 0.90
    max_pairwise = max(matrix[a][b] for a in ATOMS for b in ATOMS if a != b)
    max_drift = max(audit["per_atom"][a]["drift_sensitivity"] for a in ATOMS)

    print(f"\n=== V21 ACCEPTANCE (resolution≥100%, preservation≥90%, ARI<0.85, drift<10%) ===")
    g1 = full["resolution_rate"] >= 1.0
    g2 = full["preservation_rate"] >= 0.90
    g3 = max_pairwise < 0.85
    g4 = max_drift < 0.10
    print(f"  Gate 1: resolution ≥100%      ... {'PASS' if g1 else 'FAIL'}  ({full['resolution_rate']*100:.1f}%)")
    print(f"  Gate 2: preservation ≥90%     ... {'PASS' if g2 else 'FAIL'}  ({full['preservation_rate']*100:.1f}%)")
    print(f"  Gate 3: max pairwise ARI <0.85 ... {'PASS' if g3 else 'FAIL'}  ({max_pairwise:.4f})")
    print(f"  Gate 4: max drift <10%         ... {'PASS' if g4 else 'FAIL'}  ({max_drift*100:.1f}%)")

    out_path = OUT_DIR / "csir_atom_audit_v2_2026-05-24.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-A-fix",
            "atom_set": ATOMS,
            "pdf_op_systems": pdf_op,
            "baselines": {
                "topology_only": {k: v for k, v in audit["baseline_topology_only"].items() if not k.startswith("_")},
                "full_atoms": {k: v for k, v in audit["baseline_full_atoms"].items() if not k.startswith("_")},
            },
            "per_atom_metrics": audit["per_atom"],
            "pairwise_ari_matrix": matrix,
            "verdicts": verdicts,
            "v21_acceptance": {
                "resolution_pass": g1, "preservation_pass": g2,
                "ari_pass": g3, "drift_pass": g4,
                "all_pass": g1 and g2 and g3 and g4,
            },
        }, f, indent=2)
    print(f"\naudit v2 results: {out_path}")


if __name__ == "__main__":
    main()
