"""Inspect GAVNN ADDENDUM 1 — the real Gulfstream project."""
import sys
from pathlib import Path
from collections import Counter

try:
    import pymupdf as fitz
except ImportError:
    import fitz

PDF = Path(r"C:\Users\tonio\Projects\myapp\blueprints\GAVNN_ADDENDUM_1.pdf")
THUMB_DIR = Path(r"C:\Users\tonio\Projects\myapp\backend\orchestrator\benchmark_corpus\thumbnails\GAVNN_ADDENDUM_1")
THUMB_DIR.mkdir(parents=True, exist_ok=True)

doc = fitz.open(str(PDF))
print(f"PDF: {PDF.name}")
print(f"size: {PDF.stat().st_size / (1024*1024):.1f} MB")
print(f"pages: {doc.page_count}\n")

# Render cover + sample pages
pages_to_render = [0]
if doc.page_count > 1:
    pages_to_render += [doc.page_count // 4, doc.page_count // 2, 3 * doc.page_count // 4, doc.page_count - 1]
pages_to_render = sorted(set(p for p in pages_to_render if 0 <= p < doc.page_count))

# Per-page stats
n_lines_total = 0
n_curves_total = 0
n_images_total = 0
text_chars_total = 0

for pg in range(doc.page_count):
    page = doc[pg]
    n_images = len(page.get_images(full=True))
    n_images_total += n_images
    text = page.get_text() or ""
    text_chars_total += len(text)
    n_lines = 0
    n_curves = 0
    for d in page.get_drawings():
        for it in d.get("items", []):
            if it and it[0] == "l":
                n_lines += 1
            elif it and it[0] == "c":
                n_curves += 1
    n_lines_total += n_lines
    n_curves_total += n_curves
    if pg < 5 or pg in pages_to_render:
        print(f"  page {pg}: {n_lines} lines, {n_curves} curves, {n_images} images, "
              f"{len(text)} text chars")
        if text:
            sample = text[:200].replace("\n", " ")
            print(f"    text: {sample[:180]}...")

print(f"\nTOTAL: {n_lines_total} lines, {n_curves_total} curves, {n_images_total} images, "
      f"{text_chars_total} text chars")

# Render thumbnails of selected pages
for pg in pages_to_render:
    try:
        page = doc[pg]
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat)
        out = THUMB_DIR / f"page_{pg:02d}.png"
        pix.save(str(out))
        print(f"  saved thumbnail: page {pg} → {out.name}")
    except Exception as exc:
        print(f"  thumbnail FAIL page {pg}: {exc}")

doc.close()
