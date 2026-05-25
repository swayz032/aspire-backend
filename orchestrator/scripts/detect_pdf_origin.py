"""Quick scan to detect raster-import / vectorized-scan PDFs in the test set.

Signals of raster origin:
  1. Embedded image XObjects (raster page backgrounds)
  2. Very high tiny-line ratio (auto-vectorization fragments curves into many tiny segments)
  3. Average segment length below CAD-typical threshold
  4. Image rendering ops in content stream
"""
import math
from pathlib import Path
import pymupdf as fitz

BLUEPRINTS = Path(r"C:\Users\tonio\Projects\myapp\blueprints")

print(f"{'PDF':<45s} {'Pages':>5} {'Imgs':>5} {'Lines':>8} {'TinyLn%':>8} {'AvgLen':>8} {'AutoVec?':>10}")
print("-" * 100)

for pdf_path in sorted(BLUEPRINTS.glob("*.pdf")):
    try:
        doc = fitz.open(str(pdf_path))
        n_images = 0
        n_lines = 0
        tiny_lines = 0  # length < 2pt
        total_len = 0.0
        for pg_idx in range(doc.page_count):
            page = doc[pg_idx]
            n_images += len(page.get_images(full=True))
            for d in page.get_drawings():
                for it in d.get("items", []):
                    if it[0] == "l":
                        try:
                            p0, p1 = it[1], it[2]
                            L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
                            n_lines += 1
                            total_len += L
                            if L < 2.0:
                                tiny_lines += 1
                        except Exception:
                            pass
        avg_len = total_len / max(n_lines, 1)
        tiny_pct = (tiny_lines / max(n_lines, 1)) * 100
        # Heuristic: raster-import if embedded images present, OR > 60% tiny lines + avg < 4pt
        auto_vec = (n_images > 0 and n_lines > 1000) or (tiny_pct > 60 and avg_len < 4 and n_lines > 5000)
        marker = "YES" if auto_vec else ""
        print(f"{pdf_path.name[:45]:<45s} {doc.page_count:>5} {n_images:>5} {n_lines:>8} "
              f"{tiny_pct:>7.1f}% {avg_len:>7.2f}pt  {marker:>10}")
        doc.close()
    except Exception as exc:
        print(f"{pdf_path.name[:45]:<45s} ERROR {type(exc).__name__}")
