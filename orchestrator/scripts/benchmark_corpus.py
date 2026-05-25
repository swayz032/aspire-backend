"""Wave 20.14.5I — Frozen Benchmark Corpus + Evaluation Harness.

Runs canonicalization probe across ALL test blueprints. For each PDF, captures:
  - Export style metadata
  - Raw primitive counts
  - Canonicalization results (synthetic curves + guardrail rejections)
  - Field distribution (before/after canonicalization)
  - Acceptance gate status

Persists as versioned JSON golden corpus. Every future wave runs this to verify
no regression. Without this, future tuning silently degrades earlier wins.

ZERO Supabase IO.
"""
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from canonicalize_local import (  # noqa: E402
    extract_lines, canonicalize, field_distribution, classify_tile_field,
    TILE_PT, _angle_rad,
)

VERSION = "20.14.5I-r2-2026-05-24"  # r2: origin_class spectrum (raster↔vector hybrid)
BLUEPRINTS_DIR = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")

# Category mapping — fills v17 corpus categories
CATEGORY_MAP = {
    "21030 ENG_Rev1 240801_Signed": ("Gulfstream", "native_curve_cad"),
    "29187P-ES (3)": ("Electrical site (12pg)", "flattened_polyline_cad"),
    "Precast Drainage": ("Precast catch basin", "mechanical_shop_drawing"),
    "Concrete blueprints Mangonia Park": ("Concrete structural", "dense_hatch_architectural"),
    "E-1 (1)": ("Electrical E-1", "electrical_one_line"),
    "E-2 (1)": ("Electrical E-2", "electrical_one_line"),
    "E-3 (1)": ("Electrical E-3", "electrical_one_line"),
    "E-4 (1)": ("Electrical E-4", "electrical_one_line"),
    "P-1 (1)": ("Plumbing P-1", "plumbing_single_sheet"),
    "21030 ENG-C2.2 GSM": ("Civil C-2.2 GSM", "civil_site_plan"),
    "21030 ENG-C2.2 GSM (1)": ("Civil C-2.2 GSM (dup)", "civil_site_plan"),
    "2101LP-R1-LP1-1of1 (11) (7)": ("Lighting plan", "site_lighting"),
    "Revised light pole plan": ("Light pole plan", "site_lighting"),
    "Open 29187P-ES (3)": ("Open electrical site", "flattened_polyline_cad"),
}


def detect_export_style(prims: list[dict], pdf_path: Path) -> dict:
    """Infer export style from raw primitive distribution AND raster image presence.
    Test-set finding: real contractor PDFs are raster-vector HYBRIDS, not pure CAD."""
    import math as _math
    types = Counter(p["primitive_type"] for p in prims)
    n_total = len(prims) or 1

    # Detect embedded raster images + tiny-line ratio
    n_images = 0
    tiny_lines = 0
    total_len = 0.0
    try:
        import pymupdf as fitz
        doc = fitz.open(str(pdf_path))
        for pg in range(doc.page_count):
            n_images += len(doc[pg].get_images(full=True))
        doc.close()
    except Exception:
        pass

    for p in prims:
        if p.get("primitive_type") == "line":
            L = p.get("length", 0) or 0
            total_len += L
            if L < 2.0:
                tiny_lines += 1
    n_lines = types.get("line", 0)
    avg_seg_len = total_len / max(n_lines, 1)
    tiny_share = tiny_lines / max(n_lines, 1)

    # Origin classification — every PDF sits on raster↔vector spectrum
    if n_images == 0 and tiny_share > 0.75:
        origin = "pure_auto_vectorized"
    elif n_images >= 10:
        origin = "embedded_raster_heavy"
    elif n_images > 0:
        origin = "hybrid_raster_vector"
    else:
        origin = "vector_only"

    return {
        "n_total": n_total,
        "n_lines": n_lines,
        "n_curves": types.get("curve", 0),
        "n_rects": types.get("rect", 0),
        "n_images": n_images,
        "tiny_line_share": round(tiny_share, 4),
        "avg_segment_length_pt": round(avg_seg_len, 3),
        "curve_share": round(types.get("curve", 0) / n_total, 4),
        "line_share": round(types.get("line", 0) / n_total, 4),
        "native_curves": types.get("curve", 0) > 50,
        "polyline_arc_export": types.get("curve", 0) < 10 and types.get("line", 0) > 1000,
        "origin_class": origin,  # NEW: raster ↔ vector spectrum classification
    }


