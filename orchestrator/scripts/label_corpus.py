"""Label every blueprint in the corpus with ground truth.

For each PDF:
  1. Extract structural signals (pages, primitives, images, text presence)
  2. Render first page + middle page as thumbnails for visual inspection
  3. Extract any available text per page
  4. Save signals + extracted text to a labeling worksheet

User reviews + I read thumbnails to assign ground truth labels.
The output: canonical_corpus_labels.json — the SINGLE SOURCE OF TRUTH.

No more filename heuristics. No more pattern matching as primary signal.
Ground truth labels drive everything downstream.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")
OUT_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus")
THUMB_DIR = OUT_DIR / "thumbnails"


def render_page_thumbnail(doc, page_idx: int, out_path: Path, scale: float = 1.5) -> bool:
    """Render page to PNG for visual inspection."""
    try:
        page = doc[page_idx]
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat)
        pix.save(str(out_path))
        return True
    except Exception as exc:
        print(f"   thumb FAIL page {page_idx}: {exc}")
        return False


def inspect(pdf_path: Path) -> dict:
    """Extract structural + textual signals for ground truth labeling."""
    doc = fitz.open(str(pdf_path))
    n_pages = doc.page_count
    pdf_key = pdf_path.stem

    # Aggregate signals
    n_lines = 0
    n_curves = 0
    n_rects = 0
    n_images_total = 0
    text_chars = 0
    per_page_text: dict[int, str] = {}

    for pg in range(n_pages):
        page = doc[pg]
        n_images_total += len(page.get_images(full=True))
        text = page.get_text() or ""
        per_page_text[pg] = text
        text_chars += len(text)
        for d in page.get_drawings():
            for it in d.get("items", []):
                op = it[0] if it else None
                if op == "l":
                    n_lines += 1
                elif op == "c":
                    n_curves += 1
                elif op == "re":
                    n_rects += 1

    # Render thumbnails: first page + middle page (or first only if 1 page)
    pdf_thumb_dir = THUMB_DIR / pdf_key.replace(" ", "_").replace("(", "_").replace(")", "_")
    pdf_thumb_dir.mkdir(parents=True, exist_ok=True)
    pages_to_render = [0]
    if n_pages > 1:
        pages_to_render.append(n_pages // 2)
    if n_pages > 3:
        pages_to_render.append(n_pages - 1)
    thumb_paths: list[str] = []
    for pg in pages_to_render:
        out_path = pdf_thumb_dir / f"page_{pg:02d}.png"
        if render_page_thumbnail(doc, pg, out_path):
            thumb_paths.append(str(out_path))

    # Extract title-block-like text (top + bottom 15% of first page)
    first_page = doc[0]
    page_rect = first_page.rect
    h = page_rect.height
    w = page_rect.width
    title_region_text = ""
    try:
        # Bottom-right title block typical
        title_clip = fitz.Rect(w * 0.5, h * 0.85, w, h)
        title_region_text = first_page.get_text(clip=title_clip) or ""
    except Exception:
        pass

    # Possible sheet codes from any text
    all_text = " ".join(per_page_text.values()).upper()
    sheet_code_patterns = [
        r"\bSHEET\s*([A-Z]{1,4}[-_]?\d{1,4}(?:\.\d{1,2})?)",
        r"\bDWG\s*[-:]?\s*([A-Z]{1,4}[-_]?\d{1,4})",
        r"\b([A-Z]{1,3}[-_]?\d{1,3})\b",
    ]
    found_codes: Counter = Counter()
    for pat in sheet_code_patterns:
        for m in re.findall(pat, all_text):
            found_codes[m] += 1

    # Discipline keyword indicators (high-confidence words across all text)
    discipline_keywords = {
        "electrical_power": [r"\bRECEPTACLE\b", r"\bPANEL\s*SCHEDULE\b", r"\bSWITCHGEAR\b",
                              r"\bBRANCH\s*CIRCUIT\b", r"\bELECTRICAL\s*POWER\b"],
        "electrical_lighting": [r"\bLUMINAIRE\b", r"\bPHOTOMETRIC\b", r"\bLIGHTING\s*FIXTURE\b",
                                 r"\bPOLE\s*PLAN\b"],
        "fire_protection": [r"\bSPRINKLER\b", r"\bSTANDPIPE\b", r"\bFIRE\s*PROTECTION\b",
                             r"\bHYDRAULIC\s*CALC"],
        "fire_alarm": [r"\bFIRE\s*ALARM\b", r"\bSMOKE\s*DETECT", r"\bHORN.*STROBE\b"],
        "plumbing": [r"\bDOMESTIC\s*WATER\b", r"\bSANITARY\b", r"\bWASTE\s*AND\s*VENT\b",
                     r"\bGREASE\s*TRAP\b"],
        "mechanical_hvac": [r"\bHVAC\b", r"\bDUCTWORK\b", r"\bDIFFUSER\b", r"\bSUPPLY\s*AIR\b",
                             r"\bRETURN\s*AIR\b", r"\bAIR\s*HANDL"],
        "structural": [r"\bFOUNDATION\b", r"\bSTRUCTURAL\b", r"\bFRAMING\b", r"\bBEAM\s*SCHEDULE\b",
                        r"\bCOLUMN\s*SCHEDULE\b"],
        "metal_building": [r"\bMETAL\s*BUILDING\b", r"\bPURLIN\b", r"\bGIRT\b", r"\bRIGID\s*FRAME\b",
                            r"\bRAFTER\b", r"\bMBMA\b", r"\bEAVE\b"],
        "civil_drainage": [r"\bCATCH\s*BASIN\b", r"\bSTORM\s*WATER\b", r"\bDRAINAGE\s*PLAN\b",
                            r"\bMANHOLE\b"],
        "civil_grading": [r"\bGRADING\b", r"\bCONTOUR\b", r"\bELEVATION\s*PROFILE\b"],
        "civil_site": [r"\bSITE\s*PLAN\b", r"\bPROPERTY\s*LINE\b", r"\bRIGHT\s*OF\s*WAY\b"],
        "architectural": [r"\bROOM\s*SCHEDULE\b", r"\bDOOR\s*SCHEDULE\b", r"\bWINDOW\s*SCHEDULE\b",
                           r"\bFLOOR\s*PLAN\b"],
        "concrete_precast": [r"\bPRECAST\b", r"\bCONCRETE\b", r"\bREBAR\b", r"\bFOOTING\b"],
    }

    discipline_evidence: dict[str, list[str]] = {}
    for disc, patterns in discipline_keywords.items():
        hits = []
        for pat in patterns:
            matches = re.findall(pat, all_text)
            if matches:
                hits.extend([m if isinstance(m, str) else str(m) for m in matches[:3]])
        if hits:
            discipline_evidence[disc] = hits[:5]

    doc.close()

    return {
        "pdf_key": pdf_key,
        "pdf_filename": pdf_path.name,
        "n_pages": n_pages,
        "size_bytes": pdf_path.stat().st_size,
        "primitive_counts": {"lines": n_lines, "curves": n_curves, "rects": n_rects},
        "n_images": n_images_total,
        "total_text_chars": text_chars,
        "has_text": text_chars > 50,
        "title_region_sample": title_region_text[:200].strip(),
        "sheet_codes_found": dict(found_codes.most_common(10)),
        "discipline_keyword_evidence": discipline_evidence,
        "thumbnails": thumb_paths,
        "first_page_text_sample": (per_page_text.get(0, "") or "")[:300].strip(),
    }


def main() -> None:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(BLUEPRINTS.glob("*.pdf"))
    print(f"=== Labeling Corpus: {len(pdfs)} PDFs ===\n")

    worksheet: list[dict] = []
    for pdf in pdfs:
        print(f"--- {pdf.name} ---")
        try:
            row = inspect(pdf)
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            continue
        worksheet.append(row)

        print(f"  pages: {row['n_pages']}  size: {row['size_bytes']//1024} KB")
        print(f"  primitives: lines={row['primitive_counts']['lines']:,} "
              f"curves={row['primitive_counts']['curves']:,} "
              f"rects={row['primitive_counts']['rects']:,}")
        print(f"  images: {row['n_images']}  text_chars: {row['total_text_chars']:,}  "
              f"has_text: {row['has_text']}")
        if row['title_region_sample']:
            print(f"  title region: {row['title_region_sample'][:120]}")
        if row['sheet_codes_found']:
            top_codes = list(row['sheet_codes_found'].items())[:5]
            print(f"  sheet codes: {top_codes}")
        if row['discipline_keyword_evidence']:
            for disc, ev in list(row['discipline_keyword_evidence'].items())[:3]:
                print(f"    disc evidence [{disc}]: {ev[:3]}")
        print(f"  thumbnails: {len(row['thumbnails'])} rendered")
        print()

    out_path = OUT_DIR / "corpus_labeling_worksheet.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": "2026-05-24",
            "n_pdfs": len(worksheet),
            "rows": worksheet,
        }, f, indent=2)

    print(f"=== WORKSHEET SAVED: {out_path} ===")
    print(f"\nNext step: read thumbnails + assign canonical labels per PDF")


if __name__ == "__main__":
    main()
