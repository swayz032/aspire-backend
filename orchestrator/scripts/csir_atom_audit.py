"""Wave 20.14.5J-A — Atom Orthogonality Audit on Phase 2 atoms.

Per v20 mandate: before Phase 3 ships, audit each Phase 2 atom against the
adversarial corpus (15-collision set + 239 healthy recurrence cases).

For each atom a_i in {discipline, position, system, scale, legend}, measure:
  1. Information gain     — Δresolution_rate when a_i removed from signature
  2. Redundancy           — signature-partition overlap with other atoms
  3. Drift sensitivity    — variance of atom assignment across same-topology instances
  4. False partition rate — % healthy recurrence broken by atom alone
  5. Resolution dependency — standalone discriminative power (atom alone w/ topology)

Verdict per atom: KEEP / FLAG / REMOVE based on combined metrics.

Joint signal locked: resolution_rate AND preservation_rate (not just one).
"""
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from canonicalize_local import extract_lines, canonicalize  # noqa: E402
from semantic_twins_probe import (  # noqa: E402
    find_symbol_field_tiles, extract_candidate_graphs, DISCIPLINE,
)
from csir_phase2_context import (  # noqa: E402
    discipline_context_atom, sheet_position_atom, system_membership_atom,
    scale_class_from_candidate, legend_alignment_atom,
)

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

ATOMS = ["discipline", "position", "system", "scale", "legend"]


# ============================================================
# Signature builders for ablation configurations
# ============================================================

def make_signature(topology_sig: str, atom_values: dict, included: list[str]) -> str:
    """Build IR signature with only `included` atoms (ablation)."""
    parts = [topology_sig]
    for a in ATOMS:  # canonical order
        if a in included:
            parts.append(str(atom_values.get(a, "_")))
        else:
            parts.append("_OMITTED_")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def measure_partition(records: list[dict], included: list[str]) -> dict:
    """For a given atom subset, compute resolution + preservation metrics."""
    sig_to_rows: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        atom_values = {
            "discipline": r["discipline"],
            "position": r["position"],
            "system": r["system"],
            "scale": r["scale_class"],
            "legend": r["legend_match_score"],  # numeric or None — Phase 2 returns 0/None
        }
        ir_sig = make_signature(r["topology_sig"], atom_values, included)
        r["_tmp_sig"] = ir_sig
        sig_to_rows[ir_sig].append(r)

    # Topology-grouped baseline (what would collide without ANY atoms)
    topo_to_rows: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        topo_to_rows[r["topology_sig"]].append(r)

    cross_disc_total = 0
    cross_disc_resolved = 0
    healthy_total = 0
    healthy_preserved = 0

    for topo_sig, recs in topo_to_rows.items():
        if len(recs) < 2:
            continue
        disciplines = {r["discipline"] for r in recs}
        projects = {r["project"] for r in recs}

        # After ablation, how many distinct IR sigs does this topology group split into?
        ir_sigs_within = {r["_tmp_sig"] for r in recs}
        split_count = len(ir_sigs_within)

        if len(disciplines) > 1:
            cross_disc_total += 1
            if split_count >= 2:
                cross_disc_resolved += 1
        elif len(disciplines) == 1 and len(projects) == 1:
            healthy_total += 1
            if split_count == 1:
                healthy_preserved += 1

    return {
        "atoms_used": included,
        "cross_disc_total": cross_disc_total,
        "cross_disc_resolved": cross_disc_resolved,
        "resolution_rate": round(cross_disc_resolved / max(cross_disc_total, 1), 4),
        "healthy_total": healthy_total,
        "healthy_preserved": healthy_preserved,
        "preservation_rate": round(healthy_preserved / max(healthy_total, 1), 4),
        "distinct_ir_sigs": len({r["_tmp_sig"] for r in records}),
    }


# ============================================================
# Build IR records (reusing Phase 2 pipeline)
# ============================================================