def run_blueprint(pdf_path: Path) -> dict:
    """Single PDF evaluation. Returns golden corpus row."""
    start = time.time()
    try:
        prims = extract_lines(str(pdf_path))
    except Exception as exc:
        return {"pdf": pdf_path.name, "error": f"{type(exc).__name__}: {exc}"}

    extract_ms = round((time.time() - start) * 1000, 1)
    style = detect_export_style(prims, pdf_path)

    # Before canonicalization
    before = field_distribution(prims)

    # Canonicalize
    canon_start = time.time()
    synthetic, rejections, n_chains = canonicalize(prims)
    canon_ms = round((time.time() - canon_start) * 1000, 1)

    # After
    after = field_distribution(prims + synthetic)

    # Acceptance gates
    sym_before = before.get("SYMBOL_FIELD", 0)
    sym_after = after.get("SYMBOL_FIELD", 0)
    hatch_before = before.get("HATCH_FIELD", 0)
    hatch_after = after.get("HATCH_FIELD", 0)

    gates = {
        "gate_symbol_field_recovery": sym_after >= sym_before,
        "gate_hatch_no_promotion": hatch_after <= hatch_before + max(2, hatch_before * 0.05),
        "gate_synthetic_produced_when_needed": (
            len(synthetic) > 0 if (style["polyline_arc_export"] and not style["native_curves"]) else True
        ),
        "gate_synthetic_acceptance_rate_bounded": (
            (len(synthetic) / n_chains <= 0.30) if n_chains > 0 else True
        ),
        # Operational gate: absolute threshold, not relative.
        # Canonicalization does real work (adjacency graph + circle fits) so
        # being slower than extraction is expected and correct.
        "gate_canonicalization_runtime_bounded": canon_ms < 60_000,  # 60s absolute cap
    }
    all_pass = all(gates.values())

    pdf_key = pdf_path.stem
    cat_name, cat_label = CATEGORY_MAP.get(pdf_key, (pdf_key, "unknown"))

    return {
        "pdf": pdf_path.name,
        "pdf_key": pdf_key,
        "category_name": cat_name,
        "category_label": cat_label,
        "size_bytes": pdf_path.stat().st_size,
        "extract_ms": extract_ms,
        "canon_ms": canon_ms,
        "export_style": style,
        "raw_primitive_counts": dict(Counter(p["primitive_type"] for p in prims)),
        "field_distribution_before": dict(before),
        "field_distribution_after": dict(after),
        "canonicalization": {
            "candidate_chains": n_chains,
            "synthetic_emitted": len(synthetic),
            "acceptance_rate": round(len(synthetic) / max(n_chains, 1), 4),
            "rejections": dict(rejections),
            "synthetic_radius_range": (
                [round(min(s["radius"] for s in synthetic), 2),
                 round(max(s["radius"] for s in synthetic), 2)]
                if synthetic else None
            ),
            "synthetic_confidence_range": (
                [round(min(s["reconstruction_confidence"] for s in synthetic), 4),
                 round(max(s["reconstruction_confidence"] for s in synthetic), 4)]
                if synthetic else None
            ),
        },
        "acceptance_gates": gates,
        "all_gates_pass": all_pass,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(BLUEPRINTS_DIR.glob("*.pdf"))
    print(f"=== Wave 20.14.5I Benchmark Corpus ({VERSION}) ===")
    print(f"blueprints discovered: {len(pdfs)}\n")

    corpus: list[dict] = []
    pass_count = 0
    fail_count = 0
    err_count = 0
    category_coverage: set[str] = set()

    for pdf in pdfs:
        print(f"--- {pdf.name} ({pdf.stat().st_size//1024} KB) ---")
        row = run_blueprint(pdf)
        corpus.append(row)
        if "error" in row:
            print(f"  ERROR: {row['error']}")
            err_count += 1
            continue
        category_coverage.add(row["category_label"])
        style = row["export_style"]
        canon = row["canonicalization"]
        before_sym = row["field_distribution_before"].get("SYMBOL_FIELD", 0)
        after_sym = row["field_distribution_after"].get("SYMBOL_FIELD", 0)
        before_hatch = row["field_distribution_before"].get("HATCH_FIELD", 0)
        after_hatch = row["field_distribution_after"].get("HATCH_FIELD", 0)
        verdict = "PASS" if row["all_gates_pass"] else "FAIL"
        print(f"  category: {row['category_label']:<28s} export: native_curves={style['native_curves']:<5}  "
              f"polyline_arc={style['polyline_arc_export']}")
        print(f"  raw: {style['n_total']:>7} prims  ({row['raw_primitive_counts']})")
        print(f"  canon: {canon['synthetic_emitted']:>4} synth / {canon['candidate_chains']} chains "
              f"({canon['acceptance_rate']*100:.1f}%)")
        print(f"  SYMBOL_FIELD: {before_sym:>4} -> {after_sym:<4}  "
              f"HATCH: {before_hatch:>4} -> {after_hatch:<4}  "
              f"{verdict}")
        if not row["all_gates_pass"]:
            failed = [k for k, v in row["acceptance_gates"].items() if not v]
            print(f"  failed gates: {failed}")
            fail_count += 1
        else:
            pass_count += 1
        print()

    # Persist corpus
    corpus_path = OUT_DIR / f"corpus_{VERSION}.json"
    with open(corpus_path, "w", encoding="utf-8") as f:
        json.dump({
            "version": VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_pdfs": len(pdfs),
            "pass": pass_count, "fail": fail_count, "error": err_count,
            "category_coverage": sorted(category_coverage),
            "rows": corpus,
        }, f, indent=2)

    print(f"=== CORPUS SUMMARY ===")
    print(f"total: {len(pdfs)}  pass: {pass_count}  fail: {fail_count}  error: {err_count}")
    print(f"category coverage ({len(category_coverage)}):")
    for c in sorted(category_coverage):
        print(f"  - {c}")
    print(f"\ncorpus saved: {corpus_path}")
    print(f"corpus size: {corpus_path.stat().st_size // 1024} KB")

    # v17 required categories check
    v17_required = {
        "native_curve_cad", "flattened_polyline_cad", "civil_site_plan",
        "electrical_one_line", "mechanical_shop_drawing",
        "dense_hatch_architectural", "raster_import_dxf",
    }
    missing = v17_required - category_coverage
    print(f"\n=== v17 CATEGORY GAP ===")
    if missing:
        print(f"missing categories: {sorted(missing)}")
    else:
        print(f"all v17 categories covered")

    # NEW: origin_class spectrum — the actual realism dimension
    origin_counts: Counter[str] = Counter()
    for row in corpus:
        if "error" not in row:
            origin_counts[row["export_style"]["origin_class"]] += 1
    print(f"\n=== ORIGIN CLASS SPECTRUM (real-world realism) ===")
    for oc, n in origin_counts.most_common():
        print(f"  {oc:<28s} {n} PDFs")


if __name__ == "__main__":
    main()
