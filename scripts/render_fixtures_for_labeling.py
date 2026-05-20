"""Render all Drew blueprint fixtures to 200 DPI PNGs for Wave 11a labeling.

Output structure (Roboflow / YOLO-compatible):
  scripts/labeling_workspace/
    raw_images/{fixture_name}_p{n}.png     200 DPI sheets, one PNG per page
    manifest.json                          metadata for the auto-label step

Then `python scripts/render_fixtures_for_labeling.py` is idempotent — skips
already-rendered pages.

Industry standard: YOLO training works well at 1280px input. Our 200 DPI
renders are typically 2400-3400px wide — they get downsized at training time
but preserve detail for small symbols.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz  # pymupdf

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "orchestrator" / "tests" / "fixtures" / "blueprints"
OUT_DIR = REPO_ROOT / "scripts" / "labeling_workspace" / "raw_images"
MANIFEST_PATH = REPO_ROOT / "scripts" / "labeling_workspace" / "manifest.json"

DPI = 200
ZOOM = DPI / 72.0  # PyMuPDF default is 72 DPI

# Skip fixtures with no symbols expected (text-only or pure dedup tests)
SKIP_FIXTURES = {
    "eng_c2_2_gsm_duplicate.pdf",  # dedup test, identical to eng_c2_2_gsm.pdf
}


def render_pdf(pdf_path: Path) -> list[dict]:
    """Render every page of a PDF to a 200 DPI PNG. Returns manifest rows."""
    pages: list[dict] = []
    stem = pdf_path.stem
    with fitz.open(pdf_path) as doc:
        for idx, page in enumerate(doc):
            out_name = f"{stem}_p{idx + 1}.png"
            out_path = OUT_DIR / out_name
            if out_path.exists():
                pages.append({
                    "fixture": pdf_path.name,
                    "page": idx + 1,
                    "image": out_name,
                    "width": 0,
                    "height": 0,
                    "rendered": False,
                    "reason": "already_exists",
                })
                continue
            mat = fitz.Matrix(ZOOM, ZOOM)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(out_path))
            pages.append({
                "fixture": pdf_path.name,
                "page": idx + 1,
                "image": out_name,
                "width": pix.width,
                "height": pix.height,
                "rendered": True,
                "reason": "new",
            })
    return pages


def main() -> int:
    if not FIXTURES_DIR.exists():
        print(f"ERROR: fixtures dir missing: {FIXTURES_DIR}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_pages: list[dict] = []
    pdfs = sorted(FIXTURES_DIR.glob("*.pdf"))
    for pdf in pdfs:
        if pdf.name in SKIP_FIXTURES:
            print(f"  skip {pdf.name}")
            continue
        try:
            rows = render_pdf(pdf)
            new_count = sum(1 for r in rows if r["rendered"])
            print(f"  {pdf.name}: {len(rows)} pages ({new_count} new)")
            all_pages.extend(rows)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {pdf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)

    manifest = {
        "dpi": DPI,
        "total_pages": len(all_pages),
        "fixtures": len(pdfs) - len(SKIP_FIXTURES),
        "skipped": list(SKIP_FIXTURES),
        "pages": all_pages,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print()
    print(f"Wrote manifest -> {MANIFEST_PATH}")
    print(f"Images dir      -> {OUT_DIR}")
    print(f"Total pages     -> {len(all_pages)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