def build_records() -> list[dict]:
    records: list[dict] = []
    for pdf in sorted(BLUEPRINTS.glob("*.pdf")):
        pdf_key = pdf.stem
        try:
            prims = extract_lines(str(pdf))
            synthetic, _, _ = canonicalize(prims)
            all_prims = prims + synthetic
            sym_tiles = find_symbol_field_tiles(all_prims)
            candidates = extract_candidate_graphs(sym_tiles)
        except Exception as exc:
            print(f"  ERR {pdf.name}: {exc}")
            continue

        page_extents: dict[int, tuple[float, float]] = {}
        for p in all_prims:
            page = p["page"]
            prev = page_extents.get(page, (0.0, 0.0))
            page_extents[page] = (max(prev[0], p["cx"]), max(prev[1], p["cy"]))

        disc = discipline_context_atom(pdf_key)
        sys_atom = system_membership_atom(disc["discipline"])

        for c in candidates:
            pe = page_extents.get(c["page"], (1.0, 1.0))
            pos = sheet_position_atom(c, pe)
            scl = scale_class_from_candidate(c)
            leg = legend_alignment_atom(c)
            records.append({
                "pdf_key": pdf_key,
                "topology_sig": c["topology_sig"],
                "discipline": disc["discipline"],
                "project": disc["project"],
                "position": pos["position"],
                "system": sys_atom["primary_system"],
                "scale_class": scl["scale_class"],
                "legend_match_score": leg["match_score"],
                "page": c["page"],
                "n_prims": c["n_prims"],
            })
        print(f"  {pdf.name[:50]:<50s} → {len(candidates)} candidates")
    return records


# ============================================================
# Per-atom audit metrics
# ============================================================

def audit_atoms(records: list[dict]) -> dict:
    """Run ablation and standalone tests, compute 5 metrics per atom."""
    # Baseline: ALL atoms
    full = measure_partition(records, ATOMS[:])

    # Topology-only baseline (no atoms): everything depends on topology alone
    topo_only = measure_partition(records, [])

    per_atom: dict[str, dict] = {}

    for atom in ATOMS:
        # 1. Information gain: how much does removing this atom degrade full resolution?
        without = measure_partition(records, [a for a in ATOMS if a != atom])
        info_gain = full["resolution_rate"] - without["resolution_rate"]

        # 2. Standalone (just topology + this atom): does it resolve anything alone?
        solo = measure_partition(records, [atom])
        standalone_resolution = solo["resolution_rate"]
        standalone_preservation = solo["preservation_rate"]

        # 3. Drift sensitivity: for each topology signature with multiple instances,
        #    does this atom value vary across instances (drift) or stay stable?
        topo_to_atom_vals: dict[str, list[str]] = defaultdict(list)
        atom_key_map = {
            "discipline": "discipline", "position": "position",
            "system": "system", "scale": "scale_class",
            "legend": "legend_match_score",
        }
        atom_key = atom_key_map[atom]
        for r in records:
            topo_to_atom_vals[r["topology_sig"]].append(str(r.get(atom_key, "")))
        # Drift = share of topology groups where atom takes multiple distinct values
        multi_instance = [(sig, vs) for sig, vs in topo_to_atom_vals.items() if len(vs) >= 2]
        if multi_instance:
            drifting = sum(1 for sig, vs in multi_instance if len(set(vs)) > 1)
            drift_sensitivity = drifting / len(multi_instance)
        else:
            drift_sensitivity = 0.0

        # 4. False partition rate: % of healthy recurrence collisions broken by this atom alone
        #    (compare healthy preservation with just-this-atom vs no-atom baseline)
        false_partition = topo_only["preservation_rate"] - solo["preservation_rate"]
        # Negative or near-zero = no false partitions caused by this atom

        # 5. Redundancy: how often does this atom split the same way as the union of others?
        #    Approximation: full_sigs_partition vs without_atom_partition — count of moves
        full_sigs = {r["pdf_key"] + ":" + r["topology_sig"]: r["_tmp_sig"] for r in records if "_tmp_sig" in r}
        # Re-tag with full + without configs
        measure_partition(records, ATOMS[:])  # tags _tmp_sig with full
        full_assignment = {(r["pdf_key"], r["page"], r["n_prims"]): r["_tmp_sig"] for r in records}
        measure_partition(records, [a for a in ATOMS if a != atom])
        without_assignment = {(r["pdf_key"], r["page"], r["n_prims"]): r["_tmp_sig"] for r in records}
        # Redundancy = share of records where full and without_atom produce same partition group
        # If atom is redundant, removing it changes nothing → high overlap
        same = sum(1 for k in full_assignment if full_assignment[k] == without_assignment[k])
        redundancy = same / max(len(full_assignment), 1)

        per_atom[atom] = {
            "information_gain": round(info_gain, 4),
            "standalone_resolution": round(standalone_resolution, 4),
            "standalone_preservation": round(standalone_preservation, 4),
            "drift_sensitivity": round(drift_sensitivity, 4),
            "false_partition_rate": round(max(0.0, false_partition), 4),
            "redundancy_with_others": round(redundancy, 4),
        }

    return {
        "baseline_topology_only": topo_only,
        "baseline_full_atoms": full,
        "per_atom": per_atom,
    }


