"""Wave 11g-v2 Stage A — classify our 11 blueprint fixtures by PDF structure.

The pivot: stop treating blueprints as pixel images. Treat them as engineering
geometry preserved in vector primitives. But first, we MUST know what kind of
PDF each fixture is — vector-native PDFs let us extract topology directly;
raster scans force us to fall back to LASE.

Classification taxonomy (from GPT review):
  VECTOR_NATIVE   — large primitive count, no/few raster images, text extractable
                    → graph extraction works pristinely
  VECTOR_DEGRADED — primitives present but flattened (Bézier chaos, many tiny segments)
                    → graph extraction works with stitching
  HYBRID          — vector geometry on raster background images
                    → mixed pipeline
  RASTER_SCAN     — predominantly raster images, low primitive count
                    → fall back to LASE

Output: per-fixture classification + per-page primitive counts + text yield.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import fitz  # pymupdf

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "orchestrator" / "tests" / "fixtures" / "blueprints"
OUT_PATH = REPO_ROOT / "scripts" / "labeling_workspace" / "vector_classification.json"

# Heuristic thresholds — tunable.
PRIMITIVES_PER_PAGE_VECTOR_NATIVE = 500     # >500 drawing primitives = real CAD export
PRIMITIVES_PER_PAGE_DEGRADED = 50           # >50 but <500 = some vector, mostly degraded
IMAGE_PAGE_AREA_RATIO_HYBRID = 0.20         # >20% of page area covered by raster = hybrid


def classify_page(page: fitz.Page) -> dict:
    """Classify a single PDF page by its structural content."""
    # Vector drawing primitives (lines, curves, fills)
    drawings = page.get_drawings()
    primitive_count = 0
    item_types = Counter()
    for d in drawings:
        for item in d.get("items", []):
            primitive_count += 1
            item_types[item[0]] += 1  # 'l'=line, 'c'=curve, 're'=rect, 'qu'=quad

    # Raster images embedded on the page
    images = page.get_images(full=True)
    page_w, page_h = page.rect.width, page.rect.height
    page_area = max(1.0, page_w * page_h)
    raster_area = 0.0
    for img_info in images:
        # img_info is a tuple; get the drawn rect via page.get_image_rects
        try:
            xref = img_info[0]
            rects = page.get_image_rects(xref)
            for r in rects:
                raster_area += max(0.0, r.width) * max(0.0, r.height)
        except Exception:  # noqa: BLE001
            continue
    raster_ratio = min(1.0, raster_area / page_area)

    # Text extractability — vector-native PDFs have native text spans
    text = page.get_text("text") or ""
    text_chars = len(text)
    text_blocks = len(page.get_text("blocks") or [])

    # Decide
    if primitive_count >= PRIMITIVES_PER_PAGE_VECTOR_NATIVE and raster_ratio < IMAGE_PAGE_AREA_RATIO_HYBRID:
        category = "VECTOR_NATIVE"
    elif primitive_count >= PRIMITIVES_PER_PAGE_VECTOR_NATIVE and raster_ratio >= IMAGE_PAGE_AREA_RATIO_HYBRID:
        category = "HYBRID"
    elif PRIMITIVES_PER_PAGE_DEGRADED <= primitive_count < PRIMITIVES_PER_PAGE_VECTOR_NATIVE:
        category = "VECTOR_DEGRADED"
    elif primitive_count < PRIMITIVES_PER_PAGE_DEGRADED and len(images) > 0:
        category = "RASTER_SCAN"
    else:
        category = "UNKNOWN_LOW_CONTENT"

    return {
        "page_size": [round(page_w, 1), round(page_h, 1)],
        "primitive_count": primitive_count,
        "primitive_types": dict(item_types),
        "drawing_groups": len(drawings),
        "image_count": len(images),
        "raster_coverage_pct": round(raster_ratio * 100, 1),
        "text_chars": text_chars,
        "text_blocks": text_blocks,
        "category": category,
    }


def classify_pdf(pdf_path: Path) -> dict:
    result = {"file": pdf_path.name, "pages": []}
    cat_counter: Counter[str] = Counter()
    try:
        with fitz.open(pdf_path) as doc:
            for idx, page in enumerate(doc):
                page_result = classify_page(page)
                page_result["page_number"] = idx + 1
                result["pages"].append(page_result)
                cat_counter[page_result["category"]] += 1
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["page_count"] = len(result["pages"])
    result["dominant_category"] = cat_counter.most_common(1)[0][0] if cat_counter else "UNKNOWN_LOW_CONTENT"
    result["category_distribution"] = dict(cat_counter)
    return result


def main() -> int:
    pdfs = sorted(FIXTURES_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {FIXTURES_DIR}", file=sys.stderr)
        return 1

    all_results = []
    print(f"Classifying {len(pdfs)} fixtures...")
    print()
    print(f"{'fixture':<45} {'pages':>5} {'dominant_category':<22} {'avg primitives/page':>20}")
    print("-" * 100)
    for pdf in pdfs:
        r = classify_pdf(pdf)
        all_results.append(r)
        if "error" in r:
            print(f"  ERROR {pdf.name}: {r['error']}")
            continue
        avg_prim = sum(p["primitive_count"] for p in r["pages"]) / max(1, len(r["pages"]))
        print(f"{pdf.name:<45} {r['page_count']:>5} {r['dominant_category']:<22} {avg_prim:>20.0f}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_results, indent=2), encoding="utf-8")

    # Summary
    overall = Counter()
    for r in all_results:
        overall[r.get("dominant_category", "UNKNOWN")] += 1
    print()
    print("Overall corpus mix:")
    for cat, n in overall.most_common():
        print(f"  {cat:<22} {n:>3} fixtures")

    print(f"\nDetailed JSON: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