def verdict_per_atom(per_atom: dict, baseline_full: dict) -> dict:
    """KEEP / FLAG / REMOVE per atom based on combined metrics."""
    verdicts = {}
    for atom, m in per_atom.items():
        flags = []
        if m["information_gain"] < 0.05 and m["redundancy_with_others"] > 0.95:
            flags.append("LOW_VALUE_AND_REDUNDANT")
        if m["drift_sensitivity"] > 0.40:
            flags.append("HIGH_DRIFT")
        if m["false_partition_rate"] > 0.20:
            flags.append("OVER_PARTITIONING")
        if m["standalone_resolution"] < 0.05 and m["information_gain"] < 0.05:
            flags.append("NO_DISCRIMINATIVE_POWER")
        if not flags:
            verdict = "KEEP"
        elif "NO_DISCRIMINATIVE_POWER" in flags or "OVER_PARTITIONING" in flags:
            verdict = "REMOVE"
        else:
            verdict = "FLAG"
        verdicts[atom] = {"verdict": verdict, "flags": flags, **m}
    return verdicts


def main() -> None:
    print("=== Wave 20.14.5J-A Atom Orthogonality Audit ===\n")
    print("--- Building IR records (Phase 2 baseline) ---")
    records = build_records()
    print(f"\ntotal IR records: {len(records)}\n")

    print("--- Running ablation audit ---")
    audit = audit_atoms(records)
    print(f"baseline (topology only):  res={audit['baseline_topology_only']['resolution_rate']*100:.1f}%  "
          f"presv={audit['baseline_topology_only']['preservation_rate']*100:.1f}%")
    print(f"baseline (all 5 atoms):    res={audit['baseline_full_atoms']['resolution_rate']*100:.1f}%  "
          f"presv={audit['baseline_full_atoms']['preservation_rate']*100:.1f}%")

    print(f"\n{'Atom':<14s} {'InfoGain':>9s} {'SoloRes':>9s} {'SoloPres':>9s} {'Drift':>8s} {'FalseParts':>11s} {'Redund':>9s}")
    print("-" * 78)
    for atom, m in audit["per_atom"].items():
        print(f"{atom:<14s} {m['information_gain']*100:>8.1f}% "
              f"{m['standalone_resolution']*100:>8.1f}% "
              f"{m['standalone_preservation']*100:>8.1f}% "
              f"{m['drift_sensitivity']*100:>7.1f}% "
              f"{m['false_partition_rate']*100:>10.1f}% "
              f"{m['redundancy_with_others']*100:>8.1f}%")

    verdicts = verdict_per_atom(audit["per_atom"], audit["baseline_full_atoms"])
    print(f"\n=== PER-ATOM VERDICTS ===")
    for atom, v in verdicts.items():
        flags_str = (", ".join(v["flags"]) if v["flags"] else "—")
        print(f"  {atom:<14s} {v['verdict']:<8s}  flags: {flags_str}")

    # Anti-overfit signal
    print(f"\n=== JOINT SIGNAL (per v20: resolution + preservation, never just one) ===")
    full = audit["baseline_full_atoms"]
    joint = (full["resolution_rate"] >= 0.90) and (full["preservation_rate"] >= 0.85)
    print(f"  Full IR:  resolution={full['resolution_rate']*100:.1f}%  preservation={full['preservation_rate']*100:.1f}%   "
          f"{'JOINT PASS' if joint else 'JOINT FAIL'}")
    print(f"  v20 minimums: resolution ≥90%, preservation ≥85%")

    # Persist
    out_path = OUT_DIR / "csir_atom_audit_2026-05-24.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "wave": "20.14.5J-A",
            "baseline_topology_only": audit["baseline_topology_only"],
            "baseline_full_atoms": audit["baseline_full_atoms"],
            "per_atom_metrics": audit["per_atom"],
            "verdicts": verdicts,
            "joint_signal_pass": joint,
        }, f, indent=2)
    print(f"\naudit results: {out_path}")


if __name__ == "__main__":
    main()
